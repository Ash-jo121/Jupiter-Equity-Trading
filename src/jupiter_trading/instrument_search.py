from __future__ import annotations

import json
from typing import List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class InstrumentSearchError(RuntimeError):
    pass


class UpstoxInstrumentSearch:
    """Adapter for Upstox's official instrument-search endpoint."""

    endpoint = "https://api.upstox.com/v2/instruments/search"

    def __init__(self, access_token: str, timeout: float = 10.0) -> None:
        self._access_token = access_token
        self._timeout = timeout

    def search(
        self,
        query: str,
        exchange: str = "NSE",
        segment: str = "EQ",
        records: int = 20,
    ) -> List[dict]:
        if not self._access_token:
            raise InstrumentSearchError("UPSTOX_ACCESS_TOKEN is not configured")
        params = urlencode(
            {
                "query": query,
                "exchanges": exchange,
                "segments": segment,
                "page_number": 1,
                "records": records,
            }
        )
        request = Request(
            self.endpoint + "?" + params,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self._access_token,
                "User-Agent": "jupiter-equity-trading/0.2",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise InstrumentSearchError(
                f"Upstox instrument search returned HTTP {error.code}: {detail}"
            ) from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise InstrumentSearchError(f"Unable to search Upstox instruments: {error}") from error
        data = payload.get("data", [])
        if isinstance(data, dict):
            data = data.get("instruments") or data.get("results") or []
        return [self._normalize(item) for item in data]

    @staticmethod
    def _normalize(item: dict) -> dict:
        return {
            "instrument_key": item.get("instrument_key"),
            "trading_symbol": item.get("trading_symbol") or item.get("tradingsymbol"),
            "name": item.get("name") or item.get("short_name"),
            "exchange": item.get("exchange"),
            "segment": item.get("segment"),
            "isin": item.get("isin"),
            "instrument_type": item.get("instrument_type"),
            "security_type": item.get("security_type"),
            "lot_size": item.get("lot_size"),
            "tick_size": item.get("tick_size"),
        }
