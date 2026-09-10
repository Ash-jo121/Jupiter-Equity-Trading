from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, List, Optional


@dataclass(frozen=True)
class MACDPoint:
    macd: Optional[float]
    signal: Optional[float]
    histogram: Optional[float]
    cross_up: bool = False

    def to_dict(self) -> dict:
        return {
            "macd": self.macd,
            "signal": self.signal,
            "histogram": self.histogram,
            "cross_up": self.cross_up,
        }


def ema(values: Iterable[float], period: int) -> List[Optional[float]]:
    """SMA-seeded EMA with undefined leading values kept as ``None``."""

    if period <= 0:
        raise ValueError("EMA period must be positive")
    samples = [float(value) for value in values]
    if any(not isfinite(value) for value in samples):
        raise ValueError("EMA values must be finite")
    result: List[Optional[float]] = [None] * len(samples)
    if len(samples) < period:
        return result
    current = sum(samples[:period]) / period
    result[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for index in range(period, len(samples)):
        current = alpha * samples[index] + (1.0 - alpha) * current
        result[index] = current
    return result


def macd_series(
    closes: Iterable[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> List[MACDPoint]:
    """Return index-aligned SMA-seeded MACD values without filling warm-up gaps."""

    if min(fast_period, slow_period, signal_period) <= 0:
        raise ValueError("MACD periods must be positive")
    if fast_period >= slow_period:
        raise ValueError("MACD fast period must be shorter than slow period")
    prices = [float(value) for value in closes]
    fast = ema(prices, fast_period)
    slow = ema(prices, slow_period)
    macd: List[Optional[float]] = [
        fast_value - slow_value
        if fast_value is not None and slow_value is not None
        else None
        for fast_value, slow_value in zip(fast, slow)
    ]
    first_macd = next((index for index, value in enumerate(macd) if value is not None), None)
    signal: List[Optional[float]] = [None] * len(prices)
    if first_macd is not None:
        available = [value for value in macd[first_macd:] if value is not None]
        seeded = ema(available, signal_period)
        for offset, value in enumerate(seeded):
            signal[first_macd + offset] = value
    histograms: List[Optional[float]] = [
        macd_value - signal_value
        if macd_value is not None and signal_value is not None
        else None
        for macd_value, signal_value in zip(macd, signal)
    ]
    points: List[MACDPoint] = []
    for index, (macd_value, signal_value, histogram) in enumerate(
        zip(macd, signal, histograms)
    ):
        previous = histograms[index - 1] if index else None
        points.append(
            MACDPoint(
                macd_value,
                signal_value,
                histogram,
                previous is not None
                and histogram is not None
                and previous <= 0
                and histogram > 0,
            )
        )
    return points

