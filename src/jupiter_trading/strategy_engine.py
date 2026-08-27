from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Dict, List, Optional
from uuid import uuid4

from .accounts import PaperAccountManager
from .domain import ACTIVE_ORDER_STATUSES, Order, OrderType, Product, Quote, Side, Validity
from .research_store import ResearchStore


class StrategyStatus(str, Enum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


@dataclass
class ProfitTargetLeg:
    instrument_key: str
    symbol: str
    quantity: int = 1
    entry_price: Optional[float] = None
    profit_target_pct: float = 1.0
    stop_loss_pct: float = 0.5
    absolute_profit_target: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.instrument_key or not self.symbol:
            raise ValueError("instrument_key and symbol are required")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.entry_price is not None and self.entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if self.profit_target_pct <= 0 or self.stop_loss_pct <= 0:
            raise ValueError("profit target and stop loss must be positive")
        if self.absolute_profit_target is not None and self.absolute_profit_target <= 0:
            raise ValueError("absolute_profit_target must be positive")


@dataclass
class StrategyDefinition:
    name: str
    account_id: str
    legs: List[ProfitTargetLeg]
    id: str = field(default_factory=lambda: str(uuid4()))
    strategy_type: str = "PROFIT_TARGET"
    status: StrategyStatus = StrategyStatus.DRAFT
    completed_instruments: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if not self.name or not self.account_id or not self.legs:
            raise ValueError("name, account_id, and at least one leg are required")

    def to_dict(self) -> dict:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: dict) -> StrategyDefinition:
        return cls(
            id=value["id"],
            name=value["name"],
            account_id=value["account_id"],
            legs=[ProfitTargetLeg(**leg) for leg in value["legs"]],
            strategy_type=value.get("strategy_type", "PROFIT_TARGET"),
            status=StrategyStatus(value.get("status", "DRAFT")),
            completed_instruments=value.get("completed_instruments", []),
            created_at=value["created_at"],
            updated_at=value["updated_at"],
        )


class ProfitTargetRule:
    @staticmethod
    def entry_reached(price: float, entry_price: Optional[float]) -> bool:
        return entry_price is None or price <= entry_price

    @staticmethod
    def exit_reason(price: float, average_price: float, leg: ProfitTargetLeg) -> Optional[str]:
        percent_target = average_price * (1 + leg.profit_target_pct / 100)
        targets = [percent_target]
        if leg.absolute_profit_target is not None:
            targets.append(average_price + leg.absolute_profit_target)
        if price >= min(targets):
            return "PROFIT_TARGET"
        if price <= average_price * (1 - leg.stop_loss_pct / 100):
            return "STOP_LOSS"
        return None


