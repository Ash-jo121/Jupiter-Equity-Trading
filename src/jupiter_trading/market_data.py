from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from threading import Condition, RLock
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


class SharedQuoteCache:
    """Coalesce near-simultaneous LTP reads from concurrent strategy runners.

    Each instrument is timestamped independently. A second runner reuses fresh
    quotes and requests only keys that were absent from the first runner's
    batch, so differently configured runs still see all of their own symbols.
    """

    def __init__(self, ttl_seconds: float = 4.5) -> None:
        if ttl_seconds <= 0:
            raise ValueError("quote cache TTL must be positive")
        self.ttl_seconds = ttl_seconds
        self._condition = Condition(RLock())
        self._quotes: dict[str, tuple[float, Quote]] = {}
        self._refreshing = False
        self._requests = 0
        self._cache_hits = 0

    def get(
        self,
        market_data: UpstoxMarketData,
        instrument_keys: Iterable[str],
        max_age_seconds: Optional[float] = None,
    ) -> Dict[str, Quote]:
        keys = list(dict.fromkeys(instrument_keys))
        if not keys:
            return {}
        max_age = min(
            self.ttl_seconds,
            max_age_seconds if max_age_seconds is not None else self.ttl_seconds,
        )
        if max_age <= 0:
            raise ValueError("quote maximum age must be positive")

        with self._condition:
            while True:
                now = monotonic()
                missing = [
                    key
                    for key in keys
                    if key not in self._quotes
                    or now - self._quotes[key][0] >= max_age
                ]
                if not missing:
                    self._cache_hits += 1
                    return {key: self._quotes[key][1] for key in keys}
                if not self._refreshing:
                    self._refreshing = True
                    break
                self._condition.wait()

        try:
            fresh = market_data.ltp(missing)
            fetched_at = monotonic()
            with self._condition:
                self._requests += 1
                for key, quote_value in fresh.items():
                    self._quotes[key] = (fetched_at, quote_value)
        finally:
            with self._condition:
                self._refreshing = False
                self._condition.notify_all()

        with self._condition:
            return {
                key: self._quotes[key][1]
                for key in keys
                if key in self._quotes
                and monotonic() - self._quotes[key][0] < max_age
            }

    def stats(self) -> dict:
        with self._condition:
            return {
                "requests": self._requests,
                "cache_hits": self._cache_hits,
                "instruments": len(self._quotes),
            }


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


@dataclass
class _CandleCacheEntry:
    generation: int
    received_at: datetime
    candles: List[Candle]
    retry_after: datetime


