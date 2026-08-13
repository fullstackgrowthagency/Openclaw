"""
Tier 3: Trigger Engine.

Watches only ARMED candidates, using the same real-time snapshot stream, and
asks each configured Strategy whether entry conditions are met. This is
meant to run off streaming data rather than REST polling once the broker's
subscribe_quotes is wired up (see interfaces/broker.py).

Entry-selectivity rework (2026-08-13, see docs/ARCHITECTURE.md): a strategy
firing here used to move a candidate straight to TRIGGERED, which
TradingLoop then submitted an order for on that exact same tick -- off a
single snapshot, with no check that the move actually held for even a
second. Now it moves to CONFIRMING instead: the Signal is still returned
(so TradingLoop can stash it), but TradingLoop's confirmation-window logic
(_poll_confirmation) is what actually decides whether this ever becomes a
real order.
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
                    CandidateState.CONFIRMING,
                    now=snapshot.timestamp,
                    reason=f"{strategy.name} v{strategy.version} generated {signal.action.value}; awaiting confirmation",
                )
                return signal
        return None
