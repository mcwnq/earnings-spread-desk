"""The screens against the course examples."""
import math

import pytest

from earningsdesk import model as M
from fixtures import rht_candidate


def test_rht_course_example_matches_slides():
    a = M.analyze(rht_candidate(), M.Params(rate=0.02))
    # natural 4.70 - 3.80 = 0.90, bid 4.20 - 4.00 = 0.20, 25-20 start 0.725 rounded down to 0.70 (slide 17)
    assert a["natural"] == pytest.approx(0.90)
    assert a["inside"] == pytest.approx(0.20)
    assert a["entry"] == pytest.approx(0.70)
    assert a["ivDiff"] == pytest.approx(34.6)
    assert a["yes"]
    # 155 back leg is 4.70 / 4.20 = 112%: review, not fail
    assert a["baStatus"] == ["pass", "review"]
    assert a["fIV1"] == pytest.approx(0.676)           # 84.5 x 80% (Mohit's reading of the crush)
    # endurable gap about $15 / 153.09 = 9.98% on the slide
    assert a["meg"] == pytest.approx(9.97, abs=0.1)
    assert a["hits"] == 6 and a["n"] == 7
    assert a["verdict"] == "Strong"
    assert a["med"] == pytest.approx(5.57, abs=0.01)
    assert not a["straddle"]


def test_rht_past_gaps_from_slides():
    gaps = [round(g["gap"], 2) for g in rht_candidate()["gaps"]]
    assert gaps == [2.53, 5.24, 9.96, 5.35, 11.89, 6.45, 5.57]


def test_retrace_mode_and_nearer_basis():
    c = rht_candidate()
    mult = M.analyze(c, M.Params(rate=0.02))
    near = M.analyze(c, M.Params(rate=0.02, gap_basis="near"))
    assert near["meg"] < mult["meg"]
    retr = M.analyze(c, M.Params(rate=0.02, crush_mode="retrace", crush_percent=80))
    assert retr["fIV1"] == pytest.approx(0.845 - 0.8 * (0.845 - 0.499))


def test_other_strike_and_back_expiry():
    a = M.analyze(rht_candidate(), M.Params(rate=0.02, K=152.5, right="C"))
    assert a["fq"] == [5.00, 5.30] and a["bq"] == [5.60, 6.00]
    b = M.analyze(rht_candidate(), M.Params(rate=0.02, back="20180413"))
    assert b["back"] == "20180413" and b["bq"] == [4.60, 5.10]


def test_missing_quotes_and_iv():
    c = rht_candidate()
    c["chain"]["20180406"]["C"][155.0]["iv"] = None
    a = M.analyze(c, M.Params())
    assert not a["ok"] and "IV" in a["error"] and a["entry"] == pytest.approx(0.70)
    c["chain"]["20180406"]["C"][155.0]["bid"] = 0
    a = M.analyze(c, M.Params())
    assert a["baOverall"] == "fail" and math.isinf(a["baRatio"][1])
    assert M.analyze(c, M.Params(K=200))["error"]


def test_ladder_and_close_offer():
    # FDS course example: 1.10 bid / 2.30 offer -> 25-20 start 2.00
    assert M.ladder(2.30, 1.20)[0]["price"] == pytest.approx(2.00)
    steps = M.ladder(0.90, 0.70)
    assert [s["price"] for s in steps] == [0.70, 0.75, 0.80, 0.85, 0.90]
    assert [s["seconds"] for s in steps][:2] == [0, 20]
    # slide 18, 13:00: front 1.60/2.10, back 2.80/3.60 -> bid 0.70, ask 2.00, offer 0.70 + 0.325 = 1.05 (50% on 0.70)
    assert M.close_offer(2.80 - 2.10, 3.60 - 1.60) == pytest.approx(1.05)


def test_ba_status_edges():
    assert M.ba_status(1.10) == "pass"
    assert M.ba_status(1.1001) == "review"
    assert M.ba_status(1.20) == "review"
    assert M.ba_status(1.21) == "fail"
    assert M.ba_status(float("inf")) == "fail"


def test_verdict_scales_with_fewer_gaps():
    p = M.Params()
    assert M.verdict_for(6, 7, p) == "Strong"
    assert M.verdict_for(5, 7, p) == "Acceptable"
    assert M.verdict_for(4, 7, p) == "Weak"
    assert M.verdict_for(4, 4, p) == "Strong"        # ceil(6*4/7) = 4
    assert M.verdict_for(3, 4, p) == "Acceptable"    # ceil(5*4/7) = 3
    assert M.verdict_for(0, 0, p) == "n/a"


