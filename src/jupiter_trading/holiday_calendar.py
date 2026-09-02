from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from threading import RLock
from typing import Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class HolidayCalendarError(RuntimeError):
    pass


# Official NSE CM calendar as published for 2026. The live holiday-master API is
# authoritative; this copy keeps the scheduler safe if NSE is temporarily down.
FALLBACK_CM_HOLIDAYS = {
    2026: {
        "2026-01-15": "Municipal Corporation Election - Maharashtra",
        "2026-01-26": "Republic Day",
        "2026-02-15": "Mahashivratri",
        "2026-03-03": "Holi",
        "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
        "2026-03-26": "Shri Ram Navami",
        "2026-03-31": "Shri Mahavir Jayanti",
        "2026-04-03": "Good Friday",
        "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
        "2026-05-01": "Maharashtra Day",
        "2026-05-28": "Bakri Id",
        "2026-06-26": "Muharram",
        "2026-08-15": "Independence Day",
        "2026-09-14": "Ganesh Chaturthi",
        "2026-10-02": "Mahatma Gandhi Jayanti",
        "2026-10-20": "Dussehra",
        "2026-11-08": "Diwali Laxmi Pujan*",
        "2026-11-10": "Diwali-Balipratipada",
        "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
        "2026-12-25": "Christmas",
    }
}


class NseHolidayCalendar:
    """Cached NSE cash-market (CM) trading holidays, fetched by calendar year."""

    url = "https://www.nseindia.com/api/holiday-master"
    segment = "CM"

    def __init__(
        self,
        cache_hours: int = 12,
        timeout: float = 10.0,
        opener: Callable = urlopen,
        fallback: Optional[Dict[int, Dict[str, str]]] = None,
    ) -> None:
        self.cache_hours = cache_hours
        self.timeout = timeout
        self._opener = opener
        self._fallback = FALLBACK_CM_HOLIDAYS if fallback is None else fallback
        self._cache: Dict[int, tuple] = {}
        self._lock = RLock()
        self.last_error: Optional[str] = None

    def holidays(self, year: int) -> List[dict]:
        with self._lock:
            now = datetime.now(timezone.utc)
            cached = self._cache.get(year)
            if cached and now - cached[0] < timedelta(hours=self.cache_hours):
                return [dict(item) for item in cached[1]]

            try:
                holidays = self._fetch(year)
                self.last_error = None
            except HolidayCalendarError as error:
                fallback = self._fallback.get(year)
                if fallback is None:
                    self.last_error = str(error)
                    raise
                self.last_error = f"{error}; using bundled {year} calendar"
                holidays = [
                    {"date": holiday_date, "description": description}
                    for holiday_date, description in sorted(fallback.items())
                ]

            self._cache[year] = (now, holidays)
            return [dict(item) for item in holidays]

    def _fetch(self, year: int) -> List[dict]:
        endpoint = self.url + "?" + urlencode({"type": "trading", "year": year})
        request = Request(
            endpoint,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays",
                "User-Agent": "Mozilla/5.0 jupiter-equity-research/0.3",
            },
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise HolidayCalendarError(
                f"NSE holiday API returned HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise HolidayCalendarError(f"Unable to retrieve NSE holidays: {error}") from error

        if not isinstance(payload, dict):
            raise HolidayCalendarError("NSE holiday API returned an invalid response")
        rows = payload.get(self.segment)
        if not isinstance(rows, list) or not rows:
            raise HolidayCalendarError(f"NSE returned no {self.segment} holidays for {year}")

        holidays = []
        for row in rows:
            raw_date = str(row.get("tradingDate") or "").strip()
            try:
                # The provider supplies a calendar date with no time or zone.
                holiday_date = datetime.strptime(raw_date, "%d-%b-%Y").date()  # noqa: DTZ007
            except ValueError as error:
                raise HolidayCalendarError(
                    f"NSE returned an invalid holiday date: {raw_date!r}"
                ) from error
            if holiday_date.year != year:
                continue
            holidays.append(
                {
                    "date": holiday_date.isoformat(),
                    "description": str(row.get("description") or "NSE holiday").strip(),
                }
            )
        if not holidays:
            raise HolidayCalendarError(f"NSE returned no {self.segment} holidays for {year}")
        holidays.sort(key=lambda item: item["date"])
        return holidays

    def is_trading_day(self, session_date: str) -> bool:
        target = date.fromisoformat(session_date)
        if target.weekday() >= 5:
            return False
        try:
            holidays = self.holidays(target.year)
        except HolidayCalendarError:
            # Fail closed if neither the live API nor a bundled calendar is
            # available. The next scheduler tick retries within the grace window.
            return False
        return session_date not in {item["date"] for item in holidays}

    def holiday_name(self, session_date: str) -> Optional[str]:
        target = date.fromisoformat(session_date)
        try:
            holidays = self.holidays(target.year)
        except HolidayCalendarError:
            return None
        return next(
            (item["description"] for item in holidays if item["date"] == session_date),
            None,
        )
