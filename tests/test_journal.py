"""Forward test: entry snapshots in the last hour, next-day quotes at the check times."""
import asyncio
from datetime import datetime

import pytest

from earningsdesk import config as C
from earningsdesk import demo as D
from earningsdesk.journal import Journal, exit_quote
from earningsdesk.service import App
from earningsdesk.timeutil import ET
from fixtures import rht_candidate
from earningsdesk import model as M


def test_exit_quote_course_example():
    # slide 18 at 13:00: front 1.60/2.10, back 2.80/3.60 -> offer 1.05, +$35 on the 0.70 entry
    q = exit_quote({"entry": 0.70}, {"bid": 1.60, "ask": 2.10, "iv": 0.372}, {"bid": 2.80, "ask": 3.60, "iv": 0.33})
    assert q["bid"] == pytest.approx(0.70) and q["ask"] == pytest.approx(2.00) and q["offer"] == pytest.approx(1.05)
    assert q["pnl"] == pytest.approx(35.0) and q["pnl_pct"] == pytest.approx(50.0)
    # 09:35: front 8.20/9.20, back 9.00/9.70 -> bid -0.20, ask 1.50, offer 0.25: a loss
    q = exit_quote({"entry": 0.70}, {"bid": 8.20, "ask": 9.20}, {"bid": 9.00, "ask": 9.70})
    assert q["offer"] == pytest.approx(0.25) and q["pnl"] == pytest.approx(-45.0)
    assert exit_quote({"entry": 0.70}, {"bid": None, "ask": 1}, {"bid": 1, "ask": 2})["error"]


def test_journal_entry_due_and_csv(tmp_path):
    j = Journal(tmp_path / "journal.json")
    c = rht_candidate()
    c.update(date="2018-03-26", timing="AMC", stage={"k": "ok", "lbl": "Strong"})
    c["a"] = M.analyze(c, M.Params(rate=0.02))
    c["chain"]["20180329"]["P"] = {155.0: {"bid": 5.70, "ask": 5.95, "iv": 0.85}}
    c["chain"]["20180406"]["P"] = {155.0: {"bid": 6.10, "ask": 6.50, "iv": 0.50}}
    c["a_alt"] = M.analyze(c, M.Params(rate=0.02, right="P"))
    now = datetime(2018, 3, 26, 15, 40, tzinfo=ET)
    rec = j.record_entry(c, now)
    assert rec["id"] == "2018-03-26|RHT" and rec["exit_date"] == "2018-03-27" and rec["sides"]["C"]["entry"] == 0.70
    assert rec["sides"]["P"]["natural"] == pytest.approx(0.80) and rec["default_right"] == "C"
    checks = ["09:45", "11:00", "13:30", "15:45"]
    assert j.due("2018-03-27", "09:40", checks) == []
    assert [t for _, t in j.due("2018-03-27", "09:50", checks)] == ["09:45"]
    assert j.due("2018-03-27", "11:30", checks)[0][1] == "11:00"     # 09:45 missed by >1h: skipped, 11:00 due
    assert j.due("2018-03-28", "09:50", checks) == []
    j.record_exit(rec["id"], "13:30", 154.26, {"C": ({"bid": 1.60, "ask": 2.10}, {"bid": 2.80, "ask": 3.60})}, now)
    again = Journal(tmp_path / "journal.json").get(rec["id"])
    assert again["exits"]["13:30"]["sides"]["C"]["pnl"] == pytest.approx(35.0)
    assert again["exits"]["13:30"]["move"] == pytest.approx(0.76, abs=0.005)
    assert j.record_entry(c, now)["exits"]                               # a later screen no longer replaces it
    csv = j.csv(checks)
    assert csv.splitlines()[0].startswith("entry_date,exit_date,symbol") and ",35.0" in csv


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_forward_test_end_to_end(tmp_path):
    async def run():
        cfg = C.AppConfig()
        cfg.data_dir = str(tmp_path)
        cfg.load.option_timeout_seconds = 2
        cfg.earnings.finnhub_api_key = "demo"
        clock = Clock(datetime(2018, 3, 26, 15, 30, tzinfo=ET))     # in the entry window on RHT's entry day
        app = App(cfg, str(tmp_path / "settings.json"), D.FakeIB, demo=True, fetcher=D.FakeFetcher(), clock=clock)
        await app.start()
        try:
            for _ in range(900):
                await asyncio.sleep(0.1)
                if app.cands and all(c["state"] in ("ready", "error") for c in app.cands.values()):
                    break
            ids = {r["id"] for r in app.journal.records}
            assert ids == {"2018-03-26|RHT", "2018-03-26|PAYX"}     # only today's entries
            rht = app.journal.get("2018-03-26|RHT")
            assert set(rht["sides"]) == {"C", "P"} and rht["default_right"] == "P"
            clock.t = datetime(2018, 3, 27, 9, 50, tzinfo=ET)        # next session, after the 09:45 check
            await app.journal_pass()
            await app.journal_pass()
            rht = app.journal.get("2018-03-26|RHT")
            assert "09:45" in rht["exits"] and rht["exits"]["09:45"]["S"] == pytest.approx(153.09)
            assert rht["exits"]["09:45"]["sides"]["P"]["offer"] is not None
            assert "09:45" in app.journal.get("2018-03-26|PAYX")["exits"]
            assert "11:00" not in rht["exits"]
        finally:
            await app.shutdown()
    asyncio.run(run())
