"""
Volume profile (Market Profile) resistance detection: instead of hand-picking
special price levels (prior day high, round numbers, ...), build a histogram
of how much volume traded at each price level over a lookback window and
treat the biggest clusters -- "high volume nodes" (HVN) -- as real
support/resistance. Those special levels usually show up as HVNs anyway,
since psychologically notable prices attract more trading, and a
consolidation/base *is* a volume cluster by definition -- so this is a more
general mechanism than maintaining a list of special cases, and it comes
with a real strength signal (volume) that a flat list of price points can't
give you.

Deliberately built on raw per-bar OHLCV (WebullBrokerClient.get_raw_bars),
not tick-level trade data -- Webull's API doesn't expose that. Each bar's
volume is spread evenly across its [low, high] range rather than assigned
to a single price (e.g. its close), since a bar's volume did not all trade
at one price.

get_raw_bars requests pre-market and after-hours bars in addition to the
regular session (see its docstring for exactly how, and the caveat that the
session-selection values are inferred, not confirmed live), so the volume
profile built here reflects a name's pre/after-market activity too, not
just 9:30am-4:00pm ET -- important for a low-float mover whose real
resistance level may have formed entirely outside regular hours.

Unvalidated starting point, same spirit as scoring/weights.yaml's
"v1-initial-unvalidated": bucket count, node significance threshold, and
lookback window are reasonable defaults, not backtested values.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence


@dataclass(frozen=True)
class VolumeNode:
    price: float   # bucket midpoint
    volume: float  # total volume distributed into this bucket


def filter_bars_by_lookback(bars: Sequence[dict], *, lookback_days: int, now: Optional[datetime] = None) -> list[dict]:
    """Webull's get_raw_bars returns the last N bars that actually have
    data, not the last N calendar time-slots -- for an illiquid symbol that
    can reach back months (confirmed live 2026-08-09: 780 5-minute bars for
    a real low-float mover spanned back to February). A volume profile
    built on stale, months-old price action isn't a useful *current*
    resistance/support map, so this trims to a bounded calendar window
    before the profile is computed -- the resulting profile may end up
    sparse for a name that genuinely hasn't traded much recently, which
    accurately reflects that there's little relevant history to draw on."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=lookback_days)
    kept = []
    for bar in bars:
        timestamp = bar["time"]
        if isinstance(timestamp, str):
            timestamp = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%f%z").replace(tzinfo=None)
        if timestamp >= cutoff:
            kept.append(bar)
    return kept


