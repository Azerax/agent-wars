# stock-agent

An autonomous paper-trading agent for low-priced US stocks, and the simulated
broker it trades on. It finds its own tips, decides, places orders, manages
stops, and keeps a record of everything it saw and did, all against real
market data and with every fee a $100 account would actually pay.

It never touches real money. There is no broker integration.

## The target, stated honestly

The brief: start with $100 and double equity every 4 days, after all fees.

Doubling every 4 trading days means **+18.9% a day, compounded, every day**.
Ten cycles in a row turns $100 into $102,400 in eight weeks; a year of it
would be about 2^63 times the starting cash. No strategy, fund or trader has
a record like that, and the setups that occasionally produce a 4-day double
(low-float runners, catalyst gaps) are also the ones that lose 30% in an
afternoon.

So the target is built in as a **scoreboard, not a promise**:

- Every account runs in consecutive 4-trading-day cycles. Each cycle records
  its starting equity, its 2x target, where it ended and whether it hit.
- The dashboard plots equity against the doubling path on a log scale, and
  `report` prints how far behind (or ahead) the account is and what daily
  return the rest of the cycle needs.
- Falling behind **never** increases risk. Sizing up to catch up is how small
  accounts go to zero, and a paper record that got lucky that way would teach
  the wrong lesson. Risk per trade is fixed by config (8% of equity to the
  stop by default, which is already aggressive).

What the agent does chase is the highest-upside setups it can find in
$0.50–$5 stocks, with the costs that eat small accounts modelled rather than
ignored.

## Quick start

```bash
cd stock-agent
pip install -r requirements.txt        # requests, pandas, numpy (+ anthropic, optional)
python -m stockagent doctor            # which data sources are reachable from here
python -m stockagent init --cash 100   # open the paper account
python -m stockagent run               # autonomous loop; Ctrl-C to stop
python -m stockagent serve             # dashboard at http://127.0.0.1:8765
```

Other commands:

```bash
python -m stockagent scout --evaluate  # find and rank tips now; score old ones first
python -m stockagent report            # account, doubling scoreboard, strategies, sources
python -m stockagent backtest --universe 40 --start 2026-05-01 --end 2026-10-30
python -m stockagent order buy 20 ABCD --limit 2.10   # manual paper order
python -m stockagent run --once        # a single tick, for cron
python -m stockagent reset --cash 100
```

Optional environment:

- `STOCKAGENT_SEC_USER_AGENT="Your Name you@example.com"` enables SEC EDGAR
  filings (SEC requires automated clients to identify themselves).
- An Anthropic credential (`ANTHROPIC_API_KEY`, or `ant auth login`) turns on
  Claude as the catalyst grader. Without one the lexicon grader is used.
- `STOCKAGENT_CONFIG` / `./config.toml` overrides any setting; see
  `config.example.toml`.

## How it finds its own tips

The scout (`scout.py`) sweeps, every 30 minutes during the session and once
before the open:

| Source | What it contributes |
| --- | --- |
| Yahoo predefined screeners | day gainers, small-cap gainers, most actives, aggressive small caps |
| Nasdaq stock screener | the whole listed universe: biggest % gainers and most-traded names in the price range |
| StockTwits | trending symbols, then bullish/bearish ratio for anything shortlisted |
| Reddit | r/pennystocks and friends, ticker mentions weighted by score and comments, validated against the listed universe |
| SEC EDGAR | fresh 8-Ks with positive items (material agreements, acquisitions) as tips; offering prospectuses (424B, S-1, S-3) as red flags |
| News (Yahoo search + RSS, Google News) | headlines for every shortlisted symbol, graded for catalysts |

**Every tip is stored**, eligible or not, with the price when it was seen and
why it was rejected (wrong price band, OTC, warrant, too illiquid). After the
fact the agent measures what each tip did over the next 1 and 4 trading days.
That record drives the source weights: a source whose tips keep fading loses
influence, one whose tips run gains it. The weights are shrunk toward neutral
until there is enough evidence, so one lucky tip does not make a guru.

Shortlisted symbols get a catalyst grade from -5 to +5. The lexicon grader
knows the patterns that move small caps (FDA decisions, contract wins,
buyouts) and the ones that kill them (offerings, ATM programs, reverse
splits, delisting notices, going-concern warnings). With a credential, Claude
reads the same headlines and filings and returns a structured grade, which is
blended with the lexicon score. Headlines are passed to it as untrusted data.

## Strategies

| Strategy | Setup | Stop / target / max hold |
| --- | --- | --- |
| `momentum_breakout` | new 20-day high, +4% or more, 2x+ relative volume, closing near the high | 1.5 ATR (4–15%) / 2.5R / 4 days |
| `squeeze_breakout` | Bollinger bandwidth in its lowest 20% for six months, then a close above the band on volume | 1.2 ATR / 3R / 4 days |
| `gap_and_go` | gap up 8–60% over an uptrend; live, it must hold above the open in the first 90 minutes | 0.8 ATR / 2R / 2 days |
| `oversold_bounce` | RSI(2) under 10 while above the 100-day average, no bad news; exits on a close above the 5-day average | 2 ATR / 1.5R / 3 days |
| `catalyst_momentum` (live) | catalyst grade ≥ +2, no dilution risk, +5% on 2x volume, above the open | 1.2 ATR / 3R / 4 days |
| `social_momentum` (live) | trending with ≥60% bullish sentiment, confirmed by price and volume, no bad news | 1.2 ATR / 2R / 3 days |

