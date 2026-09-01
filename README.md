# Jupiter Equity Trading

A private, paper-only Indian-equity research service. Jupiter consumes read-only Upstox market
data and performs every order, fill, cash movement, position update, and risk check inside its
own local simulator. It never sends an order to a real broker.

## Implemented

- Named SQLite-backed paper accounts that survive application restarts
- Explicit, confirmed account resets; capital never resets automatically
- Market, limit, and stop-market orders with DAY and IOC validity
- Full lifecycle states: `OPEN`, `TRIGGER_PENDING`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`,
  `REJECTED`, and `EXPIRED`
- Order modification, cancellation, and persisted lifecycle-event history
- Five-level Upstox depth consumption and partial fills at each available price
- Bid/ask-aware execution and configurable adverse slippage
- Indian CNC/MIS charge breakdowns: brokerage, STT, NSE transaction charge, SEBI fee, stamp
  duty, GST, and delivery-sell DP charge
- Read-only Upstox V3 quotes, candles, market-status gating, live streaming, and instrument
  search
- Cash, position, order-notional, daily-loss, and kill-switch controls
- Persistent profit-target strategies with entry ceilings, percentage or absolute targets,
  stop losses, and start/pause/stop controls
- Historical candle-close replay with net P&L, Indian costs, drawdown, fills, and equity curves
- Local dashboard for surveys, accounts, strategies, positions, orders, and reports

This is an execution simulator, not a prediction system. Market depth is only a snapshot, so
paper fills can approximate but cannot guarantee the queue position or latency of a real order.

## Set up and run

Python 3.9 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
jupiter-api
```

Add your read-only token to `.env`:

```dotenv
UPSTOX_ACCESS_TOKEN=your-read-only-analytics-token
PAPER_INITIAL_CASH=1000000
```

The local `.env` is ignored by Git. Open
[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) for the interactive API.

`PAPER_INITIAL_CASH` initializes the default account only when it is first created. Restarting
the API restores cash, orders, fills, and positions from `PAPER_DB_PATH`.

Run the dashboard in a second terminal:

```bash
cd dashboard
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The account selector switches every
portfolio panel between persistent paper accounts. The dashboard remains local because it
contains private strategy and account data.

## Survey and profit-target strategy

`GET /universes/nifty100` downloads and caches the current official NSE constituent file.
`GET /universes/nifty50` remains available for comparison and compatibility.
`POST /market/survey` ranks that universe using session and trailing 15-minute momentum. The
momentum runner scans all 100 stocks and shortlists only those passing its entry filters. It describes current price action; it is
not a prediction or recommendation.

```bash
curl -X POST http://127.0.0.1:8000/strategies \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"BIOCON paper target",
    "account_id":"default",
    "legs":[{
      "symbol":"BIOCON",
      "instrument_key":"NSE_EQ|INE376G01013",
      "quantity":25,
      "entry_price":417.25,
      "profit_target_pct":1.0,
      "stop_loss_pct":0.5,
      "absolute_profit_target":4.2
    }]
  }'
