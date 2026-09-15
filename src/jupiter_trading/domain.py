from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple
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


class Product(str, Enum):
    DELIVERY = "CNC"
    INTRADAY = "MIS"


class Validity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


ACTIVE_ORDER_STATUSES = frozenset(
    {OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING, OrderStatus.PARTIALLY_FILLED}
)


@dataclass(frozen=True)
class DepthLevel:
    price: float
    quantity: int

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError("depth price must be positive")
        if self.quantity <= 0:
            raise ValueError("depth quantity must be positive")


@dataclass(frozen=True)
class Quote:
    instrument_key: str
    last_price: float
    timestamp: datetime = field(default_factory=utc_now)
    bid: Optional[float] = None
    ask: Optional[float] = None
    bids: Tuple[DepthLevel, ...] = ()
    asks: Tuple[DepthLevel, ...] = ()

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
    provider_order_id: Optional[str] = None
    product: Product = Product.DELIVERY
    validity: Validity = Validity.DAY
    account_id: str = "default"
    id: str = field(default_factory=lambda: str(uuid4()))
    status: OrderStatus = OrderStatus.OPEN
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    filled_at: Optional[datetime] = None
    filled_price: Optional[float] = None
    filled_quantity: int = 0
    average_filled_price: Optional[float] = None
    triggered_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    expired_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.instrument_key:
            raise ValueError("instrument_key is required")
        if not self.account_id:
            raise ValueError("account_id is required")
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
        if self.filled_quantity < 0 or self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity must be between zero and quantity")
        if self.order_type == OrderType.STOP_MARKET and self.status == OrderStatus.OPEN:
            self.status = OrderStatus.TRIGGER_PENDING

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["side"] = self.side.value
        value["order_type"] = self.order_type.value
        value["status"] = self.status.value
        value["product"] = self.product.value
        value["validity"] = self.validity.value
        value["created_at"] = self.created_at.isoformat()
        for key in (
            "updated_at",
            "filled_at",
            "triggered_at",
            "cancelled_at",
            "expired_at",
        ):
            timestamp = getattr(self, key)
            value[key] = timestamp.isoformat() if timestamp else None
        value["remaining_quantity"] = self.remaining_quantity
        return value


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_transaction: float = 0.0
    sebi: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0
    dp: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.brokerage
            + self.stt
            + self.exchange_transaction
            + self.sebi
            + self.stamp_duty
            + self.gst
            + self.dp,
            2,
        )

    def to_dict(self) -> Dict[str, float]:
        value = asdict(self)
        value["total"] = self.total
        return value


@dataclass
class PaperAccount:
    id: str
    name: str
    initial_cash: float
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    reset_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("account id and name are required")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        for key in ("created_at", "updated_at", "reset_at"):
            timestamp = getattr(self, key)
            value[key] = timestamp.isoformat() if timestamp else None
        return value


@dataclass(frozen=True)
class Fill:
    order_id: str
    instrument_key: str
    side: Side
    quantity: int
    price: float
    fees: float
    product: Product = Product.DELIVERY
    charges: ChargeBreakdown = field(default_factory=ChargeBreakdown)
    timestamp: datetime = field(default_factory=utc_now)
    id: str = field(default_factory=lambda: str(uuid4()))

    @property
    def gross_value(self) -> float:
        return self.quantity * self.price

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["side"] = self.side.value
        value["product"] = self.product.value
        value["charges"] = self.charges.to_dict()
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
