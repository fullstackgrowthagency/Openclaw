"""Tests for runtime/loop_registry.py's LoopRegistry -- the {user_id:
TradingLoop} manager that replaced scripts/run_dashboard.py's old single
build_trading_loop() call. Uses a fake loop (not a real TradingLoop/
broker) so these tests only exercise the registry's own start/stop/
lookup/crash-isolation logic."""
from __future__ import annotations

import threading
import time

import pytest

from webull_bot.config import Settings
from webull_bot.runtime.loop_registry import LoopRegistry


class _FakeLoop:
    def __init__(self, user_id, *, raise_immediately=False):
        self.user_id = user_id
        self.raise_immediately = raise_immediately
        self.run_forever_called = threading.Event()

    def run_forever(self, stop_flag=None):
        self.run_forever_called.set()
        if self.raise_immediately:
            raise RuntimeError(f"boom for user {self.user_id}")
        while stop_flag is not None and not stop_flag():
            time.sleep(0.01)


@pytest.fixture
def settings():
    return Settings()


def test_start_for_user_builds_and_runs_a_loop(settings):
    built = {}

    def factory(user_id, s):
        loop = _FakeLoop(user_id)
        built[user_id] = loop
        return loop

    registry = LoopRegistry(factory)
    loop = registry.start_for_user(1, settings)
    assert loop is built[1]
    assert loop.run_forever_called.wait(timeout=1.0)
    registry.stop_for_user(1)


def test_start_for_user_is_a_noop_if_already_running(settings):
    call_count = 0

    def factory(user_id, s):
        nonlocal call_count
        call_count += 1
        return _FakeLoop(user_id)

    registry = LoopRegistry(factory)
    first = registry.start_for_user(1, settings)
    second = registry.start_for_user(1, settings)
    assert first is second
    assert call_count == 1
    registry.stop_for_user(1)


def test_get_returns_none_for_a_user_with_no_running_loop(settings):
    registry = LoopRegistry(lambda user_id, s: _FakeLoop(user_id))
    assert registry.get(999) is None


def test_get_returns_the_running_loop(settings):
    registry = LoopRegistry(lambda user_id, s: _FakeLoop(user_id))
    loop = registry.start_for_user(1, settings)
    assert registry.get(1) is loop
    registry.stop_for_user(1)


def test_running_user_ids_reflects_started_and_stopped_loops(settings):
    registry = LoopRegistry(lambda user_id, s: _FakeLoop(user_id))
    registry.start_for_user(1, settings)
    registry.start_for_user(2, settings)
    assert sorted(registry.running_user_ids()) == [1, 2]

    registry.stop_for_user(1)
    assert registry.running_user_ids() == [2]
    registry.stop_for_user(2)


def test_stop_for_user_joins_the_thread(settings):
    registry = LoopRegistry(lambda user_id, s: _FakeLoop(user_id))
    registry.start_for_user(1, settings)
    registry.stop_for_user(1, timeout=2.0)
    assert registry.get(1) is None
    assert registry.running_user_ids() == []


def test_stop_for_user_is_a_noop_when_nothing_is_running(settings):
    registry = LoopRegistry(lambda user_id, s: _FakeLoop(user_id))
    registry.stop_for_user(404)  # must not raise


def test_two_users_get_fully_independent_loops(settings):
    built = {}

    def factory(user_id, s):
        loop = _FakeLoop(user_id)
        built[user_id] = loop
        return loop

    registry = LoopRegistry(factory)
    loop1 = registry.start_for_user(1, settings)
    loop2 = registry.start_for_user(2, settings)
    assert loop1 is not loop2
    assert loop1.user_id == 1
    assert loop2.user_id == 2
    registry.stop_all()
    assert registry.running_user_ids() == []


def test_a_crashing_loop_does_not_affect_other_users(settings):
    def factory(user_id, s):
        return _FakeLoop(user_id, raise_immediately=(user_id == 1))

    registry = LoopRegistry(factory)
    crashing_loop = registry.start_for_user(1, settings)
    healthy_loop = registry.start_for_user(2, settings)

    assert crashing_loop.run_forever_called.wait(timeout=1.0)
    # Give the crashing thread a moment to hit its except block and clean
    # itself out of the registry.
    for _ in range(100):
        if registry.get(1) is None:
            break
        time.sleep(0.01)

    assert registry.get(1) is None  # crashed loop removed itself
    assert registry.get(2) is healthy_loop  # unaffected
    registry.stop_for_user(2)


def test_stop_all_stops_every_running_user(settings):
    registry = LoopRegistry(lambda user_id, s: _FakeLoop(user_id))
    for user_id in (1, 2, 3):
        registry.start_for_user(user_id, settings)
    registry.stop_all(timeout=2.0)
    assert registry.running_user_ids() == []
