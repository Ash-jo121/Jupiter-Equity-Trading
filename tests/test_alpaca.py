from __future__ import annotations

import json
from datetime import datetime, timezone

from jupiter_trading.alpaca import (
    US_SEGMENT,
    AlpacaMarketData,
    AlpacaPaperBroker,
    AlpacaRestClient,
)
from jupiter_trading.domain import Order, OrderStatus, OrderType, Quote, Side, Validity
from jupiter_trading.research_store import ResearchStore
from jupiter_trading.universe import Nasdaq100Universe
from jupiter_trading.us_automation import UsDailyScheduler, UsSchedulerConfig


def test_nasdaq100_parser_builds_us_instrument_keys():
    payload = {
        "data": {
            "data": {
                "rows": [
                    {"symbol": "AAPL", "companyName": "Apple Inc."},
                    {"symbol": "MSFT", "companyName": "Microsoft Corporation"},
                ]
            }
        }
    }
    rows = Nasdaq100Universe.parse(json.dumps(payload))
    assert rows[0]["instrument_key"] == "US_EQ|AAPL"
    assert rows[1]["name"] == "Microsoft Corporation"


def test_alpaca_client_is_permanently_paper_only():
    client = AlpacaRestClient("paper-key", "paper-secret", transport=lambda *_: {})
    assert client.trading_base_url == "https://paper-api.alpaca.markets"
    assert "live" not in client.trading_base_url


def test_market_data_maps_batch_snapshots_to_internal_quotes():
    class Client:
        def snapshots(self, symbols):
            assert symbols == ["AAPL", "MSFT"]
            return {
                "AAPL": {
                    "latestTrade": {"p": 250.5, "t": "2026-09-14T15:00:00Z"},
                    "latestQuote": {"bp": 250.49, "ap": 250.51},
                },
                "MSFT": {
                    "latestTrade": {"p": 610.0, "t": "2026-09-14T15:00:00Z"},
                    "latestQuote": {"bp": 609.9, "ap": 610.1},
                },
            }

    quotes = AlpacaMarketData(Client()).ltp(["US_EQ|AAPL", "US_EQ|MSFT"])
    assert quotes["US_EQ|AAPL"].last_price == 250.5
    assert quotes["US_EQ|MSFT"].ask == 610.1


def test_alpaca_broker_uses_remote_paper_fill():
    class Client:
        def account(self):
            return {"equity": "100000", "cash": "100000", "trading_blocked": False}

        def submit_market_order(self, symbol, quantity, side, client_order_id):
            assert (symbol, quantity, side) == ("AAPL", 2, Side.BUY)
            assert client_order_id.startswith("jup-")
            return {
                "id": "remote-1",
                "status": "filled",
                "filled_qty": "2",
                "filled_avg_price": "250.25",
                "filled_at": "2026-09-14T15:00:00Z",
            }

    broker = AlpacaPaperBroker(Client())
    broker.update_market_status({US_SEGMENT: "NORMAL_OPEN"})
    broker.on_quote(Quote("US_EQ|AAPL", 250.0))
    order = broker.submit(
        Order(
            "US_EQ|AAPL",
            Side.BUY,
            2,
            OrderType.MARKET,
            validity=Validity.IOC,
            account_id="alpaca-us",
        )
    )
    assert order.status == OrderStatus.FILLED
    assert broker.positions["US_EQ|AAPL"].quantity == 2
    assert broker.snapshot()["provider"] == "ALPACA_PAPER"


def test_us_scheduler_uses_alpaca_clock_and_launches_once(tmp_path):
    class Client:
        def clock(self):
            return {
                "timestamp": "2026-09-14T10:00:00-04:00",
                "is_open": True,
                "next_open": "2026-09-15T09:30:00-04:00",
                "next_close": "2026-09-14T16:00:00-04:00",
            }

    launched = []
    store = ResearchStore(str(tmp_path / "research.db"))
    scheduler = UsDailyScheduler(
        store,
        Client(),
        lambda session_date, duration, config: launched.append(
            (session_date, duration, config.entry_mode)
        )
        or "runner-1",
        UsSchedulerConfig(enabled=True),
    )
    assert scheduler.tick()[0]["action"] == "LAUNCHED"
    assert scheduler.tick() == []
    assert launched == [("2026-09-14", 21_600, "MACD_FRESH_CONFIRMED")]


def test_observation_session_date_uses_market_timezone(tmp_path):
    store = ResearchStore(str(tmp_path / "research.db"))
    store.add_observations(
        "us-run",
        [
            {
                "timestamp": datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc).isoformat(),
                "market_timezone": "America/New_York",
                "instrument_key": "US_EQ|AAPL",
                "symbol": "AAPL",
                "price": 250,
                "decision": "WATCHING",
            }
        ],
    )
    rows = store.observations(session_date="2026-09-14")
    assert len(rows) == 1
