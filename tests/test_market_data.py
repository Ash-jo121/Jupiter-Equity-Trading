from datetime import date, datetime, timedelta, timezone

from jupiter_trading.domain import Quote
from jupiter_trading.market_data import (
    Candle,
    SharedCandleCache,
    SharedQuoteCache,
    UpstoxMarketData,
)


class FakeUpstox(UpstoxMarketData):
    def __init__(self) -> None:
        super().__init__("test-token")
        self.path = None

    def _get(self, path: str) -> dict:
        self.path = path
        return {
            "data": {
                "candles": [
                    ["2026-08-21T09:16:00+05:30", 101, 103, 100, 102, 1200, 0],
                    ["2026-08-21T09:15:00+05:30", 100, 102, 99, 101, 1000, 0],
                ]
            }
        }


def test_historical_candles_encode_instrument_and_sort_ascending() -> None:
    client = FakeUpstox()

    candles = client.historical_candles(
        "NSE_EQ|INE123", "minutes", 1, date(2026, 8, 21), date(2026, 8, 20)
    )

    assert client.path == (
        "/v3/historical-candle/NSE_EQ%7CINE123/minutes/1/2026-08-21/2026-08-20"
    )
    assert candles[0].close == 101
    assert candles[1].close == 102


def test_shared_quote_cache_reuses_quotes_and_only_fetches_missing_keys() -> None:
    class CountingMarket:
        def __init__(self) -> None:
            self.calls = []

        def ltp(self, keys):
            requested = list(keys)
            self.calls.append(requested)
            return {
                key: Quote(key, 100 + len(self.calls), timestamp=datetime.now(timezone.utc))
                for key in requested
            }

    market = CountingMarket()
    cache = SharedQuoteCache(ttl_seconds=5)

    first = cache.get(market, ["A", "B"])
    second = cache.get(market, ["A", "B"])
    extended = cache.get(market, ["A", "B", "C"])

    assert market.calls == [["A", "B"], ["C"]]
    assert second == first
    assert extended["A"] is first["A"]
    assert cache.stats() == {"requests": 2, "cache_hits": 1, "instruments": 3}


def test_shared_candle_cache_fetches_once_for_all_arms_in_a_logical_minute() -> None:
    class CountingMarket:
        def __init__(self) -> None:
            self.calls = 0

        def intraday_candles(self, instrument_key, unit, interval):
            self.calls += 1
            return [Candle(datetime.now(timezone.utc), 99, 101, 98, 100, 10, 0)]

    market = CountingMarket()
    cache = SharedCandleCache()
    clock = datetime(2026, 9, 10, 5, 0, 5, tzinfo=timezone.utc)

    first = cache.get(market, "NSE_EQ|TEST", clock)
    second = cache.get(market, "NSE_EQ|TEST", clock + timedelta(seconds=20))

    assert market.calls == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert cache.stats() == {"hits": 1, "misses": 1, "entries": 1}
