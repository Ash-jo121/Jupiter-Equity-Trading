from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from statistics import median
from threading import Condition, RLock
from time import monotonic
from typing import List, Optional

from .market_data import UpstoxMarketData


@dataclass(frozen=True)
class SurveyInstrument:
    symbol: str
    instrument_key: str


class MarketSurvey:
    """Transparent intraday ranking used to shortlist, not predict, paper trades."""

    def __init__(self, market_data: UpstoxMarketData, minimum_relative_volume: float = 1.2) -> None:
        if minimum_relative_volume <= 0:
            raise ValueError("minimum_relative_volume must be positive")
        self.market_data = market_data
        self.minimum_relative_volume = minimum_relative_volume

    def run(self, instruments: List[SurveyInstrument]) -> dict:
        keys = [item.instrument_key for item in instruments]
        quotes = self.market_data.ltp(keys)
        # Alpaca can return the complete multi-symbol bar set in one request.
        # Upstox has no equivalent method, so its adapter simply skips this
        # optional optimization and keeps the existing per-instrument reads.
        prefetch = getattr(self.market_data, "prefetch_intraday", None)
        if prefetch:
            prefetch(keys, "minutes", 5)
        rows = []
        failures = []

        def analyze(instrument: SurveyInstrument):
            quote = quotes.get(instrument.instrument_key)
            if not quote:
                raise ValueError("LTP was not returned")
            candles = self.market_data.intraday_candles(instrument.instrument_key, "minutes", 5)
            if not candles:
                raise ValueError("intraday candles were not returned")
            session_open = candles[0].open
            last_price = quote.last_price
            session_change = (last_price / session_open - 1) * 100
            recent_base = candles[-4].close if len(candles) >= 4 else candles[0].close
            recent_change = (last_price / recent_base - 1) * 100
            session_high = max(candle.high for candle in candles)
            session_low = min(candle.low for candle in candles)
            range_position = (
                (last_price - session_low) / (session_high - session_low) * 100
                if session_high > session_low
                else 50.0
            )
            score = session_change * 0.4 + recent_change * 0.6
            volume = _relative_volume(candles)
            volume_confirmed = (
                volume["relative_volume"] is not None
                and volume["relative_volume"] >= self.minimum_relative_volume
            )
            return {
                "symbol": instrument.symbol,
                "instrument_key": instrument.instrument_key,
                "last_price": round(last_price, 4),
                "session_open": round(session_open, 4),
                "session_change_pct": round(session_change, 4),
                "recent_15m_change_pct": round(recent_change, 4),
                "session_high": round(session_high, 4),
                "session_low": round(session_low, 4),
                "range_position_pct": round(range_position, 2),
                "momentum_score": round(score, 4),
                **volume,
                "minimum_relative_volume": self.minimum_relative_volume,
                "volume_signal": (
                    "UNAVAILABLE"
                    if volume["relative_volume"] is None
                    else "HIGH"
                    if volume_confirmed
                    else "LOW"
                ),
                "volume_confirmed": volume_confirmed,
                "suggested_entry_price": round(last_price, 2),
                "suggested_profit_target_pct": 1.0,
                "suggested_stop_loss_pct": 0.5,
                "eligible": score > 0 and range_position < 95 and volume_confirmed,
            }

        with ThreadPoolExecutor(max_workers=min(5, len(instruments))) as executor:
            futures = {
                executor.submit(analyze, instrument): instrument for instrument in instruments
            }
            for future in as_completed(futures):
                instrument = futures[future]
                try:
                    rows.append(future.result())
                except Exception as error:  # noqa: BLE001 - isolates external-data failures
                    failures.append({"symbol": instrument.symbol, "error": str(error)[:200]})
        rows.sort(key=lambda row: row["momentum_score"], reverse=True)
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank
        return {
            "method": (
                "40% session momentum + 60% trailing 15-minute momentum, "
                f"with mandatory relative volume >= {self.minimum_relative_volume:.2f}x"
            ),
            "note": "Descriptive paper-trading screen; it is not a forecast or recommendation.",
            "results": rows,
            "requested": len(instruments),
            "analyzed": len(rows),
            "failures": failures,
        }


