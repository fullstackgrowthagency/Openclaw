"""
MomentumQualificationEngine: the Real-Time Momentum Qualification Layer,
inserted between an existing strategy's trigger firing and the existing
CONFIRMING state (see runtime/trading_loop.py's _process_candidate_inner
and _poll_confirmation for exactly where this is called). Answers a
question neither MIS nor the 8 strategies' own trigger conditions do:
"is this stock actually moving upward fast enough RIGHT NOW to enter, and
if it's pulling back, is that pullback healthy or a real failure."

Two entry points:
  - on_snapshot: runs every tick for ARMED/CONFIRMING candidates,
    independent of whether a strategy fired this tick -- updates
    candidate.momentum (phase, RTMS, impulse/pullback tracking) in place.
    This unconditional per-tick run is what lets pullback/reacceleration
    tracking persist between a strategy's own re-fires, which aren't
    guaranteed every tick (see scanner/momentum_structure.py's docstring
    on why IgnitionPullbackStrategy in particular can't be leaned on for
    continuous structural context).
  - evaluate_trigger: called only when a strategy actually fired a Signal
    this tick -- reads whatever on_snapshot already computed THIS tick and
    decides whether to start (or resume) CONFIRMING, keep tracking a
    pullback, or simply leave the candidate ARMED.

Pure reader of candidate.latest_metrics (already refreshed every tick by
CandidateWatcher.update() before this runs) -- no broker/tick access of
its own; the tick-derived short-window math lives in metrics/rolling.py,
upstream of this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional

from ..enums import MomentumPhase
from ..models import Candidate, MarketSnapshot, MomentumState, Signal
from ..scoring.rtms import RTMSConfig, compute_rtms
from .momentum_structure import momentum_structure_intact

TriggerOutcome = Literal["stay_armed", "track_pullback", "start_confirmation"]


@dataclass
class TriggerDecision:
    outcome: TriggerOutcome
    reason: str
    phase: MomentumPhase
    rtms: Optional[float]


class MomentumQualificationEngine:
    def __init__(self, config: Optional[RTMSConfig] = None):
        self.config = config or RTMSConfig.load()

    # -- per-tick phase/RTMS/impulse tracking --------------------------------

    def on_snapshot(
        self, candidate: Candidate, signal: Optional[Signal], snapshot: MarketSnapshot, now: datetime,
    ) -> None:
        metrics = candidate.latest_metrics
        price = snapshot.last_price
        rtms_score, rtms_components = compute_rtms(metrics, self.config, now, current_price=price)

        state = candidate.momentum
        prior_rtms = state.rtms
        # This tick's own velocity_5s, captured now so it can be stashed as
        # NEXT tick's prior_velocity_5s at the very end -- _advance_impulse
        # below still needs to read the STALE (previous-tick) value off
        # `state` first, so this must not overwrite state.prior_velocity_5s
        # until after that call.
        this_tick_velocity_5s = metrics.velocity_5s if metrics is not None else None

        regime_active = (
            metrics is not None and metrics.return_5m is not None
            and metrics.return_5m >= self.config.thresholds["min_return_5m_pct"]
        )
        strategy_name = signal.strategy_name if signal is not None else None

        if not regime_active:
            # Regime materially failed -- a full reset if we'd already
            # built up real impulse/pullback state; NONE/IGNITING candidates
            # have nothing to lose from re-evaluating in place.
            if state.phase in (MomentumPhase.IMPULSING, MomentumPhase.PULLING_BACK, MomentumPhase.REACCELERATING):
                candidate.momentum = MomentumState()
                state = candidate.momentum
            state.rtms = rtms_score
            state.rtms_components = rtms_components
            state.prior_rtms = prior_rtms
            state.phase = MomentumPhase.IGNITING if self._is_igniting(metrics) else MomentumPhase.NONE
            state.prior_velocity_5s = this_tick_velocity_5s
            state.last_evaluated_at = now
            return

        state.rtms = rtms_score
        state.rtms_components = rtms_components
        state.prior_rtms = prior_rtms

        if state.phase in (MomentumPhase.NONE, MomentumPhase.IGNITING):
            if self._meets_impulsing_quality(metrics, price, now):
                state.phase = MomentumPhase.IMPULSING
                state.phase_changed_at = now
                # Back-compute an anchor price for the impulse start from
                # the 60s return, since there's no continuous price history
                # object available here (only the derived metrics) --
                # approximate, not exact, but good enough to seed
                # impulse_size_pct/retracement tracking from this point on.
                anchor_return = metrics.return_60s if metrics.return_60s is not None else 0.0
                state.impulse_start_price = price / (1.0 + anchor_return / 100.0) if anchor_return > -100.0 else price
                state.impulse_start_time = now - timedelta(seconds=60)
                state.impulse_high = price
                state.impulse_high_time = now
                state.active_strategy_name = strategy_name
            else:
                state.phase = MomentumPhase.IGNITING if self._is_igniting(metrics) else MomentumPhase.NONE
            state.prior_velocity_5s = this_tick_velocity_5s
            state.last_evaluated_at = now
            return

        # phase in (IMPULSING, PULLING_BACK, REACCELERATING)
        self._advance_impulse(candidate, strategy_name, snapshot, now)
        state = candidate.momentum  # _advance_impulse may have replaced this on a structural-failure reset
        state.prior_velocity_5s = this_tick_velocity_5s
        state.last_evaluated_at = now

    def _is_igniting(self, metrics) -> bool:
        if metrics is None:
            return False
        th = self.config.thresholds
        return (
            metrics.return_5m is not None and metrics.return_5m >= th["igniting_min_return_5m_pct"]
            and metrics.return_60s is not None and metrics.return_60s > 0
            and metrics.acceleration_60s is not None and metrics.acceleration_60s > 0
        )

    def _meets_impulsing_quality(self, metrics, price: float, now: datetime) -> bool:
        if metrics is None:
            return False
        th = self.config.thresholds
        if metrics.return_60s is None or metrics.return_60s < th["impulsing_min_return_60s_pct"]:
            return False
        if metrics.return_30s is None or metrics.return_30s < th["impulsing_min_return_30s_pct"]:
            return False
        if metrics.return_15s is None or metrics.return_15s < th["impulsing_min_return_15s_pct"]:
            return False
        if metrics.return_5s is None or metrics.return_5s <= th["impulsing_min_return_5s_pct"]:
            return False
        if metrics.trend_efficiency_15s < th["impulsing_min_trend_efficiency_15s"]:
            return False
        if metrics.recent_high_15s is None or metrics.recent_high_15s_time is None or metrics.recent_high_15s <= 0:
            return False
        distance_pct = (metrics.recent_high_15s - price) / metrics.recent_high_15s * 100.0
        if distance_pct > th["impulsing_high_proximity_pct"]:
            return False
        seconds_since_high = max(0.0, (now - metrics.recent_high_15s_time).total_seconds())
        if seconds_since_high > th["impulsing_high_freshness_seconds"]:
            return False
        return True

    def _advance_impulse(
        self, candidate: Candidate, strategy_name: Optional[str], snapshot: MarketSnapshot, now: datetime,
    ) -> None:
        state = candidate.momentum
        metrics = candidate.latest_metrics
        price = snapshot.last_price

        if state.impulse_start_price is None or state.impulse_high is None:
            return  # defensive -- shouldn't happen, impulse wasn't properly started

        reclaimed_full_high = False
        if price > state.impulse_high:
            state.impulse_high = price
            state.impulse_high_time = now
            if state.phase != MomentumPhase.IMPULSING:
                reclaimed_full_high = True

        impulse_size = state.impulse_high - state.impulse_start_price
        state.impulse_size_pct = (impulse_size / state.impulse_start_price * 100.0) if state.impulse_start_price else None
        pullback_size = max(0.0, state.impulse_high - price)
        state.current_retracement_pct = (pullback_size / state.impulse_high * 100.0) if state.impulse_high else None
        state.retracement_ratio = (pullback_size / impulse_size) if impulse_size > 0 else None

        if reclaimed_full_high:
            # A full reclaim of the impulse high -- back to IMPULSING
            # regardless of what pullback state preceded it, and the
            # pullback-specific tracking no longer applies.
            state.phase = MomentumPhase.IMPULSING
            state.phase_changed_at = now
            state.pullback_low = None
            state.pullback_low_time = None
            state.pullback_micro_high = None
            state.active_strategy_name = strategy_name or state.active_strategy_name
            return

        if pullback_size <= 0:
            # Sitting right at (or making a fresh, non-reclaiming) high --
            # plain IMPULSING, nothing to track.
            if state.phase != MomentumPhase.IMPULSING:
                state.phase = MomentumPhase.IMPULSING
                state.phase_changed_at = now
            return

        # In a pullback from the impulse high -- classify healthy vs. broken.
        active_strategy = strategy_name or state.active_strategy_name
        structure_ok = momentum_structure_intact(candidate, active_strategy, snapshot) if active_strategy else True
        state.structure_intact = structure_ok

        th = self.config.thresholds
        healthy_retracement = (
            (state.retracement_ratio is not None and state.retracement_ratio <= th["max_pullback_retracement_ratio"])
            or (state.current_retracement_pct is not None and state.current_retracement_pct <= th["max_absolute_pullback_pct"])
        )

        if not (healthy_retracement and structure_ok):
            # Structural failure -- the setup that created this trade is
            # invalidated. Full reset back to NONE, preserving only this
            # tick's already-computed RTMS (it's still a real, current
            # reading, just no longer attached to a live impulse).
            candidate.momentum = MomentumState(rtms=state.rtms, rtms_components=state.rtms_components, prior_rtms=state.prior_rtms)
            return

        pre_tick_micro_high = state.pullback_micro_high
        was_pulling_back_or_reaccelerating = state.phase in (MomentumPhase.PULLING_BACK, MomentumPhase.REACCELERATING)

        if state.pullback_low is None or price < state.pullback_low:
            # A fresh, lower low resets the recovery leg -- nothing to
            # reclaim yet on THIS tick, so the reacceleration check below
            # correctly can't fire off a micro-high that's about to be
            # replaced.
            state.pullback_low = price
            state.pullback_low_time = now
            state.pullback_micro_high = price
            pre_tick_micro_high = None
        elif pre_tick_micro_high is None or price > pre_tick_micro_high:
            state.pullback_micro_high = price

        state.active_strategy_name = active_strategy

        reaccelerating = (
            was_pulling_back_or_reaccelerating
            and metrics is not None
            and metrics.return_5s is not None and metrics.return_5s > 0
            and metrics.velocity_5s is not None and state.prior_velocity_5s is not None
            and metrics.velocity_5s > state.prior_velocity_5s
            and pre_tick_micro_high is not None and price > pre_tick_micro_high
            and state.rtms is not None and state.prior_rtms is not None and state.rtms > state.prior_rtms
        )

        if reaccelerating:
            state.phase = MomentumPhase.REACCELERATING
            state.phase_changed_at = now
        elif state.phase != MomentumPhase.PULLING_BACK:
            state.phase = MomentumPhase.PULLING_BACK
            state.phase_changed_at = now

    # -- trigger-time qualification decision ---------------------------------

    def evaluate_trigger(
        self, candidate: Candidate, signal: Signal, snapshot: MarketSnapshot, now: datetime,
    ) -> TriggerDecision:
        state = candidate.momentum
        phase = state.phase

        if phase not in (MomentumPhase.IMPULSING, MomentumPhase.REACCELERATING):
            if phase == MomentumPhase.PULLING_BACK:
                reason = f"{candidate.symbol}: pulling back (structure intact) -- tracking for reacceleration"
                outcome: TriggerOutcome = "track_pullback"
            else:
                metrics = candidate.latest_metrics
                min_regime = self.config.thresholds["min_return_5m_pct"]
                if metrics is not None and metrics.return_5m is not None:
                    reason = f"{candidate.symbol}: 5m momentum regime not met ({metrics.return_5m:.2f}% < {min_regime:.2f}%)"
                else:
                    reason = f"{candidate.symbol}: 5m momentum regime not yet measurable"
                outcome = "stay_armed"
            state.momentum_qualified = False
            state.block_reason = reason
            return TriggerDecision(outcome=outcome, reason=reason, phase=phase, rtms=state.rtms)

        allowed_phases = self.config.strategy_phase_policy.get(
            signal.strategy_name, frozenset({MomentumPhase.IMPULSING, MomentumPhase.REACCELERATING}),
        )
        if phase not in allowed_phases:
            reason = f"{candidate.symbol}: {signal.strategy_name} does not accept entries from phase {phase.value}"
            state.momentum_qualified = False
            state.block_reason = reason
            return TriggerDecision(outcome="stay_armed", reason=reason, phase=phase, rtms=state.rtms)

        rtms_floor = (
            self.config.thresholds["rtms_reaccelerating_threshold"] if phase == MomentumPhase.REACCELERATING
            else self.config.thresholds["rtms_entry_threshold"]
        )
        if state.rtms is None or state.rtms < rtms_floor:
            reason = f"{candidate.symbol}: RTMS {state.rtms} below {rtms_floor:.1f} required to enter confirmation from {phase.value}"
            state.momentum_qualified = False
            state.block_reason = reason
            return TriggerDecision(outcome="stay_armed", reason=reason, phase=phase, rtms=state.rtms)

        reason = f"{candidate.symbol}: momentum qualified (phase={phase.value}, RTMS={state.rtms:.1f})"
        state.momentum_qualified = True
        state.block_reason = None
        return TriggerDecision(outcome="start_confirmation", reason=reason, phase=phase, rtms=state.rtms)
