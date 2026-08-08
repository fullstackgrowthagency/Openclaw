"""
Tier 2: Candidate Watcher.

Recomputes momentum metrics and the Momentum Ignition Score on every new
snapshot for symbols already in WATCHING/HEATING_UP/ARMED, and drives the
corresponding state transitions. Reaching ARMED means "hand this to the
Trigger Engine for real-time entry monitoring" -- it is not itself a trade
decision.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from ..enums import CandidateState
from ..metrics.rolling import MAX_HISTORY_MINUTES, compute_metrics
from ..models import Candidate, MarketSnapshot
from ..scoring.momentum_ignition_score import MISConfig, compute_score
from ..state_machine import transition


@dataclass
class WatcherConfig:
    heating_up_score_threshold: float = 40.0
    armed_score_threshold: float = 70.0
    cooling_off_ratio: float = 0.5   # drop back a stage if score falls below threshold * this ratio
    max_spread_pct: float = 2.0
    min_dollar_volume: float = 500_000


class CandidateWatcher:
    def __init__(self, mis_config: MISConfig | None = None, config: WatcherConfig | None = None):
        self.mis_config = mis_config or MISConfig.load()
        self.config = config or WatcherConfig()
        self._history: dict[str, list[MarketSnapshot]] = defaultdict(list)

    def _push_history(self, symbol: str, snapshot: MarketSnapshot) -> list[MarketSnapshot]:
        history = self._history[symbol]
        history.append(snapshot)
        cutoff = snapshot.timestamp - timedelta(minutes=MAX_HISTORY_MINUTES)
        while history and history[0].timestamp < cutoff:
            history.pop(0)
        return history

    def update(self, candidate: Candidate, snapshot: MarketSnapshot) -> Candidate:
        if candidate.state == CandidateState.REJECTED:
            return candidate

        history = self._push_history(candidate.symbol, snapshot)

        free_float = candidate.float_data.free_float_shares if candidate.float_data else None
        # IMPORTANT: metrics/strategies must see the resistance level as it
        # stood *before* this snapshot -- a bar's high_of_day always includes
        # its own last_price, so folding it in before the breakout check
        # would make resistance >= current price on every single bar and no
        # breakout could ever trigger. `update_resistance` (called by the
        # caller *after* the trigger engine has looked at this bar) is what
        # rolls the current bar's high into resistance for the *next* bar.
        metrics = compute_metrics(free_float, history, resistance_level=candidate.resistance_level)
        candidate.latest_metrics = metrics
        candidate.latest_score = compute_score(metrics, candidate.float_data, self.mis_config)

        if metrics.spread_pct > self.config.max_spread_pct or metrics.dollar_volume < self.config.min_dollar_volume:
            transition(candidate, CandidateState.REJECTED, now=snapshot.timestamp, reason="failed liquidity/spread check")
            return candidate

        score = candidate.latest_score.score

        if candidate.state == CandidateState.WATCHING and score >= self.config.heating_up_score_threshold:
            transition(candidate, CandidateState.HEATING_UP, now=snapshot.timestamp, reason=f"MIS {score:.1f} crossed heating-up threshold")
        elif candidate.state == CandidateState.HEATING_UP:
            if score >= self.config.armed_score_threshold:
                transition(candidate, CandidateState.ARMED, now=snapshot.timestamp, reason=f"MIS {score:.1f} crossed armed threshold")
            elif score < self.config.heating_up_score_threshold * self.config.cooling_off_ratio:
                transition(candidate, CandidateState.WATCHING, now=snapshot.timestamp, reason=f"MIS {score:.1f} cooled off")
        elif candidate.state == CandidateState.ARMED and score < self.config.heating_up_score_threshold:
            transition(candidate, CandidateState.HEATING_UP, now=snapshot.timestamp, reason=f"MIS {score:.1f} cooled off from armed")

        return candidate

    def update_resistance(self, candidate: Candidate, snapshot: MarketSnapshot) -> None:
        """Roll this bar's high into the resistance level for future bars.
        Callers must invoke this AFTER the trigger engine has evaluated the
        current snapshot against `candidate.resistance_level`, not before --
        see the note in `update()`.

        Resistance tracking here is intentionally simple: the running high
        of day. Swap in real level detection (prior consolidation, premarket
        high, round numbers, etc.) without changing this call site.
        """
        candidate.resistance_level = max(candidate.resistance_level or 0.0, snapshot.high_of_day) or None
