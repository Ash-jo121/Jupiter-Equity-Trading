from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class UniverseError(RuntimeError):
    pass


class Nifty50Universe:
    """Current NIFTY 50 constituents from NSE's official index CSV."""

    url = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"

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
                raise UniverseError(f"Unable to retrieve NIFTY 50 constituents: {error}") from error
            constituents = self.parse(content)
            if len(constituents) != 50:
                raise UniverseError(
                    f"Expected 50 NIFTY constituents but NSE returned {len(constituents)}"
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
