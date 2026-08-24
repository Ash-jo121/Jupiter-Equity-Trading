from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Protocol

from .domain import Order, OrderType, Quote, Side


class Strategy(Protocol):
    def on_quote(self, quote: Quote, current_quantity: int) -> Optional[Order]: ...


@dataclass
class MovingAverageCrossStrategy:
    """Small example strategy; deliberately simple and not investment advice."""

    instrument_key: str
    quantity: int = 1
    fast_window: int = 5
    slow_window: int = 20
    strategy_id: str = "sma-cross-example"

    def __post_init__(self) -> None:
        if not 1 < self.fast_window < self.slow_window:
            raise ValueError("windows must satisfy 1 < fast_window < slow_window")
        self._prices: Deque[float] = deque(maxlen=self.slow_window)
        self._last_signal: Optional[Side] = None

    def on_quote(self, quote: Quote, current_quantity: int) -> Optional[Order]:
        if quote.instrument_key != self.instrument_key:
            return None
        self._prices.append(quote.last_price)
        if len(self._prices) < self.slow_window:
            return None
        prices = list(self._prices)
        fast = sum(prices[-self.fast_window :]) / self.fast_window
        slow = sum(prices) / self.slow_window
        signal = Side.BUY if fast > slow else Side.SELL
        if signal == self._last_signal:
            return None
        self._last_signal = signal
        if signal == Side.BUY and current_quantity <= 0:
            quantity = self.quantity + abs(current_quantity)
        elif signal == Side.SELL and current_quantity > 0:
            quantity = current_quantity
        else:
            return None
        return Order(
            instrument_key=self.instrument_key,
            side=signal,
            quantity=quantity,
            order_type=OrderType.MARKET,
            strategy_id=self.strategy_id,
        )


class StrategyRunner:
    def __init__(self, strategy: Strategy, broker) -> None:
        self.strategy = strategy
        self.broker = broker

    def on_quote(self, quote: Quote) -> None:
        self.broker.on_quote(quote)
        position = self.broker.positions.get(quote.instrument_key)
        current_quantity = position.quantity if position else 0
        order = self.strategy.on_quote(quote, current_quantity)
        if order:
            self.broker.submit(order)

