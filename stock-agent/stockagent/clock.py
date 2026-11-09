"""US equity market calendar, session times and T+1 settlement.

Holidays are computed from the NYSE rules rather than hard-coded per year, so
the calendar does not silently go wrong in a year nobody typed in.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN_TIME = time(9, 30)
CLOSE_TIME = time(16, 0)
EARLY_CLOSE_TIME = time(13, 0)
PREMARKET_START = time(4, 0)
AFTERHOURS_END = time(20, 0)

# One-off closures that no rule predicts.
SPECIAL_CLOSURES = {
    date(2025, 1, 9),  # National Day of Mourning, President Carter
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def nyse_holidays(year: int) -> frozenset[date]:
    days = set()
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        days.add(new_year + timedelta(days=1))
    elif new_year.weekday() != 5:  # a Saturday New Year is not observed on Dec 31
        days.add(new_year)
    days.add(_nth_weekday(year, 1, 0, 3))  # Martin Luther King Jr. Day
    days.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday
    days.add(_easter(year) - timedelta(days=2))  # Good Friday
    days.add(_last_weekday(year, 5, 0))  # Memorial Day
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    days.add(_observed(date(year, 7, 4)))
    days.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    days.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    days.add(_observed(date(year, 12, 25)))
    days.update(d for d in SPECIAL_CLOSURES if d.year == year)
    return frozenset(days)


@lru_cache(maxsize=None)
def early_closes(year: int) -> frozenset[date]:
    days = set()
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 4 and date(year, 7, 4).weekday() < 5:
        days.add(jul3)
    days.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))  # day after Thanksgiving
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and dec24 not in nyse_holidays(year):
        days.add(dec24)
    return frozenset(d for d in days if is_trading_day(d))


@lru_cache(maxsize=None)
def settlement_holidays(year: int) -> frozenset[date]:
    """Days the market trades but nothing settles (bank holidays)."""
    days = {_nth_weekday(year, 10, 0, 2)}  # Columbus Day
    vets = date(year, 11, 11)
    if vets.weekday() == 6:
        days.add(vets + timedelta(days=1))
    elif vets.weekday() < 5:
        days.add(vets)
    return frozenset(days)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def is_settlement_day(d: date) -> bool:
    return is_trading_day(d) and d not in settlement_holidays(d.year)


def next_trading_day(d: date, n: int = 1) -> date:
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    cur = d
    while remaining:
        cur += timedelta(days=step)
        if is_trading_day(cur):
            remaining -= 1
    return cur


def prev_trading_day(d: date) -> date:
    return next_trading_day(d, -1)


def last_trading_day_on_or_before(d: date) -> date:
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_between(start: date, end: date) -> int:
    """Number of trading days in (start, end]. Negative if end < start."""
    if end == start:
        return 0
    if end < start:
        return -trading_days_between(end, start)
    count = 0
    cur = start
    while cur < end:
        cur += timedelta(days=1)
        if is_trading_day(cur):
            count += 1
    return count


def trading_days_in_range(start: date, end: date) -> list[date]:
    out = []
    cur = start
    while cur <= end:
        if is_trading_day(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def settlement_date(trade_date: date, days: int = 1) -> date:
    """T+1 (US equities since 2024-05-28): the next day that is both a trading
    day and a bank business day."""
    cur = trade_date
    remaining = days
    while remaining:
        cur += timedelta(days=1)
        if is_settlement_day(cur):
            remaining -= 1
    return cur


def session_times(d: date) -> tuple[datetime, datetime]:
    close = EARLY_CLOSE_TIME if d in early_closes(d.year) else CLOSE_TIME
    return (datetime.combine(d, OPEN_TIME, ET), datetime.combine(d, close, ET))


def market_phase(now: datetime) -> str:
    """One of: closed, premarket, open, afterhours."""
    now = now.astimezone(ET)
    d = now.date()
    if not is_trading_day(d):
        return "closed"
    open_dt, close_dt = session_times(d)
    if now < datetime.combine(d, PREMARKET_START, ET):
        return "closed"
    if now < open_dt:
        return "premarket"
    if now < close_dt:
        return "open"
    if now < datetime.combine(d, AFTERHOURS_END, ET):
        return "afterhours"
    return "closed"


def minutes_since_open(now: datetime) -> float:
    open_dt, _ = session_times(now.astimezone(ET).date())
    return (now - open_dt).total_seconds() / 60.0


def minutes_to_close(now: datetime) -> float:
    _, close_dt = session_times(now.astimezone(ET).date())
    return (close_dt - now).total_seconds() / 60.0


def session_fraction_elapsed(now: datetime) -> float:
    open_dt, close_dt = session_times(now.astimezone(ET).date())
    total = (close_dt - open_dt).total_seconds()
    return min(1.0, max(0.0, (now - open_dt).total_seconds() / total))


class Clock:
    """Real wall clock in US/Eastern."""

    def now(self) -> datetime:
        return datetime.now(ET)

    def today(self) -> date:
        return self.now().date()


class SimClock(Clock):
    """A clock the backtester and tests move by hand."""

    def __init__(self, start: datetime):
        self._now = start.astimezone(ET) if start.tzinfo else start.replace(tzinfo=ET)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when.astimezone(ET) if when.tzinfo else when.replace(tzinfo=ET)

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)
