"""Local web server: the desk page, a small JSON API and a WebSocket that pushes every update."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from pathlib import Path

from aiohttp import WSMsgType, web

from . import config as config_mod
from .service import ScreenError
from .timeutil import parse_date

log = logging.getLogger("earningsdesk")
WEB = Path(__file__).resolve().parent.parent / "web"
SYM = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _clean(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def dumps(o) -> str:
    return json.dumps(_clean(o), separators=(",", ":"), default=str)


def json_response(o, status=200):
    return web.Response(text=dumps(o), content_type="application/json", status=status)


def _sym(s) -> str | None:
    s = str(s or "").strip().upper()
    return s if SYM.match(s) else None


def make_app(app) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_):
        return web.FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @routes.get("/api/state")
    async def state(_):
        return json_response(app.state())

    @routes.get("/api/settings")
    async def get_settings(_):
        return json_response({"settings": app.cfg.public_dict(),
                              "options": {"market_data_types": list(config_mod.MARKET_DATA_TYPES),
                                          "crush_modes": config_mod.CRUSH_MODES, "gap_bases": config_mod.GAP_BASES}})

    @routes.post("/api/settings")
    async def post_settings(request):
        try:
            new_cfg = config_mod.from_dict(await request.json(), keep_key_from=app.cfg)
        except Exception as e:  # noqa: BLE001
            return json_response({"errors": [f"Could not read the settings: {e}"]}, 400)
        errors = config_mod.validate(new_cfg)
        if errors:
            return json_response({"errors": errors}, 400)
        await app.apply_config(new_cfg)
        return json_response({"ok": True})

    @routes.post("/api/analyze")
    async def analyze(request):
        body = await request.json()
        sym = _sym(body.get("symbol"))
        if not sym:
            return json_response({"error": "Bad symbol."}, 400)
        return json_response(app.analyze(sym, body))

    @routes.post("/api/screen")
    async def screen(request):
        sym = _sym(request.query.get("symbol")) if request.query.get("symbol") else None
        app.request_screen(sym)
        return json_response({"ok": True, "connected": app.connected()})

    @routes.post("/api/earnings/refresh")
    async def refresh(_):
        app._earnings_now.set()
        return json_response({"ok": True})

    @routes.post("/api/earnings/override")
    async def override(request):
        """Confirm or correct an upcoming date: {symbol, date, timing, note}; date empty removes the override."""
        b = await request.json()
        sym = _sym(b.get("symbol"))
        if not sym:
            return json_response({"error": "Bad symbol."}, 400)
        if not b.get("date"):
            app.book.overrides.clear_upcoming(sym)
        else:
            d = parse_date(b["date"])
            if not d or b.get("timing") not in ("BMO", "AMC"):
                return json_response({"error": "Give a date and BMO or AMC."}, 400)
            app.book.overrides.set_upcoming(sym, d.isoformat(), b["timing"], str(b.get("note") or "")[:200])
        app._earnings_now.set()
        return json_response({"ok": True})

    @routes.post("/api/earnings/past")
    async def past(request):
        """Past report dates for a symbol: {symbol, rows: [{date, timing}]}. They win over the sources."""
        b = await request.json()
        sym = _sym(b.get("symbol"))
        rows = []
        for r in b.get("rows") or []:
            d = parse_date(str(r.get("date") or ""))
            if d:
                rows.append({"date": d.isoformat(), "timing": r.get("timing") if r.get("timing") in ("BMO", "AMC") else ""})
        if not sym:
            return json_response({"error": "Bad symbol."}, 400)
        app.book.overrides.set_past(sym, rows)
        app.request_screen(sym)
        return json_response({"ok": True})

    @routes.post("/api/quote")
    async def quote(request):
        b = await request.json()
        legs = []
        for leg in (b.get("legs") or [])[:4]:
            sym = _sym(leg.get("symbol"))
            if not sym or leg.get("right") not in ("C", "P") or not re.match(r"^\d{8}$", str(leg.get("expiry"))):
                return json_response({"error": "Bad leg."}, 400)
            legs.append({"symbol": sym, "expiry": str(leg["expiry"]), "strike": float(leg["strike"]), "right": leg["right"]})
        if not legs:
            return json_response({"error": "No legs."}, 400)
        try:
            return json_response({"quotes": await app.quote_legs(legs)})
        except ScreenError as e:
            return json_response({"error": str(e)}, 409)

    @routes.post("/api/trades")
    async def trades(request):
        """The trades you entered yourself (this app places no orders): the whole list is sent back each time."""
        b = await request.json()
        rows = [r for r in (b.get("trades") or []) if isinstance(r, dict)][:200]
        app.save_trades(rows)
        app.broadcast({"type": "trades", "trades": rows})
        return json_response({"ok": True})

    @routes.get("/api/screens")
    async def screens(request):
        """Screen history from desk.db, newest first: /api/screens?symbol=RHT&limit=100"""
        sym = (request.query.get("symbol") or "").upper() or None
        try:
            limit = max(1, min(5000, int(request.query.get("limit", 500))))
        except ValueError:
            limit = 500
        return web.json_response({"screens": app.store.screens(sym, limit)})

    @routes.get("/api/journal.csv")
    async def journal_csv(_):
        return web.Response(text=app.journal.csv(sorted(app.cfg.strategy.exit_checks)), content_type="text/csv",
                            headers={"Content-Disposition": 'attachment; filename="forward_test.csv"'})

    @routes.get("/ws")
    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        app.clients.add(q)
        try:
            await ws.send_str(dumps(app.state()))

            async def pump():
                while True:
                    await ws.send_str(dumps(await q.get()))

            async def periodic():
                while True:
                    await asyncio.sleep(2)
                    q.put_nowait({"type": "connection", "connection": app.connection_state()})

            tasks = [asyncio.create_task(pump()), asyncio.create_task(periodic())]
            try:
                async for m in ws:
                    if m.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                        break
            finally:
                for t in tasks:
                    t.cancel()
        finally:
            app.clients.discard(q)
        return ws

    web_app = web.Application()

    async def no_cache(request, response):
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
    web_app.on_response_prepare.append(no_cache)
    web_app.add_routes(routes)
    web_app.router.add_static("/static/", WEB, show_index=False)
    return web_app


async def serve(app, host: str, port: int):
    runner = web.AppRunner(make_app(app), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
