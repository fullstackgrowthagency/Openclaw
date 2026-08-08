"""
Candidate lifecycle state machine.

A high Momentum Ignition Score must never directly create a trade -- it can
only move a candidate towards ARMED, which means "watch closely." Only a
Strategy's entry confirmation (checked against real-time data) can trigger
an actual order, and every order still has to clear the risk engine.
"""
from __future__ import annotations

from datetime import datetime

from .enums import CandidateState
from .models import Candidate

# Allowed forward transitions. Any candidate can also be forced to REJECTED
# or COOLDOWN from most states (disqualification / post-trade cooldown),
# handled separately below rather than listed for every source state.
_ALLOWED_TRANSITIONS: dict[CandidateState, set[CandidateState]] = {
    CandidateState.DISCOVERED: {CandidateState.WATCHING, CandidateState.REJECTED},
    CandidateState.WATCHING: {CandidateState.HEATING_UP, CandidateState.REJECTED, CandidateState.COOLDOWN},
    CandidateState.HEATING_UP: {CandidateState.ARMED, CandidateState.WATCHING, CandidateState.REJECTED},
    CandidateState.ARMED: {CandidateState.TRIGGERED, CandidateState.HEATING_UP, CandidateState.REJECTED, CandidateState.COOLDOWN},
    CandidateState.TRIGGERED: {CandidateState.ENTERED, CandidateState.ARMED, CandidateState.REJECTED},
    CandidateState.ENTERED: {CandidateState.MANAGING},
    CandidateState.MANAGING: {CandidateState.EXITED},
    CandidateState.EXITED: {CandidateState.COOLDOWN},
    CandidateState.COOLDOWN: {CandidateState.DISCOVERED, CandidateState.WATCHING},
    CandidateState.REJECTED: set(),  # terminal
}

# States from which an immediate disqualification (bad spread, halt, float
# data revised upward, etc.) is always legal.
_ALWAYS_CAN_REJECT = {
    CandidateState.DISCOVERED,
    CandidateState.WATCHING,
    CandidateState.HEATING_UP,
    CandidateState.ARMED,
    CandidateState.TRIGGERED,
}


class InvalidStateTransition(ValueError):
    pass


def can_transition(current: CandidateState, target: CandidateState) -> bool:
    if target == CandidateState.REJECTED and current in _ALWAYS_CAN_REJECT:
        return True
    return target in _ALLOWED_TRANSITIONS.get(current, set())


def transition(candidate: Candidate, target: CandidateState, *, now: datetime | None = None, reason: str = "") -> Candidate:
    now = now or datetime.utcnow()
    if not can_transition(candidate.state, target):
        raise InvalidStateTransition(
            f"{candidate.symbol}: cannot transition {candidate.state.value} -> {target.value}"
        )
    candidate.state_history.append((candidate.state, now))
    candidate.state = target
    candidate.last_updated_at = now
    if reason:
        candidate.notes = f"{candidate.notes}\n[{now.isoformat()}] -> {target.value}: {reason}".strip()
    return candidate


def new_candidate(symbol: str, *, now: datetime | None = None) -> Candidate:
    now = now or datetime.utcnow()
    return Candidate(
        symbol=symbol,
        state=CandidateState.DISCOVERED,
        discovered_at=now,
        last_updated_at=now,
    )
