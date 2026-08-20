"""
In-memory domain models shared across scanner, strategy, risk, execution,
and data-collection layers. These are plain dataclasses -- persistence
mapping lives separately in db/models.py so the trading logic never depends
on the ORM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .enums import (
    CandidateState,
    ExitReason,
    MomentumOutcome,
    MomentumPhase,
    OrderSide,
    OrderStatus,
    OrderType,
    SignalAction,
    TimeInForce,
    TradeBlockReason,
    TradeSide,
)
from .metrics.volume_baseline import VolumeBaseline


@dataclass
class FloatData:
    symbol: str
    free_float_shares: float
    shares_outstanding: float
    market_cap: Optional[float]
    float_percent: Optional[float]           # free_float / shares_outstanding
    effective_date: Optional[datetime]        # as-of date reported by the provider
    fetched_at: datetime
    source: str = "massive"


@dataclass
class MarketSnapshot:
    """A single point-in-time read of a symbol's tape/quote state."""
    symbol: str
    timestamp: datetime
    last_price: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    cumulative_volume: float
    vwap: float
    high_of_day: float
    low_of_day: float
    open_price: float
    premarket_high: Optional[float] = None
    prev_close: Optional[float] = None


@dataclass
class TickRecord:
    """A single individual trade print from Webull's TICK streaming
    sub-type (see brokers/webull/client.py's subscribe_quotes/
    _tick_from_streamed_result) -- fundamentally different from
    MarketSnapshot, which is a periodic AGGREGATED state (cumulative
    volume, OHLC), not one trade. `volume` here is THIS trade's own size,
    not a running total. `side` is the trade's aggressor classification,
    TradeSide.UNKNOWN by default until Webull's real wire encoding for it
    is confirmed live -- see enums.TradeSide's docstring and
    brokers/webull/client.py's _TICK_SIDE_MAP."""
    symbol: str
    timestamp: datetime
    price: float
    volume: float
    side: TradeSide


