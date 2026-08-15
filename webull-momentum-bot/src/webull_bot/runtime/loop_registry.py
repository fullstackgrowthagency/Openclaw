"""Manages one TradingLoop per user (2026-08-15 multi-tenant conversion),
keyed by user_id -- the process-wide replacement for the old single
build_trading_loop() call scripts/run_dashboard.py used to make exactly
once at startup. Mirrors that exact daemon-thread pattern, just looped
over N users' verified BrokerCredentials instead of one hardcoded .env
account.

In-process multi-threaded, not one OS process per user: TradingLoop/
RiskEngine/PositionManager/WebullBrokerClient are already fully separate
object graphs per build_trading_loop() call (confirmed during this
conversion's planning), so most of the isolation benefit of a subprocess
is already free -- a real subprocess-per-user model would instead require
rewriting dashboard/app.py's entire live-state read path (today it reads
straight off a TradingLoop's in-memory attributes, no DB round-trip) into
cross-process RPC, a much bigger and unjustified change at the scale this
runs at (one small VPS). Revisit only if real measured CPU/RAM pressure
from concurrently-running users demands it.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from ..config import Settings
from .trading_loop import TradingLoop

logger = logging.getLogger(__name__)

_DEFAULT_STOP_JOIN_TIMEOUT_SECONDS = 10.0


class LoopRegistry:
    """Thread-safe {user_id: TradingLoop} registry. `loop_factory(user_id,
    settings) -> TradingLoop` is injected (same DI pattern as
    dashboard/app.py's create_app/build_auth_router) rather than this
    class importing build_trading_loop directly, so the caller (
    scripts/run_dashboard.py) decides exactly which persistence hooks
    (on_trade_closed/on_order_update/etc., each user_id-scoped -- see
    db/repository.py) get threaded into each user's loop, and tests can
    inject a fake."""

    def __init__(self, loop_factory: Callable[[int, Settings], TradingLoop]):
        self._loop_factory = loop_factory
        self._lock = threading.Lock()
        self._loops: dict[int, TradingLoop] = {}
        self._threads: dict[int, threading.Thread] = {}
        self._stop_flags: dict[int, threading.Event] = {}

    def start_for_user(self, user_id: int, settings: Settings) -> TradingLoop:
        """No-op if this user already has a running loop -- callers (the
        broker-credential save/test endpoints, and this process's own
        startup sweep over every verified user) don't need to check
        running_user_ids() first."""
        with self._lock:
            existing = self._loops.get(user_id)
            if existing is not None:
                return existing
            loop = self._loop_factory(user_id, settings)
            stop_flag = threading.Event()
            thread = threading.Thread(
                target=self._run_loop_safely, args=(user_id, loop, stop_flag),
                daemon=True, name=f"trading-loop-user-{user_id}",
            )
            self._loops[user_id] = loop
            self._threads[user_id] = thread
            self._stop_flags[user_id] = stop_flag
            thread.start()
            logger.info("Started TradingLoop for user_id=%s.", user_id)
            return loop

    def _run_loop_safely(self, user_id: int, loop: TradingLoop, stop_flag: threading.Event) -> None:
        """A crash in one user's loop must not take down the process (every
        other user's loop is a fully independent thread/object graph) and
        must not go unnoticed either -- threading.Thread's own default
        behavior for an uncaught exception is to print a traceback to
        stderr and quietly let the thread die, easy to miss in a long-running
        service's logs. logger.exception here guarantees it's logged loudly
        via this project's normal logging config, not just stderr."""
        try:
            loop.run_forever(stop_flag=stop_flag.is_set)
        except Exception:
            logger.exception("TradingLoop for user_id=%s crashed and stopped.", user_id)
        finally:
            with self._lock:
                self._loops.pop(user_id, None)
                self._threads.pop(user_id, None)
                self._stop_flags.pop(user_id, None)

    def stop_for_user(self, user_id: int, timeout: float = _DEFAULT_STOP_JOIN_TIMEOUT_SECONDS) -> None:
        with self._lock:
            stop_flag = self._stop_flags.get(user_id)
            thread = self._threads.get(user_id)
        if stop_flag is None:
            return  # not running -- nothing to stop
        stop_flag.set()
        if thread is not None:
            thread.join(timeout=timeout)
        # _run_loop_safely's own finally block removes this user_id from
        # every dict once run_forever actually returns -- not repeated
        # here, to avoid a race where this method's cleanup and that
        # thread's own cleanup disagree about whether the entry still
        # exists.
        logger.info("Stopped TradingLoop for user_id=%s.", user_id)

    def get(self, user_id: int) -> Optional[TradingLoop]:
        with self._lock:
            return self._loops.get(user_id)

    def running_user_ids(self) -> list[int]:
        with self._lock:
            return list(self._loops.keys())

    def stop_all(self, timeout: float = _DEFAULT_STOP_JOIN_TIMEOUT_SECONDS) -> None:
        for user_id in self.running_user_ids():
            self.stop_for_user(user_id, timeout=timeout)
