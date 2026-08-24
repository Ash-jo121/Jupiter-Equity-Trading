from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock, Thread
from typing import Callable, Iterable, List, Optional

from .domain import DepthLevel, Quote

STREAM_MODES = {"ltpc", "full"}


def parse_market_statuses(message: dict) -> dict[str, str]:
    """Extract exchange-segment statuses from a V3 market-info message."""

    if message.get("type") != "market_info":
        return {}
    statuses = message.get("marketInfo", {}).get("segmentStatus", {})
    if not isinstance(statuses, dict):
        return {}
    return {
        str(segment): str(status)
        for segment, status in statuses.items()
        if segment and status
    }


def parse_upstox_quotes(message: dict) -> List[Quote]:
    """Normalize V3 SDK messages from LTPC or full mode into domain quotes."""

    if message.get("type") not in {None, "live_feed", "market_info"}:
        return []
    current_timestamp = _timestamp(message.get("currentTs"))
    quotes = []
    for instrument_key, feed in message.get("feeds", {}).items():
        ltpc, market_level = _feed_parts(feed)
        if not ltpc:
            continue
        last_price = _number(ltpc.get("ltp"))
        if not last_price or last_price <= 0:
            continue
        bids, asks = _depth(market_level)
        bid = bids[0].price if bids else None
        ask = asks[0].price if asks else None
        quotes.append(
            Quote(
                instrument_key=instrument_key,
                last_price=last_price,
                bid=bid,
                ask=ask,
                bids=tuple(bids),
                asks=tuple(asks),
                timestamp=_timestamp(ltpc.get("ltt"))
                or current_timestamp
                or datetime.now(timezone.utc),
            )
        )
    return quotes


def _feed_parts(feed: dict) -> tuple:
    if feed.get("ltpc"):
        return feed["ltpc"], None
    if feed.get("firstLevelWithGreeks"):
        first_level = feed["firstLevelWithGreeks"]
        return first_level.get("ltpc"), {"bidAskQuote": [first_level.get("firstDepth", {})]}
    full_feed = feed.get("fullFeed", {})
    body = full_feed.get("marketFF") or full_feed.get("indexFF") or {}
    return body.get("ltpc"), body.get("marketLevel")


def _depth(market_level: Optional[dict]) -> tuple:
    if not market_level:
        return [], []
    levels = market_level.get("bidAskQuote") or []
    if not levels:
        return [], []
    bids = []
    asks = []
    for level in levels:
        bid = _number(level.get("bidP") or level.get("bp"))
        ask = _number(level.get("askP") or level.get("ap"))
        bid_quantity = _quantity(level.get("bidQ") or level.get("bq"))
        ask_quantity = _quantity(level.get("askQ") or level.get("aq"))
        if bid and bid > 0 and bid_quantity:
            bids.append(DepthLevel(bid, bid_quantity))
        if ask and ask > 0 and ask_quantity:
            asks.append(DepthLevel(ask, ask_quantity))
    return sorted(bids, key=lambda item: item.price, reverse=True), sorted(
        asks, key=lambda item: item.price
    )


def _quantity(value) -> Optional[int]:
    number = _number(value)
    return int(number) if number and number > 0 else None