@dataclass
class MomentumMetrics:
    """Derived metrics computed from a rolling window of MarketSnapshots + FloatData."""
    symbol: str
    timestamp: datetime

    float_turnover: float             # cumulative_volume / free_float -- i.e. today's float turnover
    float_velocity_1m: float          # volume in last 1m / free_float
    float_velocity_3m: float
    float_velocity_5m: float          # i.e. 5-minute float turnover (volume_last_5m / free_float)

    relative_volume: float            # current volume vs typical volume at this time of day (whole session)
    # Same idea as relative_volume above but windowed: current 1m/5m volume
    # vs. this symbol's typical 1m/5m volume. Defaults to a neutral 1.0
    # (via relative_volume()'s own safe-division default) until a real
    # per-symbol intraday volume-distribution baseline exists to compare
    # against -- see compute_metrics' typical_volume_1m/5m parameters,
    # which mirror the already-existing typical_volume_same_time pattern
    # rather than inventing a new one.
    relative_volume_1m: float
    relative_volume_5m: float
    volume_accel_1m_3m: float         # recent 1m volume rate vs preceding 3m average rate

    # Raw share volume in each trailing window (not float- or dollar-normalized).
    volume_1m: float
    volume_5m: float
    volume_15m: float

    # Dollar volume in each trailing window, approximated via
    # dollar_volume_from_avg_price (boundary-price averaging -- see that
    # function's docstring). Distinct from `dollar_volume` below, which is
    # today's *cumulative* dollar volume at the current price.
    dollar_volume_1m: float
    dollar_volume_5m: float
    dollar_volume_15m: float
    # Recent-vs-preceding dollar-volume rate ratio, mirroring
    # volume_accel_1m_3m's share-based version but in dollars -- genuinely
    # distinct (not just a rescaled duplicate) because dollar_volume_1m/5m
    # use different boundary-price averages per window, so this also
    # reflects price movement between windows, not just volume alone.
    dollar_volume_accel_1m_3m: float

    price_velocity_1m: float          # % price change over window (i.e. price_change_1m)
    price_velocity_3m: float
    price_velocity_5m: float          # i.e. price_change_5m
    price_velocity_15m: float
    price_acceleration: float         # change in price velocity itself

    vwap: float
    distance_from_vwap_pct: float
    distance_from_hod_pct: float
    distance_from_premarket_high_pct: Optional[float]
    distance_from_resistance_pct: Optional[float]

    spread_abs: float
    spread_pct: float
    dollar_volume: float               # today's cumulative dollar volume (i.e. dollar_volume_today)
    # High-low range of last_price samples as a % of their average, over a
    # tight recent window and a broader context window -- see
    # calculations.price_range_pct's docstring for why this is a coarse,
    # snapshot-level proxy rather than true intra-window OHLC. Used by
    # strategy/volatility_contraction.py to detect a recent range
    # contraction (price_range_pct_3m much smaller than price_range_pct_15m)
    # ahead of a volume-backed expansion.
    price_range_pct_3m: float = 0.0
    price_range_pct_15m: float = 0.0
    trade_velocity: Optional[float] = None  # trades/sec, once tick data is wired up

    # -- TICK-derived order flow (2026-08-14, see metrics/rolling.py's
    # compute_metrics `ticks` parameter and brokers/webull/client.py's
    # get_recent_ticks) -- the one signal SNAPSHOT/QUOTE structurally can't
    # give us: which side was aggressive, not just that volume happened.
    # buy_volume_1m/sell_volume_1m are 0.0 (not None) even with zero TICK
    # data -- there's genuinely zero classified volume in that case, a
    # real (if uninteresting) number, unlike order_flow_imbalance_1m below.
    buy_volume_1m: float = 0.0
    sell_volume_1m: float = 0.0
    # (buy_volume_1m - sell_volume_1m) / (buy_volume_1m + sell_volume_1m),
    # -1.0 (all seller-initiated) to +1.0 (all buyer-initiated). None
    # whenever buy_volume_1m + sell_volume_1m is 0 -- no TICK subscription
    # yet, no trades in the window, or every entry in
    # brokers/webull/client.py's _TICK_SIDE_MAP guess turns out wrong for
    # this account's real feed. Deliberately None, not 0.0/"confirmed
    # neutral," so scoring/momentum_ignition_score.py's compute_score can
    # exclude it from the weighted average exactly like it already does
    # for room_to_target_score, rather than scoring unmeasured flow as
    # balanced.
    order_flow_imbalance_1m: Optional[float] = None
    # How many classified (BUY or SELL, never UNKNOWN) prints fed the
    # imbalance above -- both scoring/momentum_ignition_score.py's
    # order_flow_score and TradingLoopConfig.order_flow_min_sample_count_for_gate
    # require this to clear a minimum before trusting the ratio at all, so
    # a symbol with e.g. one lone classified print in the window doesn't
    # score/gate on a statistically meaningless reading.
    order_flow_sample_count_1m: int = 0

    # -- Real-Time Momentum Qualification Layer (2026-08-17, see
    # scanner/momentum_qualification.py and scoring/rtms.py) -- short-window
    # returns/velocity/acceleration derived from TICK-level trade prints
    # (WebullBrokerClient.get_recent_ticks, ~90s/2000-tick retention),
    # distinct from price_velocity_1m/3m/5m/15m above (which are
    # snapshot-history derived and untouched by this addition -- no
    # existing consumer of those fields is affected). return_5s/15s/30s/60s
    # use the tick buffer; return_2m/3m/5m fall back to the existing
    # snapshot-history windows (the tick buffer's ~90s retention can't
    # reach 2-5 minutes back) -- see metrics/rolling.py's compute_metrics
    # for exactly how each is derived. All are None (never 0.0) whenever
    # there isn't enough history yet to measure the window -- every
    # downstream momentum gate must treat None as "not met" (fail-closed),
    # never as a neutral/zero reading.
    return_5s: Optional[float] = None
    return_15s: Optional[float] = None
    return_30s: Optional[float] = None
    return_60s: Optional[float] = None
    return_2m: Optional[float] = None
    return_3m: Optional[float] = None
    return_5m: Optional[float] = None

    # Price velocity (return_pct / window_seconds) at each short window --
    # reuses calculations.price_velocity_pct's own output divided by the
    # window length, not a separate calculation.
    velocity_5s: Optional[float] = None
    velocity_15s: Optional[float] = None
    velocity_30s: Optional[float] = None
    velocity_60s: Optional[float] = None

    # Change in velocity itself vs. the immediately preceding same-length
    # window (calculations.price_acceleration, reused verbatim -- not
    # reimplemented) -- is the short-term move itself speeding up or
    # slowing down. None whenever either the current or the prior window's
    # velocity isn't measurable yet.
    acceleration_5s: Optional[float] = None
    acceleration_15s: Optional[float] = None
    acceleration_30s: Optional[float] = None
    acceleration_60s: Optional[float] = None

    # Directional trend efficiency (calculations.trend_efficiency:
    # net_move / total_path, long-only) over the trailing window -- how
    # "clean" the move has been, not just how big. 0.0 (not None) when the
    # window doesn't have enough ticks yet -- see trend_efficiency's own
    # docstring for why 0.0 is the correct "no measurable efficiency"
    # reading here, unlike the return_*/velocity_*/acceleration_* fields
    # above. trend_efficiency_5m is logged for future research only --
    # deliberately not used as a hard gate anywhere yet.
    trend_efficiency_15s: float = 0.0
    trend_efficiency_60s: float = 0.0
    trend_efficiency_5m: float = 0.0

    # The highest trade print in the trailing 15 seconds, and when it
    # happened -- feeds the IMPULSING quality gate's "current price within
    # X% of the recent high, and that high is fresh (made within the last
    # ~4s)" check, and RTMS's fresh_high_reclaim_score. None whenever the
    # tick buffer doesn't have any prints in that window yet.
    recent_high_15s: Optional[float] = None
    recent_high_15s_time: Optional[datetime] = None


