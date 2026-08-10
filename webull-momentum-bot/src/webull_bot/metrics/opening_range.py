"""
Opening range detection: the high of the first N minutes of the regular
trading session (9:30am US/Eastern), a classic, independent breakout
reference distinct from the volume-profile resistance levels in
volume_profile.py -- see strategy/opening_range_breakout.py.

Deliberately built on the SAME raw bars BroadScanner already fetches for
volume-profile analysis (WebullBrokerClient.get_raw_bars), not a second
network call -- see BroadScanner._compute_opening_range_high, which passes
the already-fetched bars in here rather than calling get_raw_bars again.

Uses `zoneinfo` (stdlib, no extra dependency) to resolve 9:30am Eastern to
UTC correctly across the EST/EDT boundary, rather than hardcoding a fixed
UTC offset that would silently drift wrong for half the year.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")
_MARKET_OPEN_TIME = time(9, 30)


def _parse_bar_time(raw_time) -> datetime:
    if isinstance(raw_time, str):
        return datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S.%f%z").replace(tzinfo=None)
    return raw_time


def compute_opening_range_high(
    bars: Sequence[dict], *, opening_range_minutes: int = 5, now: Optional[datetime] = None
) -> Optional[float]:
    """Returns the highest `high` among bars falling within the first
    `opening_range_minutes` minutes of "today"'s (per `now`, naive UTC)
    regular session, or None if no bars fall in that window -- e.g. the
    symbol was only discovered well after the open and the fetched bars
    don't reach back that far, or the market hasn't opened yet today.
    Bars are Webull's raw dict shape (string-encoded "time"/"high"/"volume"),
    same as volume_profile.py's compute_volume_profile expects -- see that
    module's docstring for why get_raw_bars returns them unprocessed."""
    now = now or datetime.utcnow()
    session_date_eastern = now.replace(tzinfo=timezone.utc).astimezone(_EASTERN).date()
    market_open_eastern = datetime.combine(session_date_eastern, _MARKET_OPEN_TIME, tzinfo=_EASTERN)
    window_end_eastern = market_open_eastern + timedelta(minutes=opening_range_minutes)
    market_open_utc = market_open_eastern.astimezone(timezone.utc).replace(tzinfo=None)
    window_end_utc = window_end_eastern.astimezone(timezone.utc).replace(tzinfo=None)

    highs = [
        float(bar["high"])
        for bar in bars
        if market_open_utc <= _parse_bar_time(bar["time"]) < window_end_utc
    ]
    return max(highs) if highs else None
