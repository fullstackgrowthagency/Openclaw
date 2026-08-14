"""
Shared rate-limit handling for Webull OpenAPI calls: a proactive,
priority-aware pacer (RateLimiter) plus a reactive safety net
(call_with_retry), with every *attempt* -- including retries -- going
through the same pacer.

Confirmed live (2026-08-08), in order of discovery:
  1. Firing 10 truly concurrent get_snapshot calls at the sandbox tripped a
     429 on 2 of them, with zero data corruption or cross-request mixups --
     the SDK is safe to call from multiple threads.
  2. A short burst test alone understated the real constraint. Sustained
     sequential calls at fixed spacing showed the sandbox enforces
     something close to a **1 request/second** sustained rate, independent
     of concurrency level or any other provider's (e.g. FMP's) limits:
     0.5s spacing -> 0/20 errors; 0.3s spacing -> 6/20 errors (30%).
     NOTE (2026-08-11): Webull's own published docs list the Market Data
     API's rate limit as "300 requests per minute" (5/s average) -- flatly
     contradicted by the above, which measured real 429s at spacing far
     looser than that (0.3s ~ 3.3 req/s, already 30% errors; 300/min would
     be sustainable at ~1 req/s or faster with room to spare if the doc
     were accurate for this account/environment). This is now the third
     time this project has found Webull's docs disagree with live sandbox
     behavior (see also `support_trading_session` in client.py, and the
     `get_open_orders` vs `get_order_open` method-name mismatch) -- the
     measured 1 req/s figure below is kept exactly because it's proven
     against this real account, not because the doc is assumed wrong in
     general. Re-verify only via another live measurement, never by
     trusting a published number over an unreproduced one.
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

Priority tiers (2026-08-11): every Webull endpoint this client calls --
market data AND order/account/position management -- shares this exact
same account-wide budget (confirmed live: the sandbox's ~1 req/s ceiling
was observed to be per-account, not per-endpoint). Previously only
market_data.* calls went through this pacer at all; order_v3.*/account_v2.*
calls (place_order, cancel_order, get_order_status, get_positions, ...)
had zero rate-limit protection of their own, meaning a burst of market-data
polling could starve out a stop-loss cancel or an entry-fill confirmation
with no retry to fall back on. Now every Webull call in this codebase goes
through the same shared `webull_limiter`, and RateLimiter is
priority-aware: when several threads are simultaneously waiting for the
next available slot, the most urgent one goes next, not whoever happened
to call wait() first. See CallPriority for the three tiers and
docs/ARCHITECTURE.md's "Priority-tiered rate limiting" section for which
call sites use which tier and why.
"""
from __future__ import annotations

import contextlib
import heapq
import itertools
import logging
import random
import threading
import time
from enum import IntEnum
from typing import Callable, Iterator, Optional, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class CallPriority(IntEnum):
    """Lower value = more urgent = wins contention for the next available
    rate-limiter slot. See RateLimiter.wait's docstring for the mechanism.

    Assigned per call site, not per endpoint -- the same get_order_status
    call is CRITICAL when polling a pending exit (money already at risk)
    but only NORMAL when polling a pending entry (not yet at risk; a few
    extra seconds of TRIGGERED is a UX/latency cost, not a safety one)."""

    # Exit/stop-loss management: pending-exit polling, broker-bracket fill
    # polling, cancelling/replacing a resting protective order, and the
    # entry-fill confirmation path once an order has actually filled
    # (get_positions() for avg_entry_price/quantity). Anything where a
    # delay directly means a position sits unprotected or untracked longer
    # than it has to.
    CRITICAL = 1
    # Pending-entry polling, the 10s entry-position-verification check,
    # periodic position reconciliation, account equity/buying-power reads
    # for sizing. Matters, but no money is at risk yet if delayed briefly.
    NORMAL = 2
    # Universe discovery/rescan and periodic resistance-level refresh --
    # finding or re-evaluating NOT-yet-triggered candidates. Free to wait
    # behind everything above; this is the traffic that used to (and, for
    # any endpoint that isn't yet paced, still can) starve out
    # money-at-risk calls under load.
    BACKGROUND = 3