@dataclass
class RTMSComponents:
    """Each sub-score is normalized to 0-100 before weighting in the RTMS
    calculator (scoring/rtms.py) -- same shape/contract as
    MomentumScoreComponents below, but for the Real-Time Momentum Score
    (RTMS), a live "is this stock moving right now" score deliberately
    separate from MIS's "is this stock structurally interesting" one. Any
    component may be None when its underlying metric isn't measurable yet
    (e.g. order_flow_trade_velocity_score before enough classified TICK
    volume exists) -- compute_rtms excludes a None component from the
    weighted average and renormalizes over the remaining active weights,
    identical to momentum_ignition_score.compute_score's own contract.

    regime_distance_score (rtms-v4, 2026-08-19): the heaviest-weighted
    component by explicit user request -- how far metrics.return_5m sits
    above the hard entry gate (min_return_5m_pct), NOT return_5m itself.
    See scoring/rtms.py's compute_rtms_components for why distance-above-
    floor rather than the raw return."""
    regime_distance_score: Optional[float]
    momentum_15s_score: Optional[float]
    momentum_30s_score: Optional[float]
    momentum_60s_score: Optional[float]
    price_acceleration_score: Optional[float]
    trend_efficiency_score: Optional[float]
    fresh_high_reclaim_score: Optional[float]
    order_flow_trade_velocity_score: Optional[float]
    volume_acceleration_score: Optional[float]


@dataclass
class RTMSScore:
    symbol: str
    timestamp: datetime
    score: float  # 0-100 final Real-Time Momentum Score
    components: RTMSComponents
    weights_version: str


@dataclass
class MomentumState:
    """Live price-behavior tracking for an ARMED/CONFIRMING candidate --
    see enums.MomentumPhase's docstring for why this is a field on
    Candidate rather than a new CandidateState. Reset to a fresh
    MomentumState() at the two genuine "starting over" seams
    (ARMED->HEATING_UP cool-off in candidate_watcher.py, COOLDOWN->WATCHING
    in trading_loop.py's _process_candidate_inner) -- deliberately NOT
    reset on a CONFIRMING->ARMED revert caused by a healthy pullback
    (TradingLoop._poll_confirmation), since that candidate needs its
    impulse/pullback tracking preserved to detect REACCELERATING on the
    very next tick."""
    phase: MomentumPhase = MomentumPhase.NONE
    phase_changed_at: Optional[datetime] = None

    # The current momentum impulse -- see scanner/momentum_qualification.py
    # for exactly how these ratchet/reset. impulse_high tracks the running
    # peak since impulse_start_price; impulse_size_pct/current_retracement_pct/
    # retracement_ratio are recomputed each tick from these two.
    impulse_start_price: Optional[float] = None
    impulse_start_time: Optional[datetime] = None
    impulse_high: Optional[float] = None
    impulse_high_time: Optional[datetime] = None
    impulse_size_pct: Optional[float] = None
    current_retracement_pct: Optional[float] = None
    retracement_ratio: Optional[float] = None

    # Set only while phase is PULLING_BACK/REACCELERATING -- pullback_low is
    # the lowest price seen since the pullback began (distinct from, and
    # generally shallower than, Candidate.pullback_low, which is the
    # BreakoutPullbackStrategy's OWN tracked low used for its trigger
    # condition); pullback_micro_high is the short-term reclaim level
    # tracked during stabilization, used to detect REACCELERATING without
    # requiring a full reclaim of impulse_high.
    pullback_low: Optional[float] = None
    pullback_low_time: Optional[datetime] = None
    pullback_micro_high: Optional[float] = None

    # The strategy whose structural level (resistance/VWAP/pullback low)
    # governs the CURRENT impulse's pullback validation
    # (scanner/momentum_structure.py) -- set when the impulse starts (from
    # whichever strategy's signal was firing then, if any) and reused on
    # later ticks where no fresh signal fires (a strategy doesn't
    # necessarily re-fire every tick during a pullback, but the phase
    # tracking here runs every tick regardless). None if the impulse
    # started from pure price action before any of the 8 strategies had
    # actually fired yet -- momentum_structure_intact is skipped (treated
    # as intact) in that case, same fail-open contract as an unknown
    # resistance level.
    active_strategy_name: Optional[str] = None

    # This tick's RTMS (and the components that produced it), plus the
    # prior tick's RTMS/5s-velocity -- needed for REACCELERATING's "RTMS
    # rising"/"velocity increasing" checks, which compare against the
    # immediately preceding tick, not a fixed window.
    rtms: Optional[float] = None
    rtms_components: Optional[RTMSComponents] = None
    prior_rtms: Optional[float] = None
    prior_velocity_5s: Optional[float] = None

    # Whether the firing strategy's own structural level (resistance/VWAP/
    # pullback low -- see scanner/momentum_structure.py) is currently
    # intact; None before it's ever been checked this arm cycle.
    structure_intact: Optional[bool] = None

    # Dashboard-visible summary of the most recent qualification decision
    # -- see scanner/momentum_qualification.py's TriggerDecision.
    momentum_qualified: bool = False
    block_reason: Optional[str] = None
    last_evaluated_at: Optional[datetime] = None


