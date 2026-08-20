"""
Real session VWAP anchor: cumulative price*volume and cumulative volume
across today's regular-session bars, up to some point in time. This is the
STARTING POINT for TradingLoop's live running VWAP (see
runtime/trading_loop.py's _update_session_vwap), not a complete VWAP
computation on its own -- see this module's docstring's last paragraph for
why a live component has to take over from here.

Real incident (2026-08-14, user report + chart evidence): the bot tried to
enter ONFO well after it had already spiked from ~$2.18 to $6.08 and faded
back down to ~$2.55 -- a textbook "already made its run" fade, not a fresh
breakout. Investigation found `MarketSnapshot.vwap` is hardcoded to
`last_price` in BOTH of WebullBrokerClient's live snapshot paths
(_snapshot_from_dict for REST, _snapshot_from_streamed_result for
streaming) because Webull's snapshot/streaming APIs simply don't return
VWAP at all. That makes `distance_from_vwap_pct` deterministically exactly
0.0 on every live tick, which silently breaks FOUR separate mechanisms
that all depend on it: scoring/momentum_ignition_score.py's
trend_quality_score component (a constant, no-signal contribution --
removed 2026-08-20, see scoring/weights.yaml's v2.7 changelog),
strategy/vwap_reclaim.py and strategy/ignition_pullback.py (both
structurally unable to ever trigger live -- their own guard conditions
read `distance_from_vwap_pct <= negative_threshold` / `> 0`, neither of
which a value pinned at exactly 0.0 can ever satisfy), and
position/position_manager.py's exit_on_vwap_failure safety backstop
(`last_price < vwap * (1 - buffer)` can never be true when vwap ==
last_price). A stock trading well below its real session VWAP -- exactly
ONFO's situation -- got zero penalty from any of these, while its
still-elevated RVOL/volume-acceleration numbers (driven by the earlier
spike, cumulative and direction-blind) kept it looking attractive.

Deliberately built on the SAME raw bars BroadScanner already fetches for
volume-profile/opening-range/RVOL-baseline analysis (WebullBrokerClient.get_raw_bars),
not a second network call -- mirrors metrics/opening_range.py's exact
"same bars, another cheap derived value" pattern, including reusing its
session-date-resolution convention (9:30am US/Eastern via zoneinfo, not a
hardcoded UTC offset that would drift wrong across the EST/EDT boundary).

Regular session only (9:30am ET onward), NOT pre-market/after-hours --
deliberately different from static_resistance_levels/volume_baseline
elsewhere in this codebase, which DO include extended hours. VWAP is a
specific, well-known intraday reference line every trading platform
computes the same way (regular session only); diverging from that
convention would make this project's "VWAP" mean something no chart a
human is looking at alongside the bot would agree with.

This anchor alone is NOT the live VWAP a candidate ends up using: bars
stop the moment BroadScanner fetches them (discovery time), but a
candidate is tracked for the rest of the day after that. TradingLoop owns
continuing the accumulation tick-by-tick from here (see
_update_session_vwap's docstring for the boundary-price approximation it
uses, the same one metrics/calculations.dollar_volume_from_avg_price
already relies on elsewhere in this codebase) -- this module only ever
produces the STARTING point for that, seeded once at discovery.
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


def compute_session_vwap_anchor(
    bars: Sequence[dict], *, now: Optional[datetime] = None
) -> tuple[Optional[float], Optional[float]]:
    """Returns (cumulative_pv, cumulative_volume) summed across today's
    (per `now`, naive UTC) regular-session bars from market open (9:30am
    ET) through `now` -- (None, None) if no bars fall in that window at
    all (discovered before the open, or the fetched bars don't reach back
    that far for an illiquid symbol -- see get_raw_bars' docstring on data
    sparsity). Each bar is priced at its own close for the numerator
    (cumulative_pv += close * volume), the same per-bar granularity
    _snapshots_from_bars already uses for its own (correct, bars-only)
    VWAP computation elsewhere in WebullBrokerClient -- true intra-bar
    tick data isn't available from get_raw_bars, so a bar's own close is
    the finest-grained price this can use for it. Bars are Webull's raw
    dict shape (string-encoded "close"/"volume"/"time"), same convention
    every other bars-consuming function in this codebase expects."""
    now = now or datetime.utcnow()
    session_date_eastern = now.replace(tzinfo=timezone.utc).astimezone(_EASTERN).date()
    market_open_eastern = datetime.combine(session_date_eastern, _MARKET_OPEN_TIME, tzinfo=_EASTERN)
    market_open_utc = market_open_eastern.astimezone(timezone.utc).replace(tzinfo=None)

    session_bars = [bar for bar in bars if market_open_utc <= _parse_bar_time(bar["time"]) <= now]
    if not session_bars:
        return None, None

    cumulative_pv = 0.0
    cumulative_volume = 0.0
    for bar in session_bars:
        volume = float(bar["volume"])
        cumulative_pv += float(bar["close"]) * volume
        cumulative_volume += volume
    return cumulative_pv, cumulative_volume
