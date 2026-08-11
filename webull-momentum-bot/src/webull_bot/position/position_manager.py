"""
Position management: tracks open positions' MFE/MAE and decides when an
exit Signal should be generated (stop, target, trailing stop, breakeven,
VWAP failure, time limit). Momentum-based exits are left as a hook for
strategy-specific logic since "momentum failed" is strategy-dependent.

Like everything else, this only *emits* Signals -- it never calls the
broker. Those signals flow through the same RiskEngine -> OrderManager path
as entries (the risk engine's spread/liquidity checks still apply on exit).

Target hits are a PARTIAL exit (SCALE_OUT, sells half), not a full close --
see check_exit's docstring for why. This is a deliberate, explicitly
chosen design (see docs/ARCHITECTURE.md's "Position management" section):
letting the whole position ride only on the trailing stop was considered
and rejected in favor of banking some profit at a known level while still
letting the remainder run.

The trailing stop itself only activates after that partial exit -- before
target is reached, the stop is governed solely by the strategy's initial
stop and the breakeven-at-+N% rule, not the continuous %-of-current-price
trailing math. See _maybe_update_trailing_stop's docstring for why these
two don't run together.

Broker-side position management (2026-08-11): when the connected broker
supports resting orders (WebullBrokerClient -- see its place_oco_bracket
docstring), TradingLoop attaches a real OCO stop+target bracket to a
position right after its entry fill is confirmed, and this class's own
stop/target price-cross checks in check_exit step aside for that position
(see check_exit's docstring) rather than racing the broker's own fill with
a redundant market order. This class still computes every stop-price
adjustment (breakeven, trailing) exactly as before -- it just no longer
also decides when to act on a stop/target cross once the broker is the one
holding the actual resting order; TradingLoop reads the mutated
stop_price and pushes it to the broker via cancel+replace. VWAP failure
and the time limit are unaffected either way: neither has a broker-side
resting-order equivalent, so this class always decides and emits those
itself, broker-managed or not.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..enums import ExitReason, SignalAction
from ..models import MarketSnapshot, Position, Signal


@dataclass
class PositionManagementConfig:
    trailing_stop_pct: Optional[float] = 3.0
    # Once price is up this % from entry, the stop ratchets up to at least
    # avg_entry_price (a guaranteed no-loss floor) if it isn't already
    # there or better. Distinct from trailing_stop_pct's continuous %-of-
    # current-price math: at a shallow trailing_stop_pct or right after
    # entry, the flat trailing calc might not reach breakeven on its own
    # yet even though price has already moved meaningfully -- this is an
    # explicit guarantee rather than an emergent side effect of that math.
    # None disables the rule entirely.
    breakeven_trigger_pct: Optional[float] = 5.0
    time_limit_minutes: Optional[int] = 30
    exit_on_vwap_failure: bool = True
    vwap_failure_buffer_pct: float = 0.5  # how far below VWAP counts as "failed"


class PositionManager:
    def __init__(self, config: Optional[PositionManagementConfig] = None):
        self.config = config or PositionManagementConfig()

    def update_excursions(self, position: Position, snapshot: MarketSnapshot) -> None:
        favorable = (snapshot.last_price - position.avg_entry_price) / position.avg_entry_price * 100.0
        adverse = (position.avg_entry_price - snapshot.last_price) / position.avg_entry_price * 100.0
        position.max_favorable_excursion = max(position.max_favorable_excursion, favorable)
        position.max_adverse_excursion = max(position.max_adverse_excursion, adverse)

    def _maybe_apply_breakeven(self, position: Position, snapshot: MarketSnapshot) -> None:
        if not self.config.breakeven_trigger_pct or not position.avg_entry_price:
            return
        trigger_price = position.avg_entry_price * (1 + self.config.breakeven_trigger_pct / 100.0)
        if snapshot.last_price < trigger_price:
            return
        if position.stop_price is None or position.avg_entry_price > position.stop_price:
            position.stop_price = position.avg_entry_price

    def _maybe_update_trailing_stop(self, position: Position, snapshot: MarketSnapshot) -> None:
        """Only takes over once the target has been reached and the partial
        exit taken -- before that, the stop is governed solely by the
        strategy's own initial stop and the breakeven rule above. This is a
        deliberate choice: trailing 3% off the *current* price starting
        from tick one would fight the breakeven rule's fixed floor and can
        ratchet the stop up faster than the trade has actually proven
        itself. Once target is hit, the position has already banked half
        its profit and earned a genuine trailing take-profit on the rest.
        A strategy that never sets a target (none currently do) never
        reaches partial_exit_taken=True, so its position rides on the
        initial stop + breakeven alone for its whole lifetime."""
        if not self.config.trailing_stop_pct or not position.partial_exit_taken:
            return
        trailing_level = snapshot.last_price * (1 - self.config.trailing_stop_pct / 100.0)
        if position.stop_price is None or trailing_level > position.stop_price:
            position.stop_price = trailing_level

    def check_exit(self, position: Position, snapshot: MarketSnapshot, *, now: Optional[datetime] = None) -> Optional[Signal]:
        """Returns an EXIT Signal (full close) for a stop/VWAP-failure/time-limit
        hit, or a SCALE_OUT Signal (sells half, position stays open) the
        first time target_price is reached -- never fires a second partial
        for the same position (see Position.partial_exit_taken). A target
        hit is downgraded to a full EXIT instead when the position is too
        small to split into two whole-share halves (< 2 shares): a
        zero-share partial order would be meaningless, and a strategy that
        never sets a target at all (e.g. VolumeIgnitionStrategy) never
        reaches this branch regardless.

        Stop/target price-crosses are skipped entirely (falling straight
        through to the VWAP-failure/time-limit checks) when
        position.broker_stop_order_id is set -- a real resting order at the
        broker is already watching for that exact cross and will execute it
        itself (see TradingLoop._attach_broker_bracket/_poll_broker_bracket),
        so this method emitting its own market order on the same cross
        would race the broker's own fill and risk over-selling the
        position. VWAP failure and a time limit have no broker-side
        equivalent (there's no resting order type for "price fell below
        VWAP" or "this many minutes have passed"), so those two still fire
        from here regardless of whether the position is broker-managed.
        Breakeven/trailing-stop math above still runs either way: it only
        mutates position.stop_price, which TradingLoop's own sync step
        reads to decide whether the resting broker order itself needs
        replacing at a new price -- this method has no way to reach the
        broker directly, by design (see order_manager.py's docstring)."""
        now = now or snapshot.timestamp
        self.update_excursions(position, snapshot)
        self._maybe_apply_breakeven(position, snapshot)
        self._maybe_update_trailing_stop(position, snapshot)

        reason: Optional[ExitReason] = None
        action = SignalAction.EXIT
        broker_managed = position.broker_stop_order_id is not None

        if not broker_managed and position.stop_price is not None and snapshot.last_price <= position.stop_price:
            # TRAILING_STOP only once trailing is actually the thing
            # governing the stop (post-partial-exit, see
            # _maybe_update_trailing_stop) -- before that, whether it's the
            # strategy's original stop or the breakeven-adjusted one, it's
            # simply a stop loss, not a trailing one.
            reason = ExitReason.TRAILING_STOP if (self.config.trailing_stop_pct and position.partial_exit_taken) else ExitReason.STOP_LOSS
        elif not broker_managed and (
            position.target_price is not None
            and not position.partial_exit_taken
            and snapshot.last_price >= position.target_price
        ):
            if position.quantity >= 2:
                reason = ExitReason.PARTIAL_PROFIT_TARGET
                action = SignalAction.SCALE_OUT
            else:
                reason = ExitReason.PROFIT_TARGET
        elif (
            self.config.exit_on_vwap_failure
            and snapshot.vwap
            and snapshot.last_price < snapshot.vwap * (1 - self.config.vwap_failure_buffer_pct / 100.0)
        ):
            reason = ExitReason.VWAP_FAILURE
        elif (
            self.config.time_limit_minutes is not None
            and now - position.opened_at >= timedelta(minutes=self.config.time_limit_minutes)
        ):
            reason = ExitReason.TIME_LIMIT

        if reason is None:
            return None

        return Signal(
            symbol=position.symbol,
            action=action,
            generated_at=now,
            strategy_name=position.strategy_name,
            strategy_version="position_manager",
            reference_price=snapshot.last_price,
            metadata={"exit_reason": reason.value},
        )