@dataclass
class MomentumScoreComponents:
    """Each sub-score is normalized to 0-100 before weighting in the MIS calculator."""
    float_score: float
    float_velocity_score: float
    relative_volume_score: float
    volume_acceleration_score: float
    price_acceleration_score: float
    breakout_proximity_score: float
    liquidity_score: float
    # v2 additions (2026-08-09): these three were already computed by
    # metrics/rolling.py's compute_metrics but sat unused on MomentumMetrics
    # -- see this dataclass's docstring history / ARCHITECTURE.md's
    # "Momentum Ignition Score" section. All three measure current/real-time
    # activity level rather than float size or price behavior, which is
    # deliberate: they're what should make an already-popular, actively-
    # trading-right-now name outrank a name that merely looks structurally
    # attractive (small float, near resistance) but isn't seeing real
    # volume yet.
    float_turnover_score: float             # today's cumulative float turnover (metrics.float_turnover) -- "how much of this stock has already changed hands today"
    short_term_relative_volume_score: float  # windowed RVOL (metrics.relative_volume_5m) -- more responsive to a fresh surge than the whole-session relative_volume
    dollar_volume_acceleration_score: float  # metrics.dollar_volume_accel_1m_3m -- distinct from volume_acceleration_score since it also reflects price movement between windows, not just share count
    # v2.2 addition (2026-08-13, entry-selectivity rework -- see
    # docs/ARCHITECTURE.md): how much room exists between the fixed +stop*R
    # target and the next known static resistance level
    # (metrics/volume_profile.py's evaluate_target_clearance). Optional,
    # unlike every component above: only None when the caller didn't pass
    # current-price/resistance context into compute_score (e.g. an older
    # direct call site, or a test exercising the other components in
    # isolation) -- compute_score excludes a None component from the
    # weighted average and renormalizes over the remaining active weights
    # rather than scoring it as a 0, so an unscoreable component never
    # silently drags the whole MIS down.
    room_to_target_score: Optional[float] = None
    # v2.3 addition (2026-08-14, TICK-derived order flow -- see
    # docs/ARCHITECTURE.md): buy-vs-sell aggressor volume from the TICK
    # streaming sub-type, the one signal SNAPSHOT/QUOTE structurally can't
    # provide (they show volume happened, never which side was
    # aggressive). Same None-when-unavailable contract as
    # room_to_target_score -- None until MomentumMetrics.order_flow_sample_count_1m
    # clears weights.yaml's min_order_flow_sample_count threshold, and
    # currently effectively inert in production until Webull's real TICK
    # `side` string encoding is confirmed live (brokers/webull/client.py's
    # _TICK_SIDE_MAP) -- deliberately fails toward "no signal" (None)
    # rather than a confidently wrong one if that map's guesses are wrong.
    order_flow_score: Optional[float] = None
    # v2.4 addition (2026-08-19): raw upward price velocity itself --
    # distinct from price_acceleration_score above, which measures whether
    # the move is SPEEDING UP, not whether/how much price is actually
    # moving up right now. Blends metrics.price_velocity_1m/5m (both
    # always-populated snapshot-history fields, unlike the newer Optional
    # TICK-buffer return_5s/15s/... fields -- this keeps the component
    # meaningful even for a just-discovered WATCHING candidate with no tick
    # stream yet) through a three-point progressive curve (metrics/
    # calculations.py's scale3) so a strong run reads as meaningfully
    # higher than a barely-positive one, not both maxing out past a single
    # threshold. Typed Optional (like room_to_target_score/order_flow_score
    # above) purely so existing positional MomentumScoreComponents(...)
    # construction in tests keeps working -- in practice this is always a
    # real float, since both source metrics are never None.
    price_momentum_score: Optional[float] = None
    # v2.6 addition (explicit user request): scores metrics.return_5m --
    # the SAME metric/threshold scanner/momentum_qualification.py's
    # evaluate_trigger hard-gates ARMED->CONFIRMING entries on (see
    # scoring/rtms_weights.yaml's min_return_5m_pct), deliberately NOT
    # price_velocity_5m (that's price_momentum_score above -- a different
    # metric with its own, unrelated thresholds). Before this addition, a
    # candidate got zero MIS credit for clearing the exact regime bar RTMS
    # later hard-gates entries on -- this closes that gap by letting a
    # strong regime reading also help push a candidate toward
    # armed_score_threshold itself, alongside (not instead of) the other
    # 13 components. Same None-when-unavailable contract as
    # room_to_target_score/order_flow_score above: metrics.return_5m is
    # None (not 0.0) until ~5 minutes of snapshot history exists, and a
    # brand-new candidate must not be scored as though it failed this
    # component when it simply isn't measurable yet.
    momentum_regime_score: Optional[float] = None


