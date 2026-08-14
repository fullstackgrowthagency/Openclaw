import threading
import time

import pytest

from webull_bot.brokers.webull.retry import CallPriority, RateLimiter, call_with_retry, is_rate_limited


class _RateLimitError(Exception):
    def __str__(self):
        return "HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: Too many requests, RequestID: abc"


def test_is_rate_limited_matches_real_error_string():
    assert is_rate_limited(_RateLimitError())
    assert is_rate_limited(Exception("429"))
    assert is_rate_limited(Exception("TOO_MANY_REQUESTS"))


def test_is_rate_limited_false_for_other_errors():
    assert not is_rate_limited(Exception("HTTP Status: 401, Code: UNAUTHORIZED"))
    assert not is_rate_limited(ValueError("something else"))


# call_with_retry paces every attempt through a RateLimiter (default: the
# real global singleton, which has a 1.0s interval). Tests below pass an
# explicit zero-delay limiter so they exercise the retry logic itself
# without waiting on -- or contending with -- that shared pacing.
_NO_DELAY_LIMITER = RateLimiter(min_interval_seconds=0.0)


def test_call_with_retry_succeeds_first_try():
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert call_with_retry(fn, limiter=_NO_DELAY_LIMITER) == "ok"
    assert len(calls) == 1


def test_call_with_retry_retries_on_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr("webull_bot.brokers.webull.retry.time.sleep", lambda _: None)
    attempts = []

    def fn():
        attempts.append(1)
        if len(attempts) < 3:
            raise _RateLimitError()
        return "ok"

    assert call_with_retry(fn, max_attempts=4, base_delay=0.01, limiter=_NO_DELAY_LIMITER) == "ok"
    assert len(attempts) == 3


def test_call_with_retry_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr("webull_bot.brokers.webull.retry.time.sleep", lambda _: None)
    attempts = []

    def fn():
        attempts.append(1)
        raise _RateLimitError()

    with pytest.raises(_RateLimitError):
        call_with_retry(fn, max_attempts=3, base_delay=0.01, limiter=_NO_DELAY_LIMITER)
    assert len(attempts) == 3


def test_call_with_retry_does_not_retry_non_rate_limit_errors():
    attempts = []

    def fn():
        attempts.append(1)
        raise ValueError("not a rate limit")

    with pytest.raises(ValueError):
        call_with_retry(fn, max_attempts=4, limiter=_NO_DELAY_LIMITER)
    assert len(attempts) == 1


def test_call_with_retry_paces_every_attempt_not_just_the_first(monkeypatch):
    """Regression test for the bug found under real concurrent load: a first
    implementation paced only the call site's initial attempt, so retries
    fired on their own backoff timer outside the limiter and could still
    stack up across threads (see retry.py's module docstring, finding #4)."""
    monkeypatch.setattr("webull_bot.brokers.webull.retry.time.sleep", lambda _: None)
    limiter = RateLimiter(min_interval_seconds=0.0)
    wait_calls = []
    original_wait = limiter.wait

    def _counting_wait(priority=2):
        wait_calls.append(1)
        original_wait(priority)

    limiter.wait = _counting_wait

    attempts = []

    def fn():
        attempts.append(1)
        if len(attempts) < 3:
            raise _RateLimitError()
        return "ok"

    assert call_with_retry(fn, max_attempts=4, base_delay=0.01, limiter=limiter) == "ok"
    assert len(wait_calls) == 3  # one wait() per attempt, including the two retries


def test_rate_limiter_does_not_delay_the_first_call():
    limiter = RateLimiter(min_interval_seconds=1.0)
    start = time.monotonic()
    limiter.wait()
    assert time.monotonic() - start < 0.1


def test_rate_limiter_delays_a_call_that_comes_too_soon():
    limiter = RateLimiter(min_interval_seconds=0.2)
    limiter.wait()
    start = time.monotonic()
    limiter.wait()
    assert time.monotonic() - start >= 0.19


def test_rate_limiter_enforces_spacing_across_threads():
    limiter = RateLimiter(min_interval_seconds=0.1)
    call_times: list[float] = []
    lock = threading.Lock()

    def worker():
        limiter.wait()
        with lock:
            call_times.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    call_times.sort()
    gaps = [b - a for a, b in zip(call_times, call_times[1:])]
    assert all(gap >= 0.09 for gap in gaps), gaps


