"""
momentum_structure_intact: is the technical level that made a given
strategy's setup real still holding, during a pullback? Used by
scanner/momentum_qualification.py to decide whether a retracement is a
healthy, trackable PULLING_BACK or a genuine structural failure that
should invalidate the setup outright.

Per-strategy, using whatever structural state that strategy's own trigger
condition actually depends on:
  - momentum_breakout/refined_breakout: candidate.resistance_level
  - opening_range_breakout: candidate.opening_range_high
  - vwap_reclaim: candidate.latest_metrics.vwap
  - breakout_pullback/ignition_pullback: candidate.momentum.pullback_low --
    the qualification layer's OWN independently-tracked pullback low, not
    either strategy's internal state. BreakoutPullbackStrategy exposes its
    low on Candidate.pullback_low, but IgnitionPullbackStrategy keeps its
    own in a private per-instance dict (see that file's docstring) that
    genuinely can't be read from outside it -- using the shared,
    independently-tracked value here sidesteps that asymmetry entirely,
    and is arguably the more correct reference anyway, since it tracks
    the exact pullback scanner/momentum_qualification.py is currently
    classifying, regardless of which strategy originally fired.
  - volatility_contraction/volume_ignition: neither has a native
    structural level (no resistance/VWAP-anchor/pullback-low the trigger
    condition itself depends on) -- falls back to a plain "still above
    session VWAP" check. volume_ignition's stricter treatment (never
    allowed to trigger CONFIRMING out of a pullback at all) is enforced
    separately via rtms_weights.yaml's strategy_phase_policy, not here.
"""
from __future__ import annotations

from ..models import Candidate, MarketSnapshot

_BREAKOUT_STRATEGIES = frozenset({"momentum_breakout", "refined_breakout"})
_PULLBACK_STRATEGIES = frozenset({"breakout_pullback", "ignition_pullback"})


def momentum_structure_intact(
    candidate: Candidate, strategy_name: str, snapshot: MarketSnapshot, *, tolerance_pct: float = 0.15,
) -> bool:
    """True if the structural level relevant to `strategy_name` is still
    holding (within `tolerance_pct`, so a tiny undershoot doesn't count as
    broken) as of `snapshot`. True whenever the relevant level isn't known
    (nothing to check against -- fails open, matching every other
    "no context available" default in this codebase's momentum-adjacent
    scoring, rather than blocking a candidate over missing data)."""
    price = snapshot.last_price
    tolerance = 1.0 - tolerance_pct / 100.0

    if strategy_name in _BREAKOUT_STRATEGIES:
        level = candidate.resistance_level
        return level is None or price >= level * tolerance

    if strategy_name == "opening_range_breakout":
        level = candidate.opening_range_high
        return level is None or price >= level * tolerance

    if strategy_name == "vwap_reclaim":
        vwap = candidate.latest_metrics.vwap if candidate.latest_metrics else None
        return not vwap or price >= vwap * tolerance

    if strategy_name in _PULLBACK_STRATEGIES:
        low = candidate.momentum.pullback_low
        return low is None or price >= low * tolerance

    # volatility_contraction, volume_ignition, and anything unrecognized:
    # no native structural level -- fall back to "still above session
    # VWAP" as the most-permissive real fact available.
    vwap = candidate.latest_metrics.vwap if candidate.latest_metrics else None
    return not vwap or price >= vwap
