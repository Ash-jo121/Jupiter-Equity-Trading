from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from threading import RLock
from typing import Dict, List, Mapping, Optional, Tuple

from .domain import (
    ACTIVE_ORDER_STATUSES,
    ChargeBreakdown,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    PaperAccount,
    Position,
    Product,
    Quote,
    Side,
    Validity,
    utc_now,
)
from .repository import InMemoryRepository, TradingRepository

EXECUTABLE_MARKET_STATUSES = frozenset({"NORMAL_OPEN"})


@dataclass(frozen=True)
class FeeSchedule:
    """Indian NSE cash-equity costs, configurable as rates change.

    Basis points are 1/100th of one percent. Brokerage and DP charges model
    Upstox's published standard pricing; statutory and exchange charges model
    NSE equity delivery (CNC) and intraday (MIS).
    """

    delivery_brokerage_flat: float = 20.0
    intraday_brokerage_bps: float = 10.0
    intraday_brokerage_cap: float = 20.0
    transaction_bps: float = 0.307
    sebi_bps: float = 0.01
    delivery_stt_bps: float = 10.0
    intraday_sell_stt_bps: float = 2.5
    delivery_buy_stamp_bps: float = 1.5
    intraday_buy_stamp_bps: float = 0.3
    gst_percent: float = 18.0
    delivery_sell_dp_flat: float = 20.0
    brokerage_bps: Optional[float] = None
    tax_bps: Optional[float] = None

    def calculate(
        self,
        notional: float,
        side: Side = Side.BUY,
        product: Product = Product.DELIVERY,
        brokerage_already_charged: bool = False,
        dp_already_charged: bool = False,
    ) -> ChargeBreakdown:
        if self.brokerage_bps is not None:
            brokerage = 0.0 if brokerage_already_charged else notional * self.brokerage_bps / 10_000
            legacy_tax = notional * (self.tax_bps or 0.0) / 10_000
            return ChargeBreakdown(brokerage=round(brokerage, 2), stt=round(legacy_tax, 2))

        if brokerage_already_charged:
            brokerage = 0.0
        elif product == Product.DELIVERY:
            brokerage = self.delivery_brokerage_flat
        else:
            brokerage = min(
                self.intraday_brokerage_cap, notional * self.intraday_brokerage_bps / 10_000
            )

        stt_bps = (
            self.delivery_stt_bps
            if product == Product.DELIVERY
            else (self.intraday_sell_stt_bps if side == Side.SELL else 0.0)
        )
        stamp_bps = 0.0
        if side == Side.BUY:
            stamp_bps = (
                self.delivery_buy_stamp_bps
                if product == Product.DELIVERY
                else self.intraday_buy_stamp_bps
            )
        exchange = notional * self.transaction_bps / 10_000
        sebi = notional * self.sebi_bps / 10_000
        stt = notional * stt_bps / 10_000
        stamp = notional * stamp_bps / 10_000
        dp = (
            self.delivery_sell_dp_flat
            if product == Product.DELIVERY and side == Side.SELL and not dp_already_charged
            else 0.0
        )
        gst = (brokerage + exchange + dp) * self.gst_percent / 100
        return ChargeBreakdown(
            brokerage=round(brokerage, 2),
            stt=round(stt, 2),
            exchange_transaction=round(exchange, 2),
            sebi=round(sebi, 2),
            stamp_duty=round(stamp, 2),
            gst=round(gst, 2),
            dp=round(dp, 2),
        )


@dataclass(frozen=True)
class RiskLimits:
    allow_short: bool = False
    max_order_notional: float = 250_000
    max_position_notional: float = 500_000
    max_daily_loss: float = 25_000


