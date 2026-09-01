"""
The whitelist of indicator types a StrategyConfig's IndicatorRef.type may
name -- the single source of truth shared by strategy_builder/validator.py
(checks a referenced type actually exists here), compiler.py/
rule_based_strategy.py (looks up the actual function to call), and later
the AI authoring assistant's tool-use schema (generated FROM this
registry, so its vocabulary can never drift from what's actually
implemented) -- same "one allowlist, everything reads from it" discipline
as webull-momentum-bot's ADJUSTABLE_RISK_FIELDS.

Every indicator function takes a plain price series (see
rule_based_strategy.py -- currently `[s.mid for s in history]`, since
MarketSnapshot has no true OHLC bars yet) plus its own params, and
returns a series the same length as the input (leading Nones where there
isn't enough history) -- see moving_average.py's docstring for why.

Deliberately NOT here yet: ATR, Bollinger Bands, MACD, Stochastic. All
four are conventionally computed from true OHLC bars (high/low/close per
period), which don't exist in this project yet -- MarketSnapshot is a
single bid/ask point, not an aggregated bar. Add them once a bar-
aggregation module exists; faking them off inadequate data would produce
numbers that look plausible but aren't actually the indicator.
"""
from __future__ import annotations

from typing import Callable, Optional, TypedDict

from .moving_average import ema, sma
from .rsi import rsi


class IndicatorSpec(TypedDict):
    fn: Callable[..., list[Optional[float]]]
    params: dict[str, type]  # param name -> expected type, for validator.py


INDICATOR_REGISTRY: dict[str, IndicatorSpec] = {
    "sma": {"fn": sma, "params": {"period": int}},
    "ema": {"fn": ema, "params": {"period": int}},
    "rsi": {"fn": rsi, "params": {"period": int}},
}
