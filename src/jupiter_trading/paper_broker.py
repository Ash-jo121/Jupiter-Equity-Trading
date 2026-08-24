from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Dict, List, Optional

from .domain import Fill, Order, OrderStatus, OrderType, Position, Quote, Side
from .repository import InMemoryRepository, TradingRepository


@dataclass(frozen=True)
class FeeSchedule:
    """Configurable all-in fee components, expressed in basis points."""

    brokerage_bps: float = 0.0
    transaction_bps: float = 0.0
    tax_bps: float = 0.0

    def calculate(self, notional: float) -> float:
        total_bps = self.brokerage_bps + self.transaction_bps + self.tax_bps
        return round(notional * total_bps / 10_000, 2)


@dataclass(frozen=True)
class RiskLimits:
    allow_short: bool = False
    max_order_notional: float = 250_000
    max_position_notional: float = 500_000
    max_daily_loss: float = 25_000


class PaperBroker:
    """Deterministic simulated broker driven by externally supplied quotes."""

    def __init__(
        self,
        initial_cash: float = 1_000_000,
        slippage_bps: float = 2.0,
        fee_schedule: Optional[FeeSchedule] = None,
        risk_limits: Optional[RiskLimits] = None,
        repository: Optional[TradingRepository] = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative")
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.slippage_bps = slippage_bps
        self.fee_schedule = fee_schedule or FeeSchedule()
        self.risk_limits = risk_limits or RiskLimits()
        self.repository = repository or InMemoryRepository()
        self.orders: Dict[str, Order] = {}
        self.fills: List[Fill] = []
        self.positions: Dict[str, Position] = {}
        self.quotes: Dict[str, Quote] = {}
        self.kill_switch = False
        self._lock = RLock()

    def submit(self, order: Order) -> Order:
        with self._lock:
            if self.kill_switch:
                return self._reject(order, "kill switch is active")
            self.orders[order.id] = order
            self.repository.save_order(order)
            quote = self.quotes.get(order.instrument_key)
            if quote:
                self._try_fill(order, quote)
            return order

    def cancel(self, order_id: str) -> Order:
        with self._lock:
            order = self.orders[order_id]
            if order.status != OrderStatus.OPEN:
                raise ValueError("only open orders can be cancelled")
            order.status = OrderStatus.CANCELLED
            self.repository.save_order(order)
            return order

    def on_quote(self, quote: Quote) -> List[Fill]:
        with self._lock:
            self.quotes[quote.instrument_key] = quote
            created = []
            for order in list(self.orders.values()):
                if order.status != OrderStatus.OPEN or order.instrument_key != quote.instrument_key:
                    continue
                fill = self._try_fill(order, quote)
                if fill:
                    created.append(fill)
            return created

    def set_kill_switch(self, active: bool) -> None:
        with self._lock:
            self.kill_switch = active
            if active:
                for order in self.orders.values():
                    if order.status == OrderStatus.OPEN:
                        order.status = OrderStatus.CANCELLED
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
                "initial_cash": self.initial_cash,
                "cash": round(self.cash, 2),
                "market_value": round(market_value, 2),
                "equity": round(self.cash + market_value, 2),
                "realized_pnl": round(realized, 2),
                "unrealized_pnl": round(unrealized, 2),
                "fees_paid": round(sum(fill.fees for fill in self.fills), 2),
                "kill_switch": self.kill_switch,
                "positions": positions,
            }

    def _try_fill(self, order: Order, quote: Quote) -> Optional[Fill]:
        base_price: Optional[float] = None
        if order.order_type == OrderType.MARKET:
            base_price = self._market_price(order.side, quote)
        elif order.order_type == OrderType.LIMIT:
            touch = quote.ask if order.side == Side.BUY else quote.bid
            touch = touch or quote.last_price
            if order.side == Side.BUY and touch <= float(order.limit_price):
                base_price = min(touch, float(order.limit_price))
            elif order.side == Side.SELL and touch >= float(order.limit_price):
                base_price = max(touch, float(order.limit_price))
        elif order.order_type == OrderType.STOP_MARKET:
            triggered = (
                order.side == Side.BUY and quote.last_price >= float(order.trigger_price)
            ) or (order.side == Side.SELL and quote.last_price <= float(order.trigger_price))
            if triggered:
                base_price = self._market_price(order.side, quote)

        if base_price is None:
            return None
        price = self._apply_slippage(base_price, order.side)
        reason = self._risk_rejection(order, price)
        if reason:
            self._reject(order, reason)
            return None
        return self._execute(order, price, quote)

    def _market_price(self, side: Side, quote: Quote) -> float:
        return (quote.ask or quote.last_price) if side == Side.BUY else (quote.bid or quote.last_price)

    def _apply_slippage(self, price: float, side: Side) -> float:
        direction = 1 if side == Side.BUY else -1
        return round(price * (1 + direction * self.slippage_bps / 10_000), 4)

    def _risk_rejection(self, order: Order, price: float) -> Optional[str]:
        notional = order.quantity * price
        if notional > self.risk_limits.max_order_notional:
            return "max order notional exceeded"
        position = self.positions.get(order.instrument_key, Position(order.instrument_key))
        delta = order.quantity if order.side == Side.BUY else -order.quantity
        projected_quantity = position.quantity + delta
        if not self.risk_limits.allow_short and projected_quantity < 0:
            return "short selling is disabled"
        if abs(projected_quantity * price) > self.risk_limits.max_position_notional:
            return "max position notional exceeded"
        projected_fees = self.fee_schedule.calculate(notional)
        if order.side == Side.BUY and notional + projected_fees > self.cash:
            return "insufficient virtual cash"
        if self.snapshot()["equity"] <= self.initial_cash - self.risk_limits.max_daily_loss:
            self.kill_switch = True
            return "max daily loss reached"
        return None

    def _execute(self, order: Order, price: float, quote: Quote) -> Fill:
        fees = self.fee_schedule.calculate(order.quantity * price)
        fill = Fill(
            order_id=order.id,
            instrument_key=order.instrument_key,
            side=order.side,
            quantity=order.quantity,
            price=price,
            fees=fees,
            timestamp=quote.timestamp,
        )
        cash_delta = fill.gross_value if fill.side == Side.SELL else -fill.gross_value
        self.cash += cash_delta - fees
        position = self.positions.setdefault(
            fill.instrument_key, Position(instrument_key=fill.instrument_key)
        )
        position.apply(fill)
        order.status = OrderStatus.FILLED
        order.filled_at = fill.timestamp
        order.filled_price = fill.price
        self.fills.append(fill)
        self.repository.save_order(order)
        self.repository.save_fill(fill)
        return fill

    def _reject(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.rejection_reason = reason
        self.orders[order.id] = order
        self.repository.save_order(order)
        return order

