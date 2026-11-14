"""Past earnings gaps (the "Was" in Is/Was) from daily bars.

gap = |close before the report - open after it| / close before
  after the close on D:  close of D,    open of the next session
  before the open on D:  close of D-1,  open of D
When the sources have fewer past report dates than needed, the missing quarters are found in the bars: the largest
overnight gap within two weeks of where the report should be (about 91 days apart). Those are marked "inferred";
taking the largest gap makes them, if anything, err on the side of a bigger past move."""
from __future__ import annotations

from datetime import date, timedelta

from .model import gap_percent


def _bar_date(b) -> date:
    d = b["date"]
    return d if isinstance(d, date) else date.fromisoformat(str(d)[:10])


def gap_for(bars: list[dict], report: date, timing: str) -> dict | None:
    """bars ascending: {"date", "open", "close"}."""
    if timing == "BMO":
        before = [b for b in bars if _bar_date(b) < report]
        after = [b for b in bars if _bar_date(b) >= report]
    else:
        before = [b for b in bars if _bar_date(b) <= report]
        after = [b for b in bars if _bar_date(b) > report]
    if not before or not after:
        return None
    b0, b1 = before[-1], after[0]
    if (_bar_date(b1) - _bar_date(b0)).days > 6:      # a hole in the data, not one overnight move
        return None
    if not b0["close"] or not b1["open"]:
        return None
    return {"date": report.isoformat(), "timing": timing or "AMC", "before_date": _bar_date(b0).isoformat(),
            "after_date": _bar_date(b1).isoformat(), "close_before": b0["close"], "open_after": b1["open"],
            "gap": gap_percent(b0["close"], b1["open"])}


def largest_gap_near(bars: list[dict], center: date, half_window: int = 14) -> dict | None:
    best = None
    for prev, cur in zip(bars, bars[1:]):
        d = _bar_date(cur)
        if abs((d - center).days) > half_window or not prev["close"] or not cur["open"]:
            continue
        g = gap_percent(prev["close"], cur["open"])
        if best is None or g > best["gap"]:
            best = {"date": d.isoformat(), "timing": "BMO", "before_date": _bar_date(prev).isoformat(),
                    "after_date": d.isoformat(), "close_before": prev["close"], "open_after": cur["open"], "gap": g}
    return best


def past_gaps(bars: list[dict], coming: date, coming_timing: str, known: list[tuple[date, str, str]],
              want: int = 7) -> list[dict]:
    """known: (date, timing, source) newest first, from the earnings sources. Unknown timing takes the coming
    report's timing (companies rarely switch). Returns newest first, at most `want`."""
    out = []
    used: list[date] = []
    prev = coming
    known = sorted(known, key=lambda x: x[0], reverse=True)
    first_bar = _bar_date(bars[0]) if bars else coming
    for _ in range(want + 4):
        if len(out) >= want:
            break
        center = prev - timedelta(days=91)
        if center < first_bar - timedelta(days=10):
            break
        k = next((x for x in known if abs((x[0] - center).days) <= 35 and x[0] not in used and x[0] < prev - timedelta(days=40)), None)
        if k is not None:
            g = gap_for(bars, k[0], k[1] or coming_timing)
            if g:
                g["source"] = k[2]
                out.append(g)
            used.append(k[0])
            prev = k[0]
            continue
        g = largest_gap_near(bars, center)
        if g is None:
            prev = center
            continue
        g["source"] = "inferred"
        out.append(g)
        prev = date.fromisoformat(g["date"])
    return out


def historical_vol(bars: list[dict], days: int = 30) -> float | None:
    """Close-to-close historical volatility over the last `days` sessions, annualized (decimal)."""
    import math
    closes = [b["close"] for b in bars if b.get("close")][-(days + 1):]
    if len(closes) < days // 2 + 1:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)
