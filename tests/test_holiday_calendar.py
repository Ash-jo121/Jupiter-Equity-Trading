import io
import json
from urllib.error import URLError

from jupiter_trading.holiday_calendar import NseHolidayCalendar


class Response:
    def __init__(self, payload: dict) -> None:
        self.body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self.body.read()


def test_cash_market_holidays_are_loaded_from_the_official_segment() -> None:
    requested = []

    def open_request(request, timeout):
        requested.append((request.full_url, timeout))
        return Response(
            {
                "CM": [
                    {
                        "tradingDate": "14-Sep-2026",
                        "description": "Ganesh Chaturthi",
                    },
                    {"tradingDate": "02-Oct-2026", "description": "Gandhi Jayanti"},
                ],
                "FO": [
                    {"tradingDate": "01-Jan-2026", "description": "F&O only"}
                ],
            }
        )

    calendar = NseHolidayCalendar(opener=open_request, fallback={})

    assert calendar.is_trading_day("2026-09-14") is False
    assert calendar.is_trading_day("2026-09-15") is True
    assert calendar.holiday_name("2026-09-14") == "Ganesh Chaturthi"
    assert len(requested) == 1  # subsequent checks use the per-year cache
    assert "type=trading" in requested[0][0]
    assert "year=2026" in requested[0][0]


def test_weekends_do_not_need_a_network_lookup() -> None:
    def unexpected_request(*args, **kwargs):
        raise AssertionError("weekends should be rejected before loading the calendar")

    calendar = NseHolidayCalendar(opener=unexpected_request, fallback={})
    assert calendar.is_trading_day("2026-09-12") is False


def test_bundled_calendar_is_used_during_an_nse_outage() -> None:
    def unavailable(*args, **kwargs):
        raise URLError("offline")

    calendar = NseHolidayCalendar(opener=unavailable)

    assert calendar.is_trading_day("2026-12-25") is False
    assert calendar.is_trading_day("2026-12-24") is True
    assert calendar.last_error is not None
    assert "bundled 2026 calendar" in calendar.last_error


def test_unknown_year_fails_closed_when_nse_is_unavailable() -> None:
    def unavailable(*args, **kwargs):
        raise URLError("offline")

    calendar = NseHolidayCalendar(opener=unavailable, fallback={})
    assert calendar.is_trading_day("2027-01-04") is False