def _number(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _timestamp(value) -> Optional[datetime]:
    if value in {None, "", 0, "0"}:
        return None
    try:
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class UpstoxMarketStream:
    """Threaded lifecycle wrapper around the official read-only V3 market streamer."""

    def __init__(
        self,
        access_token: str,
        quote_handler: Callable[[Quote], list],
        market_status_handler: Optional[Callable[[dict[str, str]], None]] = None,
        streamer_factory: Optional[Callable] = None,
    ) -> None:
        self._access_token = access_token
        self._quote_handler = quote_handler
        self._market_status_handler = market_status_handler
        self._streamer_factory = streamer_factory or _official_streamer
        self._lock = RLock()
        self._streamer = None
        self._thread: Optional[Thread] = None
        self._state = "disconnected"
        self._instruments = set()
        self._mode = "full"
        self._messages_received = 0
        self._quotes_received = 0
        self._paper_fills = 0
        self._market_statuses: dict[str, str] = {}
        self._last_message_at: Optional[datetime] = None
        self._last_error: Optional[str] = None

    def start(self, instrument_keys: Iterable[str], mode: str = "full") -> dict:
        keys = list(dict.fromkeys(key.strip() for key in instrument_keys if key.strip()))
        if not self._access_token:
            raise ValueError("UPSTOX_ACCESS_TOKEN is not configured")
        if not keys:
            raise ValueError("at least one instrument key is required")
        if mode not in STREAM_MODES:
            raise ValueError("stream mode must be 'ltpc' or 'full'")
        with self._lock:
            if self._state in {"connecting", "connected", "reconnecting"}:
                raise RuntimeError("market stream is already active")
            self._state = "connecting"
            self._instruments = set(keys)
            self._mode = mode
            self._last_error = None
            self._thread = Thread(
                target=self._run,
                args=(keys, mode),
                name="upstox-market-stream",
                daemon=True,
            )
            self._thread.start()
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            streamer = self._streamer
            thread = self._thread
            if self._state == "disconnected":
                return self.status()
            self._state = "stopping"
        if streamer:
            try:
                streamer.disconnect()
            except Exception as error:  # noqa: BLE001 - SDK/network boundary
                self._set_error(error)
        if thread and thread.is_alive():
            thread.join(timeout=5)
        with self._lock:
            self._state = "disconnected"
            self._streamer = None
            self._thread = None
        return self.status()

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "mode": self._mode,
                "instruments": sorted(self._instruments),
                "messages_received": self._messages_received,
                "quotes_received": self._quotes_received,
                "paper_fills": self._paper_fills,
                "market_statuses": dict(sorted(self._market_statuses.items())),
                "last_message_at": (
                    self._last_message_at.isoformat() if self._last_message_at else None
                ),
                "last_error": self._last_error,
            }

    def _run(self, keys: List[str], mode: str) -> None:
        try:
            streamer = self._streamer_factory(self._access_token, keys, mode)
            with self._lock:
                self._streamer = streamer
            streamer.on("open", self._on_open)
            streamer.on("message", self._on_message)
            streamer.on("error", self._on_error)
            streamer.on("close", self._on_close)
            streamer.on("reconnecting", self._on_reconnecting)
            streamer.on("autoReconnectStopped", self._on_reconnect_stopped)
            streamer.auto_reconnect(True, 10, 10)
            streamer.connect()
        except Exception as error:  # noqa: BLE001 - SDK/network boundary
            self._set_error(error)
        finally:
            with self._lock:
                if self._state not in {"error", "stopping"}:
                    self._state = "disconnected"

    def _on_open(self, *_) -> None:
        with self._lock:
            self._state = "connected"

    def _on_message(self, message: dict, *_) -> None:
        statuses = parse_market_statuses(message)
        if statuses and self._market_status_handler:
            self._market_status_handler(statuses)
        quotes = parse_upstox_quotes(message)
        fill_count = 0
        for quote in quotes:
            fills = self._quote_handler(quote)
            fill_count += len(fills or [])
        with self._lock:
            self._messages_received += 1
            self._quotes_received += len(quotes)
            self._paper_fills += fill_count
            self._market_statuses.update(statuses)
            self._last_message_at = datetime.now(timezone.utc)

    def _on_error(self, error=None, *_) -> None:
        self._set_error(error or "unknown stream error")

    def _on_close(self, *_) -> None:
        with self._lock:
            if self._state not in {"stopping", "error"}:
                self._state = "disconnected"

    def _on_reconnecting(self, *_) -> None:
        with self._lock:
            self._state = "reconnecting"

    def _on_reconnect_stopped(self, message=None, *_) -> None:
        self._set_error(message or "automatic reconnect attempts exhausted")

    def _set_error(self, error) -> None:
        with self._lock:
            self._state = "error"
            self._last_error = str(error)[:500]


def _official_streamer(access_token: str, instrument_keys: List[str], mode: str):
    import upstox_client

    configuration = upstox_client.Configuration()
    configuration.access_token = access_token
    return upstox_client.MarketDataStreamerV3(
        upstox_client.ApiClient(configuration), instrument_keys, mode
    )
