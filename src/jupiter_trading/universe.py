from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class UniverseError(RuntimeError):
    pass


class NiftyUniverse:
    """Current NSE index constituents loaded from an official index CSV."""

    name = "NIFTY"
    expected_count = 0
    url = ""

    def __init__(self, cache_hours: int = 6, timeout: float = 15.0) -> None:
        self.cache_hours = cache_hours
        self.timeout = timeout
        self._cached = []
        self._cached_at = None
        self._lock = RLock()

    def constituents(self) -> list:
        with self._lock:
            now = datetime.now(timezone.utc)
            if (
                self._cached
                and self._cached_at
                and now - self._cached_at < timedelta(hours=self.cache_hours)
            ):
                return list(self._cached)
            request = Request(
                self.url,
                headers={
                    "Accept": "text/csv",
                    "User-Agent": "Mozilla/5.0 jupiter-equity-research/0.3",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    content = response.read().decode("utf-8-sig")
            except HTTPError as error:
                raise UniverseError(
                    f"NSE constituent file returned HTTP {error.code}"
                ) from error
            except (URLError, TimeoutError) as error:
                raise UniverseError(
                    f"Unable to retrieve {self.name} constituents: {error}"
                ) from error
            constituents = self.parse(content)
            if len(constituents) != self.expected_count:
                raise UniverseError(
                    f"Expected {self.expected_count} {self.name} constituents "
                    f"but NSE returned {len(constituents)}"
                )
            self._cached = constituents
            self._cached_at = now
            return list(constituents)

    @staticmethod
    def parse(content: str) -> list:
        rows = []
        for row in csv.DictReader(io.StringIO(content)):
            symbol = (row.get("Symbol") or "").strip()
            isin = (row.get("ISIN Code") or "").strip()
            if not symbol or not isin:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "instrument_key": "NSE_EQ|" + isin,
                    "name": (row.get("Company Name") or symbol).strip(),
                    "industry": (row.get("Industry") or "").strip(),
                    "series": (row.get("Series") or "EQ").strip(),
                    "isin": isin,
                }
            )
        return rows


class Nifty50Universe(NiftyUniverse):
    """Current NIFTY 50 constituents."""

    name = "NIFTY 50"
    expected_count = 50
    url = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"


class Nifty100Universe(NiftyUniverse):
    """Current NIFTY 100 constituents used by the momentum stock pool."""

    name = "NIFTY 100"
    expected_count = 100
    url = "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv"


class Nasdaq100Universe:
    """Current NASDAQ-100 members from Nasdaq's public index endpoint.

    The index can contain 101 securities because one company may have two share
    classes. We therefore validate the documented company-sized range instead
    of assuming the response must always contain exactly 100 ticker symbols.
    """

    name = "NASDAQ 100"
    expected_count = 100
    url = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"

    def __init__(self, cache_hours: int = 6, timeout: float = 15.0) -> None:
        self.cache_hours = cache_hours
        self.timeout = timeout
        self._cached: list[dict] = []
        self._cached_at: datetime | None = None
        self._lock = RLock()

    def constituents(self) -> list[dict]:
        with self._lock:
            now = datetime.now(timezone.utc)
            if (
                self._cached
                and self._cached_at
                and now - self._cached_at < timedelta(hours=self.cache_hours)
            ):
                return list(self._cached)
            request = Request(
                self.url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 jupiter-equity-research/0.4",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    content = response.read().decode("utf-8")
            except HTTPError as error:
                raise UniverseError(
                    f"Nasdaq constituent endpoint returned HTTP {error.code}"
                ) from error
            except (URLError, TimeoutError) as error:
                raise UniverseError(
                    f"Unable to retrieve {self.name} constituents: {error}"
                ) from error
            constituents = self.parse(content)
            if not 100 <= len(constituents) <= 101:
                raise UniverseError(
                    f"Expected 100-101 {self.name} securities but Nasdaq returned "
                    f"{len(constituents)}"
                )
            self._cached = constituents
            self._cached_at = now
            return list(constituents)

    @staticmethod
    def parse(content: str) -> list[dict]:
        try:
            payload = json.loads(content)
            rows = payload["data"]["data"]["rows"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise UniverseError("Nasdaq returned an invalid constituent response") from error
        result = []
        seen = set()
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            result.append(
                {
                    "symbol": symbol,
                    "instrument_key": f"US_EQ|{symbol}",
                    "name": str(row.get("companyName") or symbol).strip(),
                    "industry": str(row.get("sector") or "").strip(),
                }
            )
        return result
