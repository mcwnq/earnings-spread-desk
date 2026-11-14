"""Demo mode: a replay of the course sample week (Mon Mar 26 2018) with a simulated TWS and canned earnings feeds.

RHT and LULU quotes and RHT's past gaps are the numbers on the course slides; the other tickers are illustrative.
FakeIB has the methods the app uses from ib_async.IB, enforces IBKR's 100-line limit and records the load, so the
tests can run the whole app without TWS or the internet."""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
from datetime import date, datetime, timedelta

from eventkit import Event
from ib_async import BarData, Contract, ContractDetails, OptionChain, OptionComputation, Ticker

from .model import bs
from .timeutil import ET, add_trading_days, is_trading_day, sessions

START = datetime(2018, 3, 26, 14, 52, tzinfo=ET)
_T0 = time.time()


def replay_clock() -> datetime:
    """Replay time: Mon Mar 26 2018 14:52 ET when the app starts, then running in real time."""
    return START + timedelta(seconds=time.time() - _T0)


def _h(*parts) -> float:
    d = hashlib.md5("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(d[:8], "big") / 2 ** 64


EXPIRIES = ["20180329", "20180406", "20180413", "20180420", "20180427"]

# symbol: name, report date, timing, price, strike step, IV by expiry, relative spread, market cap ($bn), past gaps %
TICKERS = {
    "RHT": ("Red Hat Inc", "2018-03-26", "AMC", 153.09, 2.5, [84.5, 49.9, 44.0, 41.5, 40.2], 0.08, 27.0, None),
    "PAYX": ("Paychex Inc", "2018-03-27", "BMO", 63.20, 1.0, [36.0, 21.5, 20.4, 19.8, 19.5], 0.07, 22.6,
             [1.6, 2.4, 2.2, 3.9, 1.1, 2.9, 1.8]),
    "LULU": ("lululemon athletica", "2018-03-27", "AMC", 79.43, 0.5, [122.6, 69.9, 60.6, 55.0, 52.1], 0.10, 10.8,
             [9.6, 14.3, 3.1, 11.2, 22.8, 6.9, 4.8]),
    "CTAS": ("Cintas Corp", "2018-03-27", "AMC", 170.40, 5.0, [31.0, 19.5, 18.9, 18.4, 18.1], 0.24, 18.1,
             [3.2, 5.8, 2.1, 4.4, 6.0, 1.9, 3.3]),
    "WBA": ("Walgreens Boots Alliance", "2018-03-28", "BMO", 66.10, 1.0, [44.0, 26.5, 25.0, 24.4, 24.0], 0.06, 65.4,
            [5.0, 1.9, 4.1, 2.3, 6.4, 3.0, 1.1]),
    "PVH": ("PVH Corp", "2018-03-28", "AMC", 150.20, 2.5, [47.0, 42.5, 38.0, 36.1, 35.0], 0.07, 11.6,
            [8.8, 11.6, 9.2, 6.0, 14.1, 10.3, 5.3]),
    "MKC": ("McCormick & Co", "2018-03-29", "BMO", 104.50, 1.0, [23.1, 22.0, 21.4, 21.0, 20.8], 0.07, 13.1,
            [2.8, 4.6, 1.5, 3.9, 2.2, 5.1, 1.8]),
    "FINL": ("Finish Line Inc", "2018-03-29", "BMO", 13.40, 0.5, [41.0, 40.2, 39.5, 39.0, 38.6], 0.20, 2.1,
             [6.1, 9.4, 12.0, 4.3, 7.7, 15.2, 3.9]),
}
# course slide quotes (bid, ask) that override the model: (symbol, expiry, strike, right)
SLIDE_QUOTES = {
    ("RHT", "20180329", 155.0, "C"): (3.80, 4.00), ("RHT", "20180406", 155.0, "C"): (4.20, 4.70),
    ("RHT", "20180329", 152.5, "C"): (5.00, 5.30), ("RHT", "20180406", 152.5, "C"): (5.60, 6.00),
    ("LULU", "20180329", 80.0, "C"): (3.20, 3.45), ("LULU", "20180406", 80.0, "C"): (3.50, 3.75),
    ("LULU", "20180413", 80.0, "C"): (3.80, 4.30), ("LULU", "20180329", 80.5, "C"): (2.89, 3.35),
    ("LULU", "20180406", 80.5, "C"): (3.25, 3.45), ("LULU", "20180413", 80.5, "C"): (3.50, 4.00),
    ("LULU", "20180329", 79.5, "P"): (3.45, 3.75),
}
RHT_PAST = [("2017-12-19", 128.86, 125.60), ("2017-09-21", 105.76, 111.30), ("2017-06-21", 89.96, 98.92),
            ("2017-03-27", 82.20, 86.60), ("2016-12-21", 79.79, 70.30), ("2016-09-21", 77.04, 82.01),
            ("2016-06-22", 79.75, 75.31)]
WIDE_BACK = {("PAYX", "20180406"): 3.0}   # a long leg whose market is too wide: the screen moves to the next expiry
NO_FINNHUB_HISTORY = {"CTAS"}       # shows past dates found from the bars ("inferred")
NASDAQ_DATE = {"FINL": "2018-03-28"}  # Nasdaq and Finnhub disagree: a conflict to resolve on the Calendar page


def past_events(sym: str) -> list[tuple[str, float, float]]:
    """(report date, close before, open after), newest first."""
    if sym == "RHT":
        return RHT_PAST
    name, rdate, timing, S, inc, ivs, sp, cap, gp = TICKERS[sym]
    out, p = [], S
    d = date.fromisoformat(rdate)
    for i, g in enumerate(gp):
        d = d - timedelta(days=91)
        while not is_trading_day(d) or (timing == "BMO" and not is_trading_day(d)):
            d -= timedelta(days=1)
        p = p * 0.96
        up = (i * 7 + len(sym)) % 3 != 0
        c = round(p, 2)
        o = round(c * (1 + (1 if up else -1) * g / 100), 2)
        out.append((d.isoformat(), c, o))
    return out


def make_bars(sym: str) -> list[dict]:
    """Daily bars from Mar 2015 to Fri Mar 23 2018 whose earnings gaps are exactly the past events above."""
    name, rdate, timing, S, *_ = TICKERS[sym]
    days = []
    d = date(2015, 3, 23)
    end = date(2018, 3, 26)
    while d <= end:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    anchors = {}      # bar date -> ("close", value) or ("open", value)
    for ds, c, o in past_events(sym):
        rd = date.fromisoformat(ds)
        before_bar, after_bar = sessions(rd, timing)
        anchors[before_bar] = anchors.get(before_bar, {}) | {"close": c}
        anchors[after_bar] = anchors.get(after_bar, {}) | {"open": o}
    anchors[days[-1]] = anchors.get(days[-1], {}) | {"close": S}
    # closes: interpolate in log space between known points (closes, and opens taken as that day's close level)
    pts = sorted((days.index(k), math.log(v.get("close", v.get("open")))) for k, v in anchors.items() if k in days)
    bars = []
    for i, dd in enumerate(days):
        prev = max((p for p in pts if p[0] <= i), default=pts[0])
        nxt = min((p for p in pts if p[0] >= i), default=pts[-1])
        w = 0 if nxt[0] == prev[0] else (i - prev[0]) / (nxt[0] - prev[0])
        base = prev[1] + (nxt[1] - prev[1]) * w
        wiggle = 0.05 * math.sin(math.pi * w) * (_h(sym, dd, "w") - 0.5)
        close = math.exp(base + wiggle)
        a = anchors.get(dd, {})
        if "close" in a:
            close = a["close"]
        prev_close = bars[-1]["close"] if bars else close
        op = a.get("open", prev_close * (1 + 0.004 * (_h(sym, dd, "o") - 0.5)))
        if "open" in a and "close" not in a:
            close = op * (1 + 0.003 * (_h(sym, dd, "c") - 0.5))
        bars.append({"date": dd, "open": round(op, 2), "close": round(close, 2)})
    return bars


def option_quote(sym: str, expiry: str, K: float, right: str, now: datetime) -> tuple[float, float, float]:
    """(bid, ask, iv) at the replay time."""
    name, rdate, timing, S, inc, ivs, sp, *_ = TICKERS[sym]
    iv = ivs[EXPIRIES.index(expiry)] / 100
    hit = SLIDE_QUOTES.get((sym, expiry, float(K), right))
    if hit:
        return hit[0], hit[1], iv
    T = (date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:])) - now.date()).days / 365
    mid = bs(S, K, T, iv, right == "C", 0.02)
    half = max(0.02, mid * sp / 2) * WIDE_BACK.get((sym, expiry), 1.0)
    q = lambda x: round(x, 2) if x < 3 else round(x * 20) / 20
    return max(0.01, q(mid - half)), q(mid + half), iv


