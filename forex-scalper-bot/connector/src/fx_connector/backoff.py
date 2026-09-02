"""
Reconnect backoff for RelayClient.run_forever(). Mirrors the SHAPE of
webull_bot's brokers/webull/retry.py::call_with_retry (base delay,
doubling multiplier, jitter) but not its exact formula -- that function
has no cap (bounded instead by a small max_attempts=4 for a single
rate-limited call), while this backoff guards an INDEFINITE reconnect
loop and needs an explicit ceiling.

Jitter here is PROPORTIONAL to the computed delay (up to
`jitter_fraction` of it), not call_with_retry's flat `+uniform(0, 0.25)`
seconds -- deliberately: a flat quarter-second of jitter does nothing to
spread out many connectors all reconnecting at exactly the 60s cap after
a shared relay-server outage, while proportional jitter actually does.
"""
from __future__ import annotations

import random


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 60.0, jitter_fraction: float = 0.2) -> float:
    """attempt is 0-indexed. delay = min(cap, base * 2**attempt), plus up
    to jitter_fraction * delay of additional random jitter."""
    delay = min(cap, base * (2 ** attempt))
    return delay + random.uniform(0, delay * jitter_fraction)
