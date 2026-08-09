"""
Shared rate-limit handling for Webull OpenAPI calls: a proactive pacer
(RateLimiter) plus a reactive safety net (call_with_retry), with every
*attempt* -- including retries -- going through the same pacer.

Confirmed live (2026-08-08), in order of discovery:
  1. Firing 10 truly concurrent get_snapshot calls at the sandbox tripped a
     429 on 2 of them, with zero data corruption or cross-request mixups --
     the SDK is safe to call from multiple threads.
  2. A short burst test alone understated the real constraint. Sustained
     sequential calls at fixed spacing showed the sandbox enforces
     something close to a **1 request/second** sustained rate, independent
     of concurrency level or any other provider's (e.g. FMP's) limits:
     0.5s spacing -> 0/20 errors; 0.3s spacing -> 6/20 errors (30%).
  3. A real 149-symbol scan using 5 concurrent workers with only reactive
     retry (no pacing) hit 101 rate-limit errors before even finishing --
     reactive backoff alone doesn't scale, since retries themselves add
     more delayed requests into an already-saturated window.
  4. A first RateLimiter cut paced only the *first* attempt of each call
     (call sites did `limiter.wait()` once, then `call_with_retry(fn)`).
     At 0.6s spacing with 10 concurrent workers (BroadScanner's real
     config), 9 of 60 calls still failed even after exhausting
     call_with_retry's 4 attempts. Raising the interval to 1.0s with that
     same structure made **no measurable difference** (still 9/60) --
     which pointed at the structure, not the number: retries triggered
     inside call_with_retry ran on their own exponential-backoff timer,
     completely outside the pacer. Under concurrency, several threads'
     retries could still land back-to-back and re-trigger each other's
     429s, exactly the failure mode from #3, just recreated one layer
     lower where RateLimiter couldn't see it.
  5. Fix: call_with_retry now calls the limiter before *every* attempt,
     not just the first, so a retry queues on the same global pacer as any
     other call rather than firing on its own schedule. This is the
     structural fix; 1.0s is kept as the interval since it was never shown
     to be the problem.

Conclusion: concurrency helps overlap *other* work (e.g. the FMP float
lookup for a symbol that already passed the Webull check) but must not be
allowed to exceed Webull's own sustained rate -- and that includes retries,
which are real requests too. RateLimiter enforces pacing globally (shared
across all threads/callers, and now across all attempts of a given call)
so BroadScanner's thread pool doesn't need to -- and can't accidentally
forget to -- get this right itself.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class RateLimiter:
    """Thread-safe: blocks the calling thread until at least `min_interval_seconds`
    has elapsed since the last call *by any thread* returned from wait()."""

    def __init__(self, min_interval_seconds: float):
        self.min_interval_seconds = min_interval_seconds
        self._lock = threading.Lock()
        self._last_call_at: float = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self.min_interval_seconds - (now - self._last_call_at)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last_call_at = time.monotonic()


# 1.0s -- matches the ~1 req/s sustained rate measured in the sequential
# test (see module docstring #2). The actual fix for the concurrent-load
# failures (#4/#5 above) was pacing every retry attempt through this same
# limiter, not this number; it's set to the measured rate itself rather
# than a number below it since call_with_retry now provides the margin via
# real pacing on every attempt, not just the first.
webull_market_data_limiter = RateLimiter(min_interval_seconds=1.0)


def is_rate_limited(exc: Exception) -> bool:
    # webull.core.exception.exceptions.ServerException stringifies as
    # "HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: ..." -- no dedicated
    # exception subclass or attribute for this was found in the SDK, so
    # matching on the stringified message is what's actually available.
    message = str(exc)
    return "429" in message or "TOO_MANY_REQUESTS" in message


def call_with_retry(
    fn: Callable[[], _T],
    *,
    max_attempts: int = 4,
    base_delay: float = 0.5,
    limiter: Optional[RateLimiter] = None,
) -> _T:
    """Paces every attempt (including retries) through `limiter`
    (defaults to the shared `webull_market_data_limiter`) and retries only
    on Webull's rate-limit error, with exponential backoff plus jitter on
    top of that pacing. Pacing every attempt -- not just the first -- is
    what makes this safe under real concurrency: an un-paced retry is a
    real request that can land back-to-back with another thread's retry
    and re-trigger the same 429 it was meant to recover from (see the
    module docstring, finding #4)."""
    limiter = limiter or webull_market_data_limiter
    for attempt in range(max_attempts):
        limiter.wait()
        try:
            return fn()
        except Exception as exc:
            if not is_rate_limited(exc) or attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.25)
            logger.warning("Webull rate limit hit (attempt %d/%d); retrying in %.2fs.", attempt + 1, max_attempts, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")  # loop above always returns or raises
