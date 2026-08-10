"""
Deterministic risk engine. Every Signal must pass through `RiskEngine.evaluate`
before it can become an Order -- see execution/order_manager.py, which is the
only caller allowed to act on a `RiskDecision`.

Nothing here is probabilistic or ML-driven on purpose: risk gating must be
auditable and reproducible from a backtest run to a live run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from ..enums import RiskEventType
from ..models import MarketSnapshot, Position, RiskDecision, RiskEvent, Signal


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 5.0          # % of account equity risked per trade (entry-to-stop)
    # Minimum reward:risk ratio required on any signal that specifies a
    # suggested_target (e.g. 2.0 means the distance from entry to target
    # must be at least 2x the distance from entry to stop). Signals with no
    # target (e.g. VolumeIgnitionStrategy, which relies on a trailing stop
    # instead of a fixed target) skip this check entirely -- there's no
    # reward distance to compare against.
    min_risk_reward_ratio: float = 2.0
    max_position_size_pct: float = 100.0     # max notional as % of buying power in a single position
    max_daily_loss_pct: float = 3.0          # halts all new trades for the day once hit
    # Total $-at-risk ceiling: the sum of entry-to-stop risk across every
    # open position, plus this trade's own budgeted risk, can't exceed this
    # % of account equity. Distinct from a notional-exposure cap (the old
    # max_account_exposure_pct) -- two positions with the same total dollar
    # size but very different stop distances carry very different amounts
    # of *risk*, and this is meant to bound the latter, not the former.
    max_total_risk_pct: float = 50.0
    max_simultaneous_positions: int = 3
    max_trades_per_day: int = 15
    max_trades_per_ticker_per_day: int = 2
    max_spread_pct: float = 2.0
    min_dollar_volume: float = 500_000
    max_slippage_pct: float = 1.0            # reject/adjust if simulated fill would exceed this vs reference price
    stop_loss_required: bool = True
    cooldown_minutes_after_loss: int = 15


@dataclass
class _DailyState:
    day: date
    realized_pnl: float = 0.0
    trade_count: int = 0
    trades_per_ticker: dict[str, int] = field(default_factory=dict)


class RiskEngine:
    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        self.kill_switch_active: bool = False
        self._daily = _DailyState(day=datetime.utcnow().date())
        # Cooldown is a rolling time window (cooldown_minutes_after_loss),
        # not a calendar-day concept -- unlike _DailyState's counters, it
        # must survive _roll_day_if_needed. Keeping it inside _DailyState
        # previously meant a loss recorded shortly before UTC midnight had
        # its cooldown silently wiped by the very next evaluate() call that
        # crossed into the new day, defeating the cooldown exactly when a
        # trade was recent enough to still need it.
        self._last_loss_at: dict[str, datetime] = {}
        self.events: list[RiskEvent] = []

    # -- bookkeeping -----------------------------------------------------

    def _roll_day_if_needed(self, now: datetime) -> None:
        if now.date() != self._daily.day:
            self._daily = _DailyState(day=now.date())

    def record_trade_closed(self, symbol: str, pnl: float, now: Optional[datetime] = None) -> None:
        now = now or datetime.utcnow()
        self._roll_day_if_needed(now)
        self._daily.realized_pnl += pnl
        if pnl < 0:
            self._last_loss_at[symbol] = now

    def record_entry_order_failed(self, symbol: str, now: Optional[datetime] = None) -> None:
        """Rolls back the optimistic trade_count/trades_per_ticker increment
        `evaluate()` makes the moment it approves an entry signal (see the
        bottom of that method) -- risk *approval* only means the signal
        passed risk criteria, not that a real position ever opened. If the
        resulting order is later confirmed REJECTED/CANCELED/EXPIRED by the
        broker without ever filling (e.g. a symbol rejected outside trading
        hours, a transient broker-side rejection), that attempt created no
        real market exposure and must not permanently consume a slot
        against max_trades_per_day/max_trades_per_ticker_per_day the same
        way an actual trade would -- confirmed as a real bug in production:
        two broker-rejected entry attempts on the same symbol, zero
        positions ever opened, silently exhausted that symbol's entire
        daily entry budget for the rest of the session.

        Called from TradingLoop._submit_entry (an immediate non-fill,
        non-pending order status) and _poll_pending_entry (a pending order
        that later resolves to REJECTED/CANCELED/EXPIRED) -- never for a
        risk-engine-level rejection (OrderRejected), since evaluate() never
        incremented anything in that case to begin with.

        Floored at 0 via max(), and re-rolls the day first like every other
        _daily mutator: if the failure is somehow confirmed after a day
        boundary has already passed (evaluate() ran right before midnight,
        the broker's rejection arrives just after), _daily is already a
        fresh, zeroed state for the new day at that point, so this is a
        harmless no-op on it rather than corrupting the new day's counts --
        the stale prior day's inflated counters are simply gone regardless,
        same as any other day-rollover, not something this needs to chase."""
        now = now or datetime.utcnow()
        self._roll_day_if_needed(now)
        self._daily.trade_count = max(0, self._daily.trade_count - 1)
        ticker_count = self._daily.trades_per_ticker.get(symbol, 0)
        if ticker_count > 0:
            self._daily.trades_per_ticker[symbol] = ticker_count - 1

    def engage_kill_switch(self, reason: str, now: Optional[datetime] = None) -> None:
        self.kill_switch_active = True
        self._log_event(RiskEventType.KILL_SWITCH_ENGAGED, None, reason, now)

    def release_kill_switch(self) -> None:
        self.kill_switch_active = False

    def _log_event(self, event_type: RiskEventType, symbol: Optional[str], reason: str, now: Optional[datetime]) -> None:
        self.events.append(
            RiskEvent(event_type=event_type.value, symbol=symbol, timestamp=now or datetime.utcnow(), reason=reason)
        )

    # -- core gate ---------------------------------------------------------

    def evaluate(
        self,
        signal: Signal,
        *,
        account_equity: float,
        account_buying_power: float,
        open_positions: list[Position],
        snapshot: MarketSnapshot,
        now: Optional[datetime] = None,
    ) -> RiskDecision:
        now = now or datetime.utcnow()
        self._roll_day_if_needed(now)

        def reject(event_type: RiskEventType, reason: str) -> RiskDecision:
            self._log_event(event_type, signal.symbol, reason, now)
            return RiskDecision(approved=False, reason=reason)

        if self.kill_switch_active:
            return reject(RiskEventType.KILL_SWITCH_ENGAGED, "Kill switch is active; no new trades.")

        daily_loss_limit = -abs(self.config.max_daily_loss_pct) / 100.0 * account_equity
        if self._daily.realized_pnl <= daily_loss_limit:
            return reject(RiskEventType.DAILY_LOSS_LIMIT_HIT, "Max daily loss limit reached.")

        if self._daily.trade_count >= self.config.max_trades_per_day:
            return reject(RiskEventType.MAX_TRADES_PER_DAY_HIT, "Max trades per day reached.")

        ticker_trades = self._daily.trades_per_ticker.get(signal.symbol, 0)
        if ticker_trades >= self.config.max_trades_per_ticker_per_day:
            return reject(RiskEventType.MAX_TRADES_PER_TICKER_HIT, "Max trades for this ticker today reached.")

        last_loss = self._last_loss_at.get(signal.symbol)
        if last_loss is not None:
            cooldown_until = last_loss + timedelta(minutes=self.config.cooldown_minutes_after_loss)
            if now < cooldown_until:
                return reject(RiskEventType.COOLDOWN_ACTIVE, f"{signal.symbol} is in post-loss cooldown until {cooldown_until.isoformat()}.")

        if len(open_positions) >= self.config.max_simultaneous_positions:
            return reject(RiskEventType.MAX_POSITIONS_HIT, "Max simultaneous positions reached.")

        spread_abs = snapshot.ask - snapshot.bid if snapshot.ask and snapshot.bid else 0.0
        mid = (snapshot.ask + snapshot.bid) / 2 if snapshot.ask and snapshot.bid else snapshot.last_price
        spread_pct = (spread_abs / mid * 100.0) if mid else 100.0
        if spread_pct > self.config.max_spread_pct:
            return reject(RiskEventType.SPREAD_TOO_WIDE, f"Spread {spread_pct:.2f}% exceeds max {self.config.max_spread_pct}%.")

        dollar_volume = snapshot.last_price * snapshot.cumulative_volume
        if dollar_volume < self.config.min_dollar_volume:
            return reject(RiskEventType.LIQUIDITY_TOO_LOW, f"Dollar volume {dollar_volume:,.0f} below minimum {self.config.min_dollar_volume:,.0f}.")

        if self.config.stop_loss_required and signal.suggested_stop is None:
            return reject(RiskEventType.TRADE_REJECTED, "Signal has no stop-loss; stop_loss_required is enabled.")

        if signal.suggested_target is not None and signal.suggested_stop is not None and signal.reference_price > signal.suggested_stop > 0:
            risk_per_share = signal.reference_price - signal.suggested_stop
            reward_per_share = signal.suggested_target - signal.reference_price
            # A tiny epsilon avoids rejecting a signal that's exactly at the
            # minimum ratio in theory but lands a hair below it in floating
            # point (e.g. a strategy computing target = entry + risk * 2.0
            # doesn't always reconstruct to a bit-identical 2.0x when
            # re-divided here).
            if risk_per_share > 0 and (reward_per_share / risk_per_share) < self.config.min_risk_reward_ratio - 1e-9:
                return reject(
                    RiskEventType.MIN_RISK_REWARD_NOT_MET,
                    f"Reward:risk ratio {reward_per_share / risk_per_share:.2f} is below the required minimum {self.config.min_risk_reward_ratio}.",
                )

        # Total assumed risk across every open position (entry-to-stop $ risk,
        # not notional size -- see RiskConfig.max_total_risk_pct's docstring
        # for why this is a materially different cap than a plain notional
        # exposure ceiling).
        total_open_risk = sum(
            max(0.0, p.avg_entry_price - p.stop_price) * p.quantity
            for p in open_positions
            if p.stop_price is not None
        )
        max_total_risk = account_equity * self.config.max_total_risk_pct / 100.0
        remaining_risk_room = max_total_risk - total_open_risk
        if remaining_risk_room <= 0:
            return reject(RiskEventType.MAX_EXPOSURE_HIT, "Max total assumed risk across open positions reached.")

        # Position sizing from $ risk between entry and stop (never fall back to a
        # position size that ignores the stop distance). The risk actually
        # budgeted for this trade is capped by whatever total-risk room is
        # left, so one trade can't single-handedly blow through the fleet-wide
        # ceiling even if its own risk_per_trade_pct budget would allow it.
        entry_price = signal.reference_price
        stop_price = signal.suggested_stop
        budgeted_risk = account_equity * self.config.risk_per_trade_pct / 100.0
        risk_amount = min(budgeted_risk, remaining_risk_room)
        max_shares_by_risk = None
        if stop_price is not None and entry_price > stop_price > 0:
            per_share_risk = entry_price - stop_price
            max_shares_by_risk = int(risk_amount // per_share_risk) if per_share_risk > 0 else 0
        elif self.config.stop_loss_required:
            return reject(RiskEventType.TRADE_REJECTED, "Stop price must be below entry price for a long signal.")

        max_notional = account_buying_power * self.config.max_position_size_pct / 100.0
        max_shares_by_notional = int(max_notional // entry_price) if entry_price > 0 else 0

        max_shares = min(v for v in (max_shares_by_risk, max_shares_by_notional) if v is not None)

        if max_shares <= 0:
            return reject(RiskEventType.TRADE_REJECTED, "Computed position size is zero given current risk/exposure limits.")

        self._daily.trade_count += 1
        self._daily.trades_per_ticker[signal.symbol] = ticker_trades + 1

        return RiskDecision(approved=True, reason="OK", max_shares=max_shares, risk_amount=risk_amount)
