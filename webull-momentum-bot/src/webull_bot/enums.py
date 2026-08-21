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
    # A strategy's trigger condition fired, but no order has been submitted
    # yet -- added 2026-08-13 as part of the entry-selectivity rework (see
    # docs/ARCHITECTURE.md's "Entry selectivity rework" section): the
    # previous behavior submitted an order the instant a strategy returned
    # a Signal off a single snapshot, which meant a one-tick noise spike
    # was indistinguishable from a real breakout. CONFIRMING holds the
    # candidate here for TradingLoopConfig.confirmation_window_seconds,
    # requiring price to keep holding above the trigger reference before
    # ever reaching TRIGGERED (order submitted).
    CONFIRMING = "confirming"
    TRIGGERED = "triggered"
    ENTERED = "entered"
    MANAGING = "managing"
    EXITED = "exited"
    COOLDOWN = "cooldown"
    REJECTED = "rejected"  # terminal: disqualified before ever arming (float too large, unsupported security, etc.)


class MomentumPhase(str, Enum):
    """Live price-behavior classification for an ARMED/CONFIRMING candidate,
    tracked on Candidate.momentum -- INDEPENDENT of CandidateState (see
    Candidate.momentum's own docstring for why this isn't a new
    CandidateState value). MIS/CandidateState answer "is this stock
    structurally interesting"; MomentumPhase answers "what is it doing
    right now" -- added 2026-08-17 as part of the Real-Time Momentum
    Qualification Layer (scanner/momentum_qualification.py).

    NONE: no momentum regime established (return_5m below the hard gate).
    IGNITING: approaching the regime with rising short-term velocity --
        informational only, never entry-eligible.
    IMPULSING: in the +5%/5m regime AND meeting the short-term
        velocity/quality thresholds -- entry-eligible.
    PULLING_BACK: a controlled, structurally-intact retracement from an
        active impulse -- NOT entry-eligible, but not a rejection either;
        the candidate keeps being tracked, waiting for REACCELERATING.
    REACCELERATING: buyers demonstrably returning after a healthy
        pullback (positive/rising short-term velocity, pullback micro-high
        reclaimed, RTMS rising) -- entry-eligible, same as IMPULSING.
    """
    NONE = "none"
    IGNITING = "igniting"
    IMPULSING = "impulsing"
    PULLING_BACK = "pulling_back"
    REACCELERATING = "reaccelerating"


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


