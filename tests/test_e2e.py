"""The whole app against the simulated TWS and the canned earnings feeds (the course sample week)."""
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from earningsdesk import config as C
from earningsdesk.__main__ import build_app
from earningsdesk.server import make_app


async def _run(tmp_path, body):
    cfg = C.AppConfig()
    cfg.load.option_timeout_seconds = 2
    app = build_app(cfg, str(tmp_path / "settings.json"), demo=True)
    await app.start()
    try:
        for _ in range(400):
            await asyncio.sleep(0.1)
            if app.cands and all(c["state"] in ("ready", "error") for c in app.cands.values()):
                break
        client = TestClient(TestServer(make_app(app)))
        await client.start_server()
        try:
            await body(app, client)
        finally:
            await client.close()
    finally:
        await app.shutdown()


def test_sample_week_end_to_end(tmp_path):
    async def body(app, client):
        assert app.ib.readonly is True                       # never connects with order rights
        assert app.connection["client_id"] == 290
        c = app.cands
        assert set(c) == {"RHT", "PAYX", "LULU", "CTAS", "WBA", "PVH", "MKC", "FINL"}
        assert all(x["state"] == "ready" for x in c.values()), {k: x["error"] for k, x in c.items()}
        assert c["FINL"]["status"] == "conflict" and c["FINL"]["stage"]["k"] == "verify"
        assert c["RHT"]["entry"] == "2018-03-26" and c["RHT"]["model"] == "2018-03-27"
        assert c["PAYX"]["entry"] == "2018-03-26" and c["PAYX"]["model"] == "2018-03-27"   # BMO: enter the day before
        assert c["RHT"]["front"] == "20180329"                # Good Friday: weekly expires Thursday
        assert [round(g["gap"], 2) for g in c["RHT"]["gaps"]] == [2.53, 5.24, 9.96, 5.35, 11.89, 6.45, 5.57]
        assert c["LULU"]["back"] == "20180413" and "Skipped Apr 06" in c["LULU"]["back_reason"]   # slide 27
        assert c["CTAS"]["stage"]["k"] == "ba"
        assert all(g["source"] == "inferred" for g in c["CTAS"]["gaps"])
        assert c["PVH"]["stage"]["lbl"] == "Straddle candidate"
        assert c["MKC"]["stage"]["k"] == "yn"
        assert app.ib.peak_lines <= app.cfg.load.max_lines and app.budget.used == 0

        r = await client.get("/api/state")
        st = await r.json()
        assert st["connection"]["state"] == "Connected" and len(st["cands"]) == 8
        assert "finnhub_api_key" in st["settings"]["earnings"] and st["settings"]["earnings"]["finnhub_api_key"] == "********"
        # the course trade: RHT 155 call calendar, 0.70 entry, Strong
        r = await client.post("/api/analyze", json={"symbol": "RHT", "K": 155})
        a = await r.json()
        assert a["right"] == "C" and a["entry"] == pytest.approx(0.70) and a["verdict"] == "Strong" and a["hits"] == 6
        r = await client.post("/api/analyze", json={"symbol": "RHT", "K": 155, "crush_percent": 100})
        assert (await r.json())["fIV1"] == pytest.approx(0.845)

        # confirm FINL's date by hand: it becomes usable
        r = await client.post("/api/earnings/override", json={"symbol": "FINL", "date": "2018-03-29", "timing": "BMO"})
        assert (await r.json())["ok"]
        for _ in range(50):
            await asyncio.sleep(0.1)
            if app.cands["FINL"]["status"] == "manual":
                break
        assert app.cands["FINL"]["status"] == "manual" and app.cands["FINL"]["verified"]
        assert (await client.post("/api/earnings/override", json={"symbol": "FINL", "date": "x", "timing": "BMO"})).status == 400

        # exit-day quotes for a recorded trade
        r = await client.post("/api/quote", json={"legs": [
            {"symbol": "RHT", "expiry": "20180329", "strike": 155, "right": "C"},
            {"symbol": "RHT", "expiry": "20180406", "strike": 155, "right": "C"}]})
        qs = (await r.json())["quotes"]
        assert qs[0]["underlying"] == pytest.approx(153.09) and (qs[1]["bid"], qs[1]["ask"]) == (3.80, 4.00)
        r = await client.post("/api/trades", json={"trades": [{"id": "a", "symbol": "RHT", "debit": 0.7, "qty": 1}]})
        assert (await r.json())["ok"] and app.trades()[0]["symbol"] == "RHT"

        # settings round trip keeps the hidden key and rejects bad values
        s = st["settings"]
        s["strategy"]["crush_percent"] = 70
        assert (await client.post("/api/settings", json=s)).status == 200
        assert app.cfg.strategy.crush_percent == 70 and app.cfg.earnings.finnhub_api_key == "demo"
        saved = json.loads((tmp_path / "settings.json").read_text())
        assert saved["strategy"]["crush_percent"] == 70
        s["strategy"]["gap_basis"] = "median"
        assert (await client.post("/api/settings", json=s)).status == 400

        r = await client.get("/")
        assert r.status == 200 and "Earnings Spread Desk" in await r.text()
        assert (await client.get("/static/app.js")).status == 200

    asyncio.run(_run(tmp_path, body))


def test_no_order_code():
    """The app is read-only: nothing in it can place, modify or cancel an order."""
    from pathlib import Path
    src = "\n".join(p.read_text() for p in (Path(__file__).parent.parent / "earningsdesk").glob("*.py"))
    for call in ("placeOrder", "cancelOrder", "reqGlobalCancel", "whatIfOrder"):
        assert call not in src


def test_after_hours_label():
    """Outside 9:30-16:00 ET with Live data TWS sends no option bid / ask: say so instead of "Fails bid-ask"."""
    from earningsdesk.service import App
    app = App.__new__(App)
    app.cfg = C.AppConfig()
    from fixtures import rht_candidate
    c = {**rht_candidate(), "state": "ready", "quotes_live": False}
    c["chain"]["20180329"]["C"][155.0]["bid"] = None          # no live bid after the close
    app.analyze_default(c)
    assert c["stage"]["k"] == "closed"
    c2 = {**c, "quotes_live": True}
    app.analyze_default(c2)
    assert c2["stage"]["k"] != "closed"