@dataclass
class MomentumScore:
    symbol: str
    timestamp: datetime
    score: float  # 0-100 final Momentum Ignition Score
    components: MomentumScoreComponents
    weights_version: str


@dataclass
class Candidate:
    symbol: str
    state: CandidateState
    discovered_at: datetime
    last_updated_at: datetime
    float_data: Optional[FloatData] = None
    latest_metrics: Optional[MomentumMetrics] = None
    latest_score: Optional[MomentumScore] = None
    last_price: Optional[float] = None  # set from the latest snapshot seen by CandidateWatcher.update(); None until its first tick
    resistance_level: Optional[float] = None
    # Static levels from volume-profile analysis at discovery time (see
    # scanner/broad_scanner.py's _compute_static_resistance_levels and
    # metrics/volume_profile.py) -- high-volume-node price levels that
    # existed before this candidate started being tracked. Merged with the
    # running intraday high in candidate_watcher.py's update_resistance:
    # the nearest one of these still above the running high becomes the
    # active resistance_level, rather than resistance_level being purely
    # the running high of day.
    static_resistance_levels: list[float] = field(default_factory=list)
    breakout_price: Optional[float] = None      # price at which the initial breakout occurred
    pullback_low: Optional[float] = None        # for breakout-pullback strategy tracking
    # High of the first opening_range_minutes of the trading session,
    # computed once at discovery from the same raw bars already fetched
    # for static_resistance_levels (see BroadScanner._compute_opening_range_high)
    # -- no extra network call. None if bars didn't cover market open (e.g.
    # discovered well after the open, or no get_raw_bars capability) or
    # opening_range_minutes couldn't be resolved from what was returned.
    # See strategy/opening_range_breakout.py.
    opening_range_high: Optional[float] = None
    # When static_resistance_levels was last (re)computed -- set at
    # discovery and then again each time TradingLoop._rescan_universe
    # refreshes it for a still-pre-entry candidate (see
    # BroadScanner.refresh_resistance_levels). Drives the throttling in
    # TradingLoopConfig.resistance_refresh_interval_seconds so a candidate
    # isn't re-fetched on every single rescan cycle regardless of how
    # frequently those run.
    resistance_last_refreshed_at: Optional[datetime] = None
    # Real session VWAP starting point (2026-08-14, see
    # docs/ARCHITECTURE.md and metrics/session_vwap.py's module docstring
    # for the ONFO incident this fixes) -- cumulative price*volume and
    # cumulative volume across today's regular-session bars from market
    # open through discovery, computed once at discovery from the same
    # raw bars already fetched for static_resistance_levels above (no
    # extra network call). TradingLoop._update_session_vwap seeds its own
    # running per-symbol state from these once, on this candidate's first
    # tick, then continues accumulating live from there -- see that
    # method's docstring. None if bars didn't cover market open (e.g.
    # discovered well after the open) or no get_raw_bars capability;
    # _update_session_vwap starts cold (accumulates from 0) in that case
    # rather than failing, same fail-soft contract as every other
    # bars-derived enrichment field on this class.
    vwap_anchor_pv: Optional[float] = None
    vwap_anchor_volume: Optional[float] = None
    state_history: list[tuple[CandidateState, datetime]] = field(default_factory=list)
    notes: str = ""

    # -- structural vs. temporary eligibility ---------------------------------
    # trade_eligible/block_reasons are ORTHOGONAL to `state` above: state is
    # driven purely by the Momentum Ignition Score (see candidate_watcher.py),
    # while these track *tradeability* conditions (spread, liquidity) that can
    # come and go tick to tick. A candidate can be ARMED (score-qualified) and
    # still not trade_eligible (e.g. its spread is temporarily too wide) --
    # that's the whole point of keeping this separate from `state` rather than
    # forcing a state transition for something that isn't permanent. Recomputed
    # from scratch every CandidateWatcher.update() call, so a resolved
    # condition clears itself automatically; nothing needs to explicitly
    # "un-block" a candidate. Contrast with CandidateState.REJECTED, which is
    # for genuinely permanent/structural disqualification (float too large,
    # unsupported security, etc.) and never clears.
    trade_eligible: bool = True
    block_reasons: list[TradeBlockReason] = field(default_factory=list)

    # -- informational volume context (see scanner/broad_scanner.py) ----------
    # None of these gate discovery anymore -- a historically-quiet low-float
    # stock suddenly seeing abnormal volume is exactly the pattern this bot
    # targets, so low readings here must not disqualify a candidate. Kept for
    # scoring/diagnostics: comparing today's/current activity against these
    # baselines is how "abnormal" gets measured, instead of requiring the
    # baseline itself to already be high.
    dollar_volume_today: Optional[float] = None
    average_daily_volume: Optional[float] = None   # N-day average shares/day, see BroadScannerConfig.avg_volume_lookback_days
    previous_day_volume: Optional[float] = None     # most recent complete trading day's volume

    # Historical "typical volume by this point in the session" reference,
    # built once at discovery from the same raw bars fetched for
    # static_resistance_levels (see BroadScanner._compute_volume_baseline
    # and metrics/volume_baseline.py) -- what relative_volume/
    # relative_volume_1m/relative_volume_5m in metrics/rolling.py compare
    # today's activity against. None for paper/backtest mode, a failed/
    # unsupported lookup, or a symbol with no historical bars to build
    # from -- compute_metrics' typical_volume_* parameters simply stay None
    # in that case, same fail-soft contract as static_resistance_levels.
    # Unlike resistance, this is NOT periodically refreshed: it reflects
    # days before today, which don't change again once the day is over, so
    # computing it once at discovery is sufficient.
    volume_baseline: Optional[VolumeBaseline] = None

    # Synthetic pre-discovery snapshots (metrics/rolling.seed_history_from_bars),
    # built once at discovery from the same raw bars as static_resistance_levels/
    # volume_baseline above, so CandidateWatcher's rolling window isn't
    # blind to a move that already happened before this candidate was
    # found -- see that function's docstring for the full "discovery
    # structurally lags the move" rationale and the cumulative-volume
    # anchoring that keeps it continuous with the live snapshot feed.
    # CandidateWatcher consumes this exactly once (only when its own
    # per-symbol history is still empty) and never touches it again.
    seed_snapshots: list[MarketSnapshot] = field(default_factory=list)

    # -- entry-selectivity rework (2026-08-13, see docs/ARCHITECTURE.md) ------
    # Dashboard-visible diagnostics for the CONFIRMING window and the
    # target-clearance gate -- all set/cleared by TradingLoop._poll_confirmation,
    # not CandidateWatcher, since they only have meaning once a real trigger
    # (and therefore a real trigger/entry price) exists. None whenever the
    # candidate isn't currently CONFIRMING or hasn't been through it yet this
    # arm cycle.
    confirmation_started_at: Optional[datetime] = None
    confirmation_expires_at: Optional[datetime] = None
    # Next static resistance level (candidate.static_resistance_levels) found
    # strictly above the confirmed entry price, if any -- None means either
    # no such level exists (open air) or confirmation hasn't run yet; see
    # target_clear below to distinguish "checked, none found" from "not
    # checked yet".
    next_resistance_price: Optional[float] = None
    # True once the target-clearance check has actually run and found no
    # blocking resistance between the confirmed entry and its target; None
    # before that check has run at all (see RESISTANCE_BEFORE_TARGET).
    target_clear: Optional[bool] = None
    # How much of the original trigger->resistance runway is already used up
    # by the confirmed entry price (0.0 = entered right at the trigger, 1.0+
    # = already consumed the whole gap to resistance) -- None when there's no
    # resistance to measure against (unconstrained) or not yet computed.
    runway_consumed_pct: Optional[float] = None
    # Human-readable reason the most recent CONFIRMING attempt failed or was
    # rejected (CONFIRMATION_FAILED / RESISTANCE_BEFORE_TARGET) -- surfaced
    # on the dashboard so "why didn't this obviously-hot candidate trade" has
    # a real answer instead of silence. Cleared on the next successful arm.
    entry_block_reason: Optional[str] = None

    # -- Real-Time Momentum Qualification Layer (2026-08-17) ------------------
    # Live phase/RTMS/impulse-pullback tracking -- see MomentumState's own
    # docstring for the full contract and its two reset seams.
    momentum: MomentumState = field(default_factory=MomentumState)