class TradeSide(str, Enum):
    """Aggressor classification for a single TICK-streamed trade print (see
    models.TickRecord, brokers/webull/client.py's TICK sub-type support) --
    distinct from OrderSide above, which describes OUR OWN orders, not a
    trade someone else made. UNKNOWN is the default/fail-safe value: it's
    used both for a genuinely unclassifiable print and for the case where
    Webull's real `side` wire encoding doesn't match any entry in
    brokers/webull/client.py's _TICK_SIDE_MAP (that mapping is an
    unconfirmed guess as of 2026-08-14 -- see its own docstring). Callers
    computing order-flow imbalance (metrics/calculations.order_flow_imbalance)
    must only ever count BUY/SELL volume, never UNKNOWN, so an entirely
    wrong _TICK_SIDE_MAP fails toward "no signal" rather than a
    confidently wrong one."""
    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    # A native broker-side trailing stop (see Order.trailing_pct) --
    # Webull's TRAILING_STOP_LOSS. Only ever placed by
    # OrderManager.place_resting_trailing_stop against a broker that
    # supports resting orders; PaperBrokerClient/backtests never see this
    # order type (see TradingLoop._attach_broker_bracket's docstring).
    TRAILING_STOP = "trailing_stop"


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
    PARTIAL_PROFIT_TARGET = "partial_profit_target"
    TRAILING_STOP = "trailing_stop"
    MOMENTUM_FAILURE = "momentum_failure"
    VWAP_FAILURE = "vwap_failure"
    TIME_LIMIT = "time_limit"
    MANUAL = "manual"
    RISK_KILL_SWITCH = "risk_kill_switch"
    END_OF_CORE_HOURS = "end_of_core_hours"
    # A position vanished from broker.get_positions() -- confirmed missing
    # across TradingLoopConfig.position_missing_confirmations_required
    # consecutive reconcile_positions_from_broker passes -- without this
    # process ever submitting or confirming the exit order itself (a
    # manual close in the Webull app, scripts/list_and_close_positions.py,
    # or any other out-of-band close). Distinct from MANUAL, which is
    # this process's OWN dashboard-driven close (TradingLoop.
    # request_manual_close) -- that one has a real fill/order to build an
    # exact Trade record from; this one doesn't, so its exit_price/pnl
    # are necessarily a best-effort approximation (see
    # TradingLoop._build_trade_for_external_close's docstring).
    EXTERNAL_CLOSE = "external_close"


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
    OUTSIDE_CORE_TRADING_HOURS = "outside_core_trading_hours"
    ENTRIES_TEMPORARILY_PAUSED = "entries_temporarily_paused"
    # A MANAGING position has gone TradingLoopConfig.
    # unprotected_position_alert_seconds or longer with no broker-side
    # stop/target bracket -- riding on this process's own software-side
    # checks alone. _attach_broker_bracket/_sync_broker_protective_orders
    # keep retrying every tick regardless (this event doesn't change that
    # -- see TradingLoop._manage_position), but a retry loop that can
    # never succeed against a structurally broken order payload would
    # otherwise fail completely silently forever. Not a rejection of any
    # SIGNAL (unlike every other RiskEventType above, all raised from
    # RiskEngine.evaluate itself) -- raised by TradingLoop via
    # RiskEngine.record_operational_event instead.
    POSITION_UNPROTECTED_TOO_LONG = "position_unprotected_too_long"
    # A software-managed exit order sat in TradingLoop._pending_exit_orders
    # for TradingLoopConfig.pending_exit_stuck_timeout_seconds or longer
    # with no terminal status (never filled, never rejected/canceled/
    # expired) -- see TradingLoop._poll_pending_exit. Real incident
    # (2026-08-13, sandbox): an exit order for a position well past its
    # stop-loss simply never resolved, and because _manage_position's
    # very first check defers entirely to _poll_pending_exit whenever a
    # symbol has a pending exit, PositionManager.check_exit was never
    # called again for that symbol -- silently, for hours, with the
    # dashboard's own "Close" button also refusing to act (it correctly
    # skips a symbol already in _pending_exit_orders). Raised right
    # before the stuck order is cancelled and dropped from tracking so a
    # fresh check_exit/exit attempt can run the very next tick.
    PENDING_EXIT_ORDER_STUCK = "pending_exit_order_stuck"
    # A tracked candidate's (open position or pre-entry -- any
    # TradingLoop._STREAMING_ELIGIBLE_STATES member) live market-data feed
    # has gone TradingLoopConfig.stale_market_data_alert_seconds or longer
    # with no fresh snapshot at all -- neither the streaming push nor the
    # REST fallback in TradingLoop._process_candidate_inner has produced
    # one, meaning get_last_known_price_age_seconds (already read by the
    # dashboard's own /api/positions AND /api/candidates staleness badges)
    # keeps climbing with nothing else surfacing why. Same "operational,
    # not a signal rejection" shape as POSITION_UNPROTECTED_TOO_LONG
    # above -- raised by TradingLoop via RiskEngine.record_operational_event,
    # not RiskEngine.evaluate. Real incident (2026-08-20, positions only):
    # a position's price silently froze for several minutes (Webull's
    # snapshot endpoint returning no data for that symbol every cycle)
    # with nothing beyond the passive dashboard badge to flag it.
    # Broadened (2026-08-21) to cover pre-entry candidates too, at the
    # user's explicit request ("ensure the data is live and accurate for
    # all candidates and open positions") -- the exact same silent
    # failure mode existed there and was simply never surfaced. See
    # TradingLoop._maybe_raise_stale_market_data_alert.
    MARKET_DATA_STALE = "market_data_stale"
    # Entry-selectivity rework (2026-08-13, see docs/ARCHITECTURE.md):
    # raised by TradingLoop._poll_confirmation, not RiskEngine.evaluate,
    # when a CONFIRMING candidate fails to hold through
    # TradingLoopConfig.confirmation_window_seconds -- price reversed past
    # the allowed pullback, MIS fell back below the armed threshold, or
    # the spread widened back out. The candidate reverts to ARMED and must
    # re-trigger fresh rather than resuming a stale confirmation clock.
    CONFIRMATION_FAILED = "confirmation_failed"
    # Raised by TradingLoop._poll_confirmation when a confirmed trigger's
    # recomputed target (from the ACTUAL confirmed entry price, not the
    # stale trigger price -- see that method's docstring) would sit past a
    # known static resistance level (candidate.static_resistance_levels)
    # before ever reaching it. This is a hard reject, independent of how
    # high MIS is: no amount of momentum matters if the fixed +stop*R
    # target has a real ceiling in the way first.
    RESISTANCE_BEFORE_TARGET = "resistance_before_target"
    # Atomic bracket entry (2026-08-13, see docs/ARCHITECTURE.md) -- the
    # entry, stop-loss, and take-profit orders are now submitted together
    # as one MASTER+STOP_LOSS+STOP_PROFIT combo (see
    # WebullBrokerClient.place_bracket_entry) so a position is never
    # broker-managed-less even for an instant during core hours. Raised
    # by TradingLoop._submit_entry when the broker rejects that combo
    # request -- per explicit instruction, this is a hard failure: the
    # trade does NOT go through at all (no fallback to a plain,
    # unprotected entry), the candidate reverts to ARMED, and this event
    # is what tells a human why nothing was opened.
    BRACKET_ENTRY_REJECTED = "bracket_entry_rejected"
    # Real-Time Momentum Qualification Layer (2026-08-17, see
    # scanner/momentum_qualification.py). Raised by
    # TradingLoop._submit_ranked_entries' final pre-submission recheck when
    # a candidate that already passed CONFIRMING and won a ranking slot no
    # longer qualifies by the time its turn to actually submit comes up
    # (RTMS decayed, phase fell back to PULLING_BACK, structure broke,
    # etc.) -- momentum can fade during the up-to-70s confirmation+slot-wait
    # window, and this is what prevents submitting a stale entry on a setup
    # that no longer looks the way it did when it first qualified. The
    # candidate reverts to ARMED and does NOT consume the open slot -- the
    # next-ranked candidate gets a chance instead.
    MOMENTUM_QUALIFICATION_LOST = "momentum_qualification_lost"


class MomentumOutcome(str, Enum):
    """Label applied to a recorded momentum event after the fact, for offline analysis."""
    CONTINUED = "continued"
    FAILED = "failed"
    CHOPPY = "choppy"
    UNKNOWN = "unknown"
