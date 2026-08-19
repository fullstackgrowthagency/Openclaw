"""
strategy_quality_score: the third (15%-weighted) input to
TradingLoop._submit_ranked_entries' final_entry_rank formula
(scanner/momentum_qualification.py / rtms_weights.yaml's ranking_weights),
alongside RTMS (65%) and MIS (20%). Answers a question neither of those
two scores does: "of the setups that already independently qualify, how
good does THIS specific triggered signal actually look" -- its
risk:reward shape, how much real room exists before a known resistance
level, and how efficiently price has been trending into it.

Deliberately reuses existing values rather than computing anything new --
see each component below for exactly what it's pulled from.
"""
from __future__ import annotations

from ..models import Candidate, Signal
from ..metrics.volume_profile import resistance_runway_score


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _scale(value: float, low: float, high: float) -> float:
    if high == low:
        return 0.0
    return _clamp((value - low) / (high - low) * 100.0)


def strategy_quality_score(
    signal: Signal, candidate: Candidate, *, min_risk_reward_ratio: float = 2.0,
) -> float:
    """0-100, the mean of whichever of the three components below are
    available for this signal/candidate, renormalized over just the
    available ones -- same None-skip contract as MIS/RTMS, so a signal
    missing one piece of context (e.g. no resistance level in play) isn't
    penalized for it.

    - risk_reward_component: the signal's OWN actual reward:risk ratio
      (reusing signal.reference_price/suggested_stop/suggested_target --
      no new stop/target computation), scaled against
      [min_risk_reward_ratio, min_risk_reward_ratio*2] -- a signal sitting
      right at the RiskEngine's own minimum-acceptable ratio scores low;
      one offering twice that scores high.
    - runway_component: metrics.volume_profile.resistance_runway_score,
      reused directly from candidate.runway_consumed_pct (already computed
      by TradingLoop._poll_confirmation's target-clearance check -- not
      recomputed here).
    - trend_efficiency_component: candidate.latest_metrics.trend_efficiency_60s
      (already computed by metrics/rolling.py) scaled to 0-100 -- how
      cleanly price has been trending into this signal, over a slightly
      longer window than the momentum-qualification gate's own 15s check.
    """
    components: list[float] = []

    if signal.suggested_stop is not None and signal.suggested_target is not None:
        risk = signal.reference_price - signal.suggested_stop
        reward = signal.suggested_target - signal.reference_price
        if risk > 0:
            actual_rr = reward / risk
            components.append(_scale(actual_rr, min_risk_reward_ratio, min_risk_reward_ratio * 2))

    runway_component = resistance_runway_score(candidate.runway_consumed_pct)
    if runway_component is not None:
        components.append(runway_component)

    if candidate.latest_metrics is not None:
        components.append(_clamp(candidate.latest_metrics.trend_efficiency_60s * 100.0))

    if not components:
        return 0.0
    return sum(components) / len(components)
