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
    risk_per_trade_pct: float = 0.5          # % of account equity risked per trade (entry-to-stop)
    max_position_size_pct: float = 10.0      # max notional as % of equity in a single position
    max_daily_loss_pct: float = 3.0          # halts all new trades for the day once hit
    max_account_exposure_pct: float = 50.0   # max total notional across all open positions
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

        current_exposure = sum(p.quantity * p.avg_entry_price for p in open_positions)
        max_exposure = account_equity * self.config.max_account_exposure_pct / 100.0
        if current_exposure >= max_exposure:
            return reject(RiskEventType.MAX_EXPOSURE_HIT, "Max account exposure reached.")

        # Position sizing from $ risk between entry and stop (never fall back to a
        # position size that ignores the stop distance).
        entry_price = signal.reference_price
        stop_price = signal.suggested_stop
        risk_amount = account_equity * self.config.risk_per_trade_pct / 100.0
        max_shares_by_risk = None
        if stop_price is not None and entry_price > stop_price > 0:
            per_share_risk = entry_price - stop_price
            max_shares_by_risk = int(risk_amount // per_share_risk) if per_share_risk > 0 else 0
        elif self.config.stop_loss_required:
            return reject(RiskEventType.TRADE_REJECTED, "Stop price must be below entry price for a long signal.")

        max_notional = account_equity * self.config.max_position_size_pct / 100.0
        max_shares_by_notional = int(max_notional // entry_price) if entry_price > 0 else 0

        max_shares = min(v for v in (max_shares_by_risk, max_shares_by_notional) if v is not None)
        remaining_exposure_room = max_exposure - current_exposure
        max_shares = min(max_shares, int(remaining_exposure_room // entry_price)) if entry_price > 0 else 0

        if max_shares <= 0:
            return reject(RiskEventType.TRADE_REJECTED, "Computed position size is zero given current risk/exposure limits.")

        self._daily.trade_count += 1
        self._daily.trades_per_ticker[signal.symbol] = ticker_trades + 1

        return RiskDecision(approved=True, reason="OK", max_shares=max_shares, risk_amount=risk_amount)