```

Strategies start as `DRAFT`. Review the response, then call
`POST /strategies/{strategy_id}/start`. A configured entry becomes a paper limit order. An exit
uses the first profit or stop threshold reached. `pause` prevents new decisions; `stop` is
permanent and does not implicitly liquidate an existing position.

## Looped momentum paper sessions

`POST /momentum-runners` starts a bounded background session over the current NIFTY 100 stock
universe.
The runner rescans the universe, polls live prices between scans, buys candidates that clear the
entry gates, and manages each position with a stop that only ever moves up. It limits concurrent
positions, trades each instrument at most once per session, and liquidates remaining positions
when the configured duration expires. Every order uses the paper broker with MIS costs and
slippage; no order is sent to Upstox.

### The cost floor

Brokerage carries a flat floor, so a round trip at 25,000 must clear roughly 0.26% before it
earns anything, while the same trip at 100,000 needs about 0.12%. `GET /cost-model?notional=` and
each run snapshot report that number. Every exit threshold below is a multiple of it rather than
a hardcoded percentage, so the rule stays honest when position size changes.

### Entry

An entry is allowed when the stock's last three polled prices rise from first to last by enough to
clear the entry bar, and the stock's last three completed 5-minute candles average at least 1.20x
the median volume of up to twelve earlier completed intraday candles. A flat or falling window is
reported separately (`FLAT_NO_TRADE`, `DOWNWARD_NO_TRADE`) so the dashboard can show which shape
blocked the trade. The low of the entry window becomes the starting stop.

Fifteen seconds of price is well inside the noise band, so the window size is not what makes the
signal real - the bar is. An entry must clear the largest of three claims, and each observation
records which one bound:

| Claim | Value | Why |
| --- | --- | --- |
| `ABSOLUTE_FLOOR` | `entry_momentum_pct` | a hard minimum |
| `COST_FLOOR` | `entry_cost_multiple` x the round-trip cost | never enter on a move smaller than what taking it costs |
| `STOCK_NOISE` | `entry_noise_multiple` x this stock's median 3-sample move | a jumpy name works harder than a quiet one |

On a recorded NIFTY 100 session the median absolute 3-sample move was 0.015% and the 90th
percentile 0.053%, while single-stock noise ranged 10x between the quietest and jumpiest names. A
fixed 0.10% bar took 24 entries there, 21 of them on moves too small to cover their own cost at a
25,000 position. The scaled bar takes 3, none below cost. Set `entry_cost_multiple` and
`entry_noise_multiple` to `0` to go back to a fixed percentage.

### Resampling the entry to a slower timeframe

At the default five-second poll, three bars covers only fifteen seconds - inside a NIFTY 100
stock's own bid-ask bounce, and its stop seed (the window low) sits inside the round-trip cost
more often than not. `entry_timeframe_seconds` resamples the tick stream into fixed-duration OHLC
bars and runs the identical three-bar check on those instead, so "three bars" can mean three
minutes rather than fifteen seconds without changing the rule itself.

```bash
curl -X POST http://127.0.0.1:8000/momentum-runners \
  -H 'Content-Type: application/json' \
  -d '{"duration_seconds":1800,"allocation_per_position":100000,"entry_timeframe_seconds":60}'
```

A bar is only exposed once it closes - the runner never evaluates an in-progress bar, since its
close is not yet known. Direction is still first-close vs. last-close across the window, but the
stop seed is the **low across all bars in the window**, not just their closes, so a wick that
dipped below the eventual close and recovered is not hidden from the stop. Set it to `0` (the
default) to keep evaluating on raw ticks.

This widens the stop roughly as the square root of the added wall-clock time, which is the correct
scaling for a random-walk price series: going from 5-second to 1-minute bars multiplies the time
inside "three bars" by 12x and widened the measured stop distance by roughly 3.5x on real NIFTY
100 data (median ~3-5bp -> 8.2bp). It does not, on its own, establish that the pattern predicts
anything - a variance-ratio test on the same data showed no exploitable autocorrelation at any
horizon from 3 to 30 minutes, and the entry signal's forward 15-minute return was statistically
indistinguishable from zero. Resampling fixes the stop's geometry; it is not a substitute for
finding an edge, which is why this is meant to be forward-tested rather than assumed.

NIFTY 50 context is recorded on every observation but does **not** gate entries by default. The
index moves an order of magnitude less than a single stock, so a print like −0.04% is noise rather
than a reason to stand aside, and as a hard gate it blocked entire sessions. Set
`require_nifty_confirmation` on the run (or the dashboard's NIFTY confirmation control) to make it
a gate again and compare.

### Exit

The default `RATCHET` exit walks three phases, each keyed to the cost floor:

| Phase | Reached when | Stop sits at |
| --- | --- | --- |
| `SURVIVE` | on entry | `survive_stop_multiple` floors below entry, clamped so the window low is never used inside the noise band |
| `LOCK` | ahead by `lock_multiple` floors | one floor above entry, so the round trip can no longer lose |
| `RIDE` | ahead by `ride_multiple` floors | the rolling `trail_window` low, never closer than `min_gap_multiple` floors under the last price |

A breach while in `SURVIVE` exits at once because that stop is the risk limit. Breaches of the
later, tighter stops need `confirmation_samples` consecutive prints, which ignores single-tick
wicks. A position that never reaches `LOCK` within `time_stop_seconds` is closed flat.

Relative volume tightens the stop rather than triggering the exit: it is derived from 5-minute
candles refreshed once per rescan, so it lags the five-second price loop by minutes. When it
decays below `volume_decay_ratio` the runner switches to `fast_trail_window`, letting price decide
when to leave and volume decide how much room to give.

Set `exit_mode` to `REVERSAL` (and `entry_mode` to `ROLLING_WINDOW`) to run the earlier fixed
−`reversal_pct` / −`hard_stop_pct` rule for comparison.

### What each run records

Each poll records a symbol-first observation: price, change from the previous sample,
rolling-window change, the three-bar entry verdict and the bar it had to clear, stock session and
15-minute momentum, NIFTY 50 context, and the resulting decision. Held positions additionally
record their phase, stop price, gap to stop, unrealised move net of cost, trailing low, volume
state, and breach count on every check. Run summaries also carry the cost model and a decision
histogram showing which gate each observation stopped at.

Observations are the raw material for every later backtest, so they are stored as their own rows
in `market_observations` - indexed by run, by instrument and time, and by IST session date - not
serialised inside the run payload. A blob rewritten on each poll is quadratic and cannot be
queried by symbol or by day; on a real session the run rows shrank from roughly 6.4 MB to 172 KB
once the trace moved out of them.

```bash
curl 'http://127.0.0.1:8000/observations/sessions'
curl 'http://127.0.0.1:8000/observations?session_date=2026-08-28&symbol=TCS&limit=500'
```

`GET /momentum-runners` returns summaries only. Ask for one run to get its trace back, rebuilt
from those rows: `GET /momentum-runners/{runner_id}` (add `?include_monitoring=false` for just the
summary). Runs recorded before this split still carry their trace inline and read back unchanged.

Start the default experiment on the dedicated `momentum` research account:

```bash
curl -X POST http://127.0.0.1:8000/momentum-runners \
  -H 'Content-Type: application/json' \
  -d '{"duration_seconds":300}'