class SharedCandleCache:
    """Share one-minute history while allowing a late provider bar to replace it.

    A request made just after the minute boundary can legitimately return the
    preceding bar.  Such a response is useful briefly, but must not be frozen
    for the whole minute: once the new bar is expected, the next runner refreshes
    it and the other experiment arms reuse that result.
    """

    def __init__(self) -> None:
        self._condition = Condition(RLock())
        self._entries: dict[tuple, _CandleCacheEntry] = {}
        self._warmups: dict[tuple, tuple[datetime, List[Candle]]] = {}
        self._refreshing: set[tuple] = set()
        self._hits = 0
        self._misses = 0
        self._late_refreshes = 0
        self._warmup_hits = 0
        self._warmup_misses = 0

    def get(
        self,
        market_data: UpstoxMarketData,
        instrument_key: str,
        clock: Optional[datetime] = None,
        finalization_grace_seconds: float = 2.0,
        retry_after_seconds: float = 2.0,
    ) -> dict:
        now = clock or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        generation = int(now.timestamp() // 60)
        key = (instrument_key, "minutes", 1)
        expected_start = self._expected_latest_start(now, finalization_grace_seconds)
        with self._condition:
            while True:
                cached = self._entries.get(key)
                cached_latest = self._latest_start(cached.candles) if cached else None
                cached_complete = bool(
                    cached_latest is not None and cached_latest >= expected_start
                )
                if cached and cached.generation == generation and (
                    cached_complete or now < cached.retry_after
                ):
                    self._hits += 1
                    return {
                        "candles": list(cached.candles),
                        "received_at": cached.received_at,
                        "cache_hit": True,
                        "generation": generation,
                        "complete": cached_complete,
                        "expected_bar_start": expected_start.isoformat(),
                        "latest_bar_start": (
                            cached_latest.isoformat() if cached_latest else None
                        ),
                    }
                if key not in self._refreshing:
                    if cached and cached.generation == generation:
                        self._late_refreshes += 1
                    self._refreshing.add(key)
                    self._misses += 1
                    break
                self._condition.wait()
        try:
            candles = market_data.intraday_candles(instrument_key, "minutes", 1)
            received_at = datetime.now(timezone.utc)
            latest = self._latest_start(candles)
            complete = bool(latest is not None and latest >= expected_start)
            entry = _CandleCacheEntry(
                generation=generation,
                received_at=received_at,
                candles=list(candles),
                retry_after=now + timedelta(seconds=max(0.25, retry_after_seconds)),
            )
            with self._condition:
                self._entries[key] = entry
            return {
                "candles": list(candles),
                "received_at": received_at,
                "cache_hit": False,
                "generation": generation,
                "complete": complete,
                "expected_bar_start": expected_start.isoformat(),
                "latest_bar_start": latest.isoformat() if latest else None,
            }
        finally:
            with self._condition:
                self._refreshing.discard(key)
                self._condition.notify_all()

    def warmup(
        self,
        market_data: UpstoxMarketData,
        instrument_key: str,
        session_date: date,
        bars: int,
        lookback_days: int = 10,
    ) -> dict:
        """Load prior-session bars once and share them across experiment arms."""

        if bars <= 0:
            return {"candles": [], "received_at": datetime.now(timezone.utc), "cache_hit": True}
        key = ("warmup", instrument_key, session_date.isoformat(), bars)
        with self._condition:
            while True:
                cached = self._warmups.get(key)
                if cached:
                    self._warmup_hits += 1
                    return {
                        "candles": list(cached[1]),
                        "received_at": cached[0],
                        "cache_hit": True,
                    }
                if key not in self._refreshing:
                    self._refreshing.add(key)
                    self._warmup_misses += 1
                    break
                self._condition.wait()
        try:
            previous_day = session_date - timedelta(days=1)
            candles = market_data.historical_candles(
                instrument_key,
                "minutes",
                1,
                previous_day,
                session_date - timedelta(days=max(2, lookback_days)),
            )
            selected = sorted(candles, key=lambda item: item.timestamp)[-bars:]
            received_at = datetime.now(timezone.utc)
            with self._condition:
                self._warmups[key] = (received_at, list(selected))
            return {
                "candles": list(selected),
                "received_at": received_at,
                "cache_hit": False,
            }
        finally:
            with self._condition:
                self._refreshing.discard(key)
                self._condition.notify_all()

    def stats(self) -> dict:
        with self._condition:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "entries": len(self._entries),
                "late_refreshes": self._late_refreshes,
                "warmup_hits": self._warmup_hits,
                "warmup_misses": self._warmup_misses,
                "warmup_entries": len(self._warmups),
            }

    @staticmethod
    def _expected_latest_start(clock: datetime, grace_seconds: float) -> datetime:
        minute = clock.replace(second=0, microsecond=0)
        completed = minute - timedelta(minutes=1)
        if clock < minute + timedelta(seconds=max(0.0, grace_seconds)):
            completed -= timedelta(minutes=1)
        return completed

    @staticmethod
    def _latest_start(candles: Iterable[Candle]) -> Optional[datetime]:
        timestamps = []
        for candle in candles:
            value = candle.timestamp
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            timestamps.append(value.astimezone(timezone.utc))
        return max(timestamps, default=None)


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
