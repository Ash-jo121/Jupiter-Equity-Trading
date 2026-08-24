from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .domain import Quote


class MarketDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    open_interest: int

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "open_interest": self.open_interest,
        }


class UpstoxMarketData:
    """Read-only Upstox V3 REST adapter suitable for an Analytics Token."""

    base_url = "https://api.upstox.com"

    def __init__(self, access_token: str, timeout: float = 10.0) -> None:
        if not access_token:
            raise ValueError("UPSTOX_ACCESS_TOKEN is not configured")
        self._access_token = access_token
        self._timeout = timeout

    def ltp(self, instrument_keys: Iterable[str]) -> Dict[str, Quote]:
        keys = list(dict.fromkeys(instrument_keys))
        if not keys:
            return {}
        payload = self._get(
            "/v3/market-quote/ltp?" + urlencode({"instrument_key": ",".join(keys)})
        )
        result: Dict[str, Quote] = {}
        for item in payload.get("data", {}).values():
            instrument_key = item["instrument_token"]
            result[instrument_key] = Quote(
                instrument_key=instrument_key,
                last_price=float(item["last_price"]),
                timestamp=datetime.now(timezone.utc),
            )
        return result

    def historical_candles(
        self,
        instrument_key: str,
        unit: str,
        interval: int,
        to_date: date,
        from_date: Optional[date] = None,
    ) -> List[Candle]:
        if unit not in {"minutes", "hours", "days", "weeks", "months"}:
            raise ValueError("unsupported candle unit")
        path = "/v3/historical-candle/{}/{}/{}/{}".format(
            quote(instrument_key, safe=""), unit, interval, to_date.isoformat()
        )
        if from_date:
            path += "/" + from_date.isoformat()
        payload = self._get(path)
        candles = []
        for row in payload.get("data", {}).get("candles", []):
            candles.append(
                Candle(
                    timestamp=datetime.fromisoformat(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=int(row[5]),
                    open_interest=int(row[6]),
                )
            )
        return sorted(candles, key=lambda candle: candle.timestamp)

    def _get(self, path: str) -> dict:
        request = Request(
            self.base_url + path,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self._access_token,
                "User-Agent": "jupiter-equity-trading/0.1",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise MarketDataError(f"Upstox returned HTTP {error.code}: {detail}")
        except (URLError, TimeoutError) as error:
            raise MarketDataError(f"Unable to reach Upstox: {error}")