# -- priority tiers -----------------------------------------------------------

def test_rate_limiter_releases_higher_priority_waiter_first():
    # First call takes the slot immediately (nothing waiting yet); every
    # thread below then queues up *while that first slot is still
    # occupied*, all before the second real slot opens -- ensures a
    # genuine priority contest rather than everyone getting their own slot
    # in arrival order regardless of priority.
    limiter = RateLimiter(min_interval_seconds=0.15)
    order: list[str] = []
    lock = threading.Lock()
    limiter.wait()  # consumes the first slot synchronously

    def worker(label: str, priority: int, start_barrier: threading.Barrier):
        start_barrier.wait()
        limiter.wait(priority)
        with lock:
            order.append(label)

    barrier = threading.Barrier(3)
    threads = [
        threading.Thread(target=worker, args=("background", CallPriority.BACKGROUND, barrier)),
        threading.Thread(target=worker, args=("normal", CallPriority.NORMAL, barrier)),
        threading.Thread(target=worker, args=("critical", CallPriority.CRITICAL, barrier)),
    ]
    # Start background/normal first and give them time to actually queue
    # (call wait() and start blocking) before critical joins -- proves
    # critical wins even though it arrived last, not just first.
    threads[0].start()
    threads[1].start()
    time.sleep(0.05)
    threads[2].start()
    for t in threads:
        t.join(timeout=2.0)

    assert order[0] == "critical"


def test_rate_limiter_same_priority_ties_break_by_arrival_order():
    limiter = RateLimiter(min_interval_seconds=0.1)
    order: list[str] = []
    lock = threading.Lock()
    limiter.wait()  # consumes the first slot

    def worker(label: str):
        limiter.wait(CallPriority.NORMAL)
        with lock:
            order.append(label)

    first = threading.Thread(target=worker, args=("first",))
    first.start()
    time.sleep(0.02)  # ensure "first" is queued before "second" arrives
    second = threading.Thread(target=worker, args=("second",))
    second.start()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert order == ["first", "second"]


def test_rate_limiter_still_enforces_interval_regardless_of_priority():
    limiter = RateLimiter(min_interval_seconds=0.15)
    limiter.wait()
    start = time.monotonic()
    limiter.wait(CallPriority.CRITICAL)
    # A CRITICAL call still has to wait out the real interval -- priority
    # only affects contention ordering among simultaneous waiters, it's
    # not a way to bypass the pacing itself.
    assert time.monotonic() - start >= 0.14


# -- anti-starvation floor (2026-08-14) --------------------------------------
# Real incident: strict priority ordering alone let a BACKGROUND-priority
# waiter (BroadScanner discovery) be passed over indefinitely as long as
# CRITICAL-priority traffic kept arriving -- zero "passed broad scanner
# filters" log lines for an entire trading day. max_wait_seconds bounds
# this: once a ticket has waited that long, it wins the next slot
# regardless of priority.

def test_rate_limiter_bounds_background_starvation_under_sustained_critical_load():
    limiter = RateLimiter(min_interval_seconds=0.02, max_wait_seconds=0.1)
    limiter.wait()  # consumes the first slot synchronously

    stop = threading.Event()

    def critical_feeder():
        # Keeps re-queuing CRITICAL-priority work for the whole test --
        # without the anti-starvation floor, this alone would be enough to
        # keep a BACKGROUND waiter starved forever, since CRITICAL always
        # wins strict priority-order contention.
        while not stop.is_set():
            limiter.wait(CallPriority.CRITICAL)

    feeders = [threading.Thread(target=critical_feeder) for _ in range(3)]
    for feeder in feeders:
        feeder.start()
    time.sleep(0.03)  # let sustained CRITICAL contention actually establish

    started = time.monotonic()
    limiter.wait(CallPriority.BACKGROUND)
    elapsed = time.monotonic() - started

    stop.set()
    for feeder in feeders:
        feeder.join(timeout=2.0)

    # Bounded by roughly max_wait_seconds plus a little scheduling slack --
    # NOT the tens of iterations (or worse) it would take strict priority
    # order alone to ever let BACKGROUND through against 3 continuously
    # re-queuing CRITICAL feeders.
    assert elapsed < 0.5


