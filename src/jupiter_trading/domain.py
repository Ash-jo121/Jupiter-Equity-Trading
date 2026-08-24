from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Quote:
    instrument_key: str
    last_price: float
    timestamp: datetime = field(default_factory=utc_now)
    bid: Optional[float] = None
    ask: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.instrument_key:
            raise ValueError("instrument_key is required")
        if self.last_price <= 0:
            raise ValueError("last_price must be positive")


@dataclass
class Order:
    instrument_key: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: Optional[float] = None
    trigger_price: Optional[float] = None
    strategy_id: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid4()))
    status: OrderStatus = OrderStatus.OPEN
    created_at: datetime = field(default_factory=utc_now)
    filled_at: Optional[datetime] = None
    filled_price: Optional[float] = None
    rejection_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.instrument_key:
            raise ValueError("instrument_key is required")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type == OrderType.LIMIT and not self.limit_price:
            raise ValueError("limit_price is required for LIMIT orders")
        if self.order_type == OrderType.STOP_MARKET and not self.trigger_price:
            raise ValueError("trigger_price is required for STOP_MARKET orders")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if self.trigger_price is not None and self.trigger_price <= 0:
            raise ValueError("trigger_price must be positive")

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["side"] = self.side.value
        value["order_type"] = self.order_type.value
        value["status"] = self.status.value
        value["created_at"] = self.created_at.isoformat()
        value["filled_at"] = self.filled_at.isoformat() if self.filled_at else None
        return value


@dataclass(frozen=True)
class Fill:
    order_id: str
    instrument_key: str
    side: Side
    quantity: int
    price: float
    fees: float
    timestamp: datetime = field(default_factory=utc_now)
    id: str = field(default_factory=lambda: str(uuid4()))

    @property
    def gross_value(self) -> float:
        return self.quantity * self.price

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["side"] = self.side.value
        value["timestamp"] = self.timestamp.isoformat()
        value["gross_value"] = self.gross_value
        return value


@dataclass
class Position:
    instrument_key: str
    quantity: int = 0
    average_price: float = 0.0
    realized_pnl: float = 0.0

    def apply(self, fill: Fill) -> None:
        delta = fill.quantity if fill.side == Side.BUY else -fill.quantity
        old_quantity = self.quantity
        new_quantity = old_quantity + delta

        if old_quantity == 0 or (old_quantity > 0) == (delta > 0):
            old_cost = abs(old_quantity) * self.average_price
            added_cost = abs(delta) * fill.price
            self.average_price = (old_cost + added_cost) / abs(new_quantity)
        else:
            closing_quantity = min(abs(old_quantity), abs(delta))
            direction = 1 if old_quantity > 0 else -1
            self.realized_pnl += (fill.price - self.average_price) * closing_quantity * direction
            if new_quantity == 0:
                self.average_price = 0.0
            elif (new_quantity > 0) != (old_quantity > 0):
                self.average_price = fill.price

        self.quantity = new_quantity

    def market_value(self, last_price: float) -> float:
        return self.quantity * last_price

    def unrealized_pnl(self, last_price: float) -> float:
        return (last_price - self.average_price) * self.quantity

