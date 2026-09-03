from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from threading import Condition
from time import monotonic
from typing import Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .domain import Quote


class MarketDataError(RuntimeError):
    pass


class _SlidingWindowRateLimiter:
    """Process-wide guard below Upstox's standard 50 requests/second ceiling."""

    def __init__(self, requests_per_second: int = 20) -> None:
        self.requests_per_second = requests_per_second
        self._condition = Condition()
        self._timestamps: deque[float] = deque()

    def acquire(self) -> None:
        with self._condition:
            while True:
                now = monotonic()
                while self._timestamps and now - self._timestamps[0] >= 1.0:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.requests_per_second:
                    self._timestamps.append(now)
                    return
                self._condition.wait(
                    timeout=max(0.001, 1.0 - (now - self._timestamps[0]))
                )


_STANDARD_API_LIMITER = _SlidingWindowRateLimiter()


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

    def intraday_candles(
        self, instrument_key: str, unit: str = "minutes", interval: int = 5
    ) -> List[Candle]:
        if unit not in {"minutes", "hours", "days"}:
            raise ValueError("unsupported intraday candle unit")
        path = "/v3/historical-candle/intraday/{}/{}/{}".format(
            quote(instrument_key, safe=""), unit, interval
        )
        payload = self._get(path)
        return self._candles(payload)

    def market_status(self, exchange: str = "NSE") -> dict:
        return self._get("/v2/market/status/" + quote(exchange, safe=""))["data"]

    def _candles(self, payload: dict) -> List[Candle]:
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
            _STANDARD_API_LIMITER.acquire()
            with urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise MarketDataError(f"Upstox returned HTTP {error.code}: {detail}")
        except (URLError, TimeoutError) as error:
            raise MarketDataError(f"Unable to reach Upstox: {error}")