class RateLimiter:
    """Thread-safe, priority-aware pacer: blocks the calling thread until
    at least `min_interval_seconds` has elapsed since the last call *by any
    thread* returned from wait(), and, when multiple threads are waiting
    simultaneously for that next slot, releases the highest-priority
    (lowest CallPriority value) one first -- ties broken by arrival order,
    so same-priority calls still queue fairly.

    Anti-starvation floor (2026-08-14): strict priority ordering alone
    means a waiting BACKGROUND ticket (BroadScanner discovery) can be
    passed over indefinitely as long as CRITICAL/NORMAL traffic keeps
    arriving -- confirmed live the same day (see brokers/webull/client.py's
    _STREAMING_SUBSCRIBE_BATCH_SIZE comment for the related incident this
    was discovered alongside): zero "passed broad scanner filters" log
    lines for an entire trading day, strongly suggesting discovery's own
    BACKGROUND-priority calls were starved out completely by a flood of
    CRITICAL-priority REST-polling fallback traffic. `max_wait_seconds`
    bounds this: once ANY waiting ticket has been queued that long, it
    wins the next available slot regardless of priority, guaranteeing
    every call -- however low its priority -- eventually gets forward
    progress instead of only when priority-order happens to reach it.

    Implementation: a single lock/condition variable guards a min-heap of
    waiting tickets `(priority, sequence_number)`, plus an arrival-time map
    keyed by sequence number. Each waiting thread holds its own ticket and,
    each time it wakes (on a bounded timeout, or a notify from whichever
    thread just got a slot), calls _select_winner to determine who gets
    the next slot -- normally the heap's minimum (highest priority), but
    the single longest-waiting ticket overall if it has aged past
    max_wait_seconds. This is a bounded busy-recheck, not an unbounded
    spin: the condition variable's timeout is always set to (at most) the
    remaining pacing interval, or max_wait_seconds as a worst-case safety-
    net poll when a slot is already free but this thread isn't (yet) the
    winner -- see wait()'s inline comment for why that fallback timeout
    (rather than blocking forever on notify alone) is what makes the aging
    guarantee actually hold under real scheduling, not just in the common
    case where the eventual winner's own thread happens to be the one that
    wakes up to claim it."""

    def __init__(self, min_interval_seconds: float, max_wait_seconds: float = 30.0):
        self.min_interval_seconds = min_interval_seconds
        # See class docstring's "Anti-starvation floor" section. 30s is a
        # starting value, not backtested: comfortably shorter than a human
        # would ever notice as "the bot stopped discovering," comfortably
        # longer than the handful of seconds a legitimately busy CRITICAL-
        # priority stretch (a burst of simultaneous entries/exits) should
        # ever realistically last.
        self.max_wait_seconds = max_wait_seconds
        self._condition = threading.Condition()
        self._last_call_at: float = 0.0
        self._waiting: list[tuple[int, int]] = []
        self._counter = itertools.count()
        # ticket sequence number -> time.monotonic() it was enqueued, used
        # by _select_winner to find the single longest-waiting ticket
        # regardless of priority. Populated when a ticket is pushed onto
        # _waiting, removed whenever that ticket leaves _waiting (claimed
        # or cancelled) -- always kept in sync with _waiting's membership.
        self._arrival_times: dict[int, float] = {}
        # Real incident (CYCU/SCKT/BIVI, 2026-08-12): CallPriority already
        # lets a CRITICAL call win contention against BACKGROUND traffic,
        # but does nothing when SEVERAL genuinely CRITICAL calls (a stuck
        # exit retry, a bracket-attach retry, reconcile's get_positions,
        # ...) are simultaneously in flight -- they just compete with each
        # other for the same ~1 req/s ceiling like any other same-priority
        # tickets, and the account-wide limit gets exceeded regardless of
        # internal ordering. `exclusive()` below is a stronger mechanism
        # for the highest-stakes moment of all -- actually placing an
        # order -- than priority alone can provide: while held, every
        # OTHER thread's wait() call blocks outright, at any priority,
        # until the holder is done, so an order submission (including its
        # own internal call_with_retry attempts) gets the shared budget
        # entirely to itself instead of competing with concurrent
        # discovery/reconcile/other-order traffic for the same slots.
        self._exclusive_holder: Optional[int] = None

    def _select_winner(self, now: float) -> tuple[int, int]:
        """Which waiting ticket gets the next available slot: the heap's
        priority-order minimum, UNLESS some other waiting ticket has been
        queued for at least max_wait_seconds, in which case THAT ticket
        (the single longest-waiting one overall) wins instead -- see this
        class's docstring for the starvation this bounds. O(n) in the
        number of concurrently waiting threads, which in practice is small
        (bounded by this codebase's own thread pool/call-site counts).
        Callers must hold self._condition."""
        priority_winner = self._waiting[0]
        oldest = min(self._waiting, key=lambda t: self._arrival_times.get(t[1], now))
        if oldest == priority_winner:
            return priority_winner
        if now - self._arrival_times.get(oldest[1], now) >= self.max_wait_seconds:
            return oldest
        return priority_winner

    def wait(self, priority: int = CallPriority.NORMAL) -> None:
        with self._condition:
            ticket = (int(priority), next(self._counter))
            heapq.heappush(self._waiting, ticket)
            self._arrival_times[ticket[1]] = time.monotonic()
            try:
                while True:
                    # Exclusive mode: every thread except the holder waits
                    # here regardless of its own priority or position in
                    # the heap -- see exclusive()'s docstring. The holder's
                    # own thread (e.g. paced retries of the same order
                    # call) is exempt and proceeds through the normal
                    # ticket/pacing logic below exactly as if no exclusive
                    # holder existed.
                    if self._exclusive_holder is not None and self._exclusive_holder != threading.get_ident():
                        self._condition.wait(timeout=self.min_interval_seconds)
                        continue
                    now = time.monotonic()
                    elapsed = now - self._last_call_at
                    if elapsed >= self.min_interval_seconds and self._select_winner(now) == ticket:
                        self._waiting.remove(ticket)
                        heapq.heapify(self._waiting)
                        self._arrival_times.pop(ticket[1], None)
                        self._last_call_at = time.monotonic()
                        self._condition.notify_all()
                        return
                    remaining = self.min_interval_seconds - elapsed
                    # A losing thread normally wakes on the winner's own
                    # notify_all() once it claims a slot, which under
                    # sustained load happens roughly every
                    # min_interval_seconds -- frequent enough to re-check
                    # aging well within max_wait_seconds in practice. But
                    # relying on that alone would leave the ACTUAL eventual
                    # winner (once its wait crosses max_wait_seconds and
                    # _select_winner starts returning it instead of the
                    # priority-order ticket) stuck asleep with no notify
                    # ever coming, if nothing else happens to wake it --
                    # the priority-order ticket's own thread would just
                    # keep losing and re-sleeping too. Falling back to
                    # max_wait_seconds here (rather than blocking
                    # indefinitely on notify alone) is what makes this
                    # thread self-recover and re-check even in that
                    # worst case, bounding every waiter's staleness to at
                    # most max_wait_seconds regardless of what any other
                    # thread happens to do.
                    timeout = remaining if remaining > 0 else self.max_wait_seconds
                    self._condition.wait(timeout=timeout)
            except BaseException:
                # A waiter that never reaches the normal return path (e.g.
                # interrupted) must not leave a phantom ticket stuck in
                # _waiting, which would permanently block every other
                # thread behind it.
                try:
                    self._waiting.remove(ticket)
                    heapq.heapify(self._waiting)
                    self._arrival_times.pop(ticket[1], None)
                    self._condition.notify_all()
                except ValueError:
                    pass
                raise

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        """Acquires exclusive access to this limiter for the calling
        thread: every OTHER thread's wait() call blocks -- regardless of
        its own priority -- until this context exits, so a critical
        sequence of calls (placing an order, including all of its own
        internal call_with_retry attempts) gets the account-wide
        rate-limit budget entirely to itself for that stretch instead of
        competing with concurrent discovery/reconcile/other-order traffic
        for the same slots. See the real incident this fixes in
        __init__'s docstring.

        Reentrant-safe for the SAME thread (wait() only blocks OTHER
        threads, via threading.get_ident() -- a thread already holding
        exclusive access that calls exclusive() again, or calls wait()
        directly, is never blocked by its own hold), but not designed to
        be literally nested (the inner exit would release the outer
        hold too, since there's only one holder slot) -- callers should
        wrap the whole order-placement sequence in one exclusive() block,
        not multiple nested ones.

        Blocks waiting for a PRIOR holder (a concurrent order submission
        already in flight on another thread) to finish before acquiring
        -- unbounded by design, same as every other wait in this class;
        an order submission is expected to complete (successfully or by
        exhausting its own retries) in a bounded, short time, not hang
        forever."""
        with self._condition:
            while self._exclusive_holder is not None:
                self._condition.wait()
            self._exclusive_holder = threading.get_ident()
        try:
            yield
        finally:
            with self._condition:
                self._exclusive_holder = None
                self._condition.notify_all()


