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
        CandidateState.CONFIRMING,
        CandidateState.TRIGGERED,
        CandidateState.ENTERED,
        CandidateState.MANAGING,
        CandidateState.EXITED,
        CandidateState.COOLDOWN,
    ]:
        transition(c, target, now=now)
        assert c.state == target
    assert len(c.state_history) == 9


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
        CandidateState.CONFIRMING,
        CandidateState.TRIGGERED,
    ]:
        assert can_transition(state, CandidateState.REJECTED)


def test_rejected_is_terminal():
    c = new_candidate("ABCD")
    transition(c, CandidateState.REJECTED)
    assert not can_transition(CandidateState.REJECTED, CandidateState.WATCHING)


# -- entry-selectivity rework (2026-08-13): ARMED must route through CONFIRMING --

def test_armed_cannot_jump_directly_to_triggered():
    # The whole point of the rework: a strategy trigger must hold through a
    # confirmation window before ever becoming an order -- see
    # enums.CandidateState.CONFIRMING's docstring.
    assert not can_transition(CandidateState.ARMED, CandidateState.TRIGGERED)


def test_armed_to_confirming_to_triggered_is_legal():
    c = new_candidate("ABCD")
    transition(c, CandidateState.WATCHING)
    transition(c, CandidateState.HEATING_UP)
    transition(c, CandidateState.ARMED)
    transition(c, CandidateState.CONFIRMING)
    assert c.state == CandidateState.CONFIRMING
    transition(c, CandidateState.TRIGGERED)
    assert c.state == CandidateState.TRIGGERED


def test_confirming_reverts_to_armed_on_failure():
    c = new_candidate("ABCD")
    transition(c, CandidateState.WATCHING)
    transition(c, CandidateState.HEATING_UP)
    transition(c, CandidateState.ARMED)
    transition(c, CandidateState.CONFIRMING)
    transition(c, CandidateState.ARMED, reason="confirmation failed")
    assert c.state == CandidateState.ARMED