```

Inspect the returned runner ID with `GET /momentum-runners/{runner_id}` or stop it early with
`POST /momentum-runners/{runner_id}/stop`. An early stop also liquidates open positions.

### Comparing several configurations in one session

`POST /momentum-runners/batch` starts several differently configured runs side by side. Each
variant inherits `base` and applies its own `overrides`, and each gets its own paper account:
concurrent runners sharing an account would compete for the same cash and positions, so neither
result would mean anything.

```bash
curl -X POST http://127.0.0.1:8000/momentum-runners/batch \
  -H 'Content-Type: application/json' \
  -d '{
        "base": {"duration_seconds": 1800, "allocation_per_position": 100000},
        "account_prefix": "fwd",
        "variants": [
          {"label": "5s-ticks", "overrides": {"entry_timeframe_seconds": 0}},
          {"label": "1m-bars",  "overrides": {"entry_timeframe_seconds": 60}}
        ]
      }'
```

That provisions `fwd-5s-ticks` and `fwd-1m-bars` and returns which arms started and which failed.
An override naming a field that does not exist is rejected rather than silently ignored. One run
per account is still enforced, so re-launching an arm while it is live returns 409. The dashboard
Home tab has the same thing as a set of selectable arms, and shows every live arm at once.

The dashboard's Runs tab lists every momentum run and opens symbol-first execution, fees, exit
reasons, and P&L details. Its monitoring trace switches between **Table** and **Chart**: the chart
plots price against the trailing stop for the selected stock, marks entries and exits, and reads
out the price, stop, phase, and three-bar verdict against its threshold as the pointer moves.

## Scheduled daily automation

The point of the automation layer is to sweep configurations unattended while
the market is open, so patterns can accumulate over days without anyone at the
keyboard. `SCHEDULER_ENABLED=true` turns it on.

At the open the scheduler launches **one full-session run per entry timeframe**:
5s ticks, 1m, 3m, and 5m bars. Every run spans 09:15-15:30, holds the same
`SCHEDULER_MAX_POSITIONS` (default 5), and is identical but for its entry
timeframe - so the day's four P&L numbers are a clean, like-for-like comparison
of that one axis, over the identical universe and session. Duration and position
count are held fixed on purpose: duration is a sampling window, not a strategy
knob, and fixing it at the whole session removes the end-of-session liquidation
artifact and gives the most representative sample. Each run is on its own paper
account, because concurrent runs sharing an account would fight over cash and
positions - so each account is funded with `SCHEDULER_INITIAL_CASH`, which must
cover `max_positions x allocation`.

A full-session run would exhaust the universe by mid-morning under the old
"trade each stock once per run" rule, so automated runs use a **re-entry
cooldown** (`SCHEDULER_COOLDOWN_SECONDS`, default 15 min): after a stock is
exited it can set up and be traded again once the cooldown passes, rather than
being locked out for the day. With the cooldown at zero the permanent
once-per-stock behaviour returns (the default for short manual runs).

```bash
curl 'http://127.0.0.1:8000/schedule/status'          # today's plan, filling in live
curl 'http://127.0.0.1:8000/schedule/plan?session_date=2026-08-31'  # preview any day
curl -X POST 'http://127.0.0.1:8000/schedule/tick'    # advance once (ops / missed tick)
```

The scheduling decision lives in a pure `tick(now)` that reads the persisted
plan and launches any slot whose start has arrived - so it is unit tested
without threads, and a restart mid-session resumes from the saved slot states
rather than relaunching or skipping. A slot missed by more than a grace window
is marked `SKIPPED`; a launch failure marks that one arm `FAILED` and the rest
proceed.

### The daily report

About three minutes after the last run settles, the scheduler compiles a report
for the day and stores it. It rolls the day's runs up by each config dimension
(timeframe, duration, positions), names the best and worst arm, totals P&L,
trades, fees and win rate, and aggregates every run's decision histogram so the
day explains its own inaction. The dashboard's Reports tab shows the live
schedule timeline and the stored reports; the same data is at:

```bash
curl 'http://127.0.0.1:8000/reports/daily'            # every stored report
curl 'http://127.0.0.1:8000/reports/daily/2026-08-28' # one day
curl -X POST 'http://127.0.0.1:8000/reports/daily/2026-08-28/build'  # (re)build
```

Runs, observations, plans and reports all persist in SQLite, so the record
survives restarts as long as the database file does. Deployment (Railway
backend, the frontend note, and the daily Upstox-token requirement that gates
unattended runs) is documented in `DEPLOY.md`.

## Backtesting and reports

`POST /backtests` downloads read-only Upstox historical candles and replays the same target and
stop rule using candle closes. Reports persist at `GET /reports/backtests` and include gross and
net P&L, charges, return, drawdown, fills, exit reason, and an equity curve.

The first backtester intentionally supports one entry and one exit per run. Candle-close replay
does not know intrabar sequencing or exchange queue position, so use it to compare hypotheses,
not to claim exact live performance.

### Replaying a recorded session under a different rule

`POST /backtests/exit-replay` re-scores a finished momentum run against its own five-second
monitoring trace, so a candidate rule can be measured without waiting for another session.

```bash
curl -X POST http://127.0.0.1:8000/backtests/exit-replay \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"<runner id>","mode":"EXITS_ONLY","trail_window":8}'
```

`EXITS_ONLY` keeps exactly the entries the live run took and swaps only the exit rule, isolating
the exit change. `FULL` re-evaluates the entry rule and the gates too, and honours
`entry_timeframe_seconds`, so a resampled bar rule can be scored against ticks already recorded.
Both route orders through a real paper broker, so replayed fees and slippage match what the live
account would have paid, and replaying a run with its own parameters reproduces its recorded
result. The report contains a baseline-versus-candidate comparison, per-trade stop paths, and a
decision histogram. The dashboard exposes the same thing on each run's page.

### Replaying a whole trading day

A single run only sees the stocks it happened to shortlist while it was alive. Because
observations are stored per day, `ExitReplayEngine.replay_session` pools every tick recorded on a
date - across all runs - and scores a rule against the lot, which is far more signal than any one
run provides. Where overlapping runs watched the same stock at the same instant, the duplicate is
dropped so a name is not counted twice; the report states both the raw and deduplicated counts.

## Find BEL and place a paper order

Use the search result's `instrument_key`; do not guess an exchange token or ISIN.

```bash
curl --get http://127.0.0.1:8000/instruments/search \
  --data-urlencode 'q=BEL' \
  --data-urlencode 'exchange=NSE' \
  --data-urlencode 'segment=EQ'
