"""Forward test: what each candidate looked like at entry, and what the same spreads were worth the next session.

At entry (the last-hour screens on the entry day) the latest screen of every candidate is saved, both the call and
the put calendar, whether or not it passed the screens, so the screens themselves can be judged later. On the exit
day the same legs are quoted at the check times (Settings) and the close is priced the way the course does it:
offer at the spread bid + 25% of the width. P&L is per spread against the 25-20 entry price.

Saved in desk.db (see store.py: journal, journal_sides, journal_exits); /api/journal.csv exports it for a spreadsheet."""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime

from . import model as M
from .store import Store

log = logging.getLogger("earningsdesk")
SIDE_FIELDS = ("right", "K", "front", "back", "fq", "bq", "fIV", "bIV", "natural", "inside", "width", "entry",
               "baOverall", "ivDiff", "yes", "meg", "hits", "n", "med", "verdict", "beLo", "beHi")


def side_snapshot(a: dict) -> dict | None:
    if not a or not a.get("fq") or not a.get("bq") or a.get("natural") is None:
        return None
    return {k: a.get(k) for k in SIDE_FIELDS}


def exit_quote(entry_side: dict, f: dict, b: dict) -> dict:
    """f, b: {"bid", "ask", "iv"} for the short (front) and long (back) leg now."""
    out = {"fq": [f.get("bid"), f.get("ask")], "bq": [b.get("bid"), b.get("ask")], "fIV": f.get("iv"), "bIV": b.get("iv")}
    if None in (f.get("bid"), f.get("ask"), b.get("bid"), b.get("ask")):
        out["error"] = "no two-sided market"
        return out
    bid = b["bid"] - f["ask"]
    ask = b["ask"] - f["bid"]
    offer = M.close_offer(bid, ask)
    entry = entry_side["entry"]
    out.update(bid=round(bid, 2), ask=round(ask, 2), offer=offer, mid=round((bid + ask) / 2, 3),
               pnl=round((offer - entry) * 100, 2), pnl_pct=round((offer - entry) / entry * 100, 1) if entry else None)
    return out


class Journal:
    def __init__(self, store: Store):
        self.store = store
        self.records: list[dict] = store.journal_records()

    def get(self, rid: str) -> dict | None:
        return next((r for r in self.records if r["id"] == rid), None)

    def record_entry(self, c: dict, now: datetime) -> dict | None:
        """Saves (or replaces with a later screen) the entry snapshot of a candidate. Returns the record."""
        a, alt = c.get("a") or {}, c.get("a_alt") or {}
        sides = {}
        for x in (a, alt):
            snap = side_snapshot(x)
            if snap:
                sides[snap["right"]] = snap
        if not sides:
            return None
        rid = f"{c['entry']}|{c['symbol']}"
        rec = self.get(rid)
        if rec is not None and rec.get("exits"):
            return rec                       # the exit day has started; the entry stays as it was
        rec = {"id": rid, "symbol": c["symbol"], "name": c.get("name", ""), "report": c["date"], "timing": c["timing"],
               "entry_date": c["entry"], "exit_date": c["model"], "S": c.get("S"), "status": c.get("status"),
               "stage": (c.get("stage") or {}).get("lbl"), "stage_k": (c.get("stage") or {}).get("k"),
               "default_right": a.get("right"), "recorded": now.strftime("%Y-%m-%d %H:%M ET"), "sides": sides,
               "exits": {}}
        self.records = [r for r in self.records if r["id"] != rid] + [rec]
        self.store.save_journal_record(rec)
        return rec

    def due(self, today: str, now_hhmm: str, checks: list[str]) -> list[tuple[dict, str]]:
        """(record, check time) pairs to quote now: exit day today, check time passed, not done yet. A check that
        was missed by more than an hour (app not running) is left out rather than taken late at the wrong price."""
        out = []
        for r in self.records:
            if r["exit_date"] != today:
                continue
            for t in checks:
                if t in r["exits"] or now_hhmm < t:
                    continue
                if _minutes(now_hhmm) - _minutes(t) > 60 or now_hhmm >= "16:00":
                    continue
                out.append((r, t))
                break                         # one check per record per pass
        return out

    def record_exit(self, rid: str, check: str, S: float | None, quotes: dict, now: datetime):
        """quotes: {right: (front quote, back quote)}"""
        r = self.get(rid)
        if r is None:
            return
        sides = {}
        for right, (f, b) in quotes.items():
            if right in r["sides"]:
                sides[right] = exit_quote(r["sides"][right], f, b)
        r["exits"][check] = {"at": now.strftime("%H:%M:%S"), "S": S, "sides": sides,
                             "move": round((S - r["S"]) / r["S"] * 100, 2) if S and r.get("S") else None}
        self.store.save_journal_record(r)

    def csv(self, checks: list[str]) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        head = ["entry_date", "exit_date", "symbol", "report", "timing", "stage", "side", "default_side", "K", "front",
                "back", "S_entry", "entry_25_20", "natural", "ba_overall", "iv_edge", "endurable_gap", "hits", "n",
                "median_gap", "verdict"]
        for t in checks:
            head += [f"S_{t}", f"move_{t}", f"offer_{t}", f"pnl_{t}"]
        w.writerow(head)
        for r in sorted(self.records, key=lambda x: (x["entry_date"], x["symbol"])):
            for right, s in sorted(r["sides"].items()):
                row = [r["entry_date"], r["exit_date"], r["symbol"], r["report"], r["timing"], r.get("stage"), right,
                       "yes" if right == r.get("default_right") else "no", s["K"], s["front"], s["back"], r.get("S"),
                       s["entry"], _r(s["natural"]), s["baOverall"], _r(s.get("ivDiff")), _r(s.get("meg")), s.get("hits"),
                       s.get("n"), _r(s.get("med")), s.get("verdict")]
                for t in checks:
                    e = r["exits"].get(t) or {}
                    q = (e.get("sides") or {}).get(right) or {}
                    row += [e.get("S"), e.get("move"), q.get("offer"), q.get("pnl")]
                w.writerow(row)
        return buf.getvalue()


def _r(x, d=3):
    return round(x, d) if isinstance(x, (int, float)) else x


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)