def compute_volume_profile(bars: Sequence[dict], *, num_buckets: int = 50) -> list[VolumeNode]:
    """Distributes each bar's volume evenly across every price bucket its
    [low, high] range touches, across `num_buckets` buckets spanning the
    full [min_low, max_high] of all bars. Returns one VolumeNode per
    non-empty bucket (empty buckets are omitted, not returned as
    zero-volume nodes), in bucket order (ascending price). Bars are plain
    dicts (Webull's raw bar shape: string-encoded "low"/"high"/"volume")
    so this doesn't need a MarketSnapshot -- get_raw_bars deliberately
    returns unprocessed dicts, see its docstring."""
    if not bars:
        return []

    lows = [float(b["low"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    min_price, max_price = min(lows), max(highs)

    if max_price <= min_price:
        total_volume = sum(float(b["volume"]) for b in bars)
        return [VolumeNode(price=min_price, volume=total_volume)] if total_volume else []

    bucket_width = (max_price - min_price) / num_buckets
    bucket_volumes = [0.0] * num_buckets

    def bucket_index(price: float) -> int:
        return min(int((price - min_price) / bucket_width), num_buckets - 1)

    for bar, low, high in zip(bars, lows, highs):
        volume = float(bar["volume"])
        if volume <= 0:
            continue
        start_idx = bucket_index(low)
        end_idx = bucket_index(high) if high > low else start_idx
        touched = range(start_idx, end_idx + 1)
        share = volume / len(touched)
        for idx in touched:
            bucket_volumes[idx] += share

    return [
        VolumeNode(price=min_price + (i + 0.5) * bucket_width, volume=volume)
        for i, volume in enumerate(bucket_volumes)
        if volume > 0
    ]


def high_volume_node_levels(
    nodes: Sequence[VolumeNode],
    *,
    top_n: int = 5,
    min_volume_pct_of_max: float = 0.3,
) -> list[float]:
    """Price levels of the top `top_n` nodes by volume, excluding any node
    under `min_volume_pct_of_max` of the single largest node's volume --
    that threshold is what separates a real cluster from ordinary
    background noise scattered across mostly-empty buckets. Order is not
    meaningful; callers combining these with the running intraday high (see
    scanner/candidate_watcher.py's update_resistance) only care about which
    levels exist, not their rank."""
    if not nodes:
        return []
    max_volume = max(node.volume for node in nodes)
    threshold = max_volume * min_volume_pct_of_max
    significant = [node for node in nodes if node.volume >= threshold]
    ranked = sorted(significant, key=lambda node: node.volume, reverse=True)
    return [node.price for node in ranked[:top_n]]


# -- target clearance / resistance runway (entry-selectivity rework, --------
# 2026-08-13, see docs/ARCHITECTURE.md's "Entry selectivity rework" section)
#
# Deliberately built on top of the SAME `static_resistance_levels` this
# module already produces (the HVN-filtered "significant" levels), not a
# new standalone strength-scored resistance engine: those levels already
# distinguish a real cluster from background noise (min_volume_pct_of_max
# above), so a second scoring pass on top of that would just be re-deriving
# the same judgment a different way. Kept lean on purpose.


def next_significant_resistance(levels: Sequence[float], above_price: float) -> Optional[float]:
    """The nearest static resistance level strictly above `above_price`, or
    None if no such level exists in `levels`. None here means "no known
    resistance blocks this" -- not "we don't know": both this function and
    its caller inherit static_resistance_levels' existing fail-soft
    contract (a failed/missing get_raw_bars lookup just means an empty
    list, treated the same as "genuinely no levels found" -- see
    scanner/broad_scanner.py's docstring for why that's an accepted
    simplification here rather than a separate valid/invalid distinction)."""
    above = [level for level in levels if level > above_price]
    return min(above) if above else None


@dataclass(frozen=True)
class TargetClearance:
    target_price: float
    next_resistance: Optional[float]
    target_clear: bool          # False only when a known resistance level sits at/before the target
    room_to_target_score: float  # 0-100: 100 = no resistance in the way at all, 0 = resistance blocks the target


def evaluate_target_clearance(
    current_price: float, target_price: float, static_resistance_levels: Sequence[float],
) -> TargetClearance:
    """The hard gate: can the fixed target actually be reached before a
    known resistance level gets in the way? `current_price` is whatever
    price the caller is evaluating from (the confirmed entry price at
    trigger-confirmation time, or the live price for a pre-trigger MIS
    scoring pass -- see momentum_ignition_score.py). No resistance found
    above current_price -> automatically clear (open air), matching the
    "don't invent an arbitrary distance limit when there's genuinely
    nothing there" principle this was built around."""
    resistance = next_significant_resistance(static_resistance_levels, current_price)
    if resistance is None:
        return TargetClearance(target_price, None, target_clear=True, room_to_target_score=100.0)
    if resistance <= target_price:
        return TargetClearance(target_price, resistance, target_clear=False, room_to_target_score=0.0)
    extra_room = resistance - target_price
    required_move = target_price - current_price
    clearance_ratio = (extra_room / required_move) if required_move > 0 else 1.0
    room_to_target_score = 50.0 + 50.0 * min(clearance_ratio, 1.0)
    return TargetClearance(target_price, resistance, target_clear=True, room_to_target_score=room_to_target_score)


def compute_runway_consumed_pct(
    trigger_price: float, entry_price: float, resistance: Optional[float],
) -> Optional[float]:
    """How much of the trigger->resistance runway the confirmed entry price
    already used up (0.0 = entered right at the trigger, 1.0 = entered
    exactly at resistance). Only meaningful once a real trigger_price
    exists (see this function's TradingLoop._poll_confirmation call site) --
    unlike room_to_target_score above, this is NOT computed as a continuous
    per-tick MIS component, since there's no real "trigger price" to
    measure from before a strategy has actually triggered; inventing one
    (e.g. reusing the running high) would produce a number that doesn't
    mean what "runway consumed" is supposed to mean. None whenever there's
    no resistance to measure against (unconstrained) or the trigger itself
    was already at/past the resistance level (shouldn't happen -- target
    clearance would already have rejected that case)."""
    if resistance is None or resistance <= trigger_price:
        return None
    return (entry_price - trigger_price) / (resistance - trigger_price)


def resistance_runway_score(consumed_pct: Optional[float]) -> Optional[float]:
    """0-100 informational score from runway_consumed_pct -- see
    docs/ARCHITECTURE.md for the starting thresholds (0-10% excellent,
    10-20% good, 20-30% acceptable, 30-40% weak, >40% treated as a hard
    reject by TradingLoopConfig.max_runway_consumed_pct, not just a low
    score here)."""
    if consumed_pct is None:
        return None
    if consumed_pct <= 0.10:
        return 100.0
    if consumed_pct <= 0.20:
        return 80.0
    if consumed_pct <= 0.30:
        return 60.0
    if consumed_pct <= 0.40:
        return 30.0
    return 0.0