@dataclass
class Signal:
    symbol: str
    action: SignalAction
    generated_at: datetime
    strategy_name: str
    strategy_version: str
    reference_price: float
    suggested_stop: Optional[float] = None
    suggested_target: Optional[float] = None
    score_at_signal: Optional[float] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    max_shares: Optional[int] = None
    risk_amount: Optional[float] = None


@dataclass
class Order:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    # Only set for order_type=TRAILING_STOP -- the trail distance as a
    # percentage of price (e.g. 3.0 = trails 3% behind the high since the
    # order was placed). Percentage-only in this model even though Webull
    # also supports a fixed dollar AMOUNT trailing_type -- this bot only
    # ever trails by PositionManagementConfig.trailing_stop_pct, a
    # percentage, so there's no use case for the other mode yet. See
    # WebullBrokerClient._order_payload for how this maps to Webull's
    # trailing_type/trailing_stop_step fields.
    trailing_pct: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    client_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    strategy_name: Optional[str] = None
    signal_id: Optional[str] = None


@dataclass
class Fill:
    order_client_id: str
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    filled_at: datetime
    fees: float = 0.0


@dataclass
class Position:
    symbol: str
    side: OrderSide
    quantity: float
    avg_entry_price: float
    stop_price: Optional[float]
    target_price: Optional[float]
    trailing_stop_pct: Optional[float]
    opened_at: datetime
    strategy_name: str
    entry_signal_id: Optional[str] = None
    realized_pnl: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    # Set True once this position's target_price has triggered one partial
    # (SCALE_OUT) exit -- prevents PositionManager.check_exit from firing
    # another partial every subsequent tick price stays above target. The
    # remaining quantity after a partial is managed purely by the
    # stop/trailing-stop/breakeven/VWAP/time-limit checks from then on.
    partial_exit_taken: bool = False

    # -- broker-side (resting) protective orders --------------------------
    # Set by TradingLoop._attach_broker_bracket right after this position's
    # entry fill is confirmed (and again after a partial exit, and again
    # any time PositionManager's breakeven/trailing math moves stop_price --
    # see _sync_broker_protective_orders), IF the connected broker actually
    # supports resting orders (WebullBrokerClient.place_oco_bracket --
    # PaperBrokerClient/backtests don't, since they fill everything
    # synchronously at market with nothing to rest against, so these stay
    # None there and this position is managed purely in software exactly as
    # before this field existed). broker_stop_order_id is not None is what
    # PositionManager.check_exit reads to decide whether ITS OWN stop/target
    # price-cross checks should fire (skipped when the broker already holds
    # a resting order enforcing them) or whether it must still do that work
    # itself (broker unsupported, or attaching/syncing the resting order
    # failed and this position fell back to software-only management).
    broker_stop_order_id: Optional[str] = None
    # Only ever set alongside broker_stop_order_id, and only when a target
    # hasn't been hit yet (never re-armed after partial_exit_taken -- see
    # _attach_broker_bracket) -- the resting take-profit leg of the same
    # OCO combo as the stop above.
    broker_target_order_id: Optional[str] = None
    # The stop_price value that was actually pushed to the broker as of
    # broker_stop_order_id's last (re)placement -- compared against the
    # live position.stop_price (which PositionManager mutates every tick
    # via breakeven/trailing, with no way to reach the broker itself) so
    # _sync_broker_protective_orders only cancels+replaces the resting
    # order when the two have actually diverged, not on every tick.
    # Meaningless (and unused) once broker_stop_is_trailing is True -- see
    # that field.
    broker_stop_price_synced: Optional[float] = None
    # True once broker_stop_order_id refers to a native TRAILING_STOP
    # order (see OrderManager.place_resting_trailing_stop) rather than a
    # plain STOP -- only ever set post-partial-exit, when
    # PositionManager's own trailing-stop math would otherwise be pushing
    # a moving stop_price to the broker via cancel+replace every time it
    # ratchets (see _sync_broker_protective_orders). When True, the
    # broker is trailing the order itself, so _sync_broker_protective_orders
    # has nothing left to push -- it's a no-op for this position from here
    # on, for as long as this stays True. Always False before the first
    # partial exit (pre-partial, the resting order is a plain stop+target
    # OCO bracket -- see RiskConfig.stop_loss_pct/_attach_broker_bracket's
    # docstrings for why trailing never applies that early) and for any
    # position too small to ever take a partial (see
    # PositionManager.check_exit's docstring) -- those ride on a plain
    # STOP for their whole lifetime, same as before this field existed.
    broker_stop_is_trailing: bool = False

    # -- software-side exit-submission backoff -----------------------------
    # Real incident (CYCU/SCKT, 2026-08-12): a genuine exit signal (stop-
    # loss/VWAP-failure/time-limit) kept firing every single tick, and
    # broker.place_order kept raising on sustained TOO_MANY_REQUESTS --
    # with no backoff of its own, TradingLoop._manage_position retried the
    # exact same place_order call again next tick regardless, adding to
    # (not easing) the very rate-limit contention blocking it, for two
    # positions simultaneously, for many consecutive minutes, while the
    # unrealized loss kept growing. Unlike Position.broker_bracket_attach_
    # failures (a nice-to-have that can safely give up and fall back to
    # software management), an exit submission can NEVER be allowed to
    # give up -- these two fields drive a growing backoff between retries
    # instead, easing self-inflicted pressure without ever stopping.
    exit_submission_failures: int = 0
    last_exit_submission_attempt_at: Optional[datetime] = None

    # True once TradingLoop._manage_position has already raised a
    # RiskEventType.POSITION_UNPROTECTED_TOO_LONG event for this position's
    # current unprotected stretch -- prevents re-logging the same warning
    # every tick for as long as _attach_broker_bracket keeps failing.
    # Reset to False by _attach_broker_bracket the moment it succeeds, so a
    # LATER stretch without protection (e.g. after a cancel+replace cycle)
    # can raise a fresh event rather than staying silently suppressed by a
    # flag from a completely different episode.
    unprotected_alert_logged: bool = False

    # True once TradingLoop._maybe_raise_stale_market_data_alert has
    # already raised a RiskEventType.POSITION_MARKET_DATA_STALE event for
    # this position's current dead-feed stretch -- same one-alert-per-
    # episode idea as unprotected_alert_logged above, just keyed off
    # get_last_known_price_age_seconds instead of the broker-bracket
    # state. Reset to False by _manage_position the moment a fresh
    # snapshot is cached again, so a LATER stretch without live data can
    # raise its own fresh alert rather than staying suppressed by a flag
    # from an earlier, already-resolved one.
    market_data_stale_alert_logged: bool = False


