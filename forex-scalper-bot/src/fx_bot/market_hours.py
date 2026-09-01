"""
Forex session calendar. Unlike webull-momentum-bot's market_hours.py (a
single US/Eastern "core hours" window), forex has no single exchange or
core session -- it's a near-continuous OTC market from Sunday evening to
Friday evening UTC, with liquidity varying by which regional session(s)
are currently open. `RiskConfig.session_windows` (added in the risk-engine
phase) will gate entries to an allowlist of the named windows below,
mirroring how `webull_bot`'s `allow_extended_hours_trading` gates entries
to a time window -- but here there are several named windows, not one.

Session bands are the commonly-cited approximate UTC hours used across
retail forex tools (fixed UTC clock times, not adjusted for each
session's own local DST) -- adequate for a scalping bot's own reasoning
about "is it currently the liquid London/NY window," but re-verify against
a specific broker's own session-time documentation if precise boundary
behavior around a DST transition ever matters.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

# (open, close) in UTC. Sydney and Tokyo wrap past midnight UTC.
SESSION_WINDOWS: dict[str, tuple[time, time]] = {
    "sydney": (time(22, 0), time(7, 0)),
    "tokyo": (time(0, 0), time(9, 0)),
    "london": (time(8, 0), time(17, 0)),
    "new_york": (time(13, 0), time(22, 0)),
}

# The most liquid window for a scalping strategy -- not a distinct entry
# in SESSION_WINDOWS (it's the intersection of london/new_york above), kept
# as a named convenience since strategy configs will want to reference it
# directly (e.g. RiskConfig.session_windows = ["london_new_york_overlap"]).
LONDON_NEW_YORK_OVERLAP: tuple[time, time] = (time(13, 0), time(17, 0))


def _time_in_window(current: time, open_: time, close_: time) -> bool:
    if open_ <= close_:
        return open_ <= current < close_
    # Wraps past midnight (e.g. Sydney 22:00-07:00).
    return current >= open_ or current < close_


def active_sessions(now_utc: datetime) -> set[str]:
    """Named sessions currently open at `now_utc` (naive UTC, same
    convention as webull_bot's `now`/`now_utc` parameters throughout).
    More than one can be active at once (e.g. london + new_york during
    the overlap) -- callers checking for the overlap specifically should
    use `is_within_london_new_york_overlap` rather than checking both
    names are present, since the exact overlap band is independently
    defined above, not derived from these two windows' intersection."""
    if not is_market_open(now_utc):
        return set()
    current = now_utc.time()
    return {
        name for name, (open_, close_) in SESSION_WINDOWS.items()
        if _time_in_window(current, open_, close_)
    }


def is_within_london_new_york_overlap(now_utc: datetime) -> bool:
    if not is_market_open(now_utc):
        return False
    return _time_in_window(now_utc.time(), *LONDON_NEW_YORK_OVERLAP)


def is_market_open(now_utc: datetime) -> bool:
    """Forex trades continuously from the Sydney session's Sunday open to
    its Friday close -- approximated here as Sunday 22:00 UTC through
    Friday 22:00 UTC, matching SESSION_WINDOWS' own Sydney band above so
    the two never disagree about when the week starts/ends."""
    weekday = now_utc.weekday()  # Monday=0 .. Sunday=6
    current = now_utc.time()
    if weekday == 5:  # Saturday: always closed
        return False
    if weekday == 6:  # Sunday: open only from 22:00
        return current >= time(22, 0)
    if weekday == 4:  # Friday: closed from 22:00
        return current < time(22, 0)
    return True  # Monday-Thursday: open all day


def seconds_until_market_open(now_utc: datetime) -> float:
    """0.0 if already open. Used by callers that want to sleep/skip work
    rather than poll every tick while the weekend close is in effect."""
    if is_market_open(now_utc):
        return 0.0
    days_ahead = (6 - now_utc.weekday()) % 7  # days until next Sunday
    next_sunday = (now_utc + timedelta(days=days_ahead)).replace(
        hour=22, minute=0, second=0, microsecond=0,
    )
    if next_sunday <= now_utc:
        next_sunday += timedelta(days=7)
    return (next_sunday - now_utc).total_seconds()
