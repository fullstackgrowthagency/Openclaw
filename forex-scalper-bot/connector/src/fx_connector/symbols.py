"""
Symbol-format translation between fx_bot's "BASE/QUOTE" pair convention
(e.g. "EUR/USD") and MT5's plain concatenated symbol names (e.g.
"EURUSD", confirmed against the official MQL5 docs -- every example uses
the no-separator form). Deliberately duplicated here rather than
imported from fx_bot.pairs: this project must never depend on fx_bot
(see this package's own README), so this is a small, self-contained
reimplementation of exactly the "BASE/QUOTE" convention, the same
reasoning relay_protocol's wire_models.py already gives for keeping
enum VALUES (not fx_bot's enum classes) on the wire.

Per-broker symbol suffixes (e.g. "EURUSD.m") are threaded through via
`suffix` but never auto-detected -- confirming/handling real broker
suffix conventions needs a live terminal and is explicitly deferred to
Phase 5g, not solved here.
"""
from __future__ import annotations


def wire_pair_to_mt5_symbol(pair: str, *, suffix: str = "") -> str:
    if "/" not in pair:
        raise ValueError(f"Expected a 'BASE/QUOTE' pair, got {pair!r}.")
    base, _, quote = pair.partition("/")
    if not base or not quote:
        raise ValueError(f"Expected a 'BASE/QUOTE' pair, got {pair!r}.")
    return f"{base}{quote}{suffix}"


def mt5_symbol_to_wire_pair(symbol: str, *, suffix: str = "") -> str:
    stripped = symbol
    if suffix:
        if not stripped.endswith(suffix):
            raise ValueError(f"Expected {symbol!r} to end with suffix {suffix!r}.")
        stripped = stripped[: -len(suffix)]
    if len(stripped) != 6:
        raise ValueError(
            f"Expected a plain 6-character BASE+QUOTE symbol after stripping the suffix, got {stripped!r} "
            f"(from {symbol!r}) -- indices/CFDs with non-6-character symbols aren't supported yet."
        )
    return f"{stripped[:3]}/{stripped[3:]}"
