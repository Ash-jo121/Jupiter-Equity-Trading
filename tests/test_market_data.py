from datetime import date

from jupiter_trading.market_data import UpstoxMarketData


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

