"""Earnings sources: parsing the real JSON shapes, cross-checking, overrides."""
import asyncio
from datetime import date

from earningsdesk import earnings as E
from earningsdesk.config import EarningsConfig
from earningsdesk.demo import FakeFetcher

NASDAQ_DAY = {"data": {"asOf": "Thu, Nov 13, 2025", "headers": {}, "rows": [
    {"lastYearRptDt": "11/13/2024", "lastYearEPS": "$1.29", "time": "time-after-hours", "symbol": "AMAT",
     "name": "Applied Materials, Inc.", "marketCap": "$185,617,153,620", "fiscalQuarterEnding": "Oct/2025",
     "epsForecast": "$2.20", "noOfEsts": "11"},
    {"time": "time-pre-market", "symbol": "JD", "name": "JD.com, Inc.", "marketCap": "$45,000,000,000"},
    {"time": "time-not-supplied", "symbol": "TINY", "name": "Tiny Co", "marketCap": "$120,000,000"},
    {"time": "time-after-hours", "symbol": "NOCAP", "name": "No Cap Inc", "marketCap": "N/A"}]},
    "message": None, "status": {"rCode": 200}}
FINNHUB = {"earningsCalendar": [
    {"date": "2025-11-13", "epsActual": None, "epsEstimate": 2.2, "hour": "amc", "quarter": 4, "revenueActual": None,
     "symbol": "AMAT", "year": 2025},
    {"date": "2025-11-14", "hour": "bmo", "symbol": "JD", "quarter": 3, "year": 2025},
    {"date": "2025-11-13", "hour": "", "symbol": "NOCAP", "quarter": 3, "year": 2025}]}


def test_parse_nasdaq_day():
    cl = E.parse_nasdaq_day(NASDAQ_DAY, date(2025, 11, 13))
    assert [(c.symbol, c.timing) for c in cl] == [("AMAT", "AMC"), ("JD", "BMO"), ("TINY", ""), ("NOCAP", "AMC")]
    assert cl[0].market_cap == 185617153620 and cl[3].market_cap is None
    assert cl[0].name.startswith("Applied")


def test_parse_finnhub_and_surprise():
    f = E.parse_finnhub(FINNHUB)
    assert f["AMAT"][0].timing == "AMC" and f["JD"][0].date == date(2025, 11, 14) and f["NOCAP"][0].timing == ""
    s = E.parse_nasdaq_surprise({"data": {"earningsSurpriseTable": {"rows": [
        {"fiscalQtrEnd": "Jul 2025", "dateReported": "8/14/2025", "eps": 2.48}, {"dateReported": "05/15/2025"}]}}})
    assert s == [date(2025, 8, 14), date(2025, 5, 15)]


def test_merge_statuses():
    n = E.parse_nasdaq_day(NASDAQ_DAY, date(2025, 11, 13))
    f = E.parse_finnhub(FINNHUB)
    amat = E.merge("AMAT", [n[0]] + f["AMAT"])
    assert amat.status == "verified" and amat.timing == "AMC"
    jd = E.merge("JD", [n[1]] + f["JD"])
    assert jd.status == "conflict" and "Nasdaq says Thu Nov 13" in jd.note
    tiny = E.merge("TINY", [n[2]])
    assert tiny.status == "single" and "Time of day not supplied" in tiny.note
    timing = E.merge("X", [E.Claim("X", "Nasdaq", date(2025, 11, 13), "AMC"), E.Claim("X", "Finnhub", date(2025, 11, 13), "BMO")])
    assert timing.status == "conflict"
    manual = E.merge("JD", [n[1]] + f["JD"], {"date": "2025-11-14", "timing": "BMO"})
    assert manual.status == "manual" and manual.date == date(2025, 11, 14)


def test_dedupe_dates_keeps_one_per_quarter():
    d = E.dedupe_dates([(date(2025, 8, 14), "", "Nasdaq"), (date(2025, 8, 14), "AMC", "Finnhub"),
                        (date(2025, 5, 15), "AMC", "Finnhub"), (date(2025, 5, 16), "", "Nasdaq")])
    assert d == [(date(2025, 8, 14), "AMC", "Nasdaq"), (date(2025, 5, 15), "AMC", "Finnhub")]


class StubFetch:
    def __init__(self, fail_nasdaq=False):
        self.calls = []
        self.fail = fail_nasdaq

    async def get_json(self, url, params):
        self.calls.append((url, params))
        if "nasdaq" in url:
            if self.fail:
                raise E.SourceError("HTTP 403")
            return NASDAQ_DAY if params.get("date") == "2025-11-13" else {"data": {"rows": []}}
        return FINNHUB


def test_book_refresh_filters_and_caches(tmp_path):
    cfg = EarningsConfig(finnhub_api_key="k", days_ahead=2)
    f = StubFetch()
    book = E.EarningsBook(cfg, tmp_path, f, today_fn=lambda: date(2025, 11, 13))
    ev = asyncio.run(book.refresh())
    assert set(ev) == {"AMAT", "JD", "NOCAP"}          # TINY is under $2B; NOCAP has no cap but Finnhub lists it
    assert ev["AMAT"].status == "verified" and ev["JD"].status == "conflict"
    n = len(f.calls)
    asyncio.run(book.refresh())                          # cached: no new requests within refresh_minutes
    assert len(f.calls) == n
    # the key goes only to Finnhub, and no request carries anything about the user
    assert all("token" not in p for u, p in f.calls if "nasdaq" in u)


def test_book_watchlist_and_override(tmp_path):
    cfg = EarningsConfig(finnhub_api_key="", watchlist=["JD", "ZZZ"], days_ahead=3)
    book = E.EarningsBook(cfg, tmp_path, StubFetch(), today_fn=lambda: date(2025, 11, 13))
    book.overrides.set_upcoming("ZZZ", "2025-11-14", "BMO", "IR page")
    ev = asyncio.run(book.refresh())
    assert set(ev) == {"JD", "ZZZ"}
    assert ev["ZZZ"].status == "manual" and ev["JD"].status == "single"
    assert book.source_status["Finnhub"]["ok"] is False


def test_nasdaq_failure_is_reported(tmp_path):
    book = E.EarningsBook(EarningsConfig(finnhub_api_key="k", days_ahead=1), tmp_path, StubFetch(fail_nasdaq=True),
                          today_fn=lambda: date(2025, 11, 13))
    ev = asyncio.run(book.refresh())
    assert "failed" in book.source_status["Nasdaq"]["text"] and ev["AMAT"].status == "single"


def test_past_dates_combines_sources(tmp_path):
    book = E.EarningsBook(EarningsConfig(finnhub_api_key="demo"), tmp_path, FakeFetcher(), today_fn=lambda: date(2018, 3, 26))
    got = asyncio.run(book.past_dates("RHT", date(2018, 3, 26), 9))
    assert [d.isoformat() for d, _, _ in got][:3] == ["2017-12-19", "2017-09-21", "2017-06-21"]
    assert len(got) == 7 and all(t == "AMC" for _, t, _ in got)
    book.overrides.set_past("RHT", [{"date": "2017-12-19", "timing": "BMO"}])
    got = asyncio.run(book.past_dates("RHT", date(2018, 3, 26), 9))
    assert got[0] == (date(2017, 12, 19), "BMO", "Manual")
