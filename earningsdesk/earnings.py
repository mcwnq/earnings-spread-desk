"""Earnings dates from free sources, cross-checked, with manual overrides.

Sources
  Nasdaq    api.nasdaq.com calendar, one request per day: symbol, name, market cap, pre-market / after-hours.
            Its earnings-surprise page gives the last four report dates per symbol (no time of day).
  Finnhub   finnhub.io calendar with a free API key: date and bmo / amc, for the coming days and for past quarters.
  Manual    overrides.json in the data folder, edited from the Calendar page: a date or time you checked on the
            company's investor-relations page wins over both sources.

An announcement is "verified" when two sources agree on the date (and on before / after the market when both say),
"single" when only one source lists it, and "conflict" when they disagree; conflicts stay out of the screens until
you confirm a date."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import aiohttp

from .timeutil import parse_date

log = logging.getLogger("earningsdesk")

NASDAQ_CAL = "https://api.nasdaq.com/api/calendar/earnings"
NASDAQ_SURPRISE = "https://api.nasdaq.com/api/company/{symbol}/earnings-surprise"
FINNHUB_CAL = "https://finnhub.io/api/v1/calendar/earnings"
# Nasdaq refuses requests without a browser-like User-Agent; a generic one, nothing that identifies the user
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36",
           "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}

TIMING_NASDAQ = {"time-pre-market": "BMO", "time-after-hours": "AMC"}
TIMING_FINNHUB = {"bmo": "BMO", "amc": "AMC", "dmh": "DMH"}   # dmh: during market hours


@dataclass
class Claim:
    symbol: str
    source: str
    date: date
    timing: str = ""          # BMO, AMC, DMH or "" (not supplied)
    name: str = ""
    market_cap: float | None = None


@dataclass
class Event:
    symbol: str
    date: date
    timing: str
    name: str = ""
    market_cap: float | None = None
    status: str = "single"    # verified | single | conflict | manual
    claims: list[Claim] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "date": self.date.isoformat(), "timing": self.timing or "?", "name": self.name,
                "market_cap": self.market_cap, "status": self.status, "note": self.note,
                "sources": [{"source": c.source, "date": c.date.isoformat(), "timing": c.timing or "?"} for c in self.claims]}


def parse_market_cap(s) -> float | None:
    if s in (None, "", "N/A"):
        return None
    try:
        return float(re.sub(r"[^0-9.]", "", str(s)) or "nan")
    except ValueError:
        return None


def parse_nasdaq_day(payload: dict, day: date) -> list[Claim]:
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    out = []
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        out.append(Claim(sym, "Nasdaq", day, TIMING_NASDAQ.get(r.get("time") or "", ""), (r.get("name") or "").strip(),
                         parse_market_cap(r.get("marketCap"))))
    return out


def parse_nasdaq_surprise(payload: dict) -> list[date]:
    rows = (((payload or {}).get("data") or {}).get("earningsSurpriseTable") or {}).get("rows") or []
    return [d for d in (parse_date(r.get("dateReported") or "") for r in rows) if d]


def parse_finnhub(payload: dict) -> dict[str, list[Claim]]:
    out: dict[str, list[Claim]] = {}
    for r in (payload or {}).get("earningsCalendar") or []:
        sym = (r.get("symbol") or "").strip().upper()
        d = parse_date(r.get("date") or "")
        if not sym or not d:
            continue
        out.setdefault(sym, []).append(Claim(sym, "Finnhub", d, TIMING_FINNHUB.get((r.get("hour") or "").lower(), "")))
    return out


def merge(symbol: str, claims: list[Claim], manual: dict | None = None) -> Event:
    """One event from what the sources say about the same quarter's report."""
    name = next((c.name for c in claims if c.name), "")
    cap = next((c.market_cap for c in claims if c.market_cap), None)
    if manual and manual.get("date"):
        d = parse_date(manual["date"])
        timing = manual.get("timing") or next((c.timing for c in claims if c.date == d and c.timing), "")
        mc = Claim(symbol, "Manual", d, timing)
        return Event(symbol, d, timing, name, cap, "manual", [mc] + claims, manual.get("note", "Checked by you."))
    dates = {c.date for c in claims}
    timings = {c.timing for c in claims if c.timing}
    first = sorted(claims, key=lambda c: 0 if c.source == "Nasdaq" else 1)[0]
    timing = first.timing or next(iter(timings), "")
    if len(dates) > 1:
        txt = "; ".join(f"{c.source} says {c.date:%a %b %d}{' ' + c.timing if c.timing else ''}" for c in claims)
        return Event(symbol, min(dates), timing, name, cap, "conflict", claims,
                     f"Sources disagree on the date ({txt}). Check the company's investor-relations page.")
    if len(timings) > 1:
        txt = "; ".join(f"{c.source} says {c.timing}" for c in claims if c.timing)
        return Event(symbol, first.date, timing, name, cap, "conflict", claims,
                     f"Sources disagree on before / after the market ({txt}).")
    srcs = {c.source for c in claims}
    if len(srcs) >= 2:
        return Event(symbol, first.date, timing, name, cap, "verified", claims, "")
    note = f"Only {first.source} lists it. Confirm on the company's investor-relations page."
    if not timing:
        note += " Time of day not supplied."
    return Event(symbol, first.date, timing, name, cap, "single", claims, note)


