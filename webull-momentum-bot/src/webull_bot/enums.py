from __future__ import annotations

from enum import Enum


class CandidateState(str, Enum):
    """
    Lifecycle of a momentum candidate. Transitions are enforced by
    state_machine.py -- nothing should set this field directly.
    """
    DISCOVERED = "discovered"
    WATCHING = "watching"
    HEATING_UP = "heating_up"
    ARMED = "armed"
    TRIGGERED = "triggered"
    ENTERED = "entered"
    MANAGING = "managing"
    EXITED = "exited"
    COOLDOWN = "cooldown"
    REJECTED = "rejected"  # terminal: disqualified before ever arming (float too large, unsupported security, etc.)


class TradeBlockReason(str, Enum):
    """A *temporary* condition currently preventing an otherwise-valid
    candidate from being trade-eligible -- distinct from CandidateState.REJECTED,
    which is permanent/structural. Cleared automatically the next time
    CandidateWatcher.update() finds the underlying condition resolved; see
    that module's docstring for why spread/liquidity moved from a
    permanent rejection to this instead."""
    SPREAD_TOO_WIDE = "spread_too_wide"
    LOW_LIQUIDITY = "low_liquidity"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"
    SELL_SHORT = "sell_short"
    BUY_TO_COVER = "buy_to_cover"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(str, Enum):
    PENDING = "pending"          # created locally, not yet sent
    SUBMITTED = "submitted"       # sent to broker, awaiting ack
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
    TRAILING_STOP = "trailing_stop"
    MOMENTUM_FAILURE = "momentum_failure"
    VWAP_FAILURE = "vwap_failure"
    TIME_LIMIT = "time_limit"
    MANUAL = "manual"
    RISK_KILL_SWITCH = "risk_kill_switch"


class RiskEventType(str, Enum):
    TRADE_REJECTED = "trade_rejected"
    DAILY_LOSS_LIMIT_HIT = "daily_loss_limit_hit"
    MAX_EXPOSURE_HIT = "max_exposure_hit"
    MAX_POSITIONS_HIT = "max_positions_hit"
    MAX_TRADES_PER_TICKER_HIT = "max_trades_per_ticker_hit"
    MAX_TRADES_PER_DAY_HIT = "max_trades_per_day_hit"
    SPREAD_TOO_WIDE = "spread_too_wide"
    LIQUIDITY_TOO_LOW = "liquidity_too_low"
    SLIPPAGE_PROTECTION_TRIGGERED = "slippage_protection_triggered"
    COOLDOWN_ACTIVE = "cooldown_active"
    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    MIN_RISK_REWARD_NOT_MET = "min_risk_reward_not_met"


class MomentumOutcome(str, Enum):
    """Label applied to a recorded momentum event after the fact, for offline analysis."""
    CONTINUED = "continued"
    FAILED = "failed"
    CHOPPY = "choppy"
    UNKNOWN = "unknown"