def test_rate_limiter_default_max_wait_does_not_affect_ordinary_priority_contention():
    # The aging floor must not change the outcome of a normal, short-lived
    # priority contest (see test_rate_limiter_releases_higher_priority_waiter_first)
    # -- it should only ever matter once a waiter has genuinely been queued
    # for max_wait_seconds, never sooner.
    limiter = RateLimiter(min_interval_seconds=0.05)
    assert limiter.max_wait_seconds == 30.0  # the documented default
    order: list[str] = []
    lock = threading.Lock()
    limiter.wait()

    def worker(label: str, priority: int, start_barrier: threading.Barrier):
        start_barrier.wait()
        limiter.wait(priority)
        with lock:
            order.append(label)

    barrier = threading.Barrier(2)
    threads = [
        threading.Thread(target=worker, args=("background", CallPriority.BACKGROUND, barrier)),
        threading.Thread(target=worker, args=("critical", CallPriority.CRITICAL, barrier)),
    ]
    threads[0].start()
    time.sleep(0.02)
    threads[1].start()
    for t in threads:
        t.join(timeout=2.0)

    assert order[0] == "critical"


# -- exclusive() -- real incident (CYCU/SCKT/BIVI, 2026-08-12): CallPriority
# alone doesn't help once several genuinely CRITICAL calls are simultaneously
# in flight -- they still compete with each other for the same slots.
# exclusive() gives one thread's order-placement call exclusive access to the
# whole limiter, regardless of any other thread's priority.

def test_exclusive_blocks_other_threads_regardless_of_priority():
    limiter = RateLimiter(min_interval_seconds=0.0)
    order: list[str] = []
    lock = threading.Lock()
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def holder():
        with limiter.exclusive():
            holder_ready.set()
            release_holder.wait(timeout=2.0)
        with lock:
            order.append("holder-released")

    def critical_waiter():
        holder_ready.wait(timeout=2.0)
        limiter.wait(CallPriority.CRITICAL)
        with lock:
            order.append("critical")

    holder_thread = threading.Thread(target=holder)
    waiter_thread = threading.Thread(target=critical_waiter)
    holder_thread.start()
    holder_ready.wait(timeout=2.0)
    waiter_thread.start()
    time.sleep(0.1)  # give the waiter a real chance to (wrongly) slip through
    with lock:
        assert order == []  # still blocked despite being CRITICAL priority
    release_holder.set()
    holder_thread.join(timeout=2.0)
    waiter_thread.join(timeout=2.0)

    assert order == ["holder-released", "critical"]


def test_exclusive_does_not_block_the_holders_own_thread():
    limiter = RateLimiter(min_interval_seconds=0.0)
    calls = []
    with limiter.exclusive():
        # Simulates call_with_retry's own paced attempts happening inside
        # an exclusive() block -- the holder's own thread must never be
        # blocked by its own hold.
        limiter.wait(CallPriority.CRITICAL)
        calls.append(1)
        limiter.wait(CallPriority.CRITICAL)
        calls.append(2)
    assert calls == [1, 2]


def test_exclusive_releases_on_exception():
    limiter = RateLimiter(min_interval_seconds=0.0)
    with pytest.raises(RuntimeError):
        with limiter.exclusive():
            raise RuntimeError("simulated failure mid-order")

    # Must not be left permanently held -- a fresh acquire must succeed
    # immediately, not hang.
    acquired = threading.Event()

    def acquirer():
        with limiter.exclusive():
            acquired.set()

    t = threading.Thread(target=acquirer)
    t.start()
    t.join(timeout=2.0)
    assert acquired.is_set()


def test_exclusive_serializes_concurrent_holders():
    limiter = RateLimiter(min_interval_seconds=0.0)
    active_count = 0
    max_concurrent = 0
    lock = threading.Lock()

    def worker():
        nonlocal active_count, max_concurrent
        with limiter.exclusive():
            with lock:
                active_count += 1
                max_concurrent = max(max_concurrent, active_count)
            time.sleep(0.05)
            with lock:
                active_count -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)

    assert max_concurrent == 1


def test_call_with_retry_passes_priority_through_to_the_limiter():
    seen_priorities = []

    class _RecordingLimiter(RateLimiter):
        def wait(self, priority=CallPriority.NORMAL):
            seen_priorities.append(priority)

    limiter = _RecordingLimiter(min_interval_seconds=0.0)
    call_with_retry(lambda: "ok", limiter=limiter, priority=CallPriority.CRITICAL)

    assert seen_priorities == [CallPriority.CRITICAL]
