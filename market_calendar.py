from __future__ import annotations

import logging
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

logger = logging.getLogger("schwab_bot.calendar")
EASTERN = ZoneInfo("America/New_York")

try:
    import pandas as pd
    import pandas_market_calendars as mcal
    _HAVE_MCAL = True
except ImportError:  # pragma: no cover
    _HAVE_MCAL = False


class MarketCalendar:

    def __init__(self, calendar_name: str = "NYSE"):
        self._naive_warned = False
        self._cache_date = None
        self._today_schedule = None  # cached pandas schedule for the current day
        if _HAVE_MCAL:
            self._cal = mcal.get_calendar(calendar_name)
        else:
            self._cal = None
            logger.warning(
                "pandas_market_calendars not installed — falling back to a naive "
                "weekday + 09:30-16:00 ET check. Holidays and early closes will "
                "NOT be respected. Install it: pip install pandas_market_calendars"
            )

    # ---- public API ----

    def is_open(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(EASTERN)
        if now.tzinfo is None:
            now = now.replace(tzinfo=EASTERN)
        if self._cal is None:
            return self._naive_is_open(now)
        return self._mcal_is_open(now)

    def describe(self, now: datetime | None = None) -> str:
        now = now or datetime.now(EASTERN)
        if self.is_open(now):
            close = self._close_time_today(now)
            if close is not None:
                # market_close from pandas_market_calendars is UTC tz-aware;
                # compare in Eastern so the 16:00 regular-close threshold is
                # meaningful (otherwise close.time() is the UTC hour and the
                # early-close tag never fires).
                early = close.astimezone(EASTERN).time() < dt_time(16, 0)
                tag = " (EARLY CLOSE)" if early else ""
                return f"OPEN until {close.astimezone(EASTERN):%H:%M ET}{tag}"
            return "OPEN"
        return "CLOSED (weekend, holiday, or outside regular hours)"

    # ---- pandas_market_calendars path ----

    def _ensure_schedule(self, now: datetime):
        day = now.date()
        if self._cache_date == day:
            return
        # Build a single-day schedule. Empty frame => holiday/weekend.
        self._today_schedule = self._cal.schedule(start_date=day, end_date=day)
        self._cache_date = day

    def _mcal_is_open(self, now: datetime) -> bool:
        self._ensure_schedule(now)
        if self._today_schedule is None or self._today_schedule.empty:
            return False
        ts = pd.Timestamp(now)
        try:
            return bool(self._cal.open_at_time(self._today_schedule, ts))
        except Exception:
            # open_at_time can raise if ts is outside the schedule's span.
            return False

    def _close_time_today(self, now: datetime):
        if self._cal is None:
            open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
            return now.replace(hour=16, minute=0, second=0, microsecond=0) \
                if now >= open_t else None
        self._ensure_schedule(now)
        if self._today_schedule is None or self._today_schedule.empty:
            return None
        return self._today_schedule.iloc[0]["market_close"].to_pydatetime()

    # ---- naive fallback ----

    def _naive_is_open(self, now: datetime) -> bool:
        if not self._naive_warned:
            logger.warning("Using naive market-hours check (no holiday awareness).")
            self._naive_warned = True
        if now.weekday() >= 5:
            return False
        open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return open_t <= now <= close_t
