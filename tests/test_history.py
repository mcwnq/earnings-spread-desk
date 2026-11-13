from datetime import date

import pytest

from earningsdesk.demo import make_bars
from earningsdesk.history import gap_for, past_gaps

BARS = [{"date": "2025-10-27", "open": 100, "close": 101}, {"date": "2025-10-28", "open": 101, "close": 102},
        {"date": "2025-10-29", "open": 110, "close": 111}, {"date": "2025-10-30", "open": 108, "close": 109}]


def test_gap_after_close_and_before_open():
    amc = gap_for(BARS, date(2025, 10, 28), "AMC")          # close 28th 102 -> open 29th 110
    assert (amc["close_before"], amc["open_after"]) == (102, 110)
    assert amc["gap"] == pytest.approx(8 / 102 * 100)
    bmo = gap_for(BARS, date(2025, 10, 29), "BMO")          # same move seen as before the open on the 29th
    assert (bmo["close_before"], bmo["open_after"]) == (102, 110)
    assert gap_for(BARS, date(2025, 10, 30), "AMC") is None  # no bar after


def test_weekend_report_and_holes():
    bars = [{"date": "2025-10-24", "open": 50, "close": 50}, {"date": "2025-10-27", "open": 55, "close": 55}]
    g = gap_for(bars, date(2025, 10, 25), "")               # Saturday report: Friday close to Monday open
    assert g["gap"] == pytest.approx(10)
    hole = [{"date": "2025-10-01", "open": 50, "close": 50}, {"date": "2025-10-20", "open": 55, "close": 55}]
    assert gap_for(hole, date(2025, 10, 1), "AMC") is None


def test_inferred_dates_when_sources_have_none():
    bars = make_bars("CTAS")
    g = past_gaps(bars, date(2018, 3, 27), "AMC", [], 7)
    assert [round(x["gap"], 1) for x in g] == [3.2, 5.8, 2.1, 4.4, 6.0, 1.9, 3.3]
    assert all(x["source"] == "inferred" for x in g)


def test_known_dates_win_and_mix_with_inferred():
    bars = make_bars("RHT")
    known = [(date(2017, 12, 19), "", "Nasdaq"), (date(2017, 9, 21), "AMC", "Finnhub")]
    g = past_gaps(bars, date(2018, 3, 26), "AMC", known, 7)
    assert [x["source"] for x in g][:2] == ["Nasdaq", "Finnhub"]
    assert [round(x["gap"], 2) for x in g] == [2.53, 5.24, 9.96, 5.35, 11.89, 6.45, 5.57]