def dedupe_dates(items: list[tuple[date, str, str]], days: int = 25) -> list[tuple[date, str, str]]:
    """Past report dates from several sources, newest first, one per quarter (dates within 25 days are the same report;
    the first source listed wins, so put the one with the time of day first)."""
    out: list[tuple[date, str, str]] = []
    for d, timing, src in items:
        same = next((i for i, x in enumerate(out) if abs((x[0] - d).days) <= days), None)
        if same is None:
            out.append((d, timing, src))
        elif not out[same][1] and timing:
            out[same] = (out[same][0], timing, out[same][2])
    return sorted(out, key=lambda x: x[0], reverse=True)


class Overrides:
    """overrides.json: {"upcoming": {SYM: {date, timing, note}}, "past": {SYM: [{date, timing}]}, "ignored": [SYM]}"""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"upcoming": {}, "past": {}, "ignored": []}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception as e:  # noqa: BLE001
                log.warning("Could not read %s: %s", path, e)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def upcoming(self, sym: str) -> dict | None:
        return self.data["upcoming"].get(sym)

    def set_upcoming(self, sym: str, d: str, timing: str, note: str = ""):
        self.data["upcoming"][sym] = {"date": d, "timing": timing, "note": note or "Checked by you."}
        self.save()

    def clear_upcoming(self, sym: str):
        self.data["upcoming"].pop(sym, None)
        self.save()

    def past(self, sym: str) -> list[tuple[date, str, str]]:
        return [(parse_date(x["date"]), x.get("timing", ""), "Manual") for x in self.data["past"].get(sym, [])
                if parse_date(x.get("date", ""))]

    def set_past(self, sym: str, rows: list[dict]):
        self.data["past"][sym] = rows
        self.save()