def test_straddle_quote():
    c = rht_candidate()
    c["chain"]["20180329"]["P"] = {155.0: {"bid": 5.5, "ask": 5.8, "iv": 0.85}}
    s = M.straddle(c, "20180329", 153.09, [g["gap"] for g in c["gaps"]])
    assert s["K"] == 155.0 and s["cost"] == pytest.approx(4.00 + 5.80)
    assert s["need"] == pytest.approx(9.80 / 153.09 * 100)
    assert s["above"] == 3     # 9.96, 11.89 and 6.45 are above 6.4%


def test_stage_and_rank():
    c = rht_candidate()
    a = M.analyze(c, M.Params(rate=0.02))
    assert M.stage(c, a)["k"] == "ok"
    assert M.stage({**c, "verified": False}, a)["k"] == "verify"
    weak = {**a, "verdict": "Weak", "straddle": True}
    assert M.stage(c, weak)["lbl"] == "Straddle candidate"
    assert sorted([{"hits": 5, "meg": 9, "med": 5}, {"hits": 6, "meg": 6, "med": 5}], key=M.rank_key)[0]["hits"] == 6


def _q(b, a, iv):
    return {"bid": b, "ask": a, "iv": iv}


def test_pick_back_next_expiry_when_it_passes():
    c = rht_candidate()
    e, why = M.pick_back(c["chain"], c["expiries"], 155.0, "C", skip_iv_points=8)
    assert e == "20180406" and why == ""          # 112% is "review" but Apr 13 (4.60/5.10, 111%) is no better


def test_pick_back_tries_later_expiries_when_wide():
    exps = ["20180329", "20180406", "20180413", "20180420", "20180427"]
    chain = {"20180406": {"C": {50.0: _q(1.00, 1.40, 0.30)}},      # 140%: fails
             "20180413": {"C": {50.0: _q(1.20, 1.45, 0.29)}},      # 121%: fails
             "20180420": {"C": {50.0: _q(1.40, 1.50, 0.28)}},      # 107%: passes
             "20180427": {"C": {50.0: _q(1.50, 1.60, 0.27)}}}
    e, why = M.pick_back(chain, exps, 50.0, "C")
    assert e == "20180420"
    assert why.startswith("Long leg moved to Apr 20") and "Apr 06: market too wide" in why and "Apr 13" in why


def test_pick_back_review_and_no_pass():
    exps = ["20180329", "20180406", "20180413"]
    chain = {"20180406": {"P": {50.0: _q(1.00, 1.40, 0.30)}}, "20180413": {"P": {50.0: _q(1.00, 1.15, 0.29)}}}
    assert M.pick_back(chain, exps, 50.0, "P")[0] == "20180413"    # review beats fail
    chain["20180413"]["P"][50.0] = _q(1.00, 1.50, 0.29)
    assert M.pick_back(chain, exps, 50.0, "P") == ("20180406", "")  # nothing better: keep the next expiry
    assert M.pick_back({}, ["20180329"], 50.0, "P") == (None, "")


def test_pick_back_skips_high_iv():
    exps = ["20180329", "20180406", "20180413"]
    chain = {"20180406": {"C": {80.0: _q(3.50, 3.75, 0.699)}}, "20180413": {"C": {80.0: _q(3.80, 4.30, 0.606)}}}
    e, why = M.pick_back(chain, exps, 80.0, "C", skip_iv_points=8)
    assert e == "20180413" and "IV still 9.3 pts" in why            # LULU, slide 27


def test_put_calendar_uses_put_quotes():
    c = rht_candidate()
    c["chain"]["20180329"]["P"] = {155.0: _q(5.70, 5.95, 0.85)}
    c["chain"]["20180406"]["P"] = {155.0: _q(6.10, 6.50, 0.50)}
    a = M.analyze(c, M.Params(rate=0.02, right="P"))
    assert a["right"] == "P" and not a["call"] and a["fq"] == [5.70, 5.95] and a["bq"] == [6.10, 6.50]
    assert a["natural"] == pytest.approx(0.80) and a["meg"] > 0
