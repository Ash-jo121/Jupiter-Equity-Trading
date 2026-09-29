from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Optional
from zoneinfo import ZoneInfo

from .candle_signals import BarFeatures, SignalConfig, admitted_candles, build_features
from .market_data import Candle, SharedCandleCache, UpstoxMarketData


@dataclass(frozen=True)
class FeatureSnapshot:
    instrument_key: str
    features: BarFeatures
    candles: tuple[Candle, ...]
    available_at: datetime
    received_at: datetime
    stale: bool
    cache_hit: bool

    @property
    def bar_id(self) -> str:
        return self.features.bar_id

    def to_dict(self) -> dict:
        return {
            "instrument_key": self.instrument_key,
            "bar_id": self.bar_id,
            "available_at": self.available_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "stale": self.stale,
            "cache_hit": self.cache_hit,
            "features": self.features.to_dict(),
        }


class SharedFeatureEngine:
    """Build one immutable feature snapshot per stock/bar for all strategies."""

    def __init__(
        self,
        market_data: UpstoxMarketData,
        candle_cache: SharedCandleCache,
        config: Optional[SignalConfig] = None,
        market_timezone: str = "Asia/Kolkata",
        max_symbols: int = 20,
    ) -> None:
        self.market_data = market_data
        self.candle_cache = candle_cache
        self.config = config or SignalConfig()
        self.market_timezone = market_timezone
        self.max_symbols = max_symbols
        self._lock = RLock()
        self._last_bar: dict[str, str] = {}
        self._warmups: dict[str, list[Candle]] = {}
        self._warmup_attempted: set[str] = set()

    def snapshot(
        self, instrument_key: str, clock: Optional[datetime] = None
    ) -> Optional[FeatureSnapshot]:
        now = clock or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        data = self.candle_cache.get(
            self.market_data,
            instrument_key,
            now,
            self.config.candle_finalization_grace_seconds,
        )
        candles = admitted_candles(
            data["candles"], now, self.config.candle_finalization_grace_seconds
        )
        if not candles:
            return None
        features = build_features(candles, self.config, self._warmup(instrument_key, now))
        with self._lock:
            if self._last_bar.get(instrument_key) == features.bar_id:
                return None
            self._last_bar[instrument_key] = features.bar_id
            self._trim()
        available_at = max(
            features.bar_end
            + timedelta(seconds=self.config.candle_finalization_grace_seconds),
            data["received_at"],
        )
        stale = (available_at - features.bar_end).total_seconds() > (
            self.config.max_signal_bar_age_seconds
        )
        return FeatureSnapshot(
            instrument_key,
            features,
            tuple(candles[-120:]),
            available_at,
            data["received_at"],
            stale,
            bool(data.get("cache_hit")),
        )

    def forget(self, instrument_key: str) -> None:
        with self._lock:
            self._last_bar.pop(instrument_key, None)
            # Warm-up data is bounded and safe to retain for a later reassignment.

    def _warmup(self, instrument_key: str, clock: datetime) -> list[Candle]:
        with self._lock:
            if instrument_key in self._warmup_attempted:
                return self._warmups.get(instrument_key, [])
            self._warmup_attempted.add(instrument_key)
        session_date = clock.astimezone(ZoneInfo(self.market_timezone)).date()
        try:
            data = self.candle_cache.warmup(
                self.market_data,
                instrument_key,
                session_date,
                self.config.warmup_bars,
            )
            candles = list(data["candles"])[-self.config.warmup_bars :]
        except Exception:  # noqa: BLE001 - live bars may still become ready later
            candles = []
        with self._lock:
            self._warmups[instrument_key] = candles
            self._trim()
        return candles

    def _trim(self) -> None:
        while len(self._last_bar) > self.max_symbols:
            self._last_bar.pop(next(iter(self._last_bar)))
        while len(self._warmups) > self.max_symbols:
            key = next(iter(self._warmups))
            self._warmups.pop(key, None)
            self._warmup_attempted.discard(key)

