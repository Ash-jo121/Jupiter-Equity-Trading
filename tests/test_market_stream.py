from threading import Event

import pytest

from jupiter_trading.market_stream import (
    UpstoxMarketStream,
    parse_market_statuses,
    parse_upstox_quotes,
)


def test_parse_ltpc_quote() -> None:
    quotes = parse_upstox_quotes(
        {
            "type": "live_feed",
            "feeds": {
                "NSE_EQ|TEST": {"ltpc": {"ltp": 101.5, "ltt": "1787541300000"}}
            },
            "currentTs": "1787541301000",
        }
    )

    assert len(quotes) == 1
    assert quotes[0].instrument_key == "NSE_EQ|TEST"
    assert quotes[0].last_price == 101.5
    assert quotes[0].bid is None


def test_parse_full_quote_with_best_bid_and_ask() -> None:
    quotes = parse_upstox_quotes(
        {
            "type": "live_feed",
            "feeds": {
                "NSE_EQ|TEST": {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 101.5, "ltt": "1787541300000"},
                            "marketLevel": {
                                "bidAskQuote": [
                                    {"bidP": 101.45, "askP": 101.55, "bidQ": "5", "askQ": "8"}
                                ]
                            },
                        }
                    }
                }
            },
        }
    )

    assert quotes[0].bid == 101.45
    assert quotes[0].ask == 101.55
    assert quotes[0].bids[0].quantity == 5
    assert quotes[0].asks[0].quantity == 8


def test_ignore_market_status_messages_without_prices() -> None:
    assert parse_upstox_quotes({"type": "market_info", "marketInfo": {}}) == []


def test_parse_market_statuses() -> None:
    statuses = parse_market_statuses(
        {
            "type": "market_info",
            "marketInfo": {
                "segmentStatus": {
                    "NSE_EQ": "NORMAL_CLOSE",
                    "NSE_FO": "NORMAL_OPEN",
                }
            },
        }
    )

    assert statuses == {"NSE_EQ": "NORMAL_CLOSE", "NSE_FO": "NORMAL_OPEN"}


class FakeStreamer:
    def __init__(self) -> None:
        self.callbacks = {}
        self.connected = Event()
        self.stopped = Event()
        self.reconnect_arguments = None

    def on(self, event, callback) -> None:
        self.callbacks[event] = callback

    def auto_reconnect(self, *arguments) -> None:
        self.reconnect_arguments = arguments

    def connect(self) -> None:
        self.callbacks["open"]()
        self.connected.set()
        self.stopped.wait(timeout=2)

    def disconnect(self) -> None:
        self.stopped.set()

    def emit_quote(self) -> None:
        self.callbacks["message"](
            {
                "type": "live_feed",
                "feeds": {"NSE_EQ|TEST": {"ltpc": {"ltp": 100}}},
            }
        )

    def emit_status(self, status: str) -> None:
        self.callbacks["message"](
            {
                "type": "market_info",
                "marketInfo": {"segmentStatus": {"NSE_EQ": status}},
            }
        )


def test_stream_lifecycle_and_quote_delivery() -> None:
    fake = FakeStreamer()
    received = []
    stream = UpstoxMarketStream(
        "token",
        lambda quote: received.append(quote) or [],
        streamer_factory=lambda *_: fake,
    )

    stream.start(["NSE_EQ|TEST"], "ltpc")
    assert fake.connected.wait(timeout=1)
    fake.emit_quote()

    assert stream.status()["state"] == "connected"
    assert stream.status()["quotes_received"] == 1
    assert received[0].last_price == 100
    assert fake.reconnect_arguments == (True, 10, 10)

    stream.stop()
    assert stream.status()["state"] == "disconnected"


def test_stream_delivers_market_status() -> None:
    fake = FakeStreamer()
    received_statuses = []
    stream = UpstoxMarketStream(
        "token",
        lambda _: [],
        received_statuses.append,
        streamer_factory=lambda *_: fake,
    )

    stream.start(["NSE_EQ|TEST"], "ltpc")
    assert fake.connected.wait(timeout=1)
    fake.emit_status("NORMAL_CLOSE")

    assert received_statuses == [{"NSE_EQ": "NORMAL_CLOSE"}]
    assert stream.status()["market_statuses"] == {"NSE_EQ": "NORMAL_CLOSE"}

    stream.stop()


def test_stream_rejects_missing_configuration() -> None:
    stream = UpstoxMarketStream("", lambda _: [])

    with pytest.raises(ValueError, match="UPSTOX_ACCESS_TOKEN"):
        stream.start(["NSE_EQ|TEST"])
