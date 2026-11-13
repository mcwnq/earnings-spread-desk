"""Eastern time, NYSE trading days and the entry / exit sessions for an announcement."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN, CLOSE = time(9, 30), time(16, 0)
ENTRY_WINDOW = (time(15, 0), time(16, 0))   # the last hour before an after-close (or next-morning) report


def now_et() -> datetime:
    return datetime.now(ET)


def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    d = date(y, m, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(days=7 * (n - 1))


def _last_weekday(y: int, m: int, weekday: int) -> date:
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def _observed(d: date) -> date | None:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def nyse_holidays(y: int) -> frozenset[date]:
    h = {
        _nth_weekday(y, 1, 0, 3),                # Martin Luther King Jr. Day
        _nth_weekday(y, 2, 0, 3),                # Washington's Birthday
        _easter(y) - timedelta(days=2),          # Good Friday
        _last_weekday(y, 5, 0),                  # Memorial Day
        _observed(date(y, 7, 4)),
        _nth_weekday(y, 9, 0, 1),                # Labor Day
        _nth_weekday(y, 11, 3, 4),               # Thanksgiving
        _observed(date(y, 12, 25)),
    }
    ny = date(y, 1, 1)
    if ny.weekday() != 5:                        # NYSE does not close the Friday before when Jan 1 is a Saturday
        h.add(_observed(ny))
    if y >= 2022:
        h.add(_observed(date(y, 6, 19)))         # Juneteenth
    return frozenset(x for x in h if x is not None)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def add_trading_days(d: date, n: int) -> date:
    step = 1 if n > 0 else -1
    k = abs(n)
    while k > 0:
        d += timedelta(days=step)
        if is_trading_day(d):
            k -= 1
    return d


def next_trading_day(d: date) -> date:
    return add_trading_days(d, 1)


def prev_trading_day(d: date) -> date:
    return add_trading_days(d, -1)


def on_or_after(d: date) -> date:
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def on_or_before(d: date) -> date:
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def sessions(report: date, timing: str) -> tuple[date, date]:
    """(entry session, exit session) for a report. After the close on D: enter D, exit D+1.
    Before the open on D: enter D-1, exit D. Unknown timing is treated as after the close, the more common case,
    and the page says it is unconfirmed."""
    if timing == "BMO":
        exit_ = on_or_after(report)
        return prev_trading_day(exit_), exit_
    entry = on_or_before(report)
    return entry, next_trading_day(entry)


def is_rth(et: datetime) -> bool:
    return is_trading_day(et.date()) and OPEN <= et.time() < CLOSE


def in_entry_window(et: datetime) -> bool:
    return is_trading_day(et.date()) and ENTRY_WINDOW[0] <= et.time() < ENTRY_WINDOW[1]


def trade_date_for(et: datetime) -> date:
    """The session a moment belongs to: after the close or on a holiday it is the next session."""
    d = et.date()
    if et.time() >= CLOSE or not is_trading_day(d):
        return on_or_after(d + timedelta(days=1)) if et.time() >= CLOSE else on_or_after(d)
    return d


def parse_expiry(s: str) -> date | None:
    head = (s or "").strip()[:8]
    try:
        return datetime.strptime(head, "%Y%m%d").date()
    except ValueError:
        return None


def parse_date(s: str) -> date | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def week_of(d: date) -> list[date]:
    mon = d - timedelta(days=d.weekday())
    return [mon + timedelta(days=i) for i in range(5)]
