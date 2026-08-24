from datetime import datetime, timezone

import pytest

from jupiter_trading.domain import (
    DepthLevel,
    Order,
    OrderStatus,
    OrderType,
    Product,
    Quote,
    Side,
    Validity,
)
from jupiter_trading.paper_broker import FeeSchedule, PaperBroker

NOW = datetime(2026, 8, 25, 4, 0, tzinfo=timezone.utc)
ZERO_FEES = FeeSchedule(brokerage_bps=0)


def depth_quote(asks=(), bids=(), last_price=100) -> Quote:
    return Quote(
        "NSE_EQ|TEST",
        last_price,
        timestamp=NOW,
        bids=tuple(DepthLevel(*level) for level in bids),
        asks=tuple(DepthLevel(*level) for level in asks),
    )


def test_market_order_partially_fills_across_depth_levels() -> None:
    broker = PaperBroker(initial_cash=100_000, slippage_bps=0, fee_schedule=ZERO_FEES)
    order = broker.submit(Order("NSE_EQ|TEST", Side.BUY, 10, OrderType.MARKET))

    fills = broker.on_quote(depth_quote(asks=((100, 5), (101, 3))))

    assert [(fill.quantity, fill.price) for fill in fills] == [(5, 100), (3, 101)]
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == 8
    assert order.remaining_quantity == 2
    assert order.average_filled_price == pytest.approx(100.375)

    broker.on_quote(depth_quote(asks=((102, 2),), last_price=102))
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity == 10
    assert order.average_filled_price == pytest.approx(100.7)


def test_depth_is_consumed_between_competing_orders() -> None:
    broker = PaperBroker(initial_cash=100_000, slippage_bps=0, fee_schedule=ZERO_FEES)
    broker.on_quote(depth_quote(asks=((100, 5),)))

    first = broker.submit(Order("NSE_EQ|TEST", Side.BUY, 4, OrderType.MARKET))
    second = broker.submit(Order("NSE_EQ|TEST", Side.BUY, 4, OrderType.MARKET))

    assert first.status == OrderStatus.FILLED
    assert second.status == OrderStatus.PARTIALLY_FILLED
    assert second.filled_quantity == 1


def test_limit_respects_price_and_ioc_expires_unfilled_remainder() -> None:
    broker = PaperBroker(initial_cash=100_000, slippage_bps=0, fee_schedule=ZERO_FEES)
    order = broker.submit(
        Order(
            "NSE_EQ|TEST",
            Side.BUY,
            5,
            OrderType.LIMIT,
            limit_price=100,
            validity=Validity.IOC,
        )
    )

    fills = broker.on_quote(depth_quote(asks=((99, 2), (101, 10))))

    assert [(fill.quantity, fill.price) for fill in fills] == [(2, 99)]
    assert order.status == OrderStatus.EXPIRED
    assert order.filled_quantity == 2
    assert order.remaining_quantity == 3


def test_stop_order_and_day_expiry_lifecycle() -> None:
    broker = PaperBroker(initial_cash=100_000, slippage_bps=0, fee_schedule=ZERO_FEES)
    stop = broker.submit(
        Order("NSE_EQ|TEST", Side.BUY, 1, OrderType.STOP_MARKET, trigger_price=105)
    )
    assert stop.status == OrderStatus.TRIGGER_PENDING
    assert broker.on_quote(depth_quote(asks=((100, 5),), last_price=100)) == []

    broker.on_quote(depth_quote(asks=((106, 5),), last_price=106))
    assert stop.status == OrderStatus.FILLED
    assert stop.triggered_at == NOW

    day_order = broker.submit(
        Order("NSE_EQ|LATER", Side.BUY, 1, OrderType.LIMIT, limit_price=1)
    )
    broker.update_market_status({"NSE_EQ": "NORMAL_OPEN"})
    broker.update_market_status({"NSE_EQ": "NORMAL_CLOSE"})
    assert day_order.status == OrderStatus.EXPIRED


def test_active_order_can_be_modified_and_cancelled() -> None:
    broker = PaperBroker(initial_cash=100_000, slippage_bps=0, fee_schedule=ZERO_FEES)
    order = broker.submit(
        Order("NSE_EQ|TEST", Side.BUY, 10, OrderType.LIMIT, limit_price=90)
    )

    modified = broker.modify(order.id, quantity=8, limit_price=95)
    cancelled = broker.cancel(order.id)

    assert modified.quantity == 8
    assert modified.limit_price == 95
    assert cancelled.status == OrderStatus.CANCELLED
    with pytest.raises(ValueError, match="only active"):
        broker.modify(order.id, quantity=7)


def test_indian_delivery_charge_breakdown_and_one_dp_charge_per_day() -> None:
    schedule = FeeSchedule()
    buy = schedule.calculate(100_000, Side.BUY, Product.DELIVERY)

    assert buy.brokerage == 20
    assert buy.stt == 100
    assert buy.exchange_transaction == 3.07
    assert buy.sebi == 0.1
    assert buy.stamp_duty == 15
    assert buy.gst == 4.15
    assert buy.dp == 0
    assert buy.total == 142.32

    first_sell = schedule.calculate(100_000, Side.SELL, Product.DELIVERY)
    later_sell = schedule.calculate(
        100_000,
        Side.SELL,
        Product.DELIVERY,
        brokerage_already_charged=True,
        dp_already_charged=True,
    )
    assert first_sell.dp == 20
    assert later_sell.dp == 0
    assert later_sell.brokerage == 0


def test_intraday_costs_use_sell_side_stt_and_no_dp() -> None:
    schedule = FeeSchedule()
    buy = schedule.calculate(10_000, Side.BUY, Product.INTRADAY)
    sell = schedule.calculate(10_000, Side.SELL, Product.INTRADAY)

    assert buy.stt == 0
    assert buy.stamp_duty == 0.3
    assert sell.stt == 2.5
    assert sell.stamp_duty == 0
    assert sell.dp == 0