class _Client:
    MaxRequests = 45
    RequestsInterval = 1

    @staticmethod
    def serverVersion():
        return 176


class FakeIB:
    MAX_LINES = 100

    def __init__(self):
        self.client = _Client()
        self.errorEvent = Event("errorEvent")
        self.disconnectedEvent = Event("disconnectedEvent")
        self._connected = False
        self._next_id = 500000
        self._ids: dict[tuple, int] = {}
        self.lines: dict[int, Ticker] = {}
        self.peak_lines = 0
        self.kinds: dict[str, int] = {}
        self.readonly = None

    def _count(self, name):
        self.kinds[name] = self.kinds.get(name, 0) + 1

    async def connectAsync(self, host="127.0.0.1", port=7497, clientId=1, timeout=4, readonly=False, account="",
                           raiseSyncErrors=False, fetchFields=None):
        await asyncio.sleep(0.02)
        self.readonly = readonly
        self._connected = True
        return self

    def isConnected(self):
        return self._connected

    def disconnect(self):
        if self._connected:
            self._connected = False
            self.lines.clear()
            self.disconnectedEvent.emit()

    def reqMarketDataType(self, t):
        self.market_data_type = t

    def _con_id(self, key) -> int:
        if key not in self._ids:
            self._next_id += 1
            self._ids[key] = self._next_id
        return self._ids[key]

    async def reqContractDetailsAsync(self, c: Contract):
        self._count("reqContractDetails")
        await asyncio.sleep(0.005)
        sym = c.symbol
        if sym not in TICKERS:
            return []
        if c.secType == "STK":
            out = Contract(secType="STK", symbol=sym, exchange="SMART", currency="USD", primaryExchange="NYSE",
                           conId=self._con_id((sym,)))
            return [ContractDetails(contract=out, longName=TICKERS[sym][0])]
        if c.secType == "OPT":
            e, K, r = c.lastTradeDateOrContractMonth, float(c.strike), c.right
            if e not in EXPIRIES or K not in self._strikes(sym) or r not in ("C", "P"):
                return []
            out = Contract(secType="OPT", symbol=sym, lastTradeDateOrContractMonth=e, strike=K, right=r,
                           exchange="SMART", currency="USD", multiplier="100", tradingClass=sym,
                           localSymbol=f"{sym} {e[2:]}{r}{int(K * 1000):08d}", conId=self._con_id((sym, e, K, r)))
            return [ContractDetails(contract=out)]
        return []

    def _strikes(self, sym) -> list[float]:
        S, inc = TICKERS[sym][3], TICKERS[sym][4]
        lo = math.floor(S * 0.8 / inc) * inc
        return [round(lo + i * inc, 2) for i in range(int(S * 0.4 / inc) + 2)]

    async def reqSecDefOptParamsAsync(self, underlyingSymbol, futFopExchange, underlyingSecType, underlyingConId):
        self._count("reqSecDefOptParams")
        await asyncio.sleep(0.005)
        s = underlyingSymbol
        return [OptionChain("SMART", underlyingConId, s, "100", list(EXPIRIES), self._strikes(s)),
                OptionChain("CBOE", underlyingConId, s, "100", list(EXPIRIES), self._strikes(s))]

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False, mktDataOptions=None):
        self._count("reqMktData")
        if len(self.lines) >= self.MAX_LINES and contract.conId not in self.lines:
            self.errorEvent.emit(0, 101, "Max number of tickers has been reached", contract)
        tk = Ticker(contract=contract)
        self.lines[contract.conId] = tk
        self.peak_lines = max(self.peak_lines, len(self.lines))
        asyncio.get_running_loop().call_later(0.05 + 0.1 * _h(contract.conId), self._fill, tk)
        return tk

    def _fill(self, tk: Ticker):
        c = tk.contract
        if not self._connected:
            return
        sym = c.symbol
        name, rdate, timing, S, *_ = TICKERS[sym]
        if c.secType == "STK":
            tk.last, tk.bid, tk.ask, tk.close = S, round(S - 0.02, 2), round(S + 0.02, 2), S
            return
        from .demo import replay_clock
        bid, ask, iv = option_quote(sym, c.lastTradeDateOrContractMonth, c.strike, c.right, replay_clock())
        tk.bid, tk.ask = bid, ask
        tk.modelGreeks = OptionComputation(0, impliedVol=iv, delta=0.5 if c.right == "C" else -0.5,
                                           optPrice=(bid + ask) / 2, undPrice=S)

    def cancelMktData(self, contract):
        self._count("cancelMktData")
        self.lines.pop(contract.conId, None)

    async def reqHistoricalDataAsync(self, contract, endDateTime, durationStr, barSizeSetting, whatToShow, useRTH,
                                     formatDate=1, keepUpToDate=False, chartOptions=(), timeout=60):
        self._count("reqHistoricalData")
        await asyncio.sleep(0.01)
        if whatToShow == "OPTION_IMPLIED_VOLATILITY":     # 6 months of 30-day IV: low at 70% of the long-leg IV
            base = TICKERS[contract.symbol][5][-1] / 100
            return [BarData(date=f"d{i}", open=0, high=0, low=0, close=base * (0.85 + 0.15 * math.cos(i / 9)),
                            volume=0, average=0, barCount=0) for i in range(126)]
        return [BarData(date=b["date"], open=b["open"], high=max(b["open"], b["close"]), low=min(b["open"], b["close"]),
                        close=b["close"], volume=1e6, average=b["close"], barCount=1000) for b in make_bars(contract.symbol)]


