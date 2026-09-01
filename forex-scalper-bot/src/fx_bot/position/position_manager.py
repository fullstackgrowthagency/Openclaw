"""
Position management -- checks an open position's stop-loss/target/
trailing-stop/breakeven levels against the current price and decides
whether it should close, mirroring webull-momentum-bot's position/
position_manager.py's role. Nothing before this phase auto-enforced a
position's stop_price/target_price at all -- only an explicit strategy
EXIT signal ever closed anything (see strategy_builder/rule_based_
strategy.py's docstring, and the repeated "position management, a later
phase" notes throughout Phases 2-3). This is that later phase.

Two deliberate simplifications, documented rather than silently done:

1. `PositionManagementConfig` is a single GLOBAL config applied to every
   open position uniformly -- NOT locked in per-position at entry time.
   `models.Position.trailing_stop_pips` and `models.Order.trailing_pips`
   already exist as per-position/per-order override fields (from earlier
   phases) but PositionManager does not read them; a future refinement
   could have OrderManager copy the config's current value into those
   fields at entry so a later config change doesn't retroactively affect
   already-open positions. Not needed for a first implementation to be
   correct, just to be maximally flexible.
2. Every stop-price exit is reported as ExitReason.STOP_LOSS, even one
   whose stop_price was moved by breakeven/trailing logic below --
   distinguishing "hit the original fixed stop" from "hit a trailing-
   adjusted stop" would need extra per-position state (e.g. "has this
   position's stop ever been moved") this doesn't track yet. stop_price
   IS the real, current, effective stop regardless of how it got there,
   so this is accurate, just not maximally descriptive.

No partial exits (SCALE_OUT) -- same deferral OrderManager itself
already documents.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..enums import ExitReason, OrderSide
from ..models import MarketSnapshot, Position
from ..pairs import pips_to_price_diff


@dataclass
class PositionManagementConfig:
    trailing_stop_pips: Optional[float] = None
    breakeven_trigger_pips: Optional[float] = None


class PositionManager:
    def __init__(self, config: Optional[PositionManagementConfig] = None):
        self.config = config or PositionManagementConfig()

    def manage(self, position: Position, snapshot: MarketSnapshot) -> Optional[ExitReason]:
        """Called every tick for every open position, BEFORE the owning
        strategy is asked for a signal (see backtest/engine.py). Mutates
        `position.stop_price` in place for breakeven/trailing-stop
        adjustments, then checks the (possibly just-adjusted) stop/target
        against the price this position would actually exit at right now.
        Returns the ExitReason to close for, or None to stay open."""
        # A long closes by SELLING (fills at bid), a short closes by
        # BUYING (fills at ask) -- see PaperBrokerClient.place_order's own
        # crossing-the-spread convention. Checking against the real exit
        # price (not an optimistic mid) means this never reports a stop/
        # target hit the position couldn't actually have filled at.
        exit_price = snapshot.bid if position.side == OrderSide.BUY else snapshot.ask

        self._apply_breakeven(position, exit_price)
        self._apply_trailing_stop(position, exit_price)

        if position.stop_price is not None:
            stop_hit = (
                exit_price <= position.stop_price if position.side == OrderSide.BUY
                else exit_price >= position.stop_price
            )
            if stop_hit:
                return ExitReason.STOP_LOSS

        if position.target_price is not None:
            target_hit = (
                exit_price >= position.target_price if position.side == OrderSide.BUY
                else exit_price <= position.target_price
            )
            if target_hit:
                return ExitReason.PROFIT_TARGET

        return None

    def _apply_breakeven(self, position: Position, exit_price: float) -> None:
        if self.config.breakeven_trigger_pips is None:
            return
        trigger_distance = pips_to_price_diff(position.symbol, self.config.breakeven_trigger_pips)
        is_long = position.side == OrderSide.BUY
        triggered = (
            exit_price >= position.avg_entry_price + trigger_distance if is_long
            else exit_price <= position.avg_entry_price - trigger_distance
        )
        if not triggered:
            return
        already_at_or_past_breakeven = position.stop_price is not None and (
            position.stop_price >= position.avg_entry_price if is_long
            else position.stop_price <= position.avg_entry_price
        )
        if not already_at_or_past_breakeven:
            position.stop_price = position.avg_entry_price

    def _apply_trailing_stop(self, position: Position, exit_price: float) -> None:
        if self.config.trailing_stop_pips is None:
            return
        trail_distance = pips_to_price_diff(position.symbol, self.config.trailing_stop_pips)
        if position.side == OrderSide.BUY:
            candidate_stop = exit_price - trail_distance
            # Only ever tightens (moves up), never loosens -- a trailing
            # stop that could slip backward as price pulls back defeats
            # its own purpose.
            if position.stop_price is None or candidate_stop > position.stop_price:
                position.stop_price = candidate_stop
        else:
            candidate_stop = exit_price + trail_distance
            if position.stop_price is None or candidate_stop < position.stop_price:
                position.stop_price = candidate_stop