class PaperBroker:
    """Persistent simulated account driven by externally supplied quotes."""

    def __init__(
        self,
        initial_cash: float = 1_000_000,
        slippage_bps: float = 2.0,
        fee_schedule: Optional[FeeSchedule] = None,
        risk_limits: Optional[RiskLimits] = None,
        repository: Optional[TradingRepository] = None,
        account_id: str = "default",
        account_name: str = "Default paper account",
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative")
        self.repository = repository or InMemoryRepository()
        proposed = PaperAccount(account_id, account_name, initial_cash)
        self.account = self.repository.ensure_account(proposed)
        self.initial_cash = self.account.initial_cash
        self.cash = self.initial_cash
        self.slippage_bps = slippage_bps
        self.fee_schedule = fee_schedule or FeeSchedule()
        self.risk_limits = risk_limits or RiskLimits()
        self.orders: Dict[str, Order] = {}
        self.fills: List[Fill] = []
        self.positions: Dict[str, Position] = {}
        self.quotes: Dict[str, Quote] = {}
        self.market_statuses: Dict[str, str] = {}
        self.kill_switch = False
        self._books: Dict[str, Tuple[List[List[float]], List[List[float]]]] = {}
        self._dp_charged: set = set()
        self._lock = RLock()
        self._hydrate()

    def _hydrate(self) -> None:
        for order in self.repository.load_orders(self.account.id):
            self.orders[order.id] = order
        for fill in self.repository.load_fills(self.account.id):
            self.fills.append(fill)
            cash_delta = fill.gross_value if fill.side == Side.SELL else -fill.gross_value
            self.cash += cash_delta - fill.fees
            self.positions.setdefault(fill.instrument_key, Position(fill.instrument_key)).apply(
                fill
            )
            if fill.product == Product.DELIVERY and fill.side == Side.SELL and fill.charges.dp:
                self._dp_charged.add((fill.instrument_key, fill.timestamp.date()))

    def submit(self, order: Order) -> Order:
        with self._lock:
            if order.account_id != self.account.id:
                raise ValueError("order account does not match broker account")
            if self.kill_switch:
                return self._reject(order, "kill switch is active")
            self.orders[order.id] = order
            self.repository.save_order(order)
            blocked = self._execution_block_reason(order.instrument_key)
            if blocked and order.validity == Validity.IOC:
                self._expire(order, reason=blocked)
                return order
            quote = self.quotes.get(order.instrument_key)
            if quote and self._is_execution_allowed(order.instrument_key):
                self._try_fill(order, quote)
            return order

    def modify(
        self,
        order_id: str,
        quantity: Optional[int] = None,
        limit_price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        validity: Optional[Validity] = None,
    ) -> Order:
        with self._lock:
            order = self.orders[order_id]
            if order.status not in ACTIVE_ORDER_STATUSES:
                raise ValueError("only active orders can be modified")
            if quantity is not None:
                if quantity <= 0 or quantity < order.filled_quantity:
                    raise ValueError("quantity cannot be below the already filled quantity")
                order.quantity = quantity
            if limit_price is not None:
                if order.order_type != OrderType.LIMIT or limit_price <= 0:
                    raise ValueError("limit_price applies only to LIMIT orders")
                order.limit_price = limit_price
            if trigger_price is not None:
                if order.order_type != OrderType.STOP_MARKET or trigger_price <= 0:
                    raise ValueError("trigger_price applies only to STOP_MARKET orders")
                order.trigger_price = trigger_price
            if validity is not None:
                order.validity = validity
            order.updated_at = utc_now()
            if order.remaining_quantity == 0:
                order.status = OrderStatus.FILLED
                order.filled_at = order.updated_at
            self.repository.save_order(order)
            quote = self.quotes.get(order.instrument_key)
            if (
                order.status in ACTIVE_ORDER_STATUSES
                and quote
                and self._is_execution_allowed(order.instrument_key)
            ):
                self._try_fill(order, quote)
            return order

    def cancel(self, order_id: str) -> Order:
        with self._lock:
            order = self.orders[order_id]
            if order.status not in ACTIVE_ORDER_STATUSES:
                raise ValueError("only active orders can be cancelled")
            order.status = OrderStatus.CANCELLED
            order.cancelled_at = utc_now()
            order.updated_at = order.cancelled_at
            self.repository.save_order(order)
            return order

    def on_quote(self, quote: Quote) -> List[Fill]:
        with self._lock:
            self.quotes[quote.instrument_key] = quote
            self._books[quote.instrument_key] = self._book_from_quote(quote)
            if not self._is_execution_allowed(quote.instrument_key):
                reason = self._execution_block_reason(quote.instrument_key)
                for order in self.orders.values():
                    if (
                        order.instrument_key == quote.instrument_key
                        and order.status in ACTIVE_ORDER_STATUSES
                        and order.validity == Validity.IOC
                    ):
                        self._expire(order, quote, reason)
                return []
            created: List[Fill] = []
            for order in list(self.orders.values()):
                if (
                    order.status not in ACTIVE_ORDER_STATUSES
                    or order.instrument_key != quote.instrument_key
                ):
                    continue
                created.extend(self._try_fill(order, quote))
            return created

    def update_market_status(self, statuses: Mapping[str, str]) -> None:
        with self._lock:
            previous = dict(self.market_statuses)
            self.market_statuses.update(statuses)
            for segment, status in statuses.items():
                # IOC orders must never survive until a later session. Any IOC
                # still active when status is refreshed is stale and is expired
                # before a new opening quote can accidentally fill it.
                self._expire_ioc_orders(segment, status)
                if (
                    previous.get(segment) in EXECUTABLE_MARKET_STATUSES
                    and status not in EXECUTABLE_MARKET_STATUSES
                ):
                    self._expire_day_orders(segment)

    def set_kill_switch(self, active: bool) -> None:
        with self._lock:
            self.kill_switch = active
            if active:
                for order in self.orders.values():
                    if order.status in ACTIVE_ORDER_STATUSES:
                        order.status = OrderStatus.CANCELLED
                        order.cancelled_at = utc_now()
                        order.updated_at = order.cancelled_at
                        self.repository.save_order(order)

    def snapshot(self) -> dict:
        with self._lock:
            positions = []
            market_value = 0.0
            unrealized = 0.0
            realized = 0.0
            for position in self.positions.values():
                quote = self.quotes.get(position.instrument_key)
                last_price = quote.last_price if quote else position.average_price
                value = position.market_value(last_price)
                open_pnl = position.unrealized_pnl(last_price)
                market_value += value
                unrealized += open_pnl
                realized += position.realized_pnl
                positions.append(
                    {
                        "instrument_key": position.instrument_key,
                        "quantity": position.quantity,
                        "average_price": round(position.average_price, 4),
                        "last_price": last_price,
                        "market_value": round(value, 2),
                        "realized_pnl": round(position.realized_pnl, 2),
                        "unrealized_pnl": round(open_pnl, 2),
                    }
                )
            return {
                "account": self.account.to_dict(),
                "initial_cash": self.initial_cash,
                "cash": round(self.cash, 2),
                "market_value": round(market_value, 2),
                "equity": round(self.cash + market_value, 2),
                "realized_pnl": round(realized, 2),
                "unrealized_pnl": round(unrealized, 2),
                "fees_paid": round(sum(fill.fees for fill in self.fills), 2),
                "charges_paid": self._aggregate_charges(),
                "kill_switch": self.kill_switch,
                "market_statuses": dict(sorted(self.market_statuses.items())),
                "positions": positions,
            }

    def _book_from_quote(self, quote: Quote) -> Tuple[List[List[float]], List[List[float]]]:
        bids = [[level.price, float(level.quantity)] for level in quote.bids]
        asks = [[level.price, float(level.quantity)] for level in quote.asks]
        bids.sort(key=lambda level: level[0], reverse=True)
        asks.sort(key=lambda level: level[0])
        return bids, asks

    def _is_execution_allowed(self, instrument_key: str) -> bool:
        segment = instrument_key.partition("|")[0]
        status = self.market_statuses.get(segment)
        return status is None or status in EXECUTABLE_MARKET_STATUSES

    def _execution_block_reason(self, instrument_key: str) -> Optional[str]:
        segment = instrument_key.partition("|")[0]
        status = self.market_statuses.get(segment)
        if status is None or status in EXECUTABLE_MARKET_STATUSES:
            return None
        return f"market status {status} does not allow execution"

    def _try_fill(self, order: Order, quote: Quote) -> List[Fill]:
        if order.order_type == OrderType.STOP_MARKET and order.triggered_at is None:
            triggered = (
                order.side == Side.BUY and quote.last_price >= float(order.trigger_price)
            ) or (order.side == Side.SELL and quote.last_price <= float(order.trigger_price))
            if not triggered:
                return []
            order.triggered_at = quote.timestamp
            order.updated_at = quote.timestamp
            order.status = OrderStatus.OPEN
            self.repository.save_order(order)

        prices = self._executable_levels(order, quote)
        if not prices:
            if order.validity == Validity.IOC:
                self._expire(order, quote)
            return []

        first_price = self._apply_slippage(prices[0][0], order.side, order.limit_price)
        reason = self._risk_rejection(order, first_price, order.remaining_quantity)
        if reason:
            if order.filled_quantity:
                order.status = OrderStatus.CANCELLED
                order.rejection_reason = reason
                order.cancelled_at = quote.timestamp
                order.updated_at = quote.timestamp
                self.repository.save_order(order)
            else:
                self._reject(order, reason)
            return []

        created = []
        remaining = order.remaining_quantity
        for level in prices:
            if remaining <= 0:
                break
            available = int(level[1]) if level[1] != float("inf") else remaining
            if available <= 0:
                continue
            quantity = min(remaining, available)
            price = self._apply_slippage(level[0], order.side, order.limit_price)
            incremental_reason = self._risk_rejection(order, price, quantity, incremental=True)
            if incremental_reason:
                break
            created.append(self._execute(order, quantity, price, quote))
            remaining -= quantity
            if level[1] != float("inf"):
                level[1] -= quantity

        if order.remaining_quantity and order.validity == Validity.IOC:
            self._expire(order, quote)
        return created

    def _executable_levels(self, order: Order, quote: Quote) -> List[List[float]]:
        bids, asks = self._books.get(quote.instrument_key, ([], []))
        levels = asks if order.side == Side.BUY else bids
        if not levels:
            fallback = (
                (quote.ask or quote.last_price)
                if order.side == Side.BUY
                else (quote.bid or quote.last_price)
            )
            levels = [[fallback, float("inf")]]
        if order.order_type == OrderType.LIMIT:
            limit = float(order.limit_price)
            if order.side == Side.BUY:
                return [level for level in levels if level[0] <= limit and level[1] > 0]
            return [level for level in levels if level[0] >= limit and level[1] > 0]
        return [level for level in levels if level[1] > 0]

    def _apply_slippage(
        self, price: float, side: Side, limit_price: Optional[float] = None
    ) -> float:
        direction = 1 if side == Side.BUY else -1
        result = round(price * (1 + direction * self.slippage_bps / 10_000), 4)
        if limit_price is not None:
            result = min(result, limit_price) if side == Side.BUY else max(result, limit_price)
        return result

    def _risk_rejection(
        self,
        order: Order,
        price: float,
        quantity: int,
        incremental: bool = False,
    ) -> Optional[str]:
        if not incremental and order.quantity * price > self.risk_limits.max_order_notional:
            return "max order notional exceeded"
        position = self.positions.get(order.instrument_key, Position(order.instrument_key))
        delta = quantity if order.side == Side.BUY else -quantity
        projected_quantity = position.quantity + delta
        if not self.risk_limits.allow_short and projected_quantity < 0:
            return "short selling is disabled"
        if abs(projected_quantity * price) > self.risk_limits.max_position_notional:
            return "max position notional exceeded"
        estimated = self.fee_schedule.calculate(
            quantity * price,
            order.side,
            order.product,
            brokerage_already_charged=any(fill.order_id == order.id for fill in self.fills),
            dp_already_charged=self._has_dp_charge(order.instrument_key, utc_now().date()),
        )
        if order.side == Side.BUY and quantity * price + estimated.total > self.cash:
            return "insufficient virtual cash"
        if self.snapshot()["equity"] <= self.initial_cash - self.risk_limits.max_daily_loss:
            self.kill_switch = True
            return "max daily loss reached"
        return None

    def _execute(self, order: Order, quantity: int, price: float, quote: Quote) -> Fill:
        notional = quantity * price
        brokerage_charged = any(fill.order_id == order.id for fill in self.fills)
        dp_key = (order.instrument_key, quote.timestamp.date())
        charges = self.fee_schedule.calculate(
            notional,
            order.side,
            order.product,
            brokerage_already_charged=brokerage_charged,
            dp_already_charged=dp_key in self._dp_charged,
        )
        fill = Fill(
            order_id=order.id,
            instrument_key=order.instrument_key,
            side=order.side,
            quantity=quantity,
            price=price,
            fees=charges.total,
            product=order.product,
            charges=charges,
            timestamp=quote.timestamp,
        )
        cash_delta = fill.gross_value if fill.side == Side.SELL else -fill.gross_value
        self.cash += cash_delta - fill.fees
        self.positions.setdefault(fill.instrument_key, Position(fill.instrument_key)).apply(fill)
        previous_value = (order.average_filled_price or 0.0) * order.filled_quantity
        order.filled_quantity += quantity
        order.average_filled_price = (previous_value + fill.gross_value) / order.filled_quantity
        order.filled_price = order.average_filled_price
        order.updated_at = fill.timestamp
        if order.remaining_quantity == 0:
            order.status = OrderStatus.FILLED
            order.filled_at = fill.timestamp
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        self.fills.append(fill)
        if charges.dp:
            self._dp_charged.add(dp_key)
        self.repository.save_order(order)
        self.repository.save_fill(self.account.id, fill)
        return fill

    def _reject(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.rejection_reason = reason
        order.updated_at = utc_now()
        self.orders[order.id] = order
        self.repository.save_order(order)
        return order

    def _expire(
        self,
        order: Order,
        quote: Optional[Quote] = None,
        reason: Optional[str] = None,
    ) -> None:
        order.status = OrderStatus.EXPIRED
        if reason:
            order.rejection_reason = reason
        order.expired_at = quote.timestamp if quote else utc_now()
        order.updated_at = order.expired_at
        self.repository.save_order(order)

    def _expire_ioc_orders(self, segment: str, status: str) -> None:
        reason = f"market status {status} refreshed before IOC execution"
        for order in self.orders.values():
            if (
                order.status in ACTIVE_ORDER_STATUSES
                and order.validity == Validity.IOC
                and order.instrument_key.partition("|")[0] == segment
            ):
                self._expire(order, reason=reason)

    def _expire_day_orders(self, segment: str) -> None:
        for order in self.orders.values():
            if (
                order.status in ACTIVE_ORDER_STATUSES
                and order.validity == Validity.DAY
                and order.instrument_key.partition("|")[0] == segment
            ):
                self._expire(order)

    def _has_dp_charge(self, instrument_key: str, day: date) -> bool:
        return (instrument_key, day) in self._dp_charged

    def _aggregate_charges(self) -> dict:
        keys = ("brokerage", "stt", "exchange_transaction", "sebi", "stamp_duty", "gst", "dp")
        result = {
            key: round(sum(getattr(fill.charges, key) for fill in self.fills), 2) for key in keys
        }
        result["total"] = round(sum(fill.fees for fill in self.fills), 2)
        return result
