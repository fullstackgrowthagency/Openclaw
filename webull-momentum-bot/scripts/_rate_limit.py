"""
Shared rate-limit retry/backoff helper for one-off ops scripts that place
real orders against the live Webull sandbox/live API directly (not through
TradingLoop's own batched, paced request flow).

The live bot (if still running) shares this same account's Webull rate
limiter (~1 req/s sustained, regardless of which process is calling -- see
WebullBrokerClient.get_snapshots' docstring). A script that fires several
requests back-to-back with no pacing of its own can collide with the bot's
own concurrent polling and get a 429 TOO_MANY_REQUESTS mid-run -- confirmed
live twice now: once closing 3 positions with
scripts/list_and_close_positions.py (fixed by adding this), and again with
scripts/verify_bracket_orders.py's very first version, which didn't import
this module and hit the identical wall on its very first live run. Import
this from any new script that places more than one order in a row instead
of writing the retry loop again.
"""
import time

RATE_LIMIT_MARKERS = ("TOO_MANY_REQUESTS", "429")
RETRY_DELAYS = (3.0, 6.0, 12.0, 20.0)


def call_with_retry(fn, *, label: str):
    """Calls fn() and retries with backoff ONLY on what looks like a
    rate-limit error (429 / TOO_MANY_REQUESTS somewhere in the exception),
    since blindly retrying every kind of failure here (e.g. a genuine order
    rejection) would be wrong. Re-raises immediately for anything else, and
    re-raises the last error if every retry is also rate-limited."""
    last_exc = None
    for attempt, delay in enumerate((0.0,) + RETRY_DELAYS):
        if delay:
            print(f"    ({label}: rate-limited, waiting {delay:.0f}s before retry {attempt}/{len(RETRY_DELAYS)})")
            time.sleep(delay)
        try:
            return fn()
        except Exception as exc:
            if not any(marker in str(exc) for marker in RATE_LIMIT_MARKERS):
                raise
            last_exc = exc
    raise last_exc
