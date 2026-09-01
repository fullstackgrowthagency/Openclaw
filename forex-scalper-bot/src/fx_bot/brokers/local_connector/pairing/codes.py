"""
Human-typed pairing codes -- short enough to read off a dashboard and
type into a connector's setup prompt, long enough that brute-forcing one
within its TTL (see config.py's `pairing_code_ttl_seconds`) is
infeasible even with no rate-limiting (see the approved Phase 5c design's
explicit reasoning for why no rate-limiting is added in this phase).
"""
from __future__ import annotations

import secrets

# No 0/O/1/I/L -- easy to misread when hand-typed from a screen.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8  # 32^8 ~= 1.1e12 combinations


def generate_pairing_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
    return f"{raw[:4]}-{raw[4:]}"
