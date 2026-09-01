"""
Currency-pair helpers: pip size and base/quote currency splitting. Kept as
small lookup functions rather than fields duplicated on every MarketSnapshot/
Order/Position instance -- pip size and base/quote currency are properties
of the PAIR, not of any one point-in-time observation or trade, so storing
them per-instance would just be redundant data that could drift out of sync
with itself.

Pairs are represented as "BASE/QUOTE" strings throughout this project, e.g.
"EUR/USD", "USD/JPY" -- matching how they're written in the approved plan
and in every forex broker/bridge's own documentation.
"""
from __future__ import annotations

# Pip size is 0.01 for any pair quoted in Japanese yen, 0.0001 for every
# other pair -- the one broadly-true rule of thumb across major/minor
# pairs. A handful of exotic pairs have their own non-standard pip sizes
# in practice; add explicit overrides here if/when this bot supports one,
# rather than guessing now for pairs nothing yet trades.
_JPY_PIP_SIZE = 0.01
_DEFAULT_PIP_SIZE = 0.0001


def base_quote(pair: str) -> tuple[str, str]:
    """"EUR/USD" -> ("EUR", "USD"). Raises ValueError on anything not in
    that exact BASE/QUOTE form, so a malformed pair fails loudly here
    rather than producing a confusing downstream KeyError/AttributeError."""
    parts = pair.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Expected a 'BASE/QUOTE' pair like 'EUR/USD', got: {pair!r}")
    return parts[0].upper(), parts[1].upper()


def pip_size(pair: str) -> float:
    _, quote = base_quote(pair)
    return _JPY_PIP_SIZE if quote == "JPY" else _DEFAULT_PIP_SIZE


def price_diff_to_pips(pair: str, price_diff: float) -> float:
    """Signed pip distance for a raw price difference, e.g. price_diff_to_pips
    ("EUR/USD", 0.00050) == 5.0."""
    return price_diff / pip_size(pair)


def pips_to_price_diff(pair: str, pips: float) -> float:
    return pips * pip_size(pair)
