"""
Core order/trade enums, mirroring webull-momentum-bot/src/webull_bot/enums.py
in shape where the concept transfers directly. Forex-specific differences:

- OrderSide is just BUY/SELL -- forex has no equities-style short-selling
  mechanic (borrowing shares); going short a pair is simply selling it.
- No CandidateState/state-machine -- resolved in Phase 3 (see
  interfaces/strategy.py's docstring): a StrategyConfig names exactly one
  pair, so there's no broad-universe discovery problem to filter noise
  from, unlike the equities bot's scanner. Strategy.on_snapshot takes an
  explicit Optional[Position] instead.
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


class RiskEventType(str, Enum):
    """Every rejection/halt reason RiskEngine.evaluate can log, mirroring
    webull_bot's RiskEventType where the concept transfers -- see
    risk/risk_engine.py for what actually raises each one. Renamed/added
    members reflect forex-specific checks that have no equities-bot
    equivalent (per-pair and correlated-currency exposure caps, session
    windows instead of a single core-hours window)."""
    TRADE_REJECTED = "trade_rejected"
    DAILY_LOSS_LIMIT_HIT = "daily_loss_limit_hit"
    MAX_TOTAL_RISK_HIT = "max_total_risk_hit"
    MAX_POSITIONS_HIT = "max_positions_hit"
    MAX_POSITIONS_PER_PAIR_HIT = "max_positions_per_pair_hit"
    MAX_CORRELATED_EXPOSURE_HIT = "max_correlated_exposure_hit"
    MAX_TRADES_PER_DAY_HIT = "max_trades_per_day_hit"
    MAX_TRADES_PER_PAIR_HIT = "max_trades_per_pair_hit"
    SPREAD_TOO_WIDE = "spread_too_wide"
    COOLDOWN_ACTIVE = "cooldown_active"
    MIN_RISK_REWARD_NOT_MET = "min_risk_reward_not_met"
    OUTSIDE_ALLOWED_SESSION = "outside_allowed_session"
