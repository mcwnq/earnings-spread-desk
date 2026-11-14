"""desk.db: the SQLite database in the data folder for everything worth keeping.

Tables
  journal         one row per forward-test candidate (entry day + symbol); `data` holds the whole record as JSON
  journal_sides   the call and put calendar at entry, one row per side, in columns you can query
  journal_models  each side's endurable gap and verdict under every crush model (target, mult, retrace)
  journal_exits   the next-day checks, one row per check time and side, with the close offer and P&L
  screens         every finished screen: price, stage and both sides' key numbers, so the screens can be judged later
  trades          the fills you recorded yourself on the exit page (this app places no orders)

`data` columns are JSON, so a new field never needs a schema change; the other columns are copies made for queries.
The database runs in WAL mode, so a viewer such as DB Browser for SQLite can read it while the app runs.

Settings, earnings-date overrides and the download caches stay as files: settings.json and overrides.json are meant
to be readable and editable by hand, and the caches can be deleted at any time.

On the first start after the switch, an existing journal.json / trades.json is imported once and renamed to
*.imported."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("earningsdesk")
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id TEXT PRIMARY KEY, entry_date TEXT, exit_date TEXT, symbol TEXT, name TEXT, report TEXT, timing TEXT,
    status TEXT, stage TEXT, default_right TEXT, S REAL, recorded TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS journal_exit ON journal(exit_date);
CREATE INDEX IF NOT EXISTS journal_symbol ON journal(symbol);
CREATE TABLE IF NOT EXISTS journal_sides (
    record_id TEXT NOT NULL REFERENCES journal(id) ON DELETE CASCADE, side TEXT NOT NULL, is_default INTEGER,
    strike REAL, front TEXT, back TEXT, front_bid REAL, front_ask REAL, back_bid REAL, back_ask REAL,
    front_iv REAL, back_iv REAL, natural REAL, width REAL, entry REAL, ba_overall TEXT, iv_edge REAL,
    yes_no INTEGER, endurable_gap REAL, hits INTEGER, n INTEGER, median_gap REAL, verdict TEXT,
    be_low REAL, be_high REAL, PRIMARY KEY (record_id, side));
CREATE TABLE IF NOT EXISTS journal_exits (
    record_id TEXT NOT NULL REFERENCES journal(id) ON DELETE CASCADE, check_time TEXT NOT NULL, side TEXT NOT NULL,
    at TEXT, S REAL, move_pct REAL, front_bid REAL, front_ask REAL, back_bid REAL, back_ask REAL,
    spread_bid REAL, spread_ask REAL, offer REAL, pnl REAL, pnl_pct REAL, error TEXT,
    PRIMARY KEY (record_id, check_time, side));
CREATE TABLE IF NOT EXISTS journal_models (
    record_id TEXT NOT NULL REFERENCES journal(id) ON DELETE CASCADE, side TEXT NOT NULL, model TEXT NOT NULL,
    endurable_gap REAL, hits INTEGER, n INTEGER, verdict TEXT, front_iv_after REAL, back_iv_after REAL,
    target_vol REAL, PRIMARY KEY (record_id, side, model));
CREATE TABLE IF NOT EXISTS screens (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, symbol TEXT, report TEXT, timing TEXT, entry_date TEXT,
    S REAL, state TEXT, quotes_live INTEGER, stage TEXT, side TEXT, strike REAL, front TEXT, back TEXT,
    natural REAL, entry REAL, ba_overall TEXT, iv_edge REAL, endurable_gap REAL, hits INTEGER, n INTEGER,
    verdict TEXT, alt_natural REAL, alt_entry REAL, alt_ba_overall TEXT, alt_verdict TEXT, error TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS screens_symbol ON screens(symbol, at);
CREATE TABLE IF NOT EXISTS trades (pos INTEGER PRIMARY KEY, id TEXT, symbol TEXT, data TEXT NOT NULL);
"""


