from datetime import datetime

import pytest

from webull_bot.enums import CandidateState
from webull_bot.state_machine import InvalidStateTransition, can_transition, new_candidate, transition


def test_new_candidate_starts_discovered():
    c = new_candidate("ABCD")
    assert c.state == CandidateState.DISCOVERED
    assert c.state_history == []


def test_full_happy_path():
    c = new_candidate("ABCD")
    now = datetime.utcnow()
    for target in [
        CandidateState.WATCHING,
        CandidateState.HEATING_UP,
        CandidateState.ARMED,
        CandidateState.TRIGGERED,
        CandidateState.ENTERED,
        CandidateState.MANAGING,
        CandidateState.EXITED,
        CandidateState.COOLDOWN,
    ]:
        transition(c, target, now=now)
        assert c.state == target
    assert len(c.state_history) == 8


def test_invalid_transition_raises():
    c = new_candidate("ABCD")
    with pytest.raises(InvalidStateTransition):
        transition(c, CandidateState.ARMED)


def test_can_reject_from_most_states():
    for state in [
        CandidateState.DISCOVERED,
        CandidateState.WATCHING,
        CandidateState.HEATING_UP,
        CandidateState.ARMED,
        CandidateState.TRIGGERED,
    ]:
        assert can_transition(state, CandidateState.REJECTED)


def test_rejected_is_terminal():
    c = new_candidate("ABCD")
    transition(c, CandidateState.REJECTED)
    assert not can_transition(CandidateState.REJECTED, CandidateState.WATCHING)
