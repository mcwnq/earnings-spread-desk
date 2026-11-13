"""The Total Earnings Domination screens as plain functions: bid-ask, Yes/No, Is/Was and the 25-20 entry price.

Everything here is pure (no TWS, no I/O) so it can be tested against the course examples."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import median


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S: float, K: float, T: float, v: float, call: bool, r: float) -> float:
    """Black-Scholes value; T in years, v as a decimal."""
    if T <= 0 or v <= 0:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    s = v * math.sqrt(T)
    d1 = (math.log(S / K) + (r + v * v / 2) * T) / s
    d2 = d1 - s
    if call:
        return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2)
    return K * math.exp(-r * T) * ncdf(-d2) - S * ncdf(-d1)


def tick(x: float) -> float:
    """Nearest nickel, halves rounded up (Python's round() would take 1.025 down to 1.00)."""
    return math.floor(x * 20 + 0.5 + 1e-9) / 20


def floor_tick(x: float) -> float:
    """Down to a nickel, at least 0.05 (the 25-20 bid is rounded down, slide 17: 0.725 -> 0.70)."""
    return max(0.05, math.floor(x * 20 + 1e-6) / 20)


def gap_percent(close_before: float, open_after: float) -> float:
    return abs(close_before - open_after) / close_before * 100.0


def ba_status(ratio: float, pass_pct: float = 110.0, review_pct: float = 120.0) -> str:
    if not math.isfinite(ratio):
        return "fail"
    if ratio <= pass_pct / 100 + 1e-9:
        return "pass"
    if ratio <= review_pct / 100 + 1e-9:
        return "review"
    return "fail"


def ladder(natural: float, width: float, steps: int = 6, step: float = 0.05, every_s: int = 20) -> list[dict]:
    """25-20 middling: start at natural - 25% of width (rounded down), add 0.05 every 20 s, never above natural."""
    p = floor_tick(natural - 0.25 * width)
    out = []
    for i in range(steps):
        if p > tick(natural) + 1e-9:
            break
        out.append({"seconds": i * every_s, "price": round(p, 2)})
        p = tick(p + step)
    return out


def close_offer(bid: float, ask: float) -> float:
    """Selling the spread back: offer at the spread bid plus 25% of the width."""
    return tick(bid + 0.25 * (ask - bid))


@dataclass
class Params:
    K: float | None = None
    right: str | None = None          # "C" or "P"; default: OTM side of the strike
    front: str | None = None          # expiry yyyymmdd
    back: str | None = None
    crush_mode: str = "mult"
    crush_percent: float = 80.0
    back_iv_change_percent: float = 0.0
    gap_basis: str = "avg"
    debit: float | None = None
    rate: float = 0.04
    ba_pass_percent: float = 110.0
    ba_review_percent: float = 120.0
    iv_edge_points: float = 2.0
    strong_hits: int = 6
    acceptable_hits: int = 5
    lookback: int = 7

    @classmethod
    def from_strategy(cls, s, **over) -> "Params":
        p = cls(crush_mode=s.crush_mode, crush_percent=s.crush_percent, back_iv_change_percent=s.back_iv_change_percent,
                gap_basis=s.gap_basis, rate=s.risk_free_rate, ba_pass_percent=s.ba_pass_percent,
                ba_review_percent=s.ba_review_percent, iv_edge_points=s.iv_edge_points, strong_hits=s.strong_hits,
                acceptable_hits=s.acceptable_hits, lookback=s.lookback)
        for k, v in over.items():
            if v is not None and hasattr(p, k):
                setattr(p, k, v)
        return p


def _d(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def quote(chain: dict, expiry: str, right: str, K: float) -> dict | None:
    """chain[expiry][right][strike] = {"bid", "ask", "iv" (decimal), ...}"""
    return ((chain.get(expiry) or {}).get(right) or {}).get(float(K))


def verdict_for(hits: int, n: int, p: Params) -> str:
    if n == 0:
        return "n/a"
    # thresholds are set for 7 past gaps; with fewer gaps they scale (5 gaps: 4.3 -> 5 for Strong)
    strong = math.ceil(p.strong_hits * n / p.lookback - 1e-9)
    acceptable = math.ceil(p.acceptable_hits * n / p.lookback - 1e-9)
    if hits >= strong:
        return "Strong"
    if hits >= acceptable:
        return "Acceptable"
    return "Weak"


def analyze(c: dict, p: Params) -> dict:
    """c: one candidate's data:
      S (price), entry / model (ISO dates: entry session, day the spread is modeled and closed),
      chain (see quote()), expiries (sorted yyyymmdd list), front / back (default picks), K (default strike),
      gaps: [{"date", "close_before", "open_after", ...}] newest first.
    Returns the screen results in the same shape the page draws."""
    S = c["S"]
    K = float(p.K if p.K is not None else c["K"])
    right = p.right or ("C" if K >= S else "P")
    call = right == "C"
    exps = c["expiries"]
    front = p.front or c["front"]
    back = p.back or c["back"]
    if back is None or back <= front:
        later = [e for e in exps if e > front]
        back = later[0] if later else None
    out = {"K": K, "right": right, "call": call, "front": front, "back": back, "ok": False}
    fq = quote(c["chain"], front, right, K)
    bq = quote(c["chain"], back, right, K) if back else None
    if not fq or not bq:
        out["error"] = "No quotes for these legs yet."
        return out
    fb, fa, bb, ba = fq.get("bid"), fq.get("ask"), bq.get("bid"), bq.get("ask")
    if not all(isinstance(x, (int, float)) and x > 0 for x in (fb, fa, bb, ba)):
        out.update(fq=[fb, fa], bq=[bb, ba], error="A leg has no two-sided market.")
        out["baRatio"] = [fa / fb if fb and fa else math.inf, ba / bb if bb and ba else math.inf]
        out["baOverall"] = "fail"
        return out
    ratios = [fa / fb, ba / bb]
    stats = [ba_status(r, p.ba_pass_percent, p.ba_review_percent) for r in ratios]
    overall = "fail" if "fail" in stats else "review" if "review" in stats else "pass"
    fIV, bIV = fq.get("iv"), bq.get("iv")
    out.update(fq=[fb, fa], bq=[bb, ba], baRatio=ratios, baStatus=stats, baOverall=overall, fIV=fIV, bIV=bIV)
    natural = ba - fb
    inside = bb - fa
    width = natural - inside
    entry = floor_tick(natural - 0.25 * width)
    debit = p.debit if p.debit else entry
    out.update(natural=natural, inside=inside, width=width, entry=entry, debit=debit, maxLoss=debit * 100,
               ladder=ladder(natural, width))
    if not fIV or not bIV:
        out["error"] = "TWS has not sent the model IV for a leg yet."
        return out
    ivDiff = (fIV - bIV) * 100
    out.update(ivDiff=ivDiff, yes=ivDiff >= p.iv_edge_points - 1e-9)

    crush = p.crush_percent / 100
    fIV1 = fIV * crush if p.crush_mode == "mult" else fIV - crush * (fIV - bIV)
    bIV1 = bIV * (1 + p.back_iv_change_percent / 100)
    e0, e1 = date.fromisoformat(c["entry"]), date.fromisoformat(c["model"])
    fd, bd = _d(front), _d(back)
    tf0, tb0 = (fd - e0).days / 365, (bd - e0).days / 365
    tf1, tb1 = (fd - e1).days / 365, (bd - e1).days / 365
    r = p.rate
    lo, hi, n = S * 0.78, S * 1.22, 160
    pts = []
    for i in range(n + 1):
        x = lo + (hi - lo) * i / n
        now = (bs(x, K, tb0, bIV, call, r) - bs(x, K, tf0, fIV, call, r) - debit) * 100
        fwd = (bs(x, K, tb1, bIV1, call, r) - bs(x, K, tf1, fIV1, call, r) - debit) * 100
        pts.append([round(x, 4), round(now, 3), round(fwd, 3)])
    be = []
    for a, b in zip(pts, pts[1:]):
        if (a[2] < 0) != (b[2] < 0):
            be.append(a[0] + (b[0] - a[0]) * (-a[2]) / (b[2] - a[2]))
    below = [x for x in be if x < S]
    above = [x for x in be if x > S]
    beLo = below[-1] if below else None
    beHi = above[0] if above else None
    dLo = S - beLo if beLo else None
    dHi = beHi - S if beHi else None
    if dLo is not None and dHi is not None:
        dist = min(dLo, dHi) if p.gap_basis == "near" else (dLo + dHi) / 2
    else:
        dist = None
    meg = dist / S * 100 if dist is not None else None
    gaps = c.get("gaps", [])[: p.lookback]
    gp = [g["gap"] for g in gaps]
    hits = sum(1 for g in gp if meg is not None and g < meg)
    med = median(gp) if gp else None
    verdict = verdict_for(hits, len(gp), p) if meg is not None else "n/a"
    out.update(fIV1=fIV1, bIV1=bIV1, pts=pts, beLo=beLo, beHi=beHi, dLo=dLo, dHi=dHi, dist=dist, meg=meg,
               hits=hits, n=len(gp), med=med, verdict=verdict, maxGain=max(q[2] for q in pts),
               straddle=meg is not None and med is not None and meg < med,
               daysFront=(fd - e1).days, daysBack=(bd - e1).days, daysFront0=(fd - e0).days, daysBack0=(bd - e0).days,
               ok=True)
    out["straddleQuote"] = straddle(c, front, S, gp)
    return out


def straddle(c: dict, front: str, S: float, gaps_pct: list[float]) -> dict | None:
    """Long straddle at the strike nearest the price in the earnings expiry: cost at the asks and the move it needs."""
    calls = (c["chain"].get(front) or {}).get("C") or {}
    puts = (c["chain"].get(front) or {}).get("P") or {}
    both = [k for k in calls if k in puts]
    if not both:
        return None
    k = min(both, key=lambda x: abs(x - S))
    ca, pa = calls[k].get("ask"), puts[k].get("ask")
    if not ca or not pa or ca <= 0 or pa <= 0:
        return None
    cost = ca + pa
    need = cost / S * 100
    return {"K": k, "call_ask": ca, "put_ask": pa, "cost": cost, "need": need,
            "above": sum(1 for g in gaps_pct if g > need), "n": len(gaps_pct)}


def stage(cand: dict, a: dict) -> dict:
    """Where a candidate stops in the funnel."""
    if not cand.get("verified"):
        return {"k": "verify", "lbl": "Needs verification", "cls": "p-bad"}
    if a.get("error") and not a.get("baRatio"):
        return {"k": "data", "lbl": "Waiting for quotes", "cls": "p-neutral"}
    if a.get("baOverall") == "fail":
        return {"k": "ba", "lbl": "Fails bid-ask", "cls": "p-bad"}
    if a.get("error"):
        return {"k": "data", "lbl": "Waiting for IV", "cls": "p-neutral"}
    if not a.get("yes"):
        return {"k": "yn", "lbl": "No (term structure)", "cls": "p-bad"}
    if a.get("verdict") in ("Weak", "n/a"):
        if a.get("straddle"):
            return {"k": "iw", "lbl": "Straddle candidate", "cls": "p-acc"}
        return {"k": "iw", "lbl": "Weak Is/Was" if a.get("verdict") == "Weak" else "No gap history", "cls": "p-warn"}
    return {"k": "ok", "lbl": a["verdict"], "cls": "p-good" if a["verdict"] == "Strong" else "p-warn"}


def rank_key(a: dict):
    """More past gaps inside the endurable gap first, then the bigger margin over the median gap."""
    return (-(a.get("hits") or 0), -((a.get("meg") or 0) - (a.get("med") or 0)))