class StrategyService:
    def __init__(self, accounts: PaperAccountManager, store: ResearchStore) -> None:
        self.accounts = accounts
        self.store = store
        self._strategies: Dict[str, StrategyDefinition] = {
            value["id"]: StrategyDefinition.from_dict(value) for value in store.strategies()
        }
        self._lock = RLock()

    def list(self) -> List[dict]:
        with self._lock:
            return [self._view(strategy) for strategy in self._strategies.values()]

    def get(self, strategy_id: str) -> dict:
        with self._lock:
            return self._view(self._strategies[strategy_id])

    def create(self, strategy: StrategyDefinition) -> dict:
        with self._lock:
            if strategy.id in self._strategies:
                raise ValueError("strategy already exists")
            self.accounts.get(strategy.account_id)
            self._strategies[strategy.id] = strategy
            self._save(strategy, "CREATED")
            return self._view(strategy)

    def set_status(self, strategy_id: str, status: StrategyStatus) -> dict:
        with self._lock:
            strategy = self._strategies[strategy_id]
            if strategy.status == StrategyStatus.STOPPED and status != StrategyStatus.STOPPED:
                raise ValueError("a stopped strategy cannot be restarted")
            strategy.status = status
            strategy.updated_at = datetime.now(timezone.utc).isoformat()
            self._save(strategy, status.value)
            return self._view(strategy)

    def on_quote(self, quote: Quote) -> List[Order]:
        with self._lock:
            created = []
            for strategy in self._strategies.values():
                if strategy.status != StrategyStatus.RUNNING:
                    continue
                if all(
                    item.instrument_key in strategy.completed_instruments
                    for item in strategy.legs
                ):
                    strategy.status = StrategyStatus.STOPPED
                    strategy.updated_at = datetime.now(timezone.utc).isoformat()
                    self._save(strategy, "COMPLETED")
                    continue
                leg = next(
                    (item for item in strategy.legs if item.instrument_key == quote.instrument_key),
                    None,
                )
                if not leg or leg.instrument_key in strategy.completed_instruments:
                    continue
                order = self._evaluate(strategy, leg, quote)
                if order:
                    created.append(order)
            return created

    def events(self, strategy_id: str) -> List[dict]:
        if strategy_id not in self._strategies:
            raise KeyError(strategy_id)
        return self.store.strategy_events(strategy_id)

    def _evaluate(
        self, strategy: StrategyDefinition, leg: ProfitTargetLeg, quote: Quote
    ) -> Optional[Order]:
        broker = self.accounts.get(strategy.account_id)
        position = broker.positions.get(leg.instrument_key)
        quantity = position.quantity if position else 0
        active = [
            order
            for order in broker.orders.values()
            if order.strategy_id == strategy.id
            and order.instrument_key == leg.instrument_key
            and order.status in ACTIVE_ORDER_STATUSES
        ]
        if active:
            return None

        completed_sell = any(
            order.strategy_id == strategy.id
            and order.instrument_key == leg.instrument_key
            and order.side == Side.SELL
            and order.filled_quantity > 0
            for order in broker.orders.values()
        )
        if quantity <= 0 and completed_sell:
            strategy.completed_instruments.append(leg.instrument_key)
            strategy.updated_at = datetime.now(timezone.utc).isoformat()
            self._save(strategy, "LEG_COMPLETED", {"instrument_key": leg.instrument_key})
            return None

        if quantity <= 0:
            if not ProfitTargetRule.entry_reached(quote.last_price, leg.entry_price):
                return None
            return self._submit(strategy, leg, Side.BUY, leg.quantity, quote, "ENTRY")

        reason = ProfitTargetRule.exit_reason(quote.last_price, position.average_price, leg)
        if not reason:
            return None
        return self._submit(strategy, leg, Side.SELL, quantity, quote, reason)

    def _submit(
        self,
        strategy: StrategyDefinition,
        leg: ProfitTargetLeg,
        side: Side,
        quantity: int,
        quote: Quote,
        reason: str,
    ) -> Order:
        broker = self.accounts.get(strategy.account_id)
        order = Order(
            instrument_key=leg.instrument_key,
            side=side,
            quantity=quantity,
            order_type=(
                OrderType.LIMIT
                if side == Side.BUY and leg.entry_price is not None
                else OrderType.MARKET
            ),
            limit_price=leg.entry_price if side == Side.BUY else None,
            strategy_id=strategy.id,
            product=Product.DELIVERY,
            validity=Validity.DAY,
            account_id=strategy.account_id,
        )
        broker.submit(order)
        self.store.add_strategy_event(
            strategy.id,
            reason,
            {
                "timestamp": quote.timestamp.isoformat(),
                "instrument_key": leg.instrument_key,
                "symbol": leg.symbol,
                "observed_price": quote.last_price,
                "order": order.to_dict(),
            },
        )
        return order

    def _save(
        self, strategy: StrategyDefinition, event_type: str, extra: Optional[dict] = None
    ) -> None:
        self.store.save_strategy(strategy.to_dict())
        self.store.add_strategy_event(
            strategy.id,
            event_type,
            {"timestamp": strategy.updated_at, **(extra or {})},
        )

    def _view(self, strategy: StrategyDefinition) -> dict:
        value = strategy.to_dict()
        try:
            broker = self.accounts.get(strategy.account_id)
        except KeyError:
            value["account_missing"] = True
            return value
        value["positions"] = [
            position
            for position in broker.snapshot()["positions"]
            if any(leg.instrument_key == position["instrument_key"] for leg in strategy.legs)
        ]
        value["orders"] = [
            order.to_dict()
            for order in broker.orders.values()
            if order.strategy_id == strategy.id
        ]
        return value


class MarketCoordinator:
    def __init__(self, accounts: PaperAccountManager, strategies: StrategyService) -> None:
        self.accounts = accounts
        self.strategies = strategies

    def on_quote(self, quote: Quote) -> list:
        fills = self.accounts.on_quote(quote)
        before = {
            account["account"]["id"]: len(self.accounts.get(account["account"]["id"]).fills)
            for account in self.accounts.list()
        }
        self.strategies.on_quote(quote)
        for account_id, count in before.items():
            fills.extend(self.accounts.get(account_id).fills[count:])
        return fills

    def update_market_status(self, statuses: dict) -> None:
        self.accounts.update_market_status(statuses)
