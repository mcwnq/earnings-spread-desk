# Earnings Spread Desk

Screens earnings calendar spreads the way Dan Passarelli's *Total Earnings Domination* course does, using
Interactive Brokers data. It finds the week's announcements, checks the bid-ask and Yes / No screens on live option
quotes, runs the Is / Was analysis against the stock's last seven earnings gaps, ranks what's left, and prepares
the entry and exit tickets.

**It never places orders.** It connects to TWS read-only, and order buttons are disabled. You enter the combo order in
TWS yourself and record the fill here, so the exit page can price the close the next session.

It is built the same way as GEX Live: Python with `ib_async` and `aiohttp`, a local web page, and settings kept in a
data folder outside the code.

## Run it

Windows: double-click `run.bat`. It sets up a `.venv` the first time. `run-demo.bat` starts the demo.
macOS / Linux: `./run.sh`, or `./run.sh --demo`.

```
python -m earningsdesk [--demo] [--config path\settings.json] [--port 8060] [--no-browser]
```

The page opens at http://127.0.0.1:8060/.

**Demo** (`--demo`) replays the course sample week, starting Mon Mar 26 2018 at 14:52 ET. It uses a simulated TWS and
canned earnings feeds, so it needs neither TWS nor the internet. RHT and LULU quotes and RHT's past gaps are the
numbers on the course slides; the other tickers are illustrative.

### TWS setup

* In TWS or IB Gateway, go to Configure → API → Settings and enable socket clients. Port 7497 is paper and 7496 live
  in TWS; IB Gateway uses 4002 and 4001. The app connects read-only, so "Read-Only API" can stay ticked.
* The app uses client ID 290 and tries 291–294 if that one is taken. GEX Live uses 190–194, so both can run at once.
* Option quotes need the **OPRA** market data subscription, and stock prices need US equity data.
* Market data lines: the app holds at most 40 at once (Settings). With GEX Live at 60, together they stay inside
  IBKR's default 100.

## Where the data comes from

| Step | Source |
| --- | --- |
| Earnings dates, BMO / AMC | **Nasdaq** earnings calendar (free, no key) and **Finnhub** (free API key from finnhub.io, entered in Settings). Cross-checked: *verified* when both agree, *1 source* when only one lists it, *conflict* when they disagree. You can confirm or correct any date on the Calendar page; your entries win. |
| Past report dates | Finnhub history, Nasdaq's last four report dates, and dates you enter. A missing quarter is found from the largest overnight gap in the stock's daily bars near where the report should be, and marked *inferred*. |
| Price, option chain, bid / ask, IV | TWS: `reqSecDefOptParams`, then snapshots via `reqMktData` until bid, ask and IBKR's model IV arrive. |
| Past gaps | TWS daily bars (`reqHistoricalData`, 3 years, cached for the day). Gap = \|close before − open after\| ÷ close before. |

Without a Finnhub key every date is "1 source". It still works, but check those dates on the company's IR page.

## The screens

* **Sessions.** For an after-close report on D, you enter on D and exit on D+1. For a before-open report on D, you
  enter on D−1 and exit on D. Weekends and NYSE holidays are skipped.
* **Legs.** The short leg is the first expiry on or after the exit session. The long leg is the next expiry, unless
  its IV is still 8+ points above the following one (LULU in the course) or its market fails bid-ask. Then the next 3
  expiries are tried, and the first that passes or is at "review" is used. The default strike is the one the course trades: the nearest
  call at or above the price (RHT at 153.09 → 155C). The put calendar at the same strike is shown next to it, and
  any other strike can be picked on the Is / Was page.
* **Call and put calendars.** Both sides are quoted at the 4 strikes nearest the price. The Is / Was page shows the
  call and put calendars side by side, and the screener shows how the other side would rate.
* **Bid-ask.** Ask ≤ 110% of bid on both legs passes, up to 120% is "review", and above that fails.
* **Yes / No.** Front IV must be ≥ back IV + 2 points.
* **Is / Was.** Black-Scholes value of the calendar on the exit day. The default crush is the course spreadsheet's
  target-vol model: target = lowest of the 6-month low of the stock's 30-day IV ("back vol bottom-out", from IBKR),
  30-day historical volatility (from the daily bars) and back IV − 1 point; each leg then loses 85% of its IV above the
  target (Settings › haircut). Front IV × 80% and "retrace premium" are options. A spread that loses money the day after
  even with no move has an endurable gap of 0. The endurable gap is the breakeven distance ÷ price, using the average of both breakevens or the nearer one.
  Strong means 6 or more of the last 7 gaps were smaller; Acceptable means 5. The thresholds scale when there are fewer
  past gaps.
* **Rank.** Candidates are ranked by hits, then by how far the endurable gap is above the median gap. You trade 1 per
  day as a newer trader, up to 5 when experienced.
* **Entry.** 25-20 middling: start at natural − 25% of width, rounded down to a nickel, then +0.05 every 20 s.
* **Exit.** The offer is the spread bid + 25% of the width.

Screens run on start-up, then every 15 minutes during the last hour for today's entries and hourly otherwise. They
cover entries today and the next session (Settings), and you can also run them from any page.

## Forward test

IBKR has no data for expired options, so past calendars can't be rebuilt from it. Instead, the app records its own:

* **Entry:** on each entry day, the last-hour screen of every candidate is saved, whether it passed or not. Both the
  call and the put calendar are kept, with the 25-20 entry price.
* **Exit:** the next session, the same legs are quoted at 09:45, 11:00, 13:30 and 15:45 ET (Settings). Each is
  priced to sell at the spread bid + 25% of the width.
* **Results page:** shows P&L per spread for every candidate, grouped by verdict. This tells you whether Strong
  really beats Weak. `forward_test.csv` exports everything for a spreadsheet.

The app has to be running during the last hour on the entry day and at the check times. A check missed by more than
an hour is skipped rather than taken late.

## Files

Data folder `~/EarningsDesk` (demo: `~/EarningsDesk-demo`):

* `desk.db`: SQLite database with the forward test (`journal`, `journal_sides`, `journal_exits`), the history of
  every screen (`screens`) and the fills you recorded (`trades`). Open it with any SQLite viewer, for example
  DB Browser for SQLite, even while the app runs. Back this file up: the forward test can't be rebuilt later.
  `/api/screens?symbol=RHT` returns a symbol's screen history.
* `settings.json`: your settings, including the Finnhub key (keep it private)
* `overrides.json`: dates you confirmed
* `cache/`: calendar answers and daily bars (safe to delete; they are fetched again)

An older `journal.json` / `trades.json` is imported into `desk.db` on the first start and renamed `*.imported`.

Example query, P&L by verdict at the 13:30 check:

```sql
SELECT s.verdict, s.side, COUNT(*) AS trades, ROUND(AVG(x.pnl), 2) AS avg_pnl
FROM journal_sides s JOIN journal_exits x ON x.record_id = s.record_id AND x.side = s.side
WHERE x.check_time = '13:30' AND x.pnl IS NOT NULL
GROUP BY s.verdict, s.side;
```

## Tests

```
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests check the model against the course numbers (RHT 0.70 entry, 9.97% endurable gap, 6 of 7, Strong; FDS
25-20; the slide 18 exit) and the earnings parsers against the Nasdaq and Finnhub JSON shapes. They also run the
whole app end to end against the simulated TWS and confirm that no order calls exist anywhere in the code.