class Cache:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def get(self, key: str, max_age_s: float | None):
        p = self.folder / (re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")
        if not p.exists():
            return None
        if max_age_s is not None and time.time() - p.stat().st_mtime > max_age_s:
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def put(self, key: str, value):
        p = self.folder / (re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")
        p.write_text(json.dumps(value), encoding="utf-8")


class SourceError(Exception):
    pass


class HttpFetcher:
    """GETs JSON. Replaced by a stub in demo mode and in tests."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def get_json(self, url: str, params: dict) -> dict:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=HEADERS, trust_env=True,
                                                  timeout=aiohttp.ClientTimeout(total=20))
        try:
            async with self._session.get(url, params=params) as r:
                if r.status != 200:
                    raise SourceError(f"HTTP {r.status}")
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise SourceError(str(e) or type(e).__name__) from None

    async def close(self):
        if self._session is not None:
            await self._session.close()


class EarningsBook:
    """Upcoming announcements and past report dates."""

    def __init__(self, cfg, data_dir: Path, fetcher=None, today_fn=None):
        self.cfg = cfg                       # EarningsConfig
        self.fetch = fetcher or HttpFetcher()
        self.cache = Cache(data_dir / "cache" / "earnings")
        self.overrides = Overrides(data_dir / "overrides.json")
        self.today_fn = today_fn or date.today
        self.events: dict[str, Event] = {}
        self.source_status: dict[str, dict] = {}
        self.updated: str | None = None

    def _status(self, src: str, ok: bool, text: str):
        self.source_status[src] = {"ok": ok, "text": text, "time": time.strftime("%H:%M:%S")}

    async def _nasdaq_day(self, d: date) -> list[Claim]:
        key = f"nasdaq_{d.isoformat()}"
        past = d < self.today_fn()
        hit = self.cache.get(key, None if past else self.cfg.refresh_minutes * 60)
        if hit is None:
            hit = await self.fetch.get_json(NASDAQ_CAL, {"date": d.isoformat()})
            self.cache.put(key, hit)
        return parse_nasdaq_day(hit, d)

    async def _finnhub_range(self, start: date, end: date, symbol: str = "") -> dict[str, list[Claim]]:
        key = f"finnhub_{symbol or 'all'}_{start}_{end}"
        hit = self.cache.get(key, self.cfg.refresh_minutes * 60 if not symbol else 7 * 86400)
        if hit is None:
            params = {"from": start.isoformat(), "to": end.isoformat(), "token": self.cfg.finnhub_api_key}
            if symbol:
                params["symbol"] = symbol
            hit = await self.fetch.get_json(FINNHUB_CAL, params)
            self.cache.put(key, hit)
        return parse_finnhub(hit)

    async def refresh(self) -> dict[str, Event]:
        today = self.today_fn()
        days = [today + timedelta(days=i) for i in range(self.cfg.days_ahead + 1)]
        claims: dict[str, list[Claim]] = {}
        if self.cfg.use_nasdaq:
            got, failed = 0, []
            for d in days:
                try:
                    for c in await self._nasdaq_day(d):
                        claims.setdefault(c.symbol, []).append(c)
                        got += 1
                except SourceError as e:
                    failed.append(f"{d:%b %d}: {e}")
            self._status("Nasdaq", not failed, f"{got} announcements over {len(days)} days" +
                         (f"; failed {', '.join(failed)}" if failed else ""))
        if self.cfg.finnhub_api_key:
            try:
                fh = await self._finnhub_range(days[0], days[-1])
                for sym, cl in fh.items():
                    claims.setdefault(sym, []).extend(cl)
                self._status("Finnhub", True, f"{sum(len(v) for v in fh.values())} announcements")
            except SourceError as e:
                self._status("Finnhub", False, f"Failed: {e}")
        else:
            self._status("Finnhub", False, "No API key in Settings, so dates come from one source.")

        events: dict[str, Event] = {}
        min_cap = self.cfg.min_market_cap_billions * 1e9
        watch = set(self.cfg.watchlist)
        ignored = set(self.overrides.data.get("ignored", []))
        for sym, cl in claims.items():
            if sym in ignored or (watch and sym not in watch):
                continue
            ev = merge(sym, cl, self.overrides.upcoming(sym))
            if not watch and ev.status != "manual":
                cap = ev.market_cap
                if cap is not None and cap < min_cap:
                    continue
                if cap is None and not any(c.source == "Finnhub" for c in cl):
                    continue
            events[sym] = ev
        for sym, m in self.overrides.data["upcoming"].items():
            if sym not in events and (not watch or sym in watch):
                d = parse_date(m.get("date", ""))
                if d and today <= d <= days[-1]:
                    events[sym] = merge(sym, [], m)
        # Nasdaq lists hundreds of names on busy days: keep the largest per day
        by_day: dict[date, list[Event]] = {}
        for ev in events.values():
            by_day.setdefault(ev.date, []).append(ev)
        keep = {}
        for d, evs in by_day.items():
            evs.sort(key=lambda e: (e.status != "manual", -(e.market_cap or 0)))
            for e in evs[: self.cfg.max_symbols_per_day]:
                keep[e.symbol] = e
        self.events = keep
        self.updated = time.strftime("%Y-%m-%d %H:%M:%S")
        return keep

    async def past_dates(self, symbol: str, before: date, want: int) -> list[tuple[date, str, str]]:
        """Past report dates (date, timing, source), newest first, strictly before the coming report."""
        items: list[tuple[date, str, str]] = list(self.overrides.past(symbol))
        if self.cfg.finnhub_api_key:
            try:
                start = before - timedelta(days=int(want * 92 + 60))
                fh = await self._finnhub_range(start, before - timedelta(days=20), symbol)
                items += [(c.date, c.timing, "Finnhub") for c in fh.get(symbol, [])]
            except SourceError as e:
                log.info("Finnhub history for %s failed: %s", symbol, e)
        if self.cfg.use_nasdaq:
            key = f"nasdaq_surprise_{symbol}"
            hit = self.cache.get(key, 7 * 86400)
            if hit is None:
                try:
                    hit = await self.fetch.get_json(NASDAQ_SURPRISE.format(symbol=symbol), {})
                    self.cache.put(key, hit)
                except SourceError as e:
                    log.info("Nasdaq history for %s failed: %s", symbol, e)
                    hit = {}
            items += [(d, "", "Nasdaq") for d in parse_nasdaq_surprise(hit)]
        items = [x for x in items if x[0] < before - timedelta(days=20)]
        return dedupe_dates(items)[:want]

    def to_list(self) -> list[dict]:
        return [e.to_dict() for e in sorted(self.events.values(), key=lambda e: (e.date, e.timing != "BMO", e.symbol))]
