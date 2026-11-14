"""The app: one read-only connection to TWS, the earnings calendar, and the option screens for each candidate.

Load control follows GEX Live: a market data line budget, request pacing, snapshots that hold a line only until
the bid, ask and IB's model IV arrive, and historical data requests spaced under IBKR's 60 per 10 minutes."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import time
from datetime import date, datetime, timedelta

from ib_async import Contract, Option, Stock
from ib_async.ib import StartupFetch

from . import config as config_mod
from . import model as M
from .config import MARKET_DATA_TYPES, AppConfig
from .earnings import EarningsBook
from .history import historical_vol, past_gaps
from .journal import Journal
from .store import Store
from .timeutil import (add_trading_days, in_entry_window, is_rth, now_et, on_or_after, parse_expiry, sessions,
                       trade_date_for)

log = logging.getLogger("earningsdesk")

INFO_CODES = {2103, 2104, 2105, 2106, 2107, 2108, 2119, 2157, 2158, 10167, 300, 10197}


class LineBudget:
    """Market data lines this app may hold at once."""

    def __init__(self, max_lines: int):
        self.max = max_lines
        self.used = 0
        self.peak = 0
        self._cond = asyncio.Condition()

    async def acquire(self):
        async with self._cond:
            await self._cond.wait_for(lambda: self.used < self.max)
            self.used += 1
            self.peak = max(self.peak, self.used)

    def release(self, n: int = 1):
        self.used = max(0, self.used - n)
        asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self):
        async with self._cond:
            self._cond.notify_all()


class Pacer:
    """Spaces requests so they leave at the rate ib_async can send them (each snapshot is a request and a cancel)."""

    def __init__(self, per_second_fn, share: float = 0.9):
        self.rate = per_second_fn
        self.share = share
        self._next = 0.0

    async def wait(self, msgs: float = 1):
        rate = max(0.01, self.rate() * self.share)
        now = time.monotonic()
        t = max(now, self._next)
        self._next = t + msgs / rate
        if t > now:
            await asyncio.sleep(t - now)


class WindowLimiter:
    """At most n requests in any rolling window (IBKR: 60 historical data requests per 10 minutes)."""

    def __init__(self, n_fn, window_s: float):
        self.n = n_fn
        self.window = window_s
        self.sent: collections.deque = collections.deque()

    async def wait(self):
        while True:
            now = time.monotonic()
            while self.sent and self.sent[0] <= now - self.window:
                self.sent.popleft()
            if len(self.sent) < max(1, self.n()):
                self.sent.append(now)
                return
            await asyncio.sleep(self.sent[0] + self.window - now + 0.01)


class ScreenError(Exception):
    pass


def _pos(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def ticker_price(tk) -> float | None:
    for v in (tk.last, (tk.bid + tk.ask) / 2 if _pos(tk.bid) and _pos(tk.ask) else None, tk.close):
        if _pos(v):
            return float(v)
    return None


class App:
    def __init__(self, cfg: AppConfig, cfg_path: str, ib_factory, demo: bool = False, fetcher=None, clock=None):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.ib_factory = ib_factory
        self.demo = demo
        self.clock = clock or now_et          # demo mode runs on a replay clock
        self.ib = None
        self.budget = LineBudget(cfg.load.max_lines)
        self.pacer = Pacer(lambda: self.cfg.load.requests_per_second / 2)
        self.hist_limit = WindowLimiter(lambda: self.cfg.load.history_per_10_minutes, 600.0)
        self.book = EarningsBook(cfg.earnings, cfg.data_path(), fetcher, today_fn=lambda: self.clock().date())
        self.cands: dict[str, dict] = {}
        self.clients: set[asyncio.Queue] = set()
        self.messages = collections.deque(maxlen=200)
        self.connection = {"state": "Disconnected", "client_id": None, "since": None, "error": None}
        self._requests = collections.deque()
        self._tasks: list[asyncio.Task] = []
        self._disconnected = asyncio.Event()
        self._reconnect_now = asyncio.Event()
        self._screen_now = asyncio.Event()
        self._earnings_now = asyncio.Event()
        self._screening: set[str] = set()
        self._stock_ids: dict[str, Contract] = {}
        self.store = Store(cfg.data_path() / "desk.db")
        self.journal = Journal(self.store)

    # ------------------------------------------------------------------ messages and broadcast
    def note_requests(self, n: int = 1):
        now = time.monotonic()
        for _ in range(n):
            self._requests.append(now)

    def request_rate(self) -> float:
        cutoff = time.monotonic() - 60.0
        while self._requests and self._requests[0] < cutoff:
            self._requests.popleft()
        return len(self._requests) / 60.0

    def broadcast(self, msg: dict):
        for q in list(self.clients):
            if q.qsize() > 500:
                continue
            q.put_nowait(msg)

    def add_message(self, level: str, text: str):
        now = time.time()
        for m in reversed(list(self.messages)[-30:]):
            if m["text"] == text and now - m.get("ts", 0) < 600:
                m["count"] = m.get("count", 1) + 1
                m["time"] = datetime.now().strftime("%H:%M:%S")
                m["ts"] = now
                self.broadcast({"type": "message", "message": m, "update": True})
                return
        self._msg_id = getattr(self, "_msg_id", 0) + 1
        self.messages.append({"id": self._msg_id, "time": datetime.now().strftime("%H:%M:%S"), "level": level,
                              "text": text, "ts": now})
        self.broadcast({"type": "message", "message": self.messages[-1]})

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        self._tasks = [asyncio.create_task(self._connection_loop(), name="connection"),
                       asyncio.create_task(self._earnings_loop(), name="earnings"),
                       asyncio.create_task(self._screen_loop(), name="screens"),
                       asyncio.create_task(self._journal_loop(), name="forward test")]

    async def shutdown(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self.ib is not None and self.ib.isConnected():
            self.ib.disconnect()
        close = getattr(self.book.fetch, "close", None)
        if close:
            await close()
        self.store.close()

    def connected(self) -> bool:
        return self.ib is not None and self.ib.isConnected() and self.connection["state"] == "Connected"

    def _set_conn(self, **kw):
        self.connection.update(kw)
        self.broadcast({"type": "connection", "connection": self.connection_state()})

    def connection_state(self) -> dict:
        c = self.cfg.connection
        return {**self.connection, "host": c.host, "port": c.port, "market_data_type": c.market_data_type,
                "demo": self.demo, "lines_used": self.budget.used, "lines_max": self.budget.max,
                "lines_peak": self.budget.peak, "requests_per_second": round(self.request_rate(), 2),
                "clock": self.clock().strftime("%a %b %d %Y %H:%M ET"),
                "clock_date": trade_date_for(self.clock()).isoformat()}

    async def _connection_loop(self):
        while True:
            c = self.cfg.connection
            self._set_conn(state="Connecting", error=None)
            ib = self.ib_factory()
            connected_id, last_error = None, None
            for attempt in range(5):
                cid = c.client_id + attempt
                try:
                    # read-only: this app never places orders
                    await ib.connectAsync(c.host, c.port, clientId=cid, timeout=8, readonly=True,
                                          fetchFields=StartupFetch(0))
                    connected_id = cid
                    break
                except Exception as e:  # noqa: BLE001
                    last_error = str(e) or type(e).__name__
                    try:
                        ib.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    m = last_error.lower()
                    if "326" in m or "already in use" in m or "client id" in m:
                        continue
                    break
            if connected_id is None:
                self._set_conn(state="Disconnected", error=f"Cannot reach TWS at {c.host}:{c.port} ({last_error}). "
                               "Check that TWS or IB Gateway is running and the API is enabled.")
                await self._sleep_or_reconnect(15)
                continue
            self.ib = ib
            ib.client.MaxRequests = self.cfg.load.requests_per_second
            ib.client.RequestsInterval = 1
            ib.reqMarketDataType(MARKET_DATA_TYPES.get(c.market_data_type, 1))
            ib.errorEvent += self._on_error
            self._disconnected.clear()
            ib.disconnectedEvent += self._on_disconnected
            self._set_conn(state="Connected", client_id=connected_id, since=datetime.now().isoformat(timespec="seconds"))
            self.add_message("info", f"Connected to TWS {c.host}:{c.port} with client ID {connected_id} (read-only).")
            self._screen_now.set()
            waiter = asyncio.create_task(self._disconnected.wait())
            reconnect = asyncio.create_task(self._reconnect_now.wait())
            await asyncio.wait({waiter, reconnect}, return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            reconnect.cancel()
            self._reconnect_now.clear()
            try:
                ib.errorEvent -= self._on_error
                ib.disconnectedEvent -= self._on_disconnected
                if ib.isConnected():
                    ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.budget.used = 0
            self._stock_ids.clear()
            self._set_conn(state="Disconnected", error="Connection to TWS closed; reconnecting.")
            await self._sleep_or_reconnect(5)

    async def _sleep_or_reconnect(self, seconds: float):
        try:
            await asyncio.wait_for(self._reconnect_now.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._reconnect_now.clear()

    def _on_disconnected(self):
        self._disconnected.set()

    def _on_error(self, req_id, code, msg, contract):
        if code in INFO_CODES:
            return
        if code == 1100:
            self.add_message("warning", "TWS lost its connection to IBKR's servers. Screens wait for it to come back.")
            return
        if code in (1101, 1102):
            self.add_message("info", "TWS is connected to IBKR's servers again.")
            self._screen_now.set()
            return
        if code in (200, 354, 10090, 10167, 10168) and contract is not None and getattr(contract, "secType", "") == "OPT":
            return   # a leg with no data shows as "no quote" on its candidate; one line per leg would flood the log
        if code == 162 and "query cancelled" in msg.lower():
            return
        what = f" ({getattr(contract, 'localSymbol', '') or getattr(contract, 'symbol', '')})" if contract is not None else ""
        if code == 354:
            msg = "Requested market data is not subscribed (option quotes need the OPRA subscription)."
        self.add_message("warning" if code in (354, 200, 10090, 10168) else "error", f"TWS {code}: {msg}{what}")

    # ------------------------------------------------------------------ earnings
    async def _earnings_loop(self):
        while True:
            try:
                await self.book.refresh()
                self._sync_candidates()
                n = len(self.book.events)
                self.add_message("info", f"Earnings calendar: {n} announcements in the next {self.cfg.earnings.days_ahead} days.")
                for src, st in self.book.source_status.items():
                    if not st["ok"]:
                        self.add_message("warning", f"{src}: {st['text']}")
                self._screen_now.set()
            except Exception as e:  # noqa: BLE001
                log.exception("earnings refresh failed")
                self.add_message("error", f"Earnings calendar refresh failed: {e}")
            self.broadcast(self.state())
            try:
                await asyncio.wait_for(self._earnings_now.wait(), self.cfg.earnings.refresh_minutes * 60)
            except asyncio.TimeoutError:
                pass
            self._earnings_now.clear()

    def _sync_candidates(self):
        """Candidates follow the calendar; option data already loaded is kept when the event is unchanged."""
        new = {}
        for sym, ev in self.book.events.items():
            entry, exit_ = sessions(ev.date, ev.timing)
            old = self.cands.get(sym)
            base = {"symbol": sym, "name": ev.name, "date": ev.date.isoformat(), "timing": ev.timing or "?",
                    "status": ev.status, "note": ev.note, "sources": ev.to_dict()["sources"],
                    "verified": ev.status in ("verified", "manual", "single"),
                    "entry": entry.isoformat(), "model": exit_.isoformat(), "market_cap": ev.market_cap}
            if old and old["date"] == base["date"] and old["timing"] == base["timing"]:
                old.update(base)
                new[sym] = old
            else:
                new[sym] = {**base, "state": "pending", "error": "", "chain": {}, "gaps": []}
        self.cands = new
        for c in new.values():
            self.analyze_default(c)

    def due_for_screen(self) -> list[dict]:
        today = trade_date_for(self.clock())
        last = add_trading_days(today, self.cfg.strategy.screen_days_ahead)
        return [c for c in self.cands.values() if today.isoformat() <= c["entry"] <= last.isoformat()]

    # ------------------------------------------------------------------ screens
    async def _screen_loop(self):
        while True:
            try:
                await asyncio.wait_for(self._screen_now.wait(), 60)
            except asyncio.TimeoutError:
                pass
            self._screen_now.clear()
            if not self.connected():
                continue
            due = self.due_for_screen()
            now = self.clock()
            for c in due:
                stale = (time.time() - c.get("screened_ts", 0)) / 60
                today = c["entry"] == trade_date_for(now).isoformat()
                every = self.cfg.strategy.screen_minutes if (today and in_entry_window(now)) else 60
                if c["state"] in ("pending", "error") or stale >= every or c.get("force"):
                    c["force"] = False
                    await self.screen(c["symbol"])

    def request_screen(self, symbol: str | None = None):
        for c in self.cands.values():
            if symbol is None or c["symbol"] == symbol:
                c["force"] = True
        self._screen_now.set()

    async def _ask(self, awaitable, what: str, timeout: float = 30.0):
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except asyncio.TimeoutError:
            raise ScreenError(f"TWS did not answer the request for {what} within {timeout:.0f} s.") from None

    async def _details(self, c: Contract, what: str):
        await self.pacer.wait(1)
        self.note_requests()
        res = await self._ask(self.ib.reqContractDetailsAsync(c), what)
        return res[0].contract if res else None

    async def _stock(self, sym: str) -> Contract:
        c = self._stock_ids.get(sym)
        if c is None:
            c = await self._details(Stock(sym, "SMART", "USD"), f"{sym} stock")
            if c is None:
                raise ScreenError(f"TWS does not know the stock {sym}.")
            self._stock_ids[sym] = c
        return c

    async def snapshot(self, contract: Contract, need_iv: bool, timeout: float | None = None):
        """Holds one line until bid / ask (and IB's model IV for options) arrive, or the timeout."""
        timeout = timeout or self.cfg.load.option_timeout_seconds
        await self.budget.acquire()
        await self.pacer.wait(2)
        tk = self.ib.reqMktData(contract, "", False, False)
        self.note_requests()
        try:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                await asyncio.sleep(0.15)
                quoted = _pos(tk.bid) and _pos(tk.ask)
                iv = tk.modelGreeks is not None and _pos(tk.modelGreeks.impliedVol)
                if quoted and (iv or not need_iv):
                    break
                if not need_iv and _pos(tk.last) and time.monotonic() > end - timeout / 2:
                    break
        finally:
            try:
                self.ib.cancelMktData(contract)
                self.note_requests()
            except Exception:  # noqa: BLE001
                pass
            self.budget.release()
        mg = tk.modelGreeks
        return {"bid": float(tk.bid) if _pos(tk.bid) else None, "ask": float(tk.ask) if _pos(tk.ask) else None,
                "last": float(tk.last) if _pos(tk.last) else None, "close": float(tk.close) if _pos(tk.close) else None,
                "iv": float(mg.impliedVol) if mg is not None and _pos(mg.impliedVol) and mg.impliedVol < 10 else None,
                "delta": float(mg.delta) if mg is not None and mg.delta is not None else None,
                "price": ticker_price(tk)}

    async def bars(self, stock: Contract) -> list[dict]:
        """About three years of daily bars (12 quarters), kept on disk and fetched again once a day."""
        sym = stock.symbol
        folder = self.cfg.data_path() / "cache" / "bars"
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"{sym}.json"
        today = self.clock().date().isoformat()
        if p.exists():
            try:
                hit = json.loads(p.read_text(encoding="utf-8"))
                if hit.get("fetched") == today:
                    return hit["bars"]
            except Exception:  # noqa: BLE001
                pass
        await self.hist_limit.wait()
        self.note_requests()
        raw = await self._ask(self.ib.reqHistoricalDataAsync(stock, "", "3 Y", "1 day", "TRADES", True, 1),
                              f"{sym} daily bars", 90)
        bars = [{"date": str(b.date)[:10], "open": b.open, "close": b.close} for b in (raw or [])]
        p.write_text(json.dumps({"fetched": today, "bars": bars}), encoding="utf-8")
        return bars

    async def iv_low(self, stock: Contract) -> float | None:
        """The course spreadsheet's "Back Vol Bottom Out": the lowest the stock's 30-day implied volatility has been
        in the last 6 months (IBKR OPTION_IMPLIED_VOLATILITY daily bars), kept on disk and fetched once a day."""
        sym = stock.symbol
        folder = self.cfg.data_path() / "cache" / "iv"
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"{sym}.json"
        today = self.clock().date().isoformat()
        if p.exists():
            try:
                hit = json.loads(p.read_text(encoding="utf-8"))
                if hit.get("fetched") == today:
                    return hit["low"]
            except Exception:  # noqa: BLE001
                pass
        await self.hist_limit.wait()
        self.note_requests()
        raw = await self._ask(self.ib.reqHistoricalDataAsync(stock, "", "6 M", "1 day", "OPTION_IMPLIED_VOLATILITY",
                                                             True, 1), f"{sym} implied volatility history", 90)
        vals = [b.close for b in (raw or []) if b.close and 0 < b.close < 10]
        low = min(vals) if vals else None
        p.write_text(json.dumps({"fetched": today, "low": low}), encoding="utf-8")
        return low

    async def screen(self, symbol: str):
        c = self.cands.get(symbol)
        if c is None or symbol in self._screening:
            return
        self._screening.add(symbol)
        c["state"] = "loading"
        self.analyze_default(c)
        self.broadcast({"type": "cand", "cand": self.public_cand(c)})
        try:
            await self._screen(c)
            c["state"], c["error"] = "ready", ""
        except ScreenError as e:
            c["state"], c["error"] = "error", str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("screen %s", symbol)
            c["state"], c["error"] = "error", f"{type(e).__name__}: {e}"
        finally:
            c["screened_ts"] = time.time()
            c["screened"] = self.clock().strftime("%H:%M ET")
            # after the close TWS sends no live option quotes (Frozen market data sends the closing ones)
            c["quotes_live"] = is_rth(self.clock()) or self.cfg.connection.market_data_type != "Live"
            self._screening.discard(symbol)
        self.analyze_default(c)
        self.broadcast({"type": "cand", "cand": self.public_cand(c)})
        self.maybe_record_entry(c)
        try:
            self.store.log_screen(self.clock().strftime("%Y-%m-%d %H:%M:%S"), c, c.get("a"), c.get("a_alt"))
        except Exception:  # noqa: BLE001
            log.exception("saving screen %s", symbol)

    def maybe_record_entry(self, c: dict):
        """Forward test: keep the latest last-hour screen of today's entries (see journal.py)."""
        now = self.clock()
        if not (self.cfg.strategy.record_forward_test and c.get("state") == "ready" and c.get("quotes_live")
                and in_entry_window(now) and c["entry"] == now.date().isoformat()):
            return
        if self.journal.record_entry(c, now) is not None:
            self.broadcast({"type": "journal", "journal": self.journal.records[-300:]})

    async def _journal_loop(self):
        while True:
            await asyncio.sleep(20)
            if not self.cfg.strategy.record_forward_test or not self.connected():
                continue
            try:
                await self.journal_pass()
            except Exception as e:  # noqa: BLE001
                log.exception("forward test pass")
                self.add_message("warning", f"Forward test: could not quote next-day spreads ({e}).")

    async def journal_pass(self):
        now = self.clock()
        due = self.journal.due(now.date().isoformat(), now.strftime("%H:%M"), sorted(self.cfg.strategy.exit_checks))
        for rec, check in due:
            legs = []
            for right, sd in rec["sides"].items():
                legs += [(right, "f", sd["front"], sd["K"]), (right, "b", sd["back"], sd["K"])]
            got = await asyncio.gather(*[self.quote_option(rec["symbol"], e, k, r) for r, _, e, k in legs],
                                       return_exceptions=True)
            q = {}
            for (r, which, _, _), g in zip(legs, got):
                q.setdefault(r, {})[which] = g if isinstance(g, dict) else {}
            stock = await self._stock(rec["symbol"])
            S = (await self.snapshot(stock, need_iv=False, timeout=4))["price"]
            self.journal.record_exit(rec["id"], check, S, {r: (v.get("f", {}), v.get("b", {})) for r, v in q.items()}, now)
            self.add_message("info", f"Forward test: {rec['symbol']} spreads quoted at the {check} check.")
        if due:
            self.broadcast({"type": "journal", "journal": self.journal.records[-300:]})

    async def quote_option(self, symbol: str, expiry: str, strike: float, right: str) -> dict:
        o = await self._details(Option(symbol, expiry, float(strike), right, "SMART", currency="USD"),
                                f"{symbol} {expiry} {strike}{right}")
        if o is None:
            return {}
        return await self.snapshot(o, need_iv=True)

    async def _screen(self, c: dict):
        sym = c["symbol"]
        st = self.cfg.strategy
        stock = await self._stock(sym)
        q = await self.snapshot(stock, need_iv=False, timeout=4)
        S = q["price"]
        if not S:
            raise ScreenError(f"No price for {sym} from TWS (market data subscription?).")
        c["S"] = round(S, 2)
        await self.pacer.wait(1)
        self.note_requests()
        chains = await self._ask(self.ib.reqSecDefOptParamsAsync(sym, "", "STK", stock.conId), f"{sym} option chain")
        chain = next((x for x in chains or [] if x.exchange == "SMART" and x.tradingClass == sym), None) \
            or next((x for x in chains or [] if x.exchange == "SMART"), None)
        if chain is None:
            raise ScreenError(f"{sym} has no listed options.")
        exit_ = date.fromisoformat(c["model"])
        exps = sorted(e for e in chain.expirations if (parse_expiry(e) or date.min) >= exit_)
        if not exps:
            raise ScreenError("No option expiry after the report.")
        front = exps[0]
        if (parse_expiry(front) - exit_).days > 10:
            raise ScreenError(f"The first expiry after the report is {parse_expiry(front):%b %d}, more than 10 days "
                              "later: no weekly options to sell into the report.")
        backs = [e for e in exps[1:] if (parse_expiry(e) - date.fromisoformat(c["entry"])).days <= st.max_back_days][:4]
        if not backs:
            raise ScreenError("No later expiry for the long leg within the allowed days.")
        strikes = sorted(set(chain.strikes))
        below = [k for k in strikes if k <= S][-2:]
        above = [k for k in strikes if k > S][:2]
        near = below + above
        if not near:
            raise ScreenError("No strikes near the price.")
        K = M.default_strike(near, S, st.strike_rule)
        c.update(expiries=[front] + backs, front=front, strikes=near, K=K, multiplier=chain.multiplier,
                 trading_class=chain.tradingClass)

        legs = []
        for e in [front] + backs:            # calls and puts at every strike: both calendars, and the straddle
            for k in near:
                legs.append((e, k, "C"))
                legs.append((e, k, "P"))
        opts = [Option(sym, e, k, r, "SMART", tradingClass=chain.tradingClass, currency="USD") for e, k, r in legs]
        qualified = await asyncio.gather(*[self._details(o, f"{sym} {o.lastTradeDateOrContractMonth} {o.strike}{o.right}")
                                           for o in opts], return_exceptions=True)
        quotes = await asyncio.gather(*[self.snapshot(o, need_iv=True) for o in qualified
                                        if isinstance(o, Contract)], return_exceptions=True)
        book: dict = {}
        qi = iter(quotes)
        for (e, k, r), o in zip(legs, qualified):
            if not isinstance(o, Contract):
                continue
            qq = next(qi)
            if isinstance(qq, Exception):
                continue
            book.setdefault(e, {}).setdefault(r, {})[float(k)] = {**qq, "conId": o.conId}
        c["chain"] = book
        c["back"], c["back_reason"] = self.pick_back(c)
        c["back_by_right"], c["back_reason_by_right"] = {}, {}
        for r in ("C", "P"):
            c["back_by_right"][r], c["back_reason_by_right"][r] = self.pick_back(c, r)

        try:
            bars = await self.bars(stock)
            c["hv"] = historical_vol(bars, 30)
            known = await self.book.past_dates(sym, date.fromisoformat(c["date"]), st.lookback + 2)
            c["gaps"] = past_gaps(bars, date.fromisoformat(c["date"]), c["timing"], known, st.lookback)
            inferred = sum(1 for g in c["gaps"] if g.get("source") == "inferred")
            c["gap_note"] = (f"{inferred} of {len(c['gaps'])} past report dates were found from the largest overnight "
                             "gap near the expected date (no source listed them)." if inferred else "")
        except ScreenError as e:
            c["gaps"], c["gap_note"] = [], f"No gap history: {e}"
        try:
            c["iv_low"] = await self.iv_low(stock)
        except Exception as e:  # noqa: BLE001
            log.info("%s IV history: %s", sym, e)
            c["iv_low"] = None

    def pick_back(self, c: dict, right: str | None = None) -> tuple[str | None, str]:
        st = self.cfg.strategy
        r = right or ("C" if c["K"] >= c["S"] else "P")
        return M.pick_back(c["chain"], c["expiries"], c["K"], r, st.skip_back_iv_points, st.ba_pass_percent,
                           st.ba_review_percent)

    # ------------------------------------------------------------------ analysis
    def params(self, **over) -> M.Params:
        return M.Params.from_strategy(self.cfg.strategy, **over)

    def analyze_default(self, c: dict):
        if c.get("S") and c.get("chain") and c.get("front") and c.get("strikes"):
            K = M.default_strike(c["strikes"], c["S"], self.cfg.strategy.strike_rule)
            if K != c.get("K"):                     # the strike rule changed in Settings: same quotes, new legs
                c["K"] = K
                c["back"], c["back_reason"] = self.pick_back(c)
                c["back_by_right"] = {r: None for r in ("C", "P")}
                c["back_reason_by_right"] = {}
                for r in ("C", "P"):
                    c["back_by_right"][r], c["back_reason_by_right"][r] = self.pick_back(c, r)
        if c.get("S") and c.get("chain") and c.get("front"):
            c["a"] = M.analyze(c, self.params())
            # the same strike on the other side: a put calendar when the default is a call, and the reverse
            other = "P" if c["a"].get("right") == "C" else "C"
            c["a_alt"] = M.analyze(c, self.params(right=other))
            c["by_strike"] = self.strike_table(c)
        else:
            c["a"], c["a_alt"], c["by_strike"] = {}, {}, []
        c["stage"] = M.stage(c, c["a"]) if c.get("state") == "ready" else (
            {"k": "verify", "lbl": "Needs verification", "cls": "p-bad"} if not c["verified"] else
            {"k": "data", "lbl": {"loading": "Loading", "error": "No data"}.get(c.get("state"), "Not screened yet"),
             "cls": "p-neutral"})
        if c.get("state") == "ready" and c.get("quotes_live") is False and c["stage"]["k"] in ("ba", "data"):
            c["stage"] = {"k": "closed", "lbl": "Market closed: quotes not live", "cls": "p-neutral"}

    def strike_table(self, c: dict) -> list[dict]:
        """The Is / Was screen at every nearby strike, call and put, at the settings' crush: there is no fixed strike
        rule beyond the course's default, so the page lets you compare IV, debit and endurable gap across strikes."""
        keep = ("K", "right", "front", "back", "fIV", "bIV", "ivDiff", "baRatio", "baOverall", "entry", "meg", "hits",
                "n", "verdict", "error")
        out = []
        for k in c.get("strikes") or []:
            for r in ("C", "P"):
                try:
                    a = M.analyze(c, self.params(K=float(k), right=r))
                except Exception as e:  # noqa: BLE001
                    a = {"K": k, "right": r, "error": str(e)}
                out.append({f: a.get(f) for f in keep} | {"K": float(k), "right": r})
        return out

    def analyze(self, symbol: str, body: dict) -> dict:
        c = self.cands.get(symbol)
        if c is None or not c.get("chain"):
            return {"error": f"{symbol} has no option data yet."}
        over = {k: body.get(k) for k in ("K", "right", "front", "back", "crush_mode", "crush_percent", "haircut_percent",
                                         "hv", "iv_low",
                                         "back_iv_change_percent", "gap_basis", "debit")}
        if over["K"] is not None:
            over["K"] = float(over["K"])
        return M.analyze(c, self.params(**over))

    def public_cand(self, c: dict) -> dict:
        out = {k: v for k, v in c.items() if k not in ("chain",)}
        out["chain"] = [{"expiry": e, "right": r, "strike": k, **{f: q.get(f) for f in ("bid", "ask", "iv", "delta")}}
                        for e, rights in sorted(c.get("chain", {}).items()) for r, ks in rights.items()
                        for k, q in sorted(ks.items())]
        return out

    # ------------------------------------------------------------------ trades log (manual, for the exit page)
    def trades(self) -> list[dict]:
        return self.store.trades()

    def save_trades(self, rows: list[dict]):
        self.store.save_trades(rows)

    async def quote_legs(self, legs: list[dict]) -> list[dict]:
        if not self.connected():
            raise ScreenError("Not connected to TWS.")
        out = []
        for leg in legs:
            o = Option(leg["symbol"], leg["expiry"], float(leg["strike"]), leg["right"], "SMART", currency="USD")
            q = await self._details(o, f"{leg['symbol']} {leg['expiry']} {leg['strike']}{leg['right']}")
            if q is None:
                out.append({**leg, "error": "unknown contract"})
                continue
            out.append({**leg, **(await self.snapshot(q, need_iv=True))})
        stock = await self._stock(legs[0]["symbol"])
        sq = await self.snapshot(stock, need_iv=False, timeout=4)
        return [{"underlying": sq["price"]}] + out

    # ------------------------------------------------------------------ settings and state
    async def apply_config(self, new_cfg: AppConfig):
        old = self.cfg
        config_mod.save(new_cfg, self.cfg_path)
        self.cfg = new_cfg
        self.book.cfg = new_cfg.earnings
        self.budget.max = new_cfg.load.max_lines
        if old.connection != new_cfg.connection and self.ib is not None:
            self._reconnect_now.set()
            if self.ib.isConnected():
                self.ib.disconnect()
        if old.earnings != new_cfg.earnings:
            self._earnings_now.set()
        for c in self.cands.values():
            self.analyze_default(c)
        self.add_message("info", "Settings saved.")
        self.broadcast(self.state())

    def state(self) -> dict:
        return {"type": "state", "connection": self.connection_state(),
                "settings": self.cfg.public_dict(),
                "earnings": {"updated": self.book.updated, "sources": self.book.source_status},
                "cands": [self.public_cand(c) for c in sorted(self.cands.values(),
                                                              key=lambda c: (c["date"], c["timing"] != "BMO", c["symbol"]))],
                "trades": self.trades(),
                "journal": self.journal.records[-300:],
                "messages": list(self.messages)}