# 1.0s -- matches the ~1 req/s sustained rate measured in the sequential
# test (see module docstring #2), which takes priority over Webull's own
# published "300 requests per minute" figure for the reasons explained
# there. The actual fix for the concurrent-load failures (#4/#5 above) was
# pacing every retry attempt through this same limiter, not this number;
# it's set to the measured rate itself rather than a number below it since
# call_with_retry now provides the margin via real pacing on every attempt,
# not just the first.
#
# Shared across EVERY Webull call this codebase makes -- market data AND
# order/account/position management alike (see this module's docstring's
# "Priority tiers" section) -- since the account-wide budget it's pacing
# against is a single shared ceiling, not one per endpoint. max_wait_seconds
# left at RateLimiter's own default (30s) -- see that default's docstring
# and the class docstring's "Anti-starvation floor" section for why this
# specific value matters for the shared singleton: it's what guarantees
# BroadScanner's BACKGROUND-priority discovery calls can never again be
# starved out entirely by sustained CRITICAL/NORMAL trading traffic, the
# way they appear to have been for the entirety of 2026-08-14.
webull_limiter = RateLimiter(min_interval_seconds=1.0)

# Backward-compatible alias -- webull_market_data_limiter was the name used
# while only market_data.* calls were paced through this. Kept so any
# external reference (scripts, in-flight branches) doesn't silently start
# pointing at a different limiter instance; both names are the exact same
# object, not two separate budgets.
webull_market_data_limiter = webull_limiter


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
    priority: int = CallPriority.NORMAL,
) -> _T:
    """Paces every attempt (including retries) through `limiter`
    (defaults to the shared `webull_limiter`) and retries only on Webull's
    rate-limit error, with exponential backoff plus jitter on top of that
    pacing. Pacing every attempt -- not just the first -- is what makes
    this safe under real concurrency: an un-paced retry is a real request
    that can land back-to-back with another thread's retry and re-trigger
    the same 429 it was meant to recover from (see the module docstring,
    finding #4).

    `priority`: see CallPriority. Passed straight through to the limiter's
    own wait() on every attempt, including retries -- a retry of a
    CRITICAL call stays CRITICAL, it doesn't fall back to the queue at
    NORMAL priority just because the first attempt failed."""
    limiter = limiter or webull_limiter
    for attempt in range(max_attempts):
        limiter.wait(priority)
        try:
            return fn()
        except Exception as exc:
            if not is_rate_limited(exc) or attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.25)
            logger.warning("Webull rate limit hit (attempt %d/%d); retrying in %.2fs.", attempt + 1, max_attempts, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")  # loop above always returns or raises
