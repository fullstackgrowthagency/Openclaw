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

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
REGULAR_SESSION_OPEN = time(9, 30)
REGULAR_SESSION_CLOSE = time(16, 0)
# Webull's standard pre-market/after-hours bounds (4:00am-9:30am and
# 4:00pm-8:00pm ET) -- see is_within_extended_trading_hours. Distinct from
# REGULAR_SESSION_OPEN/CLOSE above, which bound the CORE session only.
EXTENDED_SESSION_OPEN = time(4, 0)
EXTENDED_SESSION_CLOSE = time(20, 0)


def is_within_core_trading_hours(now_utc: datetime) -> bool:
    """True only Monday-Friday, 9:30am <= now < 4:00pm US/Eastern. `now_utc`
    is naive UTC, same convention as everywhere else this project passes a
    `now` around (e.g. RiskEngine.evaluate's `now` parameter)."""
    eastern = now_utc.replace(tzinfo=timezone.utc).astimezone(EASTERN)
    if eastern.weekday() >= 5:  # Saturday/Sunday
        return False
    return REGULAR_SESSION_OPEN <= eastern.time() < REGULAR_SESSION_CLOSE


def is_within_extended_trading_hours(now_utc: datetime) -> bool:
    """True Monday-Friday, 4:00am <= now < 8:00pm US/Eastern -- CORE hours
    plus Webull's standard pre-market and after-hours windows. Does NOT
    cover Webull's separate overnight session (which runs on a different
    calendar-day boundary and isn't wired into this bot at all yet).

    Added 2026-08-12 alongside RiskConfig.allow_extended_hours_trading:
    before this, `RiskEngine.evaluate` only ever consulted
    `is_within_core_trading_hours`, an unconditional gate with no config
    escape hatch. Whether an order actually reaches the market once this
    gate is opened is a SEPARATE, still-unresolved question -- see
    `WebullBrokerClient._order_payload`'s `support_trading_session` note
    for the broker-side half of extended-hours support, which as of this
    writing is still hardcoded to "CORE" pending live verification."""
    eastern = now_utc.replace(tzinfo=timezone.utc).astimezone(EASTERN)
    if eastern.weekday() >= 5:
        return False
    return EXTENDED_SESSION_OPEN <= eastern.time() < EXTENDED_SESSION_CLOSE


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


def is_within_closing_buffer(now_utc: datetime, buffer_minutes: float) -> bool:
    """True once within `buffer_minutes` of the 4:00pm ET close (i.e. `now`
    is at or past `close - buffer_minutes`), through the rest of the day,
    or any time on a weekend. Deliberately NOT the same trigger as
    `is_after_core_trading_hours` -- every order this project submits
    (including the auto-flatten's own exit order) is scoped to the "CORE"
    trading session (see `WebullBrokerClient._order_payload`'s
    `support_trading_session` note). Observed live 2026-08-11: a position
    still open right at the 4:00pm close never actually flattened -- the
    auto-flatten was firing (confirmed via the dashboard/logs), just not
    succeeding, tick after tick. The leading diagnosis (not independently
    confirmed via a captured rejection message -- the fix below was
    applied on the strength of this reasoning plus the symptom match, not
    a proven root cause) is that a "CORE"-scoped order needs a still-live
    CORE session to execute at all, and by the time
    `is_after_core_trading_hours` first turns true that session has
    already ended. Firing the auto-flatten `buffer_minutes` BEFORE the
    close instead, while CORE is still unambiguously active, sidesteps
    the question entirely regardless of the exact rejection mechanism.
    Fires every tick from that point on (not just once), so a position
    opened in the last few minutes before close -- RiskEngine still
    allows entries right up to 4:00pm -- is caught on its very next tick,
    not missed. If positions still don't flatten within the buffer
    window, re-open this diagnosis rather than assuming the timing fix
    alone was sufficient."""
    eastern = now_utc.replace(tzinfo=timezone.utc).astimezone(EASTERN)
    if eastern.weekday() >= 5:
        return True
    close = eastern.replace(
        hour=REGULAR_SESSION_CLOSE.hour, minute=REGULAR_SESSION_CLOSE.minute, second=0, microsecond=0,
    )
    return eastern >= close - timedelta(minutes=buffer_minutes)
