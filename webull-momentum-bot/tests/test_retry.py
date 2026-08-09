import threading
import time

import pytest

from webull_bot.brokers.webull.retry import RateLimiter, call_with_retry, is_rate_limited


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

    def _counting_wait():
        wait_calls.append(1)
        original_wait()

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