Each signal is scored by the strategy, multiplied by a **learned weight**
(Thompson sampling on the strategy's own net-of-fees win rate and payoff,
which explores untried strategies and leans on proven ones) and by the
symbol's tip score. A strategy with 15+ trades averaging worse than -0.3R
is quarantined automatically. Every signal is stored with the decision and
the reason: entered, or skipped because of dilution risk, a full book, a
daily loss limit, costs too high relative to the reward, and so on.

## Risk and position management

- Size: the smaller of 8% of equity at risk to the stop, and 65% of equity in
  one position. At most two positions.
- Entries are marketable limit orders that will not chase more than 1.5%.
- Every entry gets an OCO bracket: a protective stop and a profit target.
- At +1R the stop moves to break-even plus fees; from +2R it trails 1 ATR
  under the high.
- Time stop: positions are closed near the close on their last allowed day.
- Held positions are re-checked for news; an offering or other negative
  catalyst triggers an exit.
- Circuit breakers: no new entries after a 15% down day, or once equity is
  50% below its peak.

## The simulated broker

`broker.py` models a US **cash account**, which is what $100 gets you
(margin needs $2,000; the old $25,000 pattern-day-trader rule was replaced in
2026, but a margin account still has the $2,000 minimum):

- **T+1 settlement**, on the bank calendar (Columbus and Veterans Day settle
  nothing). Sale proceeds cannot be spent until they settle, so capital turns
  over at most once a day. Optionally allow buying with unsettled funds; the
  broker then counts good-faith violations.
- **Fees** on every fill: commission per preset (`zero_commission` by
  default, `ibkr_pro_fixed`, `flat_4_95`), SEC Section 31 fee ($20.60 per $1M
  sold from 2026-04-04), FINRA TAF ($0.000195/share sold, max $9.79), each
  rounded up to the cent the way brokers charge it, and an optional CAT
  pass-through.
- **Spread and slippage**: buys fill at the ask, sells at the bid. When the
  feed has no bid/ask, the spread is modelled from price and dollar volume.
  Market and stop orders pay extra slippage; a stop that gaps fills at the gap
  price, not the stop.
- Resting limit orders fill at their limit even if the next poll shows a
  better price, and the broker looks at the low/high traded between polls so
  a stop hit and recovered between two quotes is not missed.
- Day/GTC orders, OCO groups, whole shares, no shorting.

The same broker and the same decision engine (`engine.py`) run in the
backtester, so a backtest tests the code that trades.

## Backtesting

```bash
python -m stockagent backtest --symbols ABCD,EFGH --start 2026-03-01 --end 2026-10-30 --out backtest.json
```

Replays real daily bars from Yahoo (Stooq as fallback). It reports return,
drawdown, win rate, R, fees, spread cost, and the scoreboard: how many
rolling 4-day windows doubled and what the median 4-day multiple was. Its
limits are printed with every run:

- Catalyst and social strategies cannot be backtested: free sources do not
  keep headlines and chatter as they looked at the time.
- Within one daily bar, the order of the high and the low is unknown; when a
  bar spans both stop and target, the stop is assumed to have filled first.
- A universe picked from today's screener contains only survivors.

## Dashboard and API

`serve` starts a local page and JSON API on 127.0.0.1: equity versus the
doubling path, the current cycle, positions, orders, trades, the watchlist,
tip-source and strategy leaderboards, every recent signal with its decision,
and the journal. Other programs (including other agents) can trade the same
paper account:

```
GET    /api/state                 everything
GET    /api/positions | orders | trades | tips | journal | pace | metrics ...
POST   /api/orders                {"symbol","side","qty","type","limit_price","stop_price","tif"}
DELETE /api/orders/<id>
```

## Data reality

Every source here is free and unofficial. Yahoo's endpoints have no SLA and
change shape; StockTwits and Reddit restrict anonymous access more each year;
Nasdaq's screener wants browser-like headers. The code treats each source as
optional: failures are journaled, a host that refuses connections is skipped
for five minutes so it cannot stall the loop, and the agent keeps trading on
whatever is still answering. Run `doctor` first; if prices themselves are
unavailable the agent will not trade.

Quotes are polled (60 seconds by default), not streamed. The agent cannot see
the order book, halts, or the pre-market auction, and fills are modelled, not
real. A paper record is evidence about the strategy, not a guarantee of
fills you would have got.

## Layout

```
stockagent/
  agent.py        the autonomous loop: premarket, session, end of day
  scout.py        tip discovery, ranking, catalyst grading, tip follow-up
  engine.py       shared decisions: which signals, how many shares, stop management
  strategies/     six strategies, each a pure function of bars + context
  risk.py         sizing, circuit breakers, the doubling scoreboard
  learning.py     strategy and tip-source weights learned from outcomes
  broker.py       the simulated cash-account broker and matching engine
  execution.py    spread and slippage model
  fees.py         commission and regulatory fees
  clock.py        NYSE calendar, early closes, T+1 settlement calendar
  catalysts.py    headline and SEC-filing grader
  llm.py          optional Claude catalyst grader (structured output)
  backtest.py     daily-bar replay through the same engine and broker
  report.py       metrics and the state behind the dashboard
  server.py       local dashboard + JSON API
  data/           Yahoo, Nasdaq, Stooq, SEC, StockTwits, Reddit, RSS
tests/            86 tests; fixtures are hand-written in each API's shape
```

```bash
pip install pytest && python -m pytest
```

The tests cover fee rounding, the holiday and settlement calendars, every
broker path (brackets, gaps, settlement, good-faith violations, expiry), the
parsers, each strategy, the scout and learners, a full scripted trading day
through the agent, backtest accounting invariants, the API, and the Claude
grader against a stubbed client. They use synthetic prices; no result in this
README comes from them.

Paper trading only. Nothing here is investment advice.
