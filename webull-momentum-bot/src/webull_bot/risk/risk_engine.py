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
from ..market_hours import is_within_core_trading_hours, is_within_extended_trading_hours
from ..models import MarketSnapshot, Position, RiskDecision, RiskEvent, Signal

# Webull's own hard ceiling on a single order's share quantity -- confirmed
# live 2026-08-12 (OAUTH_OPENAPI_ORDER_QUANTITY_EXCEED_LIMIT, HTTP 417,
# "Order quantity must be below 200,000") when a cheap/penny-priced signal
# (DOGZ) combined with max_position_size_pct=100% and a large sandbox
# buying-power balance to compute a share count well past this. This is a
# broker-side constraint independent of whatever max_position_size_pct is
# configured to -- evaluate() clamps to it below so a misconfigured (or
# just unlucky: cheap stock + large buying power) sizing calculation
# reliably produces a rejected/undersized order at worst, never the
# "every single entry silently fails" starvation this incident caused
# (every trigger on the symbol kept re-attempting the same oversized
# order and reverting to ARMED, never actually opening a position).
_WEBULL_MAX_ORDER_QUANTITY = 199_999


@dataclass
class RiskConfig:
    # Renamed from risk_per_trade_pct (2026-08-11) -- and not just a rename,
    # a real change in what this number MEANS. It used to be "% of account
    # equity to risk on this trade" (an entry-to-stop dollar budget that,
    # combined with the stop distance, determined share count -- see
    # max_shares_by_risk in a prior version of evaluate() below). It's now
    # a genuine per-position stop-loss distance: % below entry (long) / above
    # entry (short) the stop is placed. Read live by every "flat %" entry
    # strategy (momentum_breakout, refined_breakout, opening_range_breakout,
    # volatility_contraction, volume_ignition) via a stop_loss_pct_fn closure
    # -- see main.py's build_trading_loop -- the same live-wiring pattern
    # already used for min_risk_reward_ratio below, so a dashboard change
    # here moves every one of those strategies' stops together. The three
    # "buffer below a technical level" strategies (breakout_pullback,
    # ignition_pullback, vwap_reclaim) are NOT wired to this -- their stop
    # is anchored to a pullback low / VWAP, a fundamentally different,
    # much smaller-scale concept (a safety margin below a level the entry
    # is betting will hold, not "how far the stop should be from entry");
    # forcing them to share this value would corrupt their design intent.
    # No longer used for position sizing at all -- see max_position_size_pct.
    stop_loss_pct: float = 5.0
    # Minimum reward:risk ratio required on any signal that specifies a
    # suggested_target (e.g. 2.0 means the distance from entry to target
    # must be at least 2x the distance from entry to stop). Signals with no
    # target (e.g. VolumeIgnitionStrategy, which relies on a trailing stop
    # instead of a fixed target) skip this check entirely -- there's no
    # reward distance to compare against.
    min_risk_reward_ratio: float = 2.0
    # The ONLY thing governing position size (2026-08-11, see evaluate()
    # below): max notional as % of buying power in a single position. A
    # signal's stop distance is no longer a sizing input at all -- it only
    # ever determined WHERE the stop sits, never HOW MANY shares to buy.
    max_position_size_pct: float = 100.0
    max_daily_loss_pct: float = 3.0          # halts all new trades for the day once hit
    # Total $-at-risk ceiling: the sum of entry-to-stop risk across every
    # currently open position can't exceed this % of account equity --
    # rejects a NEW entry outright once already at/over the cap. Distinct
    # from a notional-exposure cap (the old max_account_exposure_pct) --
    # two positions with the same total dollar size but very different stop
    # distances carry very different amounts of *risk*, and this is meant
    # to bound the latter, not the former. Since sizing is now purely
    # notional-based (max_position_size_pct), this is a coarser gate than
    # it used to be: it no longer trims a new trade's OWN size to fit
    # whatever room remains (there is no "size the trade to fit the
    # remaining risk budget" step anymore) -- it can only refuse the trade
    # outright once the ceiling is already breached by existing positions.
    max_total_risk_pct: float = 50.0
    # Dashboard-adjustable (2026-08-11). 0 means unlimited -- not "reject
    # every entry," which a literal 0-as-a-count reading would otherwise
    # imply (see the `if` guard in evaluate() below).
    max_simultaneous_positions: int = 3
    max_trades_per_day: int = 15
    max_trades_per_ticker_per_day: int = 2
    max_spread_pct: float = 2.0
    min_dollar_volume: float = 500_000
    max_slippage_pct: float = 1.0            # reject/adjust if simulated fill would exceed this vs reference price
    stop_loss_required: bool = True
    cooldown_minutes_after_loss: int = 15
    # Dashboard-adjustable (2026-08-12). Off by default -- widens the entry
    # gate below from CORE-only (9:30am-4:00pm ET) to CORE+pre-market+
    # after-hours (4:00am-8:00pm ET, see market_hours.is_within_extended_
    # trading_hours). Deliberately does NOT, by itself, guarantee an
    # extended-hours signal actually reaches the market: WebullBrokerClient
    # still submits every order with a `support_trading_session` value
    # controlled by Settings.webull_support_trading_session (env var, still
    # defaults to "CORE" in code) -- see that field's docstring and
    # brokers/webull/client.py's _order_payload docstring for the full
    # history. "ALL" is now confirmed live (sandbox, 2026-08-12) to be
    # accepted by this account, so turning this flag on is only meaningful
    # once WEBULL_SUPPORT_TRADING_SESSION=ALL is also set in the
    # deployment's env -- with the code default still "CORE", flipping
    # only this flag lets entries THROUGH this gate but the broker will
    # still receive a "CORE"-scoped order underneath.
    allow_extended_hours_trading: bool = False


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
        # Dashboard's bot ON/OFF toggle -- see disable_bot/enable_bot
        # below. Deliberately a separate flag from kill_switch_active
        # (not reused under the hood) so the two controls stay
        # independently readable/settable: engaging the kill switch must
        # not silently flip this, and vice versa.
        self.bot_enabled: bool = True
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
        # Set by pause_new_entries (dashboard's per-position "Close"
        # button, see TradingLoop.request_manual_close) -- a short,
        # self-expiring block on NEW entries only, distinct from
        # kill_switch_active in every way that matters: it clears itself
        # (no manual disengage needed), it never touches existing
        # positions, and its purpose is purely to free up rate-limiter
        # contention for a specific in-flight exit, not to halt trading.
        self._entries_paused_until: Optional[datetime] = None

    # -- bookkeeping -----------------------------------------------------

    def _roll_day_if_needed(self, now: datetime) -> None:
        if now.date() != self._daily.day:
            self._daily = _DailyState(day=now.date())

    def pause_new_entries(self, seconds: float, *, now: Optional[datetime] = None) -> None:
        """Blocks new entries in evaluate() below until `seconds` from
        `now` (real wall clock if omitted) -- see TradingLoopConfig.
        manual_close_entry_pause_seconds' docstring for the incident this
        exists to address. Idempotent-ish: calling this again before the
        current pause expires simply extends it to the new end time,
        rather than stacking -- there's only ever one pause window, not a
        counter."""
        now = now or datetime.utcnow()
        self._entries_paused_until = now + timedelta(seconds=seconds)

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

    def disable_bot(self, reason: str, now: Optional[datetime] = None) -> None:
        """Dashboard's bot ON/OFF toggle, turned off. Blocks all new
        entries immediately (evaluate() below checks this every call,
        same as kill_switch_active) -- the actual force-close of open
        positions happens on TradingLoop's own processing thread (see
        TradingLoop.disable_bot/_process_all_candidates), not here."""
        self.bot_enabled = False
        self._log_event(RiskEventType.BOT_DISABLED, None, reason, now)

    def enable_bot(self) -> None:
        self.bot_enabled = True

    def _log_event(self, event_type: RiskEventType, symbol: Optional[str], reason: str, now: Optional[datetime]) -> None:
        self.events.append(
            RiskEvent(event_type=event_type.value, symbol=symbol, timestamp=now or datetime.utcnow(), reason=reason)
        )

    def record_operational_event(
        self, event_type: RiskEventType, symbol: Optional[str], reason: str, now: Optional[datetime] = None,
    ) -> None:
        """Public counterpart to _log_event, for callers OUTSIDE this class
        that need to surface something onto the same events list/dashboard
        panel `evaluate()`'s own internal rejections already use -- e.g.
        TradingLoop reporting a position that's gone too long without a
        broker-side protective bracket (POSITION_UNPROTECTED_TOO_LONG).
        Not a signal rejection, just a shared, already-visible place to put
        an operational warning a human should notice."""
        self._log_event(event_type, symbol, reason, now)

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

        if not self.bot_enabled:
            return reject(RiskEventType.BOT_DISABLED, "Bot is turned off; no new trades.")

        # Short, self-expiring pause (see pause_new_entries) -- distinct
        # from kill_switch_active above: this clears itself and exists
        # purely to free up rate-limiter contention for an in-flight
        # manual position close, not to halt trading generally.
        if self._entries_paused_until is not None and now < self._entries_paused_until:
            return reject(
                RiskEventType.ENTRIES_TEMPORARILY_PAUSED,
                "New entries temporarily paused while a manual position close is in progress.",
            )

        # New entries only, by design -- OrderManager.submit_signal never
        # routes EXIT/SCALE_OUT signals through evaluate() at all (see its
        # docstring), so this can't block closing an existing position. This
        # is a hard, unconditional gate (no config flag) fixing a real
        # production case: a signal triggered and filled during core hours
        # itself, but with no application-level check confirming that before
        # ever attempting the order, a rejected/queued out-of-hours fill was
        # only ever caught after the fact (or not at all) rather than
        # prevented up front. Deliberately checked against `now` (the
        # engine's own clock parameter, defaulted to datetime.utcnow() above)
        # rather than the snapshot's timestamp, so a stale/replayed snapshot
        # can't be used to slip a signal through outside real market hours.
        if self.config.allow_extended_hours_trading:
            if not is_within_extended_trading_hours(now):
                return reject(
                    RiskEventType.OUTSIDE_CORE_TRADING_HOURS,
                    "Outside extended trading hours (4:00am-8:00pm ET, Mon-Fri); new entries are not permitted.",
                )
        elif not is_within_core_trading_hours(now):
            return reject(
                RiskEventType.OUTSIDE_CORE_TRADING_HOURS,
                "Outside core trading hours (9:30am-4:00pm ET, Mon-Fri); new entries are not permitted.",
            )

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

        if self.config.max_simultaneous_positions and len(open_positions) >= self.config.max_simultaneous_positions:
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
        # exposure ceiling). A pure reject-gate now (2026-08-11) -- it no
        # longer trims this trade's OWN size to fit remaining room, since
        # sizing isn't risk-budget-driven anymore; see max_position_size_pct
        # below for what actually determines share count.
        total_open_risk = sum(
            max(0.0, p.avg_entry_price - p.stop_price) * p.quantity
            for p in open_positions
            if p.stop_price is not None
        )
        max_total_risk = account_equity * self.config.max_total_risk_pct / 100.0
        if total_open_risk >= max_total_risk:
            return reject(RiskEventType.MAX_EXPOSURE_HIT, "Max total assumed risk across open positions reached.")

        entry_price = signal.reference_price
        stop_price = signal.suggested_stop
        if stop_price is not None and entry_price > stop_price > 0:
            pass  # a valid long stop -- nothing further needed for sizing
        elif self.config.stop_loss_required:
            return reject(RiskEventType.TRADE_REJECTED, "Stop price must be below entry price for a long signal.")

        # Position sizing (2026-08-11): purely notional, from
        # max_position_size_pct of buying power -- the stop distance no
        # longer plays any role in HOW MANY shares to buy, only in WHERE
        # the stop sits (see RiskConfig.stop_loss_pct's docstring for why
        # that split was made). A signal simply can't be sized at all
        # without account_buying_power > 0 or entry_price > 0.
        max_notional = account_buying_power * self.config.max_position_size_pct / 100.0
        max_shares = int(max_notional // entry_price) if entry_price > 0 else 0
        # Clamp to Webull's own hard per-order ceiling -- see
        # _WEBULL_MAX_ORDER_QUANTITY's docstring for the real incident this
        # guards against (a cheap stock + a generous max_position_size_pct
        # computing a share count the broker rejects outright).
        max_shares = min(max_shares, _WEBULL_MAX_ORDER_QUANTITY)

        if max_shares <= 0:
            return reject(RiskEventType.TRADE_REJECTED, "Computed position size is zero given current risk/exposure limits.")

        self._daily.trade_count += 1
        self._daily.trades_per_ticker[signal.symbol] = ticker_trades + 1

        # Informational only (2026-08-11) -- the actual $ this specific,
        # already-sized trade would lose if its stop is hit. Purely
        # descriptive now, computed AFTER sizing rather than driving it;
        # None whenever there's no valid stop distance to compute from
        # (stop_loss_required=False and no suggested_stop).
        risk_amount = None
        if stop_price is not None and entry_price > stop_price > 0:
            risk_amount = (entry_price - stop_price) * max_shares

        return RiskDecision(approved=True, reason="OK", max_shares=max_shares, risk_amount=risk_amount)
