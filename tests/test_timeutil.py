from datetime import date, datetime

from earningsdesk.timeutil import ET, in_entry_window, is_trading_day, nyse_holidays, sessions, trade_date_for


def test_holidays():
    assert date(2026, 11, 26) in nyse_holidays(2026)      # Thanksgiving
    assert date(2026, 7, 3) in nyse_holidays(2026)        # July 4 on a Saturday, observed Friday
    assert date(2018, 3, 30) in nyse_holidays(2018)       # Good Friday
    assert date(2027, 12, 31) not in nyse_holidays(2027)  # no Friday close when Jan 1 falls on Saturday
    assert not is_trading_day(date(2026, 11, 14))


def test_sessions():
    assert sessions(date(2026, 11, 25), "AMC") == (date(2026, 11, 25), date(2026, 11, 27))   # over Thanksgiving
    assert sessions(date(2026, 11, 30), "BMO") == (date(2026, 11, 27), date(2026, 11, 30))
    assert sessions(date(2026, 11, 14), "AMC") == (date(2026, 11, 13), date(2026, 11, 16))   # Saturday report
    assert sessions(date(2026, 11, 16), "") == (date(2026, 11, 16), date(2026, 11, 17))


def test_clock_helpers():
    assert in_entry_window(datetime(2026, 11, 13, 15, 30, tzinfo=ET))
    assert not in_entry_window(datetime(2026, 11, 13, 16, 0, tzinfo=ET))
    assert trade_date_for(datetime(2026, 11, 13, 17, 0, tzinfo=ET)) == date(2026, 11, 16)
    assert trade_date_for(datetime(2026, 11, 13, 10, 0, tzinfo=ET)) == date(2026, 11, 13)