```

Then submit an order using the returned key:

```bash
curl -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' \
  -d '{
    "instrument_key": "NSE_EQ|INE263A01024",
    "side": "BUY",
    "quantity": 100,
    "order_type": "MARKET",
    "product": "CNC",
    "validity": "DAY"
  }'
```

Start the full feed so the order uses market depth and only fills during `NORMAL_OPEN`:

```bash
curl -X POST http://127.0.0.1:8000/market/stream/start \
  -H 'Content-Type: application/json' \
  -d '{
    "instrument_keys": ["NSE_EQ|INE263A01024"],
    "mode": "full"
  }'
```

Inspect the result:

```bash
curl http://127.0.0.1:8000/orders
curl http://127.0.0.1:8000/fills
curl http://127.0.0.1:8000/portfolio
```

Use `GET /orders/{order_id}/events` to inspect every persisted state transition for one order.

## Paper accounts and capital reset

Create an isolated account for a strategy:

```bash
curl -X POST http://127.0.0.1:8000/paper/accounts \
  -H 'Content-Type: application/json' \
  -d '{"id":"bel-test","name":"BEL strategy","initial_cash":500000}'
```

Set `"account_id":"bel-test"` when placing orders and use `?account_id=bel-test` on portfolio,
order, fill, modification, cancellation, and kill-switch endpoints.

Capital and trading history are erased only by an explicit confirmed reset:

```bash
curl -X POST http://127.0.0.1:8000/paper/accounts/bel-test/reset \
  -H 'Content-Type: application/json' \
  -d '{"confirm":true,"initial_cash":500000}'