class SharedSurveyCache:
    """Share one expensive NIFTY survey between concurrent strategy runners.

    Intraday candles are five-minute bars, so asking Upstox for the same 100
    histories once per runner, once per minute, adds load without adding new
    information. The first caller refreshes the snapshot while concurrent and
    subsequent callers reuse it. A failed or partial refresh is cached only
    briefly so the service can recover without another synchronized burst.
    """

    def __init__(self, ttl_seconds: float = 285.0, failure_ttl_seconds: float = 60.0) -> None:
        if ttl_seconds <= 0 or failure_ttl_seconds <= 0:
            raise ValueError("survey cache TTLs must be positive")
        self.ttl_seconds = ttl_seconds
        self.failure_ttl_seconds = failure_ttl_seconds
        self._condition = Condition(RLock())
        self._entries: dict[tuple, tuple[float, float, dict]] = {}
        self._refreshing: set[tuple] = set()

    def run(
        self,
        market_data: UpstoxMarketData,
        instruments: List[SurveyInstrument],
        minimum_relative_volume: float,
        context_instrument_key: Optional[str] = None,
    ) -> dict:
        key = (
            tuple(item.instrument_key for item in instruments),
            round(minimum_relative_volume, 6),
            context_instrument_key,
        )
        with self._condition:
            while True:
                now = monotonic()
                entry = self._entries.get(key)
                if entry and now < entry[0]:
                    return self._copy(entry, cache_hit=True, now=now)
                if key not in self._refreshing:
                    self._refreshing.add(key)
                    break
                self._condition.wait()

        try:
            result = MarketSurvey(market_data, minimum_relative_volume).run(instruments)
            if context_instrument_key:
                result["market_context"] = _candle_context(market_data, context_instrument_key)
            built_at = monotonic()
            healthy = result["analyzed"] == result["requested"]
            ttl = self.ttl_seconds if healthy else self.failure_ttl_seconds
            entry = (built_at + ttl, built_at, deepcopy(result))
            with self._condition:
                self._entries[key] = entry
            return self._copy(entry, cache_hit=False, now=built_at)
        finally:
            with self._condition:
                self._refreshing.discard(key)
                self._condition.notify_all()

    @staticmethod
    def _copy(entry: tuple[float, float, dict], cache_hit: bool, now: float) -> dict:
        _expires_at, built_at, result = entry
        payload = deepcopy(result)
        payload["shared_cache"] = {
            "hit": cache_hit,
            "age_seconds": round(max(0.0, now - built_at), 2),
        }
        return payload


def _candle_context(market_data: UpstoxMarketData, instrument_key: str) -> dict:
    """Capture shared session and trailing-15-minute reference prices."""

    try:
        candles = market_data.intraday_candles(instrument_key, "minutes", 5)
        if not candles:
            raise ValueError("intraday candles were not returned")
        return {
            "session_open": candles[0].open,
            "recent_15m": (candles[-4].close if len(candles) >= 4 else candles[0].close),
            "error": None,
        }
    except Exception as error:  # noqa: BLE001 - context is descriptive, not a gate
        return {
            "session_open": None,
            "recent_15m": None,
            "error": str(error)[:200],
        }


def _relative_volume(candles) -> dict:
    """Compare the last three completed 5-minute candles with the prior intraday median."""

    completed = candles[:-1]
    if len(completed) < 6:
        return {
            "recent_volume": None,
            "baseline_volume": None,
            "relative_volume": None,
        }
    recent = completed[-3:]
    baseline = completed[:-3][-12:]
    recent_average = sum(candle.volume for candle in recent) / len(recent)
    baseline_median = median(candle.volume for candle in baseline)
    ratio = recent_average / baseline_median if baseline_median > 0 else None
    return {
        "recent_volume": round(recent_average, 2),
        "baseline_volume": round(baseline_median, 2),
        "relative_volume": round(ratio, 4) if ratio is not None else None,
    }
