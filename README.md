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

## Looped momentum/reversal paper sessions

`POST /momentum-runners` starts a bounded background session over the current NIFTY 100 stock
universe.
The runner rescans the universe, polls live prices between scans, buys only candidates with
positive intraday and observed short-window momentum, and sells after a trailing reversal or
hard stop. It limits concurrent positions, trades each instrument at most once per session, and
liquidates remaining positions when the configured duration expires. Every order uses the paper
broker with MIS costs and slippage; no order is sent to Upstox.

An entry is allowed only when the stock's rolling movement and latest sample are positive, both
the NIFTY 50 rolling movement and trailing 15-minute movement are positive, and the stock's last
three completed 5-minute candles average at least 1.20x the median volume of up to twelve earlier
completed intraday candles. These NIFTY and relative-volume confirmations are mandatory.

Each poll persists a symbol-first monitoring trace: price, change from the previous sample,
rolling-window change, stock session and 15-minute momentum, NIFTY 50 session and 15-minute
context, and the resulting decision. Filled entries also persist an explicit reason and whether
the stock's strength was aligned with or against the broader NIFTY move.

Start the default five-minute experiment on the dedicated `momentum` research account:

```bash
curl -X POST http://127.0.0.1:8000/momentum-runners \
  -H 'Content-Type: application/json' \
  -d '{"duration_seconds":300}'
```

Inspect the returned runner ID with `GET /momentum-runners/{runner_id}` or stop it early with
`POST /momentum-runners/{runner_id}/stop`. An early stop also liquidates open positions. Run
snapshots persist in SQLite, while profit or loss accumulates in the account's total equity.
The dashboard Home tab shows that balance and starts new runs; the Runs tab lists every momentum
run and opens symbol-first execution, fees, exit reasons, and P&L details.

## Backtesting and reports

`POST /backtests` downloads read-only Upstox historical candles and replays the same target and
stop rule using candle closes. Reports persist at `GET /reports/backtests` and include gross and
net P&L, charges, return, drawdown, fills, exit reason, and an equity curve.

The first backtester intentionally supports one entry and one exit per run. Candle-close replay
does not know intrabar sequencing or exchange queue position, so use it to compare hypotheses,
not to claim exact live performance.

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
for market data and instrument discovery. High-value next steps are repeated-trade and
walk-forward backtests, exchange circuit limits, corporate actions, benchmark comparisons, and
a more advanced queue/latency model.