```

## Manual depth test

`/paper/ticks` is useful for deterministic tests and historical replay. This example exposes
only eight shares across two ask levels, so a ten-share market buy remains partially filled.

```bash
curl -X POST http://127.0.0.1:8000/paper/ticks \
  -H 'Content-Type: application/json' \
  -d '{
    "instrument_key":"NSE_EQ|INE263A01024",
    "last_price":409,
    "asks":[
      {"price":409,"quantity":5},
      {"price":412,"quantity":3}
    ],
    "bids":[{"price":408.9,"quantity":20}]
  }'
```

Each price level creates a separate fill. A later quote can fill the remaining quantity.

## Cost assumptions

Defaults were checked on 25 August 2026 against Upstox's published equity schedule and the NSE
cash-market revision effective 1 March 2026. Rates remain configurable in `.env` because broker,
exchange, and government charges can change. BSE transaction charges vary by scrip group and
are not modeled by the current NSE-focused defaults.

The model currently uses ₹20 delivery brokerage per executed order, capped intraday brokerage,
0.1% delivery STT on both sides, 0.025% intraday STT on sells, 0.00307% NSE cash transaction
charges, ₹10/crore SEBI fees, buy-side stamp duty, 18% GST on applicable service charges, and one
₹20 DP charge per delivery scrip per sell day.

## Test

```bash
pytest
ruff check .
```

## Boundaries and next work

The simulator deliberately remains separate from real broker order APIs. Upstox is used only
for market data and instrument discovery.

### What the data says so far

Measured on 337,500 real one-minute bars (60 NIFTY 100 names, 15 sessions), the variance ratio is
flat at 0.94-1.00 from three minutes out to thirty once the one-minute bid-ask bounce is removed:
a random walk, with no momentum autocorrelation to harvest at any horizon tested. The current
entry signal's forward fifteen-minute return over 11,684 occurrences was -0.33bp, with a 95%
confidence interval of [-0.70, +0.04]bp against the +12.26bp needed to break even at a 1,00,000
position. Relative volume - the one component that is not pure price - showed no predictive
power across buckets from below 1.0x to above 2.5x.

Resampling the entry to slower bars widens the stop out of the noise band, and scaling the entry
bar to cost stops entries that cannot pay for themselves, but neither creates an edge. Under a
random walk a trailing stop has zero expectancy before costs and negative after, so better
geometry makes a loss slower rather than turning it into a gain. The forward test is the way to
check whether live conditions disagree.

### High-value next steps

The `full` websocket feed already parses five levels of bid/ask depth in `market_stream.py`, but
the runner never uses it - it REST-polls LTP. Order-flow imbalance is one of the few effects with
documented short-horizon predictive power, and it is a different information source from the
price autocorrelation that the data above rules out. Testing whether depth imbalance predicts the
next thirty to sixty seconds is the highest-value unexplored question, and the observation store,
cost model and replay harness are already built to score it honestly.

Beyond that: walk-forward backtests across many stored sessions, exchange circuit limits,
corporate actions, benchmark comparisons, and a more advanced queue/latency model.