def _q(pair, i):
    return pair[i] if isinstance(pair, (list, tuple)) and len(pair) > i else None


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()
        self._import_json()

    def close(self):
        self.db.close()

    # ------------------------------------------------------------------ forward test
    def journal_records(self) -> list[dict]:
        rows = self.db.execute("SELECT data FROM journal ORDER BY entry_date, recorded, symbol").fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_journal_record(self, r: dict):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO journal VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r.get("entry_date"), r.get("exit_date"), r.get("symbol"), r.get("name"), r.get("report"),
                 r.get("timing"), r.get("status"), r.get("stage"), r.get("default_right"), r.get("S"),
                 r.get("recorded"), json.dumps(r)))
            self.db.execute("DELETE FROM journal_sides WHERE record_id=?", (r["id"],))
            self.db.execute("DELETE FROM journal_exits WHERE record_id=?", (r["id"],))
            self.db.execute("DELETE FROM journal_models WHERE record_id=?", (r["id"],))
            for side, s in (r.get("sides") or {}).items():
                for model, m in (s.get("models") or {}).items():
                    self.db.execute("INSERT INTO journal_models VALUES (?,?,?,?,?,?,?,?,?,?)",
                                    (r["id"], side, model, m.get("meg"), m.get("hits"), m.get("n"), m.get("verdict"),
                                     m.get("fIV1"), m.get("bIV1"), m.get("target")))
            for side, s in (r.get("sides") or {}).items():
                self.db.execute(
                    "INSERT INTO journal_sides VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], side, int(side == r.get("default_right")), s.get("K"), s.get("front"), s.get("back"),
                     _q(s.get("fq"), 0), _q(s.get("fq"), 1), _q(s.get("bq"), 0), _q(s.get("bq"), 1),
                     s.get("fIV"), s.get("bIV"), s.get("natural"), s.get("width"), s.get("entry"),
                     s.get("baOverall"), s.get("ivDiff"), _bool(s.get("yes")), s.get("meg"), s.get("hits"),
                     s.get("n"), s.get("med"), s.get("verdict"), s.get("beLo"), s.get("beHi")))
            for check, e in (r.get("exits") or {}).items():
                for side, q in (e.get("sides") or {}).items():
                    self.db.execute(
                        "INSERT INTO journal_exits VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r["id"], check, side, e.get("at"), e.get("S"), e.get("move"),
                         _q(q.get("fq"), 0), _q(q.get("fq"), 1), _q(q.get("bq"), 0), _q(q.get("bq"), 1),
                         q.get("bid"), q.get("ask"), q.get("offer"), q.get("pnl"), q.get("pnl_pct"), q.get("error")))

    # ------------------------------------------------------------------ screen history
    def log_screen(self, at: str, c: dict, a: dict, alt: dict):
        a, alt = a or {}, alt or {}
        keep = ("right", "K", "front", "back", "fq", "bq", "fIV", "bIV", "natural", "width", "entry", "baOverall",
                "ivDiff", "yes", "meg", "hits", "n", "med", "verdict", "beLo", "beHi", "backWhy")
        data = {"a": {k: a.get(k) for k in keep if k in a}, "a_alt": {k: alt.get(k) for k in keep if k in alt},
                "status": c.get("status"), "model": c.get("model")}
        with self.db:
            self.db.execute(
                "INSERT INTO screens (at, symbol, report, timing, entry_date, S, state, quotes_live, stage, side,"
                " strike, front, back, natural, entry, ba_overall, iv_edge, endurable_gap, hits, n, verdict,"
                " alt_natural, alt_entry, alt_ba_overall, alt_verdict, error, data)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (at, c.get("symbol"), c.get("date"), c.get("timing"), c.get("entry"), c.get("S"), c.get("state"),
                 _bool(c.get("quotes_live")), (c.get("stage") or {}).get("lbl"), a.get("right"), a.get("K"),
                 a.get("front"), a.get("back"), a.get("natural"), a.get("entry"), a.get("baOverall"),
                 a.get("ivDiff"), a.get("meg"), a.get("hits"), a.get("n"), a.get("verdict"), alt.get("natural"),
                 alt.get("entry"), alt.get("baOverall"), alt.get("verdict"), c.get("error") or None,
                 json.dumps(data, default=str)))

    def screens(self, symbol: str | None = None, limit: int = 500) -> list[dict]:
        cur = self.db.execute(
            "SELECT * FROM screens " + ("WHERE symbol=? " if symbol else "") + "ORDER BY id DESC LIMIT ?",
            ((symbol, limit) if symbol else (limit,)))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------------ trades
    def trades(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM trades ORDER BY pos")]

    def save_trades(self, rows: list[dict]):
        with self.db:
            self.db.execute("DELETE FROM trades")
            self.db.executemany("INSERT INTO trades VALUES (?,?,?,?)",
                                [(i, r.get("id"), r.get("symbol"), json.dumps(r)) for i, r in enumerate(rows)])

    # ------------------------------------------------------------------ one-time import of the old JSON files
    def _import_json(self):
        folder = self.path.parent
        for name, loader in (("journal.json", self._import_journal), ("trades.json", self._import_trades)):
            p = folder / name
            if not p.exists():
                continue
            try:
                n = loader(json.loads(p.read_text(encoding="utf-8")))
                p.replace(p.with_name(name + ".imported"))
                log.info("Imported %d rows from %s into %s", n, name, self.path.name)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not import %s (left in place): %s", p, e)

    def _import_journal(self, records) -> int:
        have = {r[0] for r in self.db.execute("SELECT id FROM journal")}
        new = [r for r in records if isinstance(r, dict) and r.get("id") and r["id"] not in have]
        for r in new:
            self.save_journal_record(r)
        return len(new)

    def _import_trades(self, rows) -> int:
        if self.trades():
            return 0
        rows = [r for r in rows if isinstance(r, dict)]
        self.save_trades(rows)
        return len(rows)


def _bool(x):
    return None if x is None else int(bool(x))
