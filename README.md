# Jupiter Equity Trading

A private, paper-only research service for experimenting with Indian equity strategies. The
application consumes read-only market data and executes orders only inside a deterministic
simulator. There is deliberately no code path that sends live orders to a broker.

## Current slice

- Paper market, limit, and stop-market orders
- Bid/ask-aware fills with configurable adverse slippage
- Virtual cash, long positions, realized/unrealized P&L, and configurable fees
- Order, position, cash, and daily-loss risk checks with a kill switch
- SQLite audit records for orders and fills
- Read-only Upstox V3 LTP and historical-candle integration
- Strategy protocol and a small moving-average crossover example
- FastAPI endpoints for manual testing and future dashboard integration

This is an execution simulator, not a prediction system, and its results are not investment
advice. A fill model can only approximate actual exchange execution.

## Set up

Python 3.9 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Copy the local configuration and add your read-only Upstox Analytics Token:

```bash
cp .env.example .env
```

```dotenv
UPSTOX_ACCESS_TOKEN=your-read-only-analytics-token
PAPER_INITIAL_CASH=1000000
```

The service loads `.env` automatically. The file is excluded by `.gitignore`; never commit or
share the real token.

Create a free, read-only Analytics Token in the Upstox developer portal. The token is only
needed for `/market/*`; the simulator endpoints work without broker credentials.

## Run

```bash
jupiter-api
```

Open [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) for the interactive API.

Place a simulated order and then send a tick that fills it:

```bash
curl -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' \
  -d '{
    "instrument_key": "NSE_EQ|INE848E01016",
    "side": "BUY",
    "quantity": 5,
    "order_type": "MARKET"
  }'

curl -X POST http://127.0.0.1:8000/paper/ticks \
  -H 'Content-Type: application/json' \
  -d '{
    "instrument_key": "NSE_EQ|INE848E01016",
    "last_price": 1500,
    "bid": 1499.9,
    "ask": 1500.1
  }'
```

Query Upstox without placing any broker order:

```bash
curl --get http://127.0.0.1:8000/market/ltp \
  --data-urlencode 'instrument_key=NSE_EQ|INE848E01016'
```

## Test

```bash
pytest
ruff check .
```

## Design boundaries

`UpstoxMarketData` owns external, read-only market-data calls. `PaperBroker` owns simulated
orders, fills, cash, risk, and positions. Strategies depend on normalized `Quote` objects and
the paper broker, so a future WebSocket feed, CSV replay source, or different Indian broker can
be added without changing strategy code.

Fees default to zero because broker and regulatory schedules change. Configure and validate
the fee basis points for the product being researched before interpreting results.

## Next milestones

1. Add Upstox V3 WebSocket streaming and reconnect/recovery handling.
2. Persist and restore the complete portfolio state across restarts.
3. Add a historical replay/backtest runner and performance reports.
4. Model partial fills, liquidity, circuits, trading sessions, and corporate actions.
5. Add a small web dashboard for strategies, orders, positions, and equity curves.
