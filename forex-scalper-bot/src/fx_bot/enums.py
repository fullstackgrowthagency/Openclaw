"""
Core order/trade enums, mirroring webull-momentum-bot/src/webull_bot/enums.py
in shape where the concept transfers directly. Forex-specific differences:

- OrderSide is just BUY/SELL -- forex has no equities-style short-selling
  mechanic (borrowing shares); going short a pair is simply selling it.
- No CandidateState/state-machine here yet -- whether a scalping bot needs
  one at all (vs. straight per-tick rule evaluation) is an open question
  for the rule-builder phase (see the approved plan), not decided here.
- No RiskEventType yet -- that belongs to the risk-engine phase, once
  something actually raises those events; adding it now would be
  designing for logic that doesn't exist.
"""
from __future__ import annotations

from enum import Enum


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(str, Enum):
    PENDING = "pending"            # created locally, not yet sent
    SUBMITTED = "submitted"        # sent to broker/connector, awaiting ack
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class SignalAction(str, Enum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"
    SCALE_IN = "scale_in"
    SCALE_OUT = "scale_out"


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    PROFIT_TARGET = "profit_target"
    PARTIAL_PROFIT_TARGET = "partial_profit_target"
    TRAILING_STOP = "trailing_stop"
    TIME_LIMIT = "time_limit"          # rule-builder's max-bars-in-trade time stop
    MANUAL = "manual"
    RISK_KILL_SWITCH = "risk_kill_switch"
    BOT_DISABLED = "bot_disabled"
    END_OF_SESSION = "end_of_session"  # forex equivalent of "end of core hours" -- see market_hours.py
    # A position vanished from the connector/broker's own reported state
    # without this process ever submitting the closing order itself (a
    # manual close in the MT5 terminal, an out-of-band close). Mirrors
    # webull_bot's identically-named, identically-reasoned member.
    EXTERNAL_CLOSE = "external_close"
