from datetime import datetime, timezone

import pytest

from jupiter_trading.domain import Order, OrderStatus, OrderType, Quote, Side
from jupiter_trading.paper_broker import FeeSchedule, PaperBroker, RiskLimits


def quote(price: float, bid=None, ask=None) -> Quote:
    return Quote(
        instrument_key="NSE_EQ|TEST",
        last_price=price,
        bid=bid,
        ask=ask,
        timestamp=datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc),
    )


def paper_broker(**kwargs) -> PaperBroker:
    kwargs.setdefault("fee_schedule", FeeSchedule(brokerage_bps=0))
    return PaperBroker(**kwargs)


def test_market_buy_uses_ask_and_adverse_slippage() -> None:
    broker = paper_broker(initial_cash=100_000, slippage_bps=10)
    broker.on_quote(quote(100, bid=99.9, ask=100.1))

    order = broker.submit(Order("NSE_EQ|TEST", Side.BUY, 10, OrderType.MARKET))

    assert order.status == OrderStatus.FILLED
    assert order.filled_price == pytest.approx(100.2001)
    assert broker.positions["NSE_EQ|TEST"].quantity == 10
    assert broker.cash == pytest.approx(98_997.999)


def test_limit_order_waits_then_fills() -> None:
    broker = paper_broker(initial_cash=100_000, slippage_bps=0)
    order = broker.submit(
        Order("NSE_EQ|TEST", Side.BUY, 10, OrderType.LIMIT, limit_price=99)
    )

    assert broker.on_quote(quote(100, ask=100)) == []
    fills = broker.on_quote(quote(98.5, ask=98.6))

    assert len(fills) == 1
    assert fills[0].price == 98.6
    assert order.status == OrderStatus.FILLED


def test_closed_market_updates_quote_without_filling_order() -> None:
    broker = paper_broker(initial_cash=100_000, slippage_bps=0)
    broker.update_market_status({"NSE_EQ": "NORMAL_CLOSE"})
    order = broker.submit(Order("NSE_EQ|TEST", Side.BUY, 10, OrderType.MARKET))

    fills = broker.on_quote(quote(100))

    assert fills == []
    assert order.status == OrderStatus.OPEN
    assert broker.quotes["NSE_EQ|TEST"].last_price == 100
    assert broker.cash == 100_000


def test_queued_order_fills_on_next_quote_after_market_opens() -> None:
    broker = paper_broker(initial_cash=100_000, slippage_bps=0)
    broker.update_market_status({"NSE_EQ": "NORMAL_CLOSE"})
    order = broker.submit(Order("NSE_EQ|TEST", Side.BUY, 10, OrderType.MARKET))
    broker.on_quote(quote(100))

    broker.update_market_status({"NSE_EQ": "NORMAL_OPEN"})
    fills = broker.on_quote(quote(101))

    assert len(fills) == 1
    assert fills[0].price == 101
    assert order.status == OrderStatus.FILLED
    assert broker.cash == 98_990


def test_realized_and_unrealized_pnl() -> None:
    broker = paper_broker(initial_cash=100_000, slippage_bps=0)
    broker.on_quote(quote(100))
    broker.submit(Order("NSE_EQ|TEST", Side.BUY, 10, OrderType.MARKET))
    broker.on_quote(quote(110))
    broker.submit(Order("NSE_EQ|TEST", Side.SELL, 4, OrderType.MARKET))

    snapshot = broker.snapshot()
    position = snapshot["positions"][0]
    assert position["quantity"] == 6
    assert position["realized_pnl"] == 40
    assert position["unrealized_pnl"] == 60
    assert snapshot["equity"] == 100_100


def test_fees_reduce_equity() -> None:
    broker = PaperBroker(
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=10),
    )
    broker.on_quote(quote(100))
    broker.submit(Order("NSE_EQ|TEST", Side.BUY, 10, OrderType.MARKET))

    assert broker.snapshot()["fees_paid"] == 1
    assert broker.snapshot()["equity"] == 99_999


def test_short_order_is_rejected_by_default() -> None:
    broker = paper_broker(initial_cash=100_000, slippage_bps=0)
    broker.on_quote(quote(100))

    order = broker.submit(Order("NSE_EQ|TEST", Side.SELL, 1, OrderType.MARKET))

    assert order.status == OrderStatus.REJECTED
    assert order.rejection_reason == "short selling is disabled"


def test_order_notional_limit_rejects_fill() -> None:
    broker = paper_broker(
        initial_cash=100_000,
        slippage_bps=0,
        risk_limits=RiskLimits(max_order_notional=500),
    )
    broker.on_quote(quote(100))

    order = broker.submit(Order("NSE_EQ|TEST", Side.BUY, 6, OrderType.MARKET))

    assert order.status == OrderStatus.REJECTED
    assert order.rejection_reason == "max order notional exceeded"