class FakeFetcher:
    """Nasdaq and Finnhub answers for the sample week, in their real JSON shapes."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def get_json(self, url: str, params: dict) -> dict:
        self.calls.append((url, dict(params)))
        if "api.nasdaq.com/api/calendar" in url:
            d = params["date"]
            rows = []
            for s, (name, rdate, timing, S, inc, ivs, sp, cap, gp) in TICKERS.items():
                if NASDAQ_DATE.get(s, rdate) == d:
                    rows.append({"symbol": s, "name": name, "marketCap": f"${cap * 1e9:,.0f}",
                                 "time": "time-pre-market" if timing == "BMO" else "time-after-hours"})
            return {"data": {"rows": rows}, "status": {"rCode": 200}}
        if "earnings-surprise" in url:
            s = url.split("/company/")[1].split("/")[0]
            rows = [{"dateReported": datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d/%Y")} for d, _, _ in past_events(s)[:4]] \
                if s in TICKERS and s not in NO_FINNHUB_HISTORY else []
            return {"data": {"earningsSurpriseTable": {"rows": rows}}}
        if "finnhub.io" in url:
            s = params.get("symbol")
            lo, hi = params["from"], params["to"]
            out = []
            for sym, (name, rdate, timing, *_rest) in TICKERS.items():
                if s and sym != s:
                    continue
                dates = [rdate] if not s else [d for d, _, _ in past_events(sym)] if sym not in NO_FINNHUB_HISTORY else []
                for d in dates:
                    if lo <= d <= hi:
                        out.append({"symbol": sym, "date": d, "hour": timing.lower()})
            return {"earningsCalendar": out}
        return {}

    async def close(self):
        pass
