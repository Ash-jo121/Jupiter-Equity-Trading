from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List

from .market_data import UpstoxMarketData


@dataclass(frozen=True)
class SurveyInstrument:
    symbol: str
    instrument_key: str


class MarketSurvey:
    """Transparent intraday ranking used to shortlist, not predict, paper trades."""

    def __init__(self, market_data: UpstoxMarketData) -> None:
        self.market_data = market_data

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
                    "suggested_entry_price": round(last_price, 2),
                "suggested_profit_target_pct": 1.0,
                "suggested_stop_loss_pct": 0.5,
                    "eligible": score > 0 and range_position < 95,
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
            "method": "40% session momentum + 60% trailing 15-minute momentum",
            "note": "Descriptive paper-trading screen; it is not a forecast or recommendation.",
            "results": rows,
            "requested": len(instruments),
            "analyzed": len(rows),
            "failures": failures,
        }
