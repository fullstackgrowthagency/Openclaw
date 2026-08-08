"""
Tier 3: Trigger Engine.

Watches only ARMED candidates, using the same real-time snapshot stream, and
asks each configured Strategy whether entry conditions are met. This is
meant to run off streaming data rather than REST polling once the broker's
subscribe_quotes is wired up (see interfaces/broker.py).
"""
from __future__ import annotations

from ..enums import CandidateState
from ..interfaces.strategy import Strategy
from ..models import Candidate, MarketSnapshot, Signal
from ..state_machine import transition


class TriggerEngine:
    def __init__(self, strategies: list[Strategy]):
        self.strategies = strategies

    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Signal | None:
        if candidate.state != CandidateState.ARMED:
            return None

        for strategy in self.strategies:
            signal = strategy.on_snapshot(candidate, snapshot)
            if signal is not None:
                transition(
                    candidate,
                    CandidateState.TRIGGERED,
                    now=snapshot.timestamp,
                    reason=f"{strategy.name} v{strategy.version} generated {signal.action.value}",
                )
                return signal
        return None
