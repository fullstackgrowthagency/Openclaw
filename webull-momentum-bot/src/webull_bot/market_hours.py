"""
Shared US/Eastern regular ("core") trading session boundary: 9:30am-4:00pm
ET, Monday-Friday.

This is deliberately a standalone module rather than importing
`brokers/webull/client.py`'s equivalent, private `_is_outside_regular_session`
helper: `risk/risk_engine.py` and `runtime/trading_loop.py` both need the
same boundary now (gating new entries to core hours, and auto-flattening
open positions once core hours end), and neither should depend on a
broker-specific module -- the risk engine and trading loop are meant to work
against any BrokerClient implementation, not just Webull's.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
REGULAR_SESSION_OPEN = time(9, 30)
REGULAR_SESSION_CLOSE = time(16, 0)


def is_within_core_trading_hours(now_utc: datetime) -> bool:
    """True only Monday-Friday, 9:30am <= now < 4:00pm US/Eastern. `now_utc`
    is naive UTC, same convention as everywhere else this project passes a
    `now` around (e.g. RiskEngine.evaluate's `now` parameter)."""
    eastern = now_utc.replace(tzinfo=timezone.utc).astimezone(EASTERN)
    if eastern.weekday() >= 5:  # Saturday/Sunday
        return False
    return REGULAR_SESSION_OPEN <= eastern.time() < REGULAR_SESSION_CLOSE


def is_after_core_trading_hours(now_utc: datetime) -> bool:
    """True once the day's regular session has closed -- at/after 4:00pm ET
    on a weekday, or any time on a weekend. Distinct from simply negating
    `is_within_core_trading_hours`: that would also be True before 9:30am,
    which is "not yet open," not "already closed" -- this is used to decide
    when to auto-flatten any still-open position at end of day, and firing
    that before the open would be wrong."""
    eastern = now_utc.replace(tzinfo=timezone.utc).astimezone(EASTERN)
    if eastern.weekday() >= 5:
        return True
    return eastern.time() >= REGULAR_SESSION_CLOSE
