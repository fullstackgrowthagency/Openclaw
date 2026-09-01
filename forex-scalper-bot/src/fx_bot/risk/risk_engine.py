"""
Deterministic risk engine. Every ENTRY Signal must pass through
RiskEngine.evaluate before it can become an Order -- see
execution/order_manager.py, the only caller allowed to act on a
RiskDecision. Nothing here is probabilistic/ML-driven, on purpose: risk
gating must be auditable and reproducible from a backtest run to a live
run, same principle as webull-momentum-bot's own risk engine.

Phase 4 expansion from the Phase 2 skeleton (see this module's git
history for that original, much smaller version) -- now the real field
set from the approved plan's forex risk-parameter mapping, with two
documented simplifications:

1. **Position sizing assumes the account's currency equals the pair's
   QUOTE currency** (e.g. a USD-denominated account trading EUR/USD,
   GBP/USD, etc.). Under that assumption, `quantity` units of the base
   currency change value by `quantity * pip_size(pair)` in the quote
   currency per pip -- which IS the account currency, so risk-%-of-equity
   sizing needs no further conversion. A pair whose quote currency
   differs from the account currency (e.g. EUR/JPY on a USD-denominated
   account) would need a real currency-conversion step this does NOT
   implement yet -- add it once this bot actually supports that
   mismatch, rather than guessing at a conversion rate now.
2. **Correlated-pair exposure is approximated by shared currency**, not a
   real historical correlation coefficient: a candidate pair counts as
   "correlated" with an open position if they share a base or quote
   currency (e.g. EUR/USD and GBP/USD both carry USD exposure). This is
   the standard practical proxy retail platforms use for "don't stack
   several differently-named bets that are all really the same
   directional risk" -- a real correlation number would need historical
   price data this bot doesn't fetch/store yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from ..enums import RiskEventType, SignalAction
from ..market_hours import active_sessions, is_market_open, is_within_london_new_york_overlap
from ..models import MarketSnapshot, Position, RiskDecision, RiskEvent, Signal
from ..pairs import base_quote


@dataclass
class RiskConfig:
    stop_loss_required: bool = True
    min_risk_reward_ratio: float = 1.5

    # -- position sizing (see module docstring's simplification #1) --
    sizing_method: Literal["fixed_units", "risk_percent"] = "risk_percent"
    fixed_units: float = 10_000.0
    risk_percent_of_equity: float = 1.0

    # -- exposure caps. 0 means "none allowed" (a hard off switch), not
    # "unlimited" -- matches this project's own Phase 2 convention, kept
    # for continuity with what's already shipped and tested. --
    max_simultaneous_positions: int = 3
    max_positions_per_pair: int = 1
    max_correlated_pair_exposure: int = 2  # see module docstring's simplification #2
    max_total_risk_pct: float = 10.0

    # -- daily limits --
    max_daily_loss_pct: float = 3.0
    max_trades_per_day: int = 20
    max_trades_per_pair_per_day: int = 5
    cooldown_minutes_after_loss: int = 15

    # -- scalping-specific entry filters --
    max_spread_pips: float = 2.0
    # Empty means "no restriction beyond the market being open at all" --
    # see market_hours.py's SESSION_WINDOWS keys plus the special value
    # "london_new_york_overlap" for the tighter overlap-only window.
    session_windows: tuple[str, ...] = ()


@dataclass
class _DailyState:
    day: date
    realized_pnl: float = 0.0
    trade_count: int = 0
    trades_per_pair: dict[str, int] = field(default_factory=dict)


class RiskEngine:
    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        self._daily = _DailyState(day=datetime.utcnow().date())
        # Rolling time window, not a calendar-day counter like _daily
        # above -- must survive _roll_day_if_needed, same reasoning as
        # webull_bot's identically-shaped _last_loss_at.
        self._last_loss_at: dict[str, datetime] = {}
        self.events: list[RiskEvent] = []

    def _roll_day_if_needed(self, now: datetime) -> None:
        if now.date() != self._daily.day:
            self._daily = _DailyState(day=now.date())

    def _log_event(self, event_type: RiskEventType, symbol: Optional[str], reason: str, now: Optional[datetime]) -> None:
        self.events.append(
            RiskEvent(event_type=event_type.value, symbol=symbol, timestamp=now or datetime.utcnow(), reason=reason)
        )

    def record_trade_closed(self, symbol: str, pnl: float, now: Optional[datetime] = None) -> None:
        """Called once a position on `symbol` actually closes (see
        execution/order_manager.py's _submit_exit) -- feeds the daily-loss
        limit and post-loss cooldown checks above. Not called for a
        rejected/never-filled entry attempt; there's no realized pnl to
        record for something that never became a position."""
        now = now or datetime.utcnow()
        self._roll_day_if_needed(now)
        self._daily.realized_pnl += pnl
        if pnl < 0:
            self._last_loss_at[symbol] = now

    def _session_currently_allowed(self, now: datetime) -> bool:
        windows = self.config.session_windows
        if not windows:
            return is_market_open(now)
        if "london_new_york_overlap" in windows and is_within_london_new_york_overlap(now):
            return True
        return bool(active_sessions(now) & set(windows))

    def evaluate(
        self,
        signal: Signal,
        *,
        account_equity: float,
        open_positions: list[Position],
        snapshot: MarketSnapshot,
        now: Optional[datetime] = None,
    ) -> RiskDecision:
        if signal.action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
            return RiskDecision(approved=False, reason=f"evaluate() only gates entries, not {signal.action.value}")

        now = now or datetime.utcnow()
        self._roll_day_if_needed(now)

        def reject(event_type: RiskEventType, reason: str) -> RiskDecision:
            self._log_event(event_type, signal.symbol, reason, now)
            return RiskDecision(approved=False, reason=reason)

        if not self._session_currently_allowed(now):
            return reject(RiskEventType.OUTSIDE_ALLOWED_SESSION, "No allowed session window is currently active.")

        daily_loss_limit = -abs(self.config.max_daily_loss_pct) / 100.0 * account_equity
        if self._daily.realized_pnl <= daily_loss_limit:
            return reject(RiskEventType.DAILY_LOSS_LIMIT_HIT, "Max daily loss limit reached.")

        if self._daily.trade_count >= self.config.max_trades_per_day:
            return reject(RiskEventType.MAX_TRADES_PER_DAY_HIT, "Max trades per day reached.")

        pair_trades_today = self._daily.trades_per_pair.get(signal.symbol, 0)
        if pair_trades_today >= self.config.max_trades_per_pair_per_day:
            return reject(RiskEventType.MAX_TRADES_PER_PAIR_HIT, "Max trades for this pair today reached.")

        last_loss = self._last_loss_at.get(signal.symbol)
        if last_loss is not None:
            cooldown_until = last_loss + timedelta(minutes=self.config.cooldown_minutes_after_loss)
            if now < cooldown_until:
                return reject(
                    RiskEventType.COOLDOWN_ACTIVE,
                    f"{signal.symbol} is in post-loss cooldown until {cooldown_until.isoformat()}.",
                )

        if len(open_positions) >= self.config.max_simultaneous_positions:
            return reject(RiskEventType.MAX_POSITIONS_HIT, "Max simultaneous positions reached.")

        pair_positions = [p for p in open_positions if p.symbol == signal.symbol]
        if len(pair_positions) >= self.config.max_positions_per_pair:
            return reject(RiskEventType.MAX_POSITIONS_PER_PAIR_HIT, "Max positions for this pair reached.")

        if self._count_correlated_positions(signal.symbol, open_positions) >= self.config.max_correlated_pair_exposure:
            return reject(RiskEventType.MAX_CORRELATED_EXPOSURE_HIT, "Max correlated-currency exposure reached.")

        if snapshot.spread_pips > self.config.max_spread_pips:
            return reject(
                RiskEventType.SPREAD_TOO_WIDE,
                f"Spread {snapshot.spread_pips:.1f} pips exceeds max {self.config.max_spread_pips}.",
            )

        if self.config.stop_loss_required and signal.suggested_stop is None:
            return reject(RiskEventType.TRADE_REJECTED, "Signal has no suggested_stop and stop_loss_required is set.")

        if signal.suggested_target is not None and signal.suggested_stop is not None:
            risk_dist = abs(signal.reference_price - signal.suggested_stop)
            reward_dist = abs(signal.suggested_target - signal.reference_price)
            # A tiny epsilon avoids rejecting a signal that's exactly at
            # the minimum ratio in theory but lands a hair below it in
            # floating point -- same reasoning as webull_bot's own check.
            if risk_dist > 0 and (reward_dist / risk_dist) < self.config.min_risk_reward_ratio - 1e-9:
                return reject(
                    RiskEventType.MIN_RISK_REWARD_NOT_MET,
                    f"Reward:risk ratio {reward_dist / risk_dist:.2f} is below the required minimum "
                    f"{self.config.min_risk_reward_ratio}.",
                )

        total_open_risk = sum(
            abs(p.avg_entry_price - p.stop_price) * p.quantity for p in open_positions if p.stop_price is not None
        )
        max_total_risk = account_equity * self.config.max_total_risk_pct / 100.0
        if total_open_risk >= max_total_risk:
            return reject(RiskEventType.MAX_TOTAL_RISK_HIT, "Max total assumed risk across open positions reached.")

        max_units = self._compute_position_size(signal, account_equity)
        if max_units is None or max_units <= 0:
            return reject(RiskEventType.TRADE_REJECTED, "Computed position size is zero given current risk/exposure limits.")

        self._daily.trade_count += 1
        self._daily.trades_per_pair[signal.symbol] = pair_trades_today + 1

        risk_amount = abs(signal.reference_price - signal.suggested_stop) * max_units if signal.suggested_stop is not None else None
        return RiskDecision(approved=True, reason="approved", max_units=max_units, risk_amount=risk_amount)

    def _compute_position_size(self, signal: Signal, account_equity: float) -> Optional[float]:
        if self.config.sizing_method == "fixed_units":
            return self.config.fixed_units
        # "risk_percent" -- see module docstring's simplification #1.
        if signal.suggested_stop is None:
            return None
        stop_distance = abs(signal.reference_price - signal.suggested_stop)
        if stop_distance <= 0:
            return None
        risk_amount = account_equity * self.config.risk_percent_of_equity / 100.0
        return risk_amount / stop_distance

    def _count_correlated_positions(self, pair: str, open_positions: list[Position]) -> int:
        base, quote = base_quote(pair)
        currencies = {base, quote}
        count = 0
        for position in open_positions:
            if position.symbol == pair:
                continue  # same-pair exposure is max_positions_per_pair's job, not this one's
            pos_base, pos_quote = base_quote(position.symbol)
            if currencies & {pos_base, pos_quote}:
                count += 1
        return count