@dataclass
class Trade:
    symbol: str
    strategy_name: str
    side: OrderSide
    entry_price: float
    exit_price: float
    quantity: float
    opened_at: datetime
    closed_at: datetime
    exit_reason: ExitReason
    pnl: float
    pnl_pct: float
    max_favorable_excursion: float
    max_adverse_excursion: float


@dataclass
class RiskEvent:
    event_type: str
    symbol: Optional[str]
    timestamp: datetime
    reason: str
    metadata: dict = field(default_factory=dict)


@dataclass
class MomentumEvent:
    """
    A meaningful momentum observation, recorded regardless of whether the
    bot traded it. This is the core artifact for the data-collection /
    strategy-improvement loop described in the project outline.
    """
    symbol: str
    detected_at: datetime
    trigger_reason: str                 # e.g. "score_crossed_armed_threshold", "breakout_confirmed"
    was_traded: bool
    score_at_event: Optional[float]
    metrics_at_event: Optional[MomentumMetrics]
    price_at_event: float

    # Forward-looking outcome snapshots, filled in asynchronously as time passes.
    outcome_30s: Optional[dict] = None
    outcome_1m: Optional[dict] = None
    outcome_3m: Optional[dict] = None
    outcome_5m: Optional[dict] = None
    outcome_10m: Optional[dict] = None
    outcome_15m: Optional[dict] = None

    max_favorable_excursion_pct: Optional[float] = None
    max_adverse_excursion_pct: Optional[float] = None
    hod_broken: Optional[bool] = None
    vwap_failed: Optional[bool] = None
    outcome_label: MomentumOutcome = MomentumOutcome.UNKNOWN

    # -- Real-Time Momentum Qualification Layer (2026-08-17) ------------------
    # Finer-grained forward-outcome windows than outcome_30s/1m/3m/5m/10m/15m
    # above -- needed because a momentum-qualification-specific trigger can
    # fail or succeed within seconds, well before the 30s window fills in.
    # Same "filled in asynchronously as time passes" contract as the
    # outcome_* fields above (see collection/event_recorder.py's
    # MomentumEventTracker) -- these three additionally include mfe_pct/
    # mae_pct in their dict at each checkpoint (reusing the tracker's
    # already-running max_favorable_excursion_pct/max_adverse_excursion_pct
    # values, not a separate computation).
    outcome_5s: Optional[dict] = None
    outcome_10s: Optional[dict] = None
    outcome_15s: Optional[dict] = None
    # Snapshot of the momentum-qualification decision at the moment this
    # event was recorded (phase, RTMS + components, impulse/pullback state,
    # strategy name, decision, rejection_reason if any), plus
    # confirmation_price/actual_entry_price filled in later at the two
    # checkpoints where those become known (TradingLoop._poll_confirmation's
    # success path, TradingLoop._submit_entry) -- see
    # scanner/momentum_qualification.py. None for any event recorded before
    # this layer existed, or (rarely) if the qualification engine wasn't
    # available at the call site.
    momentum_qualification_at_event: Optional[dict] = None
