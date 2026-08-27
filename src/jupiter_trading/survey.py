from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from statistics import median
from typing import List

from .market_data import UpstoxMarketData


@dataclass(frozen=True)
class SurveyInstrument:
    symbol: str
    instrument_key: str


class MarketSurvey:
    """Transparent intraday ranking used to shortlist, not predict, paper trades."""

    def __init__(
        self, market_data: UpstoxMarketData, minimum_relative_volume: float = 1.2
    ) -> None:
        if minimum_relative_volume <= 0:
            raise ValueError("minimum_relative_volume must be positive")
        self.market_data = market_data
        self.minimum_relative_volume = minimum_relative_volume

    def run(self, instruments: List[SurveyInstrument]) -> dict:
        keys = [item.instrument_key for item in instruments]
        quotes = self.market_data.ltp(keys)
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
                        else "HIGH" if volume_confirmed else "LOW"
                    ),
                    "volume_confirmed": volume_confirmed,
                    "suggested_entry_price": round(last_price, 2),
                "suggested_profit_target_pct": 1.0,
                "suggested_stop_loss_pct": 0.5,
                    "eligible": score > 0 and range_position < 95 and volume_confirmed,
                }

        with ThreadPoolExecutor(max_workers=min(5, len(instruments))) as executor:
            futures = {executor.submit(analyze, instrument): instrument for instrument in instruments}
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
