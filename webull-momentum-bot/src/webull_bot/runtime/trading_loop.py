"""
Poll-based production run-loop.

Streaming market data (2026-08-11, extended 2026-08-11): for a broker that
implements `subscribe_quotes` (confirmed live for `WebullBrokerClient`
against the sandbox MQTT host -- see that module's docstring), this loop
uses a pushed live snapshot in place of a REST poll for every state in
`_STREAMING_ELIGIBLE_STATES` -- both exit-management price checks
(`ENTERED`/`MANAGING`) and pre-entry monitoring
(`WATCHING`/`HEATING_UP`/`ARMED`). `DISCOVERED` (never has anything to
subscribe -- a candidate leaves it on its first tick) and `TRIGGERED`
(`_poll_pending_entry` manages a pending order, not a live price) are
excluded. A position's symbol is subscribed the moment its broker-side
bracket is attached (`_confirm_entry_filled`'s fresh-fill path and
`reconcile_positions_from_broker`'s adoption-on-restart path); a watch-
stage candidate's symbol is (re-)subscribed once per tick in
`_process_all_candidates` (a no-op once already requested -- see
`_ensure_streaming_subscribed`). `_on_streaming_snapshot` stores each
pushed update keyed by symbol, and `_get_streaming_snapshot` returns it
only if it arrived within `TradingLoopConfig.streaming_staleness_seconds`
(10s default) -- otherwise `_process_candidate_inner` falls back to the
REST-polled snapshot exactly as before, and `_process_all_candidates`'s
batched `get_snapshots` call skips any symbol already covered by a fresh
stream. Pre-entry momentum scoring and entry-time spread gating are safe
to run on streamed data because `WebullBrokerClient.subscribe_quotes`
subscribes both the `SNAPSHOT` (price/OHLC/volume) and `QUOTE` (top-of-
book bid/ask) message types and merges them per symbol before this loop
ever sees a pushed update -- see `WebullBrokerClient._merge_streamed_snapshot`'s
docstring for why a symbol with only one of the two cached never reaches
`on_update` at all (fails closed to "nothing streamed yet," not to a
fabricated zero spread). If `subscribe_quotes` isn't implemented by the
broker in use (e.g. `PaperBrokerClient`) or a subscribe call raises, this
loop permanently falls back to pure REST polling for that run rather than
retrying a call that can't succeed -- see `_ensure_streaming_subscribed`'s
docstring, including its note on the still-unconfirmed long-run
subscription-count/rate tradeoff of covering the much larger watch-stage
population this way.

Key design point: `WebullBrokerClient.place_order` returns status=SUBMITTED,
not FILLED (a 2xx response means Webull accepted the order for processing,
confirmed live -- see that module's docstring). PaperBrokerClient, by
contrast, fills synchronously. This loop has to handle both: a freshly
submitted order parks the candidate in TRIGGERED (entries) or leaves it in
MANAGING with a pending-exit marker (exits) and polls `OrderManager.get_status`
on subsequent ticks until it resolves to FILLED or a terminal failure state.
A TRIGGERED candidate also gets one extra, independent check roughly
`entry_position_verify_delay_seconds` (10s default) after submission --
`_poll_pending_entry` falls through to `_maybe_verify_entry_via_positions`,
which queries `broker.get_positions()` directly and self-heals into a
tracked position if Webull already shows one open even though
`get_order_status` hasn't (or couldn't be) reported FILLED yet -- see that
method's docstring for why relying on order-status polling alone wasn't
judged sufficient here.

Position tracking is intentionally NOT re-fetched from the broker every
tick: `PositionManager.check_exit` mutates trailing-stop/MFE/MAE state in
place on a Position object, and `broker.get_positions()` returns fresh
objects on every call, so re-fetching each tick would silently discard that
running state. Instead, a local Position is seeded once (from the broker,
for an accurate avg_entry_price) right when an entry fill is confirmed, and
this loop's own dict is the source of truth for it until the position closes.

Broker-side stop/target management (2026-08-11): against a broker that
supports resting orders (see `WebullBrokerClient.place_oco_bracket`'s
docstring -- PaperBrokerClient/backtests don't), `_attach_broker_bracket`
places a real OCO stop+target bracket right after an entry fill is
confirmed, `_poll_broker_bracket` notices when either leg fills (finalizing
the trade the same way a software-submitted exit would, without this loop
ever having submitted the fill-causing order itself), and
`_sync_broker_protective_orders` cancels+replaces the resting stop whenever
`PositionManager`'s own breakeven/trailing math moves `position.stop_price`
-- EXCEPT once a position's protective order has become a native
TRAILING_STOP (see `Position.broker_stop_is_trailing`'s docstring):
`_attach_broker_bracket` switches to that order type instead of a plain
STOP the moment a position takes its one partial exit (never before --
trailing only ever applies post-partial, same as the pure-software path),
and from then on Webull moves the stop itself, so
`_sync_broker_protective_orders` has nothing left to push and is a no-op
for that position for its remaining lifetime. `PositionManager.check_exit`
steps aside from its own stop/target checks
for a broker-managed position (see its docstring) but still owns
VWAP-failure/time-limit, which have no broker-side equivalent -- a
software-submitted exit for either of those, and the kill-switch/end-of-
core-hours flatten, all cancel any resting orders first (see
`_cancel_broker_protective_orders`) before submitting their own market
order. A position rides on pure software-side management (this loop's
pre-2026-08-11 behavior, unchanged) only TEMPORARILY whenever a broker
call in this chain fails -- `_sync_broker_protective_orders` retries
`_attach_broker_bracket` every tick (extended 2026-08-11) until a real
broker-side bracket actually gets placed, since giving up permanently
after one failed attempt was the root cause of a real incident (RDGT,
2026-08-11 -- see `_attach_broker_bracket`'s docstring). The only
PERMANENT fallback to pure software-side management is a broker that
doesn't support resting orders at all (PaperBrokerClient/backtests) --
see each method's docstring for the exact condition.

Concurrency model (run_forever only -- run_once() stays single-threaded,
see below): universe rescanning is slow (see TradingLoopConfig's docstring
for measured per-symbol timing) and used to run inline in the main loop,
which meant a candidate/position tick -- including live stop-loss/exit
management -- could be blocked behind a full rescan for its entire
duration. run_forever() now runs the rescan on its own background daemon
thread (_universe_rescan_loop) while the main thread runs
_process_all_candidates() back-to-back on its own tight
poll_interval_seconds cadence, so exit management is never stuck waiting
on a scan. Both threads touch self.candidates (the rescan thread inserts
newly discovered candidates; the main thread iterates and mutates existing
ones), so all access to it goes through self._candidates_lock -- see
_snapshot_candidates/get_candidates (read) and _rescan_universe (write).
The lock is only ever held briefly to copy/insert into the dict itself,
never across a network call or a full candidate-processing pass.
run_once() is unchanged and still does the rescan inline on its own
thread, synchronously, for backward compatibility with callers (mainly
tests) that call it directly and expect a single deterministic pass.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

from ..brokers.webull.retry import CallPriority
from ..collection.event_recorder import MomentumEventTracker
from ..enums import CandidateState, ExitReason, OrderSide, OrderStatus, SignalAction
from ..execution.order_manager import OrderManager, OrderRejected
from ..interfaces.broker import BrokerClient
from ..data.universe import SymbolUniverseProvider
from ..market_hours import is_within_closing_buffer, is_within_core_trading_hours
from ..models import Candidate, MarketSnapshot, MomentumEvent, MomentumScore, Order, Position, Signal, Trade
from ..position.position_manager import PositionManager
from ..risk.risk_engine import RiskEngine
from ..scanner.broad_scanner import BroadScanner
from ..scanner.candidate_watcher import CandidateWatcher
from ..scanner.trigger_engine import TriggerEngine
from ..state_machine import new_candidate, transition

logger = logging.getLogger(__name__)


@dataclass
class TradingLoopConfig:
    poll_interval_seconds: float = 5.0
    # Webull's sandbox enforces a real sustained rate limit paced globally
    # by webull_market_data_limiter (1.0s minimum interval) regardless of
    # BroadScanner's concurrency -- see brokers/webull/retry.py's module
    # docstring for the live discovery process. Measured live: ~1.25s/symbol
    # with just get_snapshot, ~2.86s/symbol once
    # BroadScanner._compute_average_volume_info added a second Webull call
    # per symbol (scanner/broad_scanner.py) -- both above the limiter's bare
    # interval since occasional retries add real time on top.
    #
    # There is deliberately no cap on how many symbols get scanned per
    # cycle -- see TradingLoop._rescan_universe and data/universe.py's
    # MultiSourceUniverseProvider. Every symbol the multi-source universe
    # returns gets checked (unless it's already a tracked candidate --
    # see _rescan_universe's already_tracked filter, a pure cost
    # optimization since a re-check there would be thrown away regardless),
    # so a real mover can never be silently dropped just because it fell
    # past some truncation point (an earlier version of this config had
    # max_universe_size for exactly that purpose; it was removed rather
    # than set to some very large number, since keeping a cap at all
    # reintroduces the risk it existed to prevent).
    #
    # The real cost: full scan duration scales with how many symbols the
    # universe returns that cycle instead of being bounded by a fixed
    # number. A live check on 2026-08-09 found 149 unique symbols in the
    # (then 3-source, $1-$20, single-page-per-source) combined universe;
    # at the measured ~2.86s/symbol that was roughly 7 minutes for a full
    # pass. The universe is now wider on three more axes -- a 4th discovery
    # source, a $0.40-$25 price range, and unbounded pagination per source
    # (see data/universe.py) -- so a fresh symbol and timing count is
    # needed rather than assuming these numbers still hold; they're kept
    # here as the last *measured* baseline, not a current estimate. This
    # interval is therefore a floor ("don't start a new scan sooner than
    # this after the last one *started*"), not a target
    # duration -- in practice a scan will usually run longer than this
    # interval, so TradingLoop.run_once ends up starting the next scan
    # immediately after the previous one finishes, back-to-back, rather
    # than waiting out an idle gap. 60s (the original pre-rate-limiting
    # default) is kept as that floor since it no longer does any real
    # throttling work on its own.
    universe_rescan_interval_seconds: float = 60.0
    # How often an already-tracked, still-pre-entry candidate's
    # static_resistance_levels gets re-fetched and recomputed (see
    # BroadScanner.refresh_resistance_levels) -- checked every
    # _rescan_universe cycle, but only actually refreshed once this many
    # seconds have passed since resistance_last_refreshed_at, so raising
    # the rescan frequency above doesn't multiply this cost. A resistance
    # level computed once at discovery is frozen at whatever bars existed
    # at that moment; a candidate discovered early in the session has a
    # necessarily incomplete volume profile, so periodic refreshing lets
    # newly-formed high-volume nodes show up as the day progresses instead
    # of the entry strategies that key off resistance_level (Refined
    # Breakout, Momentum Breakout, Breakout Pullback, Opening Range
    # Breakout's stop) working off stale data for the rest of the session.
    # Deliberately longer than universe_rescan_interval_seconds's default:
    # each refresh is a real Webull-paced call per eligible candidate, and
    # unlike new-symbol discovery (which only pays this cost once per
    # symbol ever), this recurs for every still-watched candidate -- an
    # unvalidated starting point, not tuned against live rate limits yet.
    resistance_refresh_interval_seconds: float = 300.0
    cooldown_seconds: float = 900.0  # 15 min before a cooled-down candidate can be watched again
    # How often reconcile_positions_from_broker re-runs after its initial
    # run_forever-startup call -- see that method's docstring for why this
    # needs to run more than once: a position can be closed OUTSIDE this
    # process entirely (e.g. scripts/list_and_close_positions.py, or a
    # manual close in the Webull app itself), and nothing else in this
    # codebase ever notices that on its own. One extra broker.get_positions()
    # call per interval (not per-candidate, unlike get_snapshots) is cheap
    # enough to run this fairly often.
    position_reconcile_interval_seconds: float = 30.0
    # How many CONSECUTIVE reconcile_positions_from_broker passes a
    # position must be absent from broker.get_positions() before it's
    # treated as closed externally -- see that method's "missing from
    # broker" branch and Position-abandonment incident (2026-08-12): a
    # position (BIVI) was dropped from tracking and its candidate pushed
    # straight to COOLDOWN after a SINGLE reconcile pass came back without
    # it, moments after a 429 TOO_MANY_REQUESTS on an unrelated call in
    # the same tick -- get_positions() itself never raised (that's
    # already handled, see the try/except around _get_positions_for_tick
    # below), it returned a normal 200 whose body just didn't include a
    # position that was, per the dashboard and this bot's own fill
    # records, very much still open. A live account under the kind of
    # sustained rate-limit contention this bot can generate is not
    # guaranteed to return a complete positions list on every single
    # request even when it responds 200 -- treating one such response as
    # ground truth is enough to silently walk away from an open,
    # unprotected position (no more broker-side bracket, no more
    # software-side stop/target checks -- PositionManager.check_exit
    # never runs again for it) for the rest of the trading day. Requiring
    # the SAME symbol to be missing across this many consecutive passes
    # (~position_reconcile_interval_seconds apart) before acting adds a
    # `(N-1) * interval` detection delay for a GENUINE external close,
    # trading a little responsiveness there for never abandoning a real,
    # still-open position on one flaky poll.
    position_missing_confirmations_required: int = 2
    # How long a candidate can sit TRIGGERED (entry order submitted, not yet
    # confirmed filled) before _poll_pending_entry also cross-checks
    # broker.get_positions() directly, on top of (not instead of) the
    # get_order_status polling every tick already does -- see
    # _maybe_verify_entry_via_positions' docstring for why a second,
    # independent confirmation path matters here specifically. Checked once
    # per pending entry, not every tick past this mark (see
    # self._pending_entry_position_checked), to avoid an extra
    # broker.get_positions() call every poll_interval_seconds on top of the
    # get_order_status call already happening.
    entry_position_verify_delay_seconds: float = 10.0
    # Minimum % move (relative to the stop price currently resting at the
    # broker) PositionManager's breakeven/trailing math has to produce
    # before _sync_broker_protective_orders actually cancels+replaces the
    # resting order -- see that method's docstring. Without this, a
    # continuously-recomputed trailing stop on a fast-moving symbol would
    # cancel+replace on nearly every single tick once trailing is active
    # (partial_exit_taken=True), since `current_price * (1 - trailing_pct)`
    # almost never lands on the exact same float twice -- hammering
    # place_order/cancel_order for a change too small to matter while also
    # burning CRITICAL-tier rate-limiter slots that could otherwise go to
    # genuinely new fills/exits. 0.25% is deliberately small relative to
    # the 3% default trailing_stop_pct: this is hysteresis against tick-to-
    # tick float noise, not a meaningful loosening of how tightly the stop
    # actually trails price.
    stop_sync_min_move_pct: float = 0.25
    # How long a live-streamed snapshot for a MANAGING/ENTERED position's
    # symbol stays usable before _get_streaming_snapshot considers it
    # stale and falls back to REST polling for that symbol instead --
    # see that method and _ensure_streaming_subscribed's docstrings.
    # Deliberately looser than poll_interval_seconds (5s default): a
    # genuinely healthy stream delivers ticks far more often than that, so
    # this mostly guards against the stream having silently stopped
    # entirely (a dropped connection, the known-but-not-yet-understood
    # reconnect issue noted in WebullBrokerClient.subscribe_quotes'
    # docstring) rather than pacing normal operation.
    streaming_staleness_seconds: float = 10.0
    # How many minutes before the 4:00pm ET core-session close the end-of-
    # day auto-flatten fires -- see market_hours.is_within_closing_buffer's
    # docstring for the full story: firing exactly at (or after) the
    # close, as this loop did before 2026-08-11, left a position observed
    # live to stay open indefinitely, retried every tick with no visible
    # progress (leading diagnosis: the flatten's MARKET/CORE exit order
    # needs a still-live CORE session, which has already ended by the
    # time the old trigger first turned true -- not independently
    # confirmed via a captured rejection message). 2 minutes is
    # comfortably enough margin for a real order to submit and fill
    # before the session actually closes without giving up meaningful
    # trading time -- not itself live-tuned against how long a flatten
    # order actually takes to fill.
    end_of_day_flatten_buffer_minutes: float = 2.0
    # How long a manual "Close Position" click (dashboard) pauses new
    # entries for (see TradingLoop.request_manual_close /
    # RiskEngine.pause_new_entries). Real incident, 2026-08-12: a
    # software-managed stop-loss exit for one symbol kept losing the
    # account-wide rate-limit race, tick after tick, against a flood of
    # OTHER CRITICAL-priority place_order calls from many simultaneous
    # entry attempts (max_simultaneous_positions=0/unlimited at the time)
    # -- CallPriority only helps CRITICAL win against BACKGROUND traffic,
    # not against other CRITICAL traffic. Briefly blocking new entries
    # removes that competing CRITICAL-tier load so the requested close
    # gets a real shot at winning the next few rate-limiter slots, without
    # engaging the full kill switch (which would also force-close every
    # OTHER open position, not just the one requested).
    manual_close_entry_pause_seconds: float = 20.0


class TradingLoop:
    # Pre-entry states only: resistance_level is what the resistance-based
    # entry strategies (Refined Breakout, Momentum Breakout, Breakout
    # Pullback, Opening Range Breakout's stop) read, and none of those
    # matter anymore once a candidate has already entered a position --
    # PositionManager's stop/target/trailing-stop rules take over from
    # there, never resistance_level. Used by _refresh_stale_resistance_levels.
    _RESISTANCE_REFRESH_STATES = (CandidateState.WATCHING, CandidateState.HEATING_UP, CandidateState.ARMED)

    # States that use live-streamed prices in place of a REST poll -- see
    # _get_streaming_snapshot/_ensure_streaming_subscribed. Deliberately
    # excludes DISCOVERED (transient -- a candidate leaves it on its very
    # first tick, before there's ever anything to subscribe) and TRIGGERED
    # (_poll_pending_entry manages a pending order, not a live price).
    _STREAMING_ELIGIBLE_STATES = (
        CandidateState.WATCHING, CandidateState.HEATING_UP, CandidateState.ARMED,
        CandidateState.ENTERED, CandidateState.MANAGING,
    )

    def __init__(
        self,
        broker: BrokerClient,
        universe_provider: SymbolUniverseProvider,
        broad_scanner: BroadScanner,
        watcher: CandidateWatcher,
        trigger_engine: TriggerEngine,
        order_manager: OrderManager,
        position_manager: PositionManager,
        risk_engine: RiskEngine,
        *,
        config: Optional[TradingLoopConfig] = None,
        on_trade_closed: Optional[Callable[[Trade], None]] = None,
        on_order_update: Optional[Callable[[Order], None]] = None,
        on_state_transition: Optional[Callable[[str, CandidateState, CandidateState, datetime], None]] = None,
        on_score_computed: Optional[Callable[[str, MomentumScore], None]] = None,
        momentum_event_tracker: Optional[MomentumEventTracker] = None,
    ):
        self.broker = broker
        self.universe_provider = universe_provider
        self.broad_scanner = broad_scanner
        self.watcher = watcher
        self.trigger_engine = trigger_engine
        self.order_manager = order_manager
        self.position_manager = position_manager
        self.risk_engine = risk_engine
        self.config = config or TradingLoopConfig()
        self.on_trade_closed = on_trade_closed
        # Called with every Order object this loop sees at each of its four
        # order-status touchpoints (submit entry, poll pending entry, submit
        # exit, poll pending exit) -- lets a caller persist order state
        # changes (e.g. SUBMITTED -> FILLED) without TradingLoop importing
        # the DB layer itself. May be called multiple times for the same
        # client_order_id as its status changes.
        self.on_order_update = on_order_update
        # Called once per state-machine transition (symbol, from_state,
        # to_state, timestamp), diffed off Candidate.state_history at the end
        # of every _process_candidate() call -- see _flush_state_transitions.
        # This covers transitions made anywhere (watcher, trigger_engine, or
        # this class itself) without those modules needing to know about it.
        self.on_state_transition = on_state_transition
        # Called with the freshly computed MomentumScore every time
        # CandidateWatcher.update() produces one.
        self.on_score_computed = on_score_computed
        # Optional collaborator (not a callback) since momentum-event
        # tracking needs ongoing state across many ticks (filling forward-
        # looking outcome windows over up to 15 minutes) -- see
        # collection/event_recorder.py.
        self.momentum_event_tracker = momentum_event_tracker

        self.candidates: dict[str, Candidate] = {}
        # Guards structural access (insert/copy) to self.candidates only --
        # see this module's docstring's "Concurrency model" section. Held
        # only briefly, never across a network call or a full processing pass.
        self._candidates_lock = threading.Lock()
        self._entry_signals: dict[str, Signal] = {}       # symbol -> signal that triggered a pending entry
        self._pending_entry_orders: dict[str, Order] = {}  # symbol -> submitted-but-not-yet-filled entry order
        # symbols whose TRIGGERED entry has already had its one
        # get_positions()-based verification check (see
        # _maybe_verify_entry_via_positions) -- prevents that check from
        # re-firing every tick once the delay threshold has passed. Cleared
        # whenever the pending entry resolves (filled, rejected, cancelled,
        # or the position-check itself confirms a fill) or a fresh entry is
        # submitted for the symbol.
        self._pending_entry_position_checked: set[str] = set()
        self._pending_exit_orders: dict[str, tuple[Order, Signal]] = {}  # symbol -> (order, exit signal)
        self._positions: dict[str, Position] = {}          # symbol -> our own tracked open position
        # symbol -> consecutive reconcile_positions_from_broker passes it's
        # been absent from broker.get_positions() -- see
        # TradingLoopConfig.position_missing_confirmations_required's
        # docstring for the incident this guards against (a single
        # degraded/rate-limited-adjacent poll abandoning a still-open
        # position). Only ever holds entries for symbols currently in
        # self._positions; cleared the moment a symbol reappears in a
        # broker response or is actually declared closed.
        self._missing_from_broker_counts: dict[str, int] = {}
        self._last_universe_scan: Optional[datetime] = None
        self._last_position_reconcile: Optional[datetime] = None
        # Reset at the start of every _process_all_candidates pass (see
        # that method) and populated lazily by _get_positions_for_tick --
        # collapses multiple independent broker.get_positions() calls that
        # would otherwise all happen within the same tick (e.g. several
        # TRIGGERED candidates each crossing their own
        # entry_position_verify_delay_seconds mark in the same pass, or
        # that lining up with the periodic reconcile) into a single real
        # network round-trip. NOT used by _confirm_entry_filled's own
        # post-fill lookup, which deliberately always calls
        # broker.get_positions() directly -- that call specifically wants
        # to see a fill that may have only just been confirmed THIS tick,
        # which a value cached earlier in the same pass could miss.
        self._tick_positions_cache: Optional[list[Position]] = None
        # Reset alongside _tick_positions_cache (see _process_all_candidates)
        # -- {broker_order_id: Order} for every resting order at the
        # broker, fetched at most once per pass via _get_open_orders_for_tick
        # and shared by every broker-managed position's _poll_broker_bracket
        # call this tick. None (not populated -- distinct from an empty
        # dict, which means "fetched, nothing resting") until first
        # accessed; also None for the whole pass if the broker doesn't
        # support list_open_orders at all.
        self._tick_open_orders_cache: Optional[dict[str, Order]] = None
        # Live-streamed prices for MANAGING/ENTERED positions (see
        # _ensure_streaming_subscribed/_on_streaming_snapshot/
        # _get_streaming_snapshot) -- {symbol: (MarketSnapshot, received_at)}.
        # Written from the broker's own MQTT background thread (never the
        # main processing thread this class otherwise runs on -- see
        # WebullBrokerClient.subscribe_quotes's docstring), so all access
        # goes through _live_snapshots_lock, the same "dedicated lock for
        # cross-thread structural access" pattern this module already uses
        # for self.candidates (_candidates_lock, see this module's
        # docstring's Concurrency model section). received_at is this
        # process's own wall-clock receipt time, not the snapshot's own
        # (Webull-reported) timestamp -- used purely to detect the stream
        # going quiet for a symbol, independent of any clock skew between
        # this process and Webull's servers.
        self._live_snapshots: dict[str, tuple[MarketSnapshot, datetime]] = {}
        self._live_snapshots_lock = threading.Lock()
        # None until the first _ensure_streaming_subscribed call resolves
        # one way or the other; False once broker.subscribe_quotes has
        # raised NotImplementedError (PaperBrokerClient/backtests, or any
        # broker that doesn't implement streaming) -- permanent for this
        # process's lifetime, so every later call short-circuits instead of
        # retrying a call already known to fail. True once at least one
        # real subscription has succeeded.
        self._streaming_supported: Optional[bool] = None
        self._streaming_requested_symbols: set[str] = set()
        self._persisted_transition_counts: dict[str, int] = {}  # symbol -> len(state_history) already flushed
        # Set by engage_kill_switch_and_flatten (callable from any thread,
        # e.g. the dashboard's request thread) and read by
        # _process_all_candidates on the main thread -- see that method's
        # docstring for why the actual position-closing work is deferred
        # to the main thread rather than run inline on the caller's thread.
        # Just a display string for _close_all_positions_now's log/reason
        # field, not itself a trigger -- see that comment for why the
        # actual retry condition is risk_engine.kill_switch_active, not a
        # one-shot flag (a real incident, 2026-08-11: a one-shot request
        # flag meant a single failed close attempt on any symbol -- a rate
        # limit, a get_snapshot hiccup, anything -- silently ended the
        # flatten for that symbol forever, with the kill switch appearing
        # to "do nothing" whenever that happened).
        self._close_all_positions_reason = ""
        # Symbols requested for a one-off manual close (dashboard's
        # per-position "Close" button) -- see request_manual_close.
        # Deliberately a separate mechanism from kill_switch_active: the
        # kill switch force-closes EVERY open position and requires manual
        # disengagement, neither of which is right for "close just this
        # one position." Retried every tick the same way the kill switch
        # is (see _process_all_candidates), so a rate-limited/failed
        # attempt self-heals instead of silently doing nothing.
        self._manual_close_requests: set[str] = set()

    # -- kill switch: halt + flatten ------------------------------------------

    def engage_kill_switch_and_flatten(self, reason: str) -> None:
        """Engages the kill switch (blocks all new entries immediately --
        RiskEngine.evaluate() checks this on every signal, so this part
        takes effect the instant it's called, from any thread) and, from
        that point on, every tick of _process_all_candidates keeps
        attempting to force-close every currently open position (see that
        method's `risk_engine.kill_switch_active` check) until either none
        remain or the switch is disengaged. Safe to call from any thread:
        engage_kill_switch itself is just a boolean flip (atomic under the
        GIL); the actual position-closing work always runs on the main
        processing thread regardless of which thread called this (the
        dashboard's request thread, typically) -- see
        _close_all_positions_now for why.

        Retried every tick rather than attempted once (fixed 2026-08-11,
        a real incident: the kill switch appeared to silently "do
        nothing" during live testing -- traced to a one-shot request flag
        that meant a single failed close attempt on any symbol, for any
        reason, permanently abandoned the flatten for it). An emergency
        stop that gives up after one failure defeats its own purpose --
        same reasoning as _sync_broker_protective_orders' retry of a
        failed broker-side bracket attach."""
        self.risk_engine.engage_kill_switch(reason)
        self._close_all_positions_reason = reason

    def request_manual_close(self, symbol: str, now: Optional[datetime] = None) -> bool:
        """Dashboard's per-position "Close" button: force-closes exactly
        `symbol`, not every open position (contrast engage_kill_switch_and_
        flatten above, which is deliberately all-or-nothing and requires a
        manual disengage). Also briefly pauses new entries (see
        RiskEngine.pause_new_entries and TradingLoopConfig.
        manual_close_entry_pause_seconds' docstring for the real incident
        this addresses) so the requested close isn't left competing for
        the same account-wide rate-limit budget against a flood of other
        CRITICAL-priority place_order calls from simultaneous entries --
        CallPriority only helps CRITICAL win against BACKGROUND traffic,
        not against other CRITICAL traffic.

        Safe to call from any thread, same contract as
        engage_kill_switch_and_flatten: this only records the request and
        flips risk_engine's pause; the actual close happens on the main
        processing thread's next tick (see _process_all_candidates) and
        keeps retrying every tick until it succeeds, is no longer an open
        position, or the request is otherwise cleared -- same self-healing
        retry-until-success contract as the kill switch and the end-of-day
        auto-flatten, for the same reason (a single rate-limited attempt
        must not silently abandon the close).

        Returns False without requesting anything if `symbol` isn't
        currently an open position (a stale button click, a race with the
        position already having closed) -- the caller (the dashboard's
        POST /api/positions/{symbol}/close) turns that into a 404."""
        symbol = symbol.strip().upper()
        if symbol not in self._positions:
            return False
        self._manual_close_requests.add(symbol)
        self.risk_engine.pause_new_entries(self.config.manual_close_entry_pause_seconds, now=now)
        return True

    def _close_all_positions_now(
        self,
        reason: str,
        now: datetime,
        exit_reason: ExitReason = ExitReason.RISK_KILL_SWITCH,
        symbols: Optional[Iterable[str]] = None,
    ) -> None:
        """Force-closes every open position immediately at market,
        regardless of PositionManager's own exit conditions -- the kill
        switch's "flatten everything" action, also reused as-is by the
        end-of-core-hours auto-flatten (see _process_all_candidates), which
        passes exit_reason=ExitReason.END_OF_CORE_HOURS instead of the
        default, and by request_manual_close's per-tick retry (see
        _process_all_candidates), which passes exit_reason=ExitReason.MANUAL
        and a single-symbol `symbols` iterable. `symbols` defaults to None,
        meaning every currently open position (self._positions) -- the
        kill switch/end-of-day callers rely on that default; only the
        manual-close path narrows it. Runs exclusively on the main processing thread (see
        _process_all_candidates), so this shares the exact same submit ->
        fill-or-pending -> finalize path _manage_position uses
        (_dispatch_exit_finalization, _pending_exit_orders) with no extra
        cross-thread coordination needed: a position that doesn't fill
        synchronously is left in _pending_exit_orders and picked up by the
        very next tick's normal _manage_position/_poll_pending_exit call for
        that symbol, same as any other exit.

        A get_snapshot or order failure for one symbol is logged and
        skipped rather than aborting the whole flatten -- one bad quote
        shouldn't leave every other position uncautiously open during an
        emergency stop.

        Skips any symbol already in `self._pending_exit_orders` (fixed
        2026-08-11, alongside making both callers of this method retry
        every tick instead of once): that symbol's close was already
        submitted on an earlier pass and hasn't resolved yet (a real
        broker returns SUBMITTED, not an instant FILLED -- see
        `WebullBrokerClient.place_order`'s docstring), and is already
        being tracked by `_manage_position`/`_poll_pending_exit`'s normal
        per-tick polling. Without this guard, a still-pending symbol
        would get a SECOND market exit order submitted against it on
        every subsequent tick this method runs -- e.g. the kill switch
        or the end-of-day buffer window retrying while a broker fill is
        still in flight -- risking a real over-sell against a live
        broker."""
        wanted = set(symbols) if symbols is not None else None
        for symbol, position in list(self._positions.items()):
            if wanted is not None and symbol not in wanted:
                continue
            if symbol in self._pending_exit_orders:
                continue
            candidate = self.candidates.get(symbol)
            if candidate is None:
                continue
            try:
                snapshot = self.broker.get_snapshot(symbol)
            except Exception:
                logger.warning("get_snapshot failed for %s while force-closing (%s).", symbol, exit_reason.value, exc_info=True)
                continue

            if position.broker_stop_order_id is not None or position.broker_target_order_id is not None:
                # Same reasoning as _manage_position's own VWAP/time-limit
                # exit path: don't leave a resting stop/target order behind
                # for a position this call is about to force-close at
                # market regardless of what the broker's own bracket would
                # otherwise have done.
                self._cancel_broker_protective_orders(symbol, position)

            exit_signal = Signal(
                symbol=symbol,
                action=SignalAction.EXIT,
                generated_at=snapshot.timestamp,
                strategy_name=position.strategy_name,
                strategy_version=exit_reason.value,
                reference_price=snapshot.last_price,
                metadata={"exit_reason": exit_reason.value, "reason": reason},
            )
            try:
                order = self.order_manager.submit_signal(exit_signal, snapshot=snapshot, position=position)
            except OrderRejected:
                logger.exception("Unexpected OrderRejected while force-closing %s (%s).", symbol, exit_reason.value)
                continue
            except Exception:
                # Same gap, same fix as _manage_position's own exit
                # submission below -- a real broker.place_order failure
                # here must not silently take down the rest of the flatten
                # (the existing "one bad symbol shouldn't block every other
                # position" contract this method already documents), and
                # must be loud about exactly which symbol/exit_reason
                # failed rather than surfacing only in a generic catch-all.
                logger.exception(
                    "broker.place_order raised while force-closing %s (%s) -- position remains open, "
                    "will retry on this symbol's normal _manage_position tick.", symbol, exit_reason.value,
                )
                continue
            self._notify_order_update(order)

            if order.status == OrderStatus.FILLED:
                self._dispatch_exit_finalization(candidate, position, order, exit_signal, now)
            else:
                self._pending_exit_orders[symbol] = (order, exit_signal)

    # -- universe / discovery ------------------------------------------------

    def _rescan_universe(self, now: datetime) -> None:
        try:
            symbols = self.universe_provider.get_symbols()
        except Exception:
            logger.exception("Universe scan failed; keeping existing candidates this cycle.")
            return

        # Cost optimization (2026-08-09): a symbol already tracked in
        # self.candidates gets nothing from the full BroadScanner.scan()
        # pipeline here -- the insert loop below has always skipped it
        # anyway (`if candidate.symbol not in self.candidates`), which meant
        # every already-known symbol still paid the full BroadScanner cost
        # (snapshot + volume history + resistance bars, each a paced
        # Webull round-trip) only to have the result thrown away. Filtering
        # them out before scan() means that cost is spent solely on
        # genuinely new discoveries each cycle. Already-tracked candidates
        # lose nothing from skipping the FULL pipeline here: they're
        # re-checked far more often anyway by _process_all_candidates on
        # its own 5s cadence (see this module's "Concurrency model"
        # docstring), which is what actually drives their score/state/exit
        # management. Resistance is the one exception -- see the
        # resistance-refresh pass below, which deliberately does still
        # touch already-tracked candidates, just via a much lighter,
        # separately-throttled path than the full pipeline.
        with self._candidates_lock:
            already_tracked = set(self.candidates.keys())
        new_symbols = [s for s in symbols if s not in already_tracked]

        try:
            discovered = self.broad_scanner.scan(new_symbols)
        except Exception:
            logger.exception("BroadScanner.scan failed.")
            return

        with self._candidates_lock:
            for candidate in discovered:
                if candidate.symbol not in self.candidates:
                    self.candidates[candidate.symbol] = candidate

        self._refresh_stale_resistance_levels(now)

    def _refresh_stale_resistance_levels(self, now: datetime) -> None:
        """Re-fetches and recomputes static_resistance_levels (see
        BroadScanner.refresh_resistance_levels) for already-tracked,
        pre-entry candidates whose last refresh is older than
        TradingLoopConfig.resistance_refresh_interval_seconds -- keeps
        resistance accurate as the day's volume profile fills in, instead
        of freezing it at whatever bars existed at discovery.

        Deliberate, narrow exception to this module's stated concurrency
        rule ("the rescan thread inserts, the main thread mutates existing
        candidates"): this runs on the rescan thread and mutates fields on
        Candidate objects the main thread is concurrently reading/writing.
        It's safe without _candidates_lock because each mutation here is a
        single attribute assignment (`candidate.static_resistance_levels =
        ...`, `candidate.resistance_last_refreshed_at = ...`) -- atomic
        under the GIL, so the main thread only ever sees the old value or
        the fully-new one, never a torn read. Worst case on a genuine race
        is the main thread using resistance_level computed from the
        previous set of levels for one extra tick, which is harmless (the
        whole point of throttling this to a period measured in minutes).
        The dict structure itself (self.candidates' keys) is untouched
        here, only values already known to exist -- the lock protects
        structural changes (insert/iterate), which this isn't."""
        with self._candidates_lock:
            candidates_snapshot = list(self.candidates.values())

        for candidate in candidates_snapshot:
            if candidate.state not in self._RESISTANCE_REFRESH_STATES:
                continue
            if (
                candidate.resistance_last_refreshed_at is not None
                and now - candidate.resistance_last_refreshed_at < timedelta(seconds=self.config.resistance_refresh_interval_seconds)
            ):
                continue
            try:
                self.broad_scanner.refresh_resistance_levels(candidate, now=now)
            except Exception:
                logger.warning("refresh_resistance_levels failed for %s this cycle.", candidate.symbol, exc_info=True)

    def _snapshot_candidates(self) -> list[Candidate]:
        """Lock-protected copy of the tracked candidates' values, safe to
        iterate while _rescan_universe concurrently inserts into the dict
        on the background rescan thread (see this module's docstring)."""
        with self._candidates_lock:
            return list(self.candidates.values())

    def scan_and_add_candidate(self, symbol: str) -> tuple[Optional[Candidate], Optional[str], bool]:
        """On-demand, single-symbol equivalent of _rescan_universe -- runs
        one ticker through BroadScanner's structural gates right now and,
        if it passes, adds it to self.candidates so it starts being
        processed on this loop's normal cadence, instead of waiting for
        the next full universe pass (which can take many minutes, see
        TradingLoopConfig). Backs the dashboard's manual "scan a ticker"
        feature (dashboard/app.py's POST /api/scan-symbol).

        If `symbol` is already tracked, it's returned as-is (its real,
        current state -- WATCHING, ARMED, REJECTED, whatever it actually
        is) without re-scanning or being overwritten, same "don't clobber
        an existing candidate" behavior _rescan_universe already has for
        the periodic path. Otherwise runs
        BroadScanner.check_symbol_verbose and, on success, inserts the new
        candidate under self._candidates_lock (guarding a race against the
        background rescan thread discovering the same symbol at the same
        time -- see this module's docstring's "Concurrency model" section);
        `dict.setdefault` inside the lock means whichever candidate object
        won that race is what gets returned, not necessarily the one this
        call just built.

        Returns (candidate_or_None, reason_or_None, was_newly_added) --
        reason is only ever set when candidate is None (a fresh rejection);
        was_newly_added is True only when THIS call is what inserted the
        candidate (checked under the same lock as the insert, so a
        concurrent rescan-thread discovery racing this call is reported
        accurately rather than guessed from a separate, unlocked check)."""
        symbol = symbol.upper()
        with self._candidates_lock:
            existing = self.candidates.get(symbol)
        if existing is not None:
            return existing, None, False

        try:
            candidate, reason = self.broad_scanner.check_symbol_verbose(symbol)
        except Exception:
            logger.exception("check_symbol_verbose failed for manually-scanned symbol %s.", symbol)
            return None, f"Unexpected error while scanning {symbol}; see server logs.", False
        if candidate is None:
            return None, reason, False

        with self._candidates_lock:
            was_newly_added = symbol not in self.candidates
            stored = self.candidates.setdefault(symbol, candidate)
        return stored, None, was_newly_added

    # -- live-streamed prices for MANAGING/ENTERED positions --------------

    def _on_streaming_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Passed to broker.subscribe_quotes as its on_update callback --
        called from the broker's own MQTT background thread (see
        WebullBrokerClient.subscribe_quotes's docstring), never this
        loop's own main processing thread. Only stores the snapshot under
        self._live_snapshots_lock; all real decision-making
        (PositionManager.check_exit, etc.) still happens on the main
        thread the next time it reads this via _get_streaming_snapshot --
        same "write from any thread under a lock, read fresh on your own
        thread" pattern this module already uses for self.candidates (see
        this module's docstring's Concurrency model section)."""
        with self._live_snapshots_lock:
            self._live_snapshots[snapshot.symbol] = (snapshot, datetime.utcnow())

    def _get_streaming_snapshot(self, symbol: str, now: datetime) -> Optional[MarketSnapshot]:
        """Returns the most recent live-streamed snapshot for `symbol` if
        one exists and is no older than
        TradingLoopConfig.streaming_staleness_seconds, else None (either
        nothing has ever streamed for this symbol -- not subscribed,
        streaming unsupported by this broker, or no message has arrived
        yet -- or the stream has gone quiet for it). Callers must treat
        None as "fall back to REST polling for this symbol", exactly the
        same fallback contract every other optional/best-effort broker
        capability in this codebase already follows (get_snapshots,
        list_open_orders, place_oco_bracket, ...)."""
        with self._live_snapshots_lock:
            entry = self._live_snapshots.get(symbol)
        if entry is None:
            return None
        snapshot, received_at = entry
        if now - received_at > timedelta(seconds=self.config.streaming_staleness_seconds):
            return None
        return snapshot

    def _ensure_streaming_subscribed(self, symbols: list[str]) -> None:
        """Best-effort: asks the broker to start streaming live prices for
        `symbols` via broker.subscribe_quotes, so _get_streaming_snapshot
        has something fresh to serve on subsequent ticks for any candidate
        in a _STREAMING_ELIGIBLE_STATES state. Called from three places:
        _confirm_entry_filled/reconcile_positions_from_broker's adoption
        path (an eager first attempt right when a position starts being
        tracked, mirroring _attach_broker_bracket's own call sites), and
        _process_all_candidates once per tick for every currently
        streaming-eligible candidate (both an eventual first attempt for a
        watch-stage candidate and a RETRY of the eager attempt above if it
        failed -- see the "retried automatically" paragraph below).

        subscribe_quotes is a required BrokerClient ABC method (unlike
        get_snapshots/list_open_orders/place_oco_bracket, which are
        optional/getattr-gated), but PaperBrokerClient's implementation of
        it deliberately raises NotImplementedError rather than doing
        something meaningful -- so the capability check here has to be a
        try/except around the call itself rather than a getattr presence
        check. self._streaming_supported=False is set permanently the
        first time that happens, so every later call this process makes
        short-circuits immediately instead of retrying a call already
        known to fail for this broker every single tick a new position
        opens.

        Any other exception (a real, potentially transient connection or
        REST failure -- see WebullBrokerClient.subscribe_quotes' docstring
        for the internal reconnect-after-timeout behavior this pairs
        with) is logged and swallowed WITHOUT marking these symbols as
        requested (self._streaming_requested_symbols is only updated on
        success, below) -- so they're simply retried the next time this
        method is called for them, which for every _STREAMING_ELIGIBLE_STATES
        candidate is every single tick via _process_all_candidates' sweep.
        In practice that means a failed subscribe recovers within
        `poll_interval_seconds` (a "short wait" in wall-clock terms, not
        multiple ticks), with no separate retry timer needed here.

        Only ever subscribes symbols not already requested this process's
        lifetime (self._streaming_requested_symbols) -- there is
        deliberately no unsubscribe path for a symbol that later leaves
        every _STREAMING_ELIGIBLE_STATES state (a closed position, a
        candidate that cooled off back to REJECTED): the extra ticks for
        an unwatched symbol are harmless (nothing reads them --
        _get_streaming_snapshot is only ever consulted for a candidate
        currently in one of those states) and simpler than tracking
        exactly when it's safe to unsubscribe. Known tradeoff worth
        watching in production, now that this covers the much larger
        WATCHING/HEATING_UP/ARMED population rather than just open
        positions: subscriptions only ever grow for the life of the
        process, one entry per symbol BroadScanner has ever surfaced --
        see docs/ARCHITECTURE.md's "Streaming market data" section for
        whether Webull's per-session subscription count/rate has a
        practical ceiling this could approach over a long trading day
        (not yet confirmed either way)."""
        if self._streaming_supported is False:
            return
        new_symbols = [s for s in symbols if s not in self._streaming_requested_symbols]
        if not new_symbols:
            return
        try:
            self.broker.subscribe_quotes(new_symbols, self._on_streaming_snapshot)
        except NotImplementedError:
            self._streaming_supported = False
            return
        except Exception:
            logger.warning(
                "subscribe_quotes failed for %s; these symbols fall back to REST polling instead.",
                new_symbols, exc_info=True,
            )
            return
        self._streaming_supported = True
        self._streaming_requested_symbols.update(new_symbols)

    # -- per-candidate processing ---------------------------------------------

    def _process_candidate(
        self, candidate: Candidate, now: datetime, prefetched_snapshot: Optional[MarketSnapshot] = None
    ) -> None:
        """Thin wrapper that guarantees _flush_state_transitions runs exactly
        once per tick regardless of which branch below returns early --
        _process_candidate_inner uses plain `return` freely.

        prefetched_snapshot: see _process_all_candidates' batch
        get_snapshots() call -- when set, this candidate's tick reuses that
        already-fetched snapshot instead of calling broker.get_snapshot()
        itself. None (the default) preserves this method's original
        behavior exactly, which is also what every direct caller other than
        _process_all_candidates (mainly tests) still gets."""
        try:
            self._process_candidate_inner(candidate, now, prefetched_snapshot)
        finally:
            self._flush_state_transitions(candidate)

    def _process_candidate_inner(
        self, candidate: Candidate, now: datetime, prefetched_snapshot: Optional[MarketSnapshot] = None
    ) -> None:
        if candidate.state == CandidateState.REJECTED:
            return

        if candidate.state == CandidateState.COOLDOWN:
            if now - candidate.last_updated_at >= timedelta(seconds=self.config.cooldown_seconds):
                transition(candidate, CandidateState.WATCHING, now=now, reason="cooldown expired")
            return

        snapshot = None
        if candidate.state in self._STREAMING_ELIGIBLE_STATES:
            # Prefer a fresh live-streamed price over REST -- see
            # _get_streaming_snapshot's docstring. Covers both exit
            # management (ENTERED/MANAGING) and pre-entry monitoring
            # (WATCHING/HEATING_UP/ARMED); DISCOVERED/TRIGGERED are
            # excluded -- see _STREAMING_ELIGIBLE_STATES' comment. Safe to
            # use for pre-entry scoring/spread gating too now that
            # WebullBrokerClient merges the QUOTE stream's real bid/ask
            # into the pushed snapshot (see
            # WebullBrokerClient._merge_streamed_snapshot's docstring) --
            # a symbol whose QUOTE side hasn't caught up yet simply has no
            # merged snapshot at all yet (None here), same fallback as any
            # other cold-start case.
            snapshot = self._get_streaming_snapshot(candidate.symbol, now)

        if snapshot is None:
            try:
                snapshot = prefetched_snapshot if prefetched_snapshot is not None else self.broker.get_snapshot(candidate.symbol)
            except Exception:
                logger.warning("get_snapshot failed for %s this cycle; skipping.", candidate.symbol, exc_info=True)
                return

        if self.momentum_event_tracker is not None:
            try:
                self.momentum_event_tracker.on_snapshot(candidate.symbol, snapshot)
            except Exception:
                logger.exception("momentum_event_tracker.on_snapshot failed for %s.", candidate.symbol)

        if candidate.state == CandidateState.TRIGGERED:
            self._poll_pending_entry(candidate, now)
            return

        if candidate.state in (CandidateState.ENTERED, CandidateState.MANAGING):
            self._manage_position(candidate, snapshot, now)
            return

        # DISCOVERED / WATCHING / HEATING_UP / ARMED
        self.watcher.update(candidate, snapshot)
        self._notify_score(candidate)
        signal = self.trigger_engine.on_snapshot(candidate, snapshot)
        # Roll this bar's high into resistance only AFTER the trigger engine
        # has checked it against the pre-bar level (see candidate_watcher.py).
        self.watcher.update_resistance(candidate, snapshot)

        if signal is None:
            return
        momentum_event = self._register_momentum_event(candidate, signal, now)
        self._submit_entry(candidate, signal, snapshot, now, momentum_event=momentum_event)

    def _notify_score(self, candidate: Candidate) -> None:
        if self.on_score_computed is not None and candidate.latest_score is not None:
            try:
                self.on_score_computed(candidate.symbol, candidate.latest_score)
            except Exception:
                logger.exception("on_score_computed callback raised for %s.", candidate.symbol)

    def _flush_state_transitions(self, candidate: Candidate) -> None:
        if self.on_state_transition is None:
            return
        already_persisted = self._persisted_transition_counts.get(candidate.symbol, 0)
        history = candidate.state_history
        total = len(history)
        if total <= already_persisted:
            return
        for i in range(already_persisted, total):
            from_state, timestamp = history[i]
            to_state = history[i + 1][0] if i + 1 < total else candidate.state
            try:
                self.on_state_transition(candidate.symbol, from_state, to_state, timestamp)
            except Exception:
                logger.exception("on_state_transition callback raised for %s.", candidate.symbol)
        self._persisted_transition_counts[candidate.symbol] = total

    def _register_momentum_event(self, candidate: Candidate, signal: Signal, now: datetime) -> Optional[MomentumEvent]:
        if self.momentum_event_tracker is None:
            return None
        event = MomentumEvent(
            symbol=candidate.symbol,
            detected_at=now,
            trigger_reason=f"{signal.strategy_name}:{signal.action.value}",
            was_traded=False,  # flipped to True in _submit_entry if the order actually gets submitted
            score_at_event=candidate.latest_score.score if candidate.latest_score else None,
            metrics_at_event=candidate.latest_metrics,
            price_at_event=signal.reference_price,
        )
        try:
            self.momentum_event_tracker.register(event)
        except Exception:
            logger.exception("momentum_event_tracker.register failed for %s.", candidate.symbol)
            return None
        return event

    def _notify_order_update(self, order: Order) -> None:
        if self.on_order_update is not None:
            try:
                self.on_order_update(order)
            except Exception:
                logger.exception("on_order_update callback raised for order %s.", order.client_order_id)

    def _submit_entry(
        self, candidate: Candidate, signal: Signal, snapshot: MarketSnapshot, now: datetime,
        momentum_event: Optional[MomentumEvent] = None,
    ) -> None:
        try:
            # open_positions=list(self._positions.values()), NOT
            # self.broker.get_positions() -- see submit_signal's docstring:
            # only this process's own locally-tracked positions carry a
            # real stop_price, which RiskEngine.evaluate's max_total_risk_pct
            # gate needs to compute actual assumed risk.
            order = self.order_manager.submit_signal(
                signal, snapshot=snapshot, open_positions=list(self._positions.values()), now=now,
            )
        except OrderRejected as exc:
            transition(candidate, CandidateState.ARMED, now=now, reason=f"risk engine rejected entry: {exc.decision.reason}")
            return
        except Exception:
            # trigger_engine.on_snapshot already transitioned this candidate
            # to TRIGGERED as a side effect before calling here -- if
            # order_manager.submit_signal raises anything other than the
            # expected OrderRejected (a real broker/network error, a bug),
            # that leaves the candidate stuck in TRIGGERED with no order
            # ever recorded in _pending_entry_orders, since we never got
            # past this call. Without this handler, that exception
            # propagates up to _process_all_candidates' generic catch-all,
            # which just logs "Unhandled error processing candidate" and
            # moves on -- the candidate then sits in TRIGGERED until
            # _poll_pending_entry's "no pending order found for TRIGGERED
            # candidate" safety net eventually notices and reverts it,
            # which can take a while if compounded by other transient
            # failures (e.g. get_snapshot also failing for this symbol on
            # subsequent cycles). Confirmed as a real production case: a
            # candidate sat TRIGGERED for over a minute with zero orders
            # ever submitted before that fallback finally caught it. Log
            # the real traceback here (the generic catch-all above logs a
            # much less specific message) and revert immediately instead of
            # relying on that fallback to eventually clean it up.
            logger.exception(
                "Unexpected error submitting entry order for %s; reverting to ARMED.", candidate.symbol
            )
            # risk_engine.evaluate() already ran (and approved/incremented
            # the counters) inside order_manager.submit_signal BEFORE the
            # broker call that just failed -- roll that back too, same as
            # the other two record_entry_order_failed call sites, or an
            # unexpected broker exception becomes yet another way to
            # silently exhaust this symbol's daily entry budget with zero
            # real positions ever opened.
            self.risk_engine.record_entry_order_failed(candidate.symbol, now)
            transition(candidate, CandidateState.ARMED, now=now, reason="unexpected error submitting entry order")
            return
        if momentum_event is not None:
            # Mutating in place is enough -- the tracker holds this same
            # object and will persist the change on its next on_snapshot()
            # call for this symbol (see _register_momentum_event).
            momentum_event.was_traded = True
        self._notify_order_update(order)

        if order.status == OrderStatus.FILLED:
            self._confirm_entry_filled(candidate, signal, order, now)
        elif order.status in (OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
            self._entry_signals[candidate.symbol] = signal
            self._pending_entry_orders[candidate.symbol] = order
            # Defensive reset -- should already be clear (see
            # _poll_pending_entry/_maybe_verify_entry_via_positions, which
            # both discard it whenever a pending entry resolves either way),
            # but a fresh entry attempt must never inherit a stale flag from
            # some earlier attempt for this symbol.
            self._pending_entry_position_checked.discard(candidate.symbol)
            # trigger_engine already moved this candidate to TRIGGERED.
        else:
            # Risk-approved but the broker itself rejected/failed the order
            # immediately (e.g. outside trading hours) -- no position ever
            # opened, so this must not permanently consume this symbol's
            # daily entry budget. See RiskEngine.record_entry_order_failed's
            # docstring for the real production case this fixes.
            self.risk_engine.record_entry_order_failed(candidate.symbol, now)
            transition(candidate, CandidateState.ARMED, now=now, reason=f"entry order {order.status.value}")

    def _poll_pending_entry(self, candidate: Candidate, now: datetime) -> None:
        pending = self._pending_entry_orders.get(candidate.symbol)
        if pending is None:
            # Shouldn't happen, but don't get stuck in TRIGGERED forever.
            transition(candidate, CandidateState.ARMED, now=now, reason="no pending order found for TRIGGERED candidate")
            return

        try:
            status_order = self.order_manager.get_status(pending.broker_order_id)
        except Exception:
            logger.warning("get_order_status failed for %s this cycle.", candidate.symbol, exc_info=True)
            status_order = None

        if status_order is not None:
            self._notify_order_update(status_order)

            if status_order.status == OrderStatus.FILLED:
                signal = self._entry_signals.pop(candidate.symbol)
                self._pending_entry_orders.pop(candidate.symbol, None)
                self._pending_entry_position_checked.discard(candidate.symbol)
                self._confirm_entry_filled(candidate, signal, status_order, now)
                return
            elif status_order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
                self._entry_signals.pop(candidate.symbol, None)
                self._pending_entry_orders.pop(candidate.symbol, None)
                self._pending_entry_position_checked.discard(candidate.symbol)
                # Same rollback as _submit_entry's immediate-failure branch --
                # this order was risk-approved and briefly pending, but never
                # actually filled, so it must not count against this symbol's
                # daily entry budget either.
                self.risk_engine.record_entry_order_failed(candidate.symbol, now)
                transition(candidate, CandidateState.ARMED, now=now, reason=f"entry order {status_order.status.value}")
                return
            # else still pending: fall through to the position-verification
            # check below instead of returning -- same as when
            # get_order_status itself failed above (status_order is None).

        self._maybe_verify_entry_via_positions(candidate, pending, now)

    def _get_positions_for_tick(self) -> list[Position]:
        """Returns broker.get_positions(), reusing a single result across
        every caller within the same _process_all_candidates pass instead
        of each one making its own network round-trip -- see
        self._tick_positions_cache's docstring for which call sites this
        is (and, importantly, is NOT) used by. Raises on failure exactly
        like broker.get_positions() itself would (no swallowing here) --
        each caller already has its own try/except around this for its own
        specific fallback behavior; caching must not change that contract."""
        if self._tick_positions_cache is None:
            self._tick_positions_cache = self.broker.get_positions()
        return self._tick_positions_cache

    def _maybe_verify_entry_via_positions(self, candidate: Candidate, pending: Order, now: datetime) -> None:
        """Extra, independent confirmation that a TRIGGERED entry actually
        filled, on top of (not instead of) the get_order_status polling in
        _poll_pending_entry above -- queries broker.get_positions() directly,
        once, roughly TradingLoopConfig.entry_position_verify_delay_seconds
        (10s default) after the entry order was submitted, and self-heals
        into a tracked position immediately if Webull already shows one open
        for this symbol even though get_order_status hasn't reported FILLED
        (or couldn't be reached at all) yet.

        Motivated by this project's own history: WebullBrokerClient's module
        docstring flags _order_from_detail's field-name mapping for a
        populated get_order_status response as UNVERIFIED (every live
        verification attempt during integration was rejected for being
        outside market hours, so a real filled order detail was never
        actually fetched/checked), and get_positions() itself already had a
        real incident where a field-name mismatch silently lost a fill (see
        _confirm_entry_filled's docstring) before being hardened to skip
        just the one bad row instead of losing everything. Checking
        positions directly gives a second, independent path to the same
        fact ("did this fill?") that doesn't depend on order-status parsing
        being right, catching a fill this loop would otherwise only notice
        once get_order_status eventually agrees -- which, per the above,
        might be delayed, or simply never happen if that mapping is wrong.

        Runs at most once per pending entry (see
        self._pending_entry_position_checked, reset in _submit_entry and
        wherever a pending entry resolves) rather than on every tick once
        the delay has passed, to avoid an extra broker.get_positions() call
        every poll_interval_seconds on top of the get_order_status call
        _poll_pending_entry already makes each tick. Uses
        _get_positions_for_tick (not broker.get_positions() directly) so
        several candidates crossing this same delay threshold within one
        _process_all_candidates pass share a single network call rather
        than each firing its own -- see that method's docstring."""
        if candidate.symbol in self._pending_entry_position_checked:
            return
        delay = timedelta(seconds=self.config.entry_position_verify_delay_seconds)
        if now - pending.created_at < delay:
            return
        self._pending_entry_position_checked.add(candidate.symbol)

        try:
            broker_positions = self._get_positions_for_tick()
        except Exception:
            logger.warning(
                "Position-verification check failed for %s (still TRIGGERED %.0fs after entry submission).",
                candidate.symbol, self.config.entry_position_verify_delay_seconds, exc_info=True,
            )
            return

        live_position = next((p for p in broker_positions if p.symbol == candidate.symbol), None)
        if live_position is None:
            return  # genuinely not filled yet (or already failed) -- normal order-status polling continues

        logger.warning(
            "%s: broker.get_positions() shows an open position %.0fs after entry submission, but "
            "get_order_status never reported FILLED -- treating the entry as filled now instead of "
            "waiting on order-status polling. qty=%s avg_entry_price=%.4f",
            candidate.symbol, self.config.entry_position_verify_delay_seconds,
            live_position.quantity, live_position.avg_entry_price,
        )
        signal = self._entry_signals.pop(candidate.symbol, None)
        self._pending_entry_orders.pop(candidate.symbol, None)
        self._pending_entry_position_checked.discard(candidate.symbol)
        if signal is None:
            # Shouldn't happen (mirrors _poll_pending_entry's own "no
            # pending order" guard for the analogous gap), but don't
            # fabricate stop/target values from nothing -- revert to ARMED
            # same as any other unexpected tracking gap.
            transition(candidate, CandidateState.ARMED, now=now, reason="position found but no entry signal on record")
            return

        filled_order = Order(
            symbol=candidate.symbol, side=pending.side, order_type=pending.order_type,
            quantity=live_position.quantity, status=OrderStatus.FILLED,
            client_order_id=pending.client_order_id, broker_order_id=pending.broker_order_id,
            created_at=pending.created_at, updated_at=now, strategy_name=pending.strategy_name,
        )
        self._notify_order_update(filled_order)
        self._confirm_entry_filled(candidate, signal, filled_order, now)

    def _confirm_entry_filled(self, candidate: Candidate, signal: Signal, order: Order, now: datetime) -> None:
        # This method is the ONLY place a filled entry order becomes a
        # locally-tracked position (self._positions), and it's called after
        # _poll_pending_entry has already popped candidate.symbol out of
        # _entry_signals/_pending_entry_orders -- so if anything below raises
        # before self._positions[candidate.symbol] is assigned, the position
        # that just filled at the broker becomes permanently invisible to
        # this bot: no stop-loss/target management, not shown as an open
        # position anywhere, buying power silently consumed with nothing to
        # show for it. This happened in production: broker.get_positions()
        # raised (a real, populated get_account_position() response hit a
        # field-name mismatch in _position_from_dict -- see that method's
        # docstring, never verified against a non-empty response) partway
        # through this method, and the `except StopIteration` here didn't
        # catch it, so the position was filled at Webull but never entered
        # self._positions at all. The broker lookup below is strictly a
        # nice-to-have (a more accurate avg_entry_price/quantity than the
        # signal/order already give us) -- it must never be allowed to
        # prevent local tracking from being recorded, so ANY failure here
        # (not just "no matching position") falls back to the signal/order's
        # own values instead.
        avg_entry_price = signal.reference_price
        quantity = order.quantity
        try:
            live_position = next(p for p in self.broker.get_positions() if p.symbol == candidate.symbol)
            avg_entry_price = live_position.avg_entry_price
            quantity = live_position.quantity
        except StopIteration:
            logger.warning(
                "No broker position found for %s immediately after fill confirmation; "
                "using signal reference price %.4f as avg_entry_price.",
                candidate.symbol, avg_entry_price,
            )
        except Exception:
            logger.exception(
                "broker.get_positions() failed while confirming the fill for %s; "
                "using signal reference price %.4f / order quantity %s instead of "
                "the broker's own position -- local position tracking still "
                "proceeds regardless so this fill is never silently lost.",
                candidate.symbol, avg_entry_price, quantity,
            )

        position = Position(
            symbol=candidate.symbol,
            side=order.side,
            quantity=quantity,
            avg_entry_price=avg_entry_price,
            stop_price=signal.suggested_stop,
            target_price=signal.suggested_target,
            trailing_stop_pct=None,
            opened_at=now,
            strategy_name=signal.strategy_name,
        )
        self._positions[candidate.symbol] = position
        self._attach_broker_bracket(candidate, position, now)
        self._ensure_streaming_subscribed([candidate.symbol])
        transition(candidate, CandidateState.ENTERED, now=now, reason="entry order filled")
        transition(candidate, CandidateState.MANAGING, now=now, reason="managing open position")

    # -- broker-side (resting) stop/target bracket ---------------------------

    def _attach_broker_bracket(self, candidate: Candidate, position: Position, now: datetime) -> None:
        """Best-effort: attaches a resting broker-side OCO stop+target
        bracket to `position` so its stop-loss and (initial) profit target
        are enforced by the broker itself, instead of relying solely on
        this loop's own polling cadence to notice a price cross and fire a
        market order after the fact. Motivated by a real incident (RDGT,
        2026-08-11): a stop sat unenforced past its level for a real loss
        because the software-side exit submission silently failed -- a
        resting broker order doesn't depend on this process being alive,
        awake, and error-free at the exact moment price crosses it.

        Called right after an entry fill is confirmed (_confirm_entry_filled),
        again after a partial exit fires at the broker (_poll_broker_bracket,
        to re-protect the remainder now that the original OCO's stop leg
        was auto-cancelled along with the target fill), and again via
        _sync_broker_protective_orders -- both whenever PositionManager's
        own breakeven/trailing math moves stop_price (cancel the stale
        resting order, then call back in here to place a fresh one) AND,
        every tick a position has no resting order yet at all, as a RETRY
        of a previous failed attempt (see that method's docstring's "no
        resting order yet" branch) -- so a single failed call here is
        never the end of the story for a broker that does support resting
        orders.

        No-op (leaves position.broker_stop_order_id unset) if
        position.stop_price is None (nothing to protect -- shouldn't
        normally happen, RiskEngine requires a stop for every entry, but
        this is defensive regardless) or if the connected broker doesn't
        support resting orders at all (see OrderManager.place_resting_stop/
        place_resting_bracket -- PaperBrokerClient/backtests, which is the
        normal case in tests): PositionManager.check_exit falls back to
        its pre-existing pure-software stop/target handling whenever
        broker_stop_order_id is unset, so nothing about non-Webull brokers
        changes.

        A broker call that raises (rejected order, rate limit, network
        error -- anything) is logged and swallowed here, leaving
        broker_stop_order_id unset so the position is protected by
        PositionManager's own software-side stop/target checks in the
        meantime -- but this is NOT a permanent fallback: real broker-side
        protection matters enough (see the RDGT incident above) that
        giving up after one failed attempt isn't acceptable.
        _sync_broker_protective_orders retries this every tick
        (~poll_interval_seconds apart) for as long as the position stays
        MANAGING and unbracketed, at CRITICAL rate-limiter priority (same
        as every order call here -- see place_order/place_oco_bracket in
        WebullBrokerClient), so a transient failure (429, a brief network
        blip) self-heals within a few ticks without this loop ever
        blocking to wait for it -- see call_with_retry's own fast,
        429-specific inner retry for the sub-second layer underneath
        this.

        Outside core hours (2026-08-12): skipped entirely, before any
        broker call is attempted -- confirmed live that a resting OCO
        stop+target bracket (STOP_LOSS+LIMIT legs) is rejected pre-market
        with support_trading_session="ALL" (OAUTH_OPENAPI_PARAM_ERR, the
        same error as the original 2026-08-10 finding) even though a plain
        LIMIT order tested clean minutes earlier -- see
        brokers/webull/client.py's _order_payload docstring. Before this
        gate existed, _sync_broker_protective_orders' every-tick retry (see
        above) kept re-attempting and re-failing this call outside core
        hours, burning CRITICAL-priority rate-limiter budget every single
        poll cycle and starving BACKGROUND-priority discovery/candidate-
        scanning calls behind it (observed live: candidates stopped
        populating during a pre-market run with an open position). Leaves
        broker_stop_order_id unset exactly like any other failed attempt,
        so PositionManager's pure-software stop/target/VWAP-failure/
        time-limit checks protect the position for the whole outside-core-
        hours duration -- see that class's check_exit docstring. Resumes
        attempting broker-side brackets normally the moment core hours
        start again (the very next _sync_broker_protective_orders retry)."""
        if position.stop_price is None:
            return
        if not is_within_core_trading_hours(now):
            return

        exit_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY_TO_COVER
        # Never re-arm a target leg once this position has already had its
        # one partial exit (see PositionManager.check_exit's
        # `not position.partial_exit_taken` gate on the equivalent
        # software-side check) -- the remainder rides on the stop alone
        # from here on, same contract as the pure-software path.
        target_price = None if position.partial_exit_taken else position.target_price
        target_quantity = None
        if target_price is not None:
            # Mirrors OrderManager.submit_signal's own SCALE_OUT floor-to-
            # half-share rule. A target too small to split into two whole
            # shares isn't bracketed with a separate leg at all -- the
            # resting stop alone still protects the full position.
            half = int(position.quantity // 2)
            if half >= 1:
                target_quantity = half
            else:
                target_price = None

        # Post-partial-exit specifically (not just "no target leg" --
        # see Position.broker_stop_is_trailing's docstring for why a
        # too-small-to-split position, which also has target_price=None
        # here but partial_exit_taken=False, must NOT take this branch),
        # protect the remainder with a native broker-side TRAILING_STOP
        # order instead of a plain STOP. PositionManager's own
        # trailing-stop math (_maybe_update_trailing_stop) still runs and
        # mutates position.stop_price every tick either way -- purely for
        # display/tracking once this is True, since Webull is now the one
        # actually moving the resting order (see
        # _sync_broker_protective_orders' skip for this case). Disabled
        # (falls through to a plain resting stop, identical to before this
        # existed) if trailing itself is off (trailing_stop_pct None/0).
        trailing_pct = self.position_manager.config.trailing_stop_pct
        use_trailing_stop = target_price is None and position.partial_exit_taken and trailing_pct

        # Defensive belt-and-suspenders, not load-bearing under normal
        # operation: every call site of this method already guarantees no
        # resting order is left over before calling in here (a fresh entry
        # has none yet; _poll_broker_bracket clears both ids before
        # re-attaching post-partial, since Webull's own OCO already
        # auto-cancelled the sibling leg; _sync_broker_protective_orders
        # cancels explicitly before replacing) -- but a broker-side
        # TRAILING_STOP order specifically cannot be added while another
        # resting sell order still reserves the same shares, so this
        # cancels first regardless of whether anything should still be
        # there, rather than trusting that invariant to hold forever as
        # this code keeps changing.
        if use_trailing_stop and (position.broker_stop_order_id is not None or position.broker_target_order_id is not None):
            self._cancel_broker_protective_orders(candidate.symbol, position)

        try:
            if target_price is not None:
                result = self.order_manager.place_resting_bracket(
                    candidate.symbol, exit_side, position.quantity, position.stop_price,
                    target_quantity, target_price, strategy_name=position.strategy_name, now=now,
                )
                if result is None:
                    return  # broker doesn't support resting orders -- software-only management for this position
                stop_order, target_order = result
                self._notify_order_update(stop_order)
                self._notify_order_update(target_order)
                position.broker_target_order_id = target_order.broker_order_id
                position.broker_stop_is_trailing = False
            elif use_trailing_stop:
                stop_order = self.order_manager.place_resting_trailing_stop(
                    candidate.symbol, exit_side, position.quantity, trailing_pct,
                    strategy_name=position.strategy_name, now=now,
                )
                if stop_order is None:
                    return
                self._notify_order_update(stop_order)
                position.broker_target_order_id = None
                position.broker_stop_is_trailing = True
            else:
                stop_order = self.order_manager.place_resting_stop(
                    candidate.symbol, exit_side, position.quantity, position.stop_price,
                    strategy_name=position.strategy_name, now=now,
                )
                if stop_order is None:
                    return
                self._notify_order_update(stop_order)
                position.broker_target_order_id = None
                position.broker_stop_is_trailing = False
        except Exception:
            logger.exception(
                "Failed to attach broker-side protective order(s) for %s -- riding on software-only "
                "position management for now, will keep retrying to attach a real broker-side "
                "bracket every tick until it succeeds.", candidate.symbol,
            )
            position.broker_stop_order_id = None
            position.broker_target_order_id = None
            position.broker_stop_price_synced = None
            position.broker_stop_is_trailing = False
            return

        position.broker_stop_order_id = stop_order.broker_order_id
        position.broker_stop_price_synced = position.stop_price

    def _cancel_broker_protective_orders(self, symbol: str, position: Position) -> None:
        """Cancels any resting broker-side stop/target orders still
        attached to `position` -- called before this loop submits its own
        market order against the same shares (a full EXIT via VWAP-
        failure/time-limit/kill-switch/end-of-core-hours; SCALE_OUT never
        reaches here, see PositionManager.check_exit's broker_managed gate:
        target hits are the broker's own job to fill once bracketed, not
        this loop's), and before replacing a resting stop at a new price
        (_sync_broker_protective_orders). Leaving a resting order in place
        after the position it protects is gone/about to change risks it
        either sitting forever as broker-side clutter or firing later
        against whatever this process opens next for the same symbol.

        Best-effort: a cancel failure (e.g. the order already filled or
        was cancelled moments earlier by the broker's own OCO logic) is
        logged and swallowed rather than blocking whatever exit/replace
        this is guarding -- and either way, the ids are cleared afterward
        so PositionManager reverts to software-only management for this
        position rather than assuming a resting order still exists that
        this call couldn't confirm was actually removed."""
        for order_id in (position.broker_stop_order_id, position.broker_target_order_id):
            if order_id is None:
                continue
            try:
                self.order_manager.cancel_resting_order(order_id)
            except Exception:
                logger.warning(
                    "Failed to cancel resting broker order %s for %s (may already be inactive).",
                    order_id, symbol, exc_info=True,
                )
        position.broker_stop_order_id = None
        position.broker_target_order_id = None
        position.broker_stop_price_synced = None
        position.broker_stop_is_trailing = False

    def _sync_broker_protective_orders(self, candidate: Candidate, position: Position, now: datetime) -> None:
        """Keeps the broker-side resting stop in step with PositionManager's
        own breakeven/trailing-stop math, which mutates position.stop_price
        in place every tick (see PositionManager.check_exit) but has no way
        to talk to the broker itself -- only TradingLoop calls
        order_manager/broker methods (see order_manager.py's docstring).

        If this position was never bracketed at the broker in the first
        place (broker_stop_order_id is None), that's either a broker that
        fundamentally doesn't support resting orders (a cheap no-op check
        every tick -- see OrderManager._broker_supports_resting_orders,
        consulted here via the same getattr pattern to avoid calling into
        _attach_broker_bracket at all for a broker that can never succeed)
        or a genuine RETRY: an earlier _attach_broker_bracket call failed
        (rate limit, network error, anything -- see that method's
        docstring) and this position is still riding on software-only
        management in the meantime. Real broker-side protection is
        important enough (see the RDGT incident note on
        _attach_broker_bracket) that this keeps calling
        _attach_broker_bracket every tick until it actually succeeds,
        rather than accepting the first failure as final.

        Once actually bracketed, this is a no-op if the stop hasn't
        moved since the last sync, or if it moved by less than
        TradingLoopConfig.stop_sync_min_move_pct (0.25% default) --
        hysteresis against the trailing-stop math recomputing to a
        different float almost every tick once active (`current_price *
        (1 - trailing_pct)` on a continuously-moving symbol), which would
        otherwise cancel+replace the resting order on nearly every single
        tick for changes too small to matter, burning CRITICAL-tier
        rate-limiter slots for no real protective benefit. When the move
        IS large enough, cancels BOTH resting legs (not just the stop) and
        re-attaches a fresh bracket via _attach_broker_bracket, rather than
        trying to update just the stop leg's price in place: Webull's
        modify_order/replace_order was live-tested and its effect on a
        resting order's price was inconclusive (see
        WebullBrokerClient.place_oco_bracket's docstring), and whether
        cancelling a single leg of an already-placed OCO combo cancels or
        orphans its sibling is unconfirmed either way -- cancelling both
        and placing a known-working fresh OCO (or lone stop, if no target
        is still active -- _attach_broker_bracket handles that branch on
        its own) sidesteps both open questions entirely.

        None of the above applies once position.broker_stop_is_trailing is
        True: that means the resting order is already a native
        TRAILING_STOP, which Webull moves on its own as price moves --
        there is nothing left for this process to compute or push, so this
        is unconditionally a no-op for as long as that stays True (see
        that field's docstring, and _attach_broker_bracket for where it
        gets set)."""
        if position.broker_stop_order_id is None:
            if getattr(self.broker, "place_oco_bracket", None) is not None:
                self._attach_broker_bracket(candidate, position, now)
            return
        if position.broker_stop_is_trailing:
            return
        if position.stop_price is None or position.stop_price == position.broker_stop_price_synced:
            return
        if position.broker_stop_price_synced:
            move_pct = abs(position.stop_price - position.broker_stop_price_synced) / position.broker_stop_price_synced * 100.0
            if move_pct < self.config.stop_sync_min_move_pct:
                return

        self._cancel_broker_protective_orders(candidate.symbol, position)
        self._attach_broker_bracket(candidate, position, now)

    def _get_open_orders_for_tick(self) -> Optional[dict[str, Order]]:
        """Returns {broker_order_id: Order} for every currently-resting
        order at the broker, fetched at most once per _process_all_candidates
        pass (see self._tick_open_orders_cache) and shared across every
        broker-managed position's _poll_broker_bracket call this tick --
        collapses what would otherwise be up to 2 get_order_status calls
        per position per tick (one per resting leg) into a single
        list_open_orders() call covering every position at once.

        Returns None (not an empty dict) if the broker doesn't support
        list_open_orders at all (PaperBrokerClient/backtests -- see
        WebullBrokerClient.list_open_orders' docstring for why it's not
        part of the BrokerClient interface) or if the call itself failed
        this tick -- callers must treat None as "fall back to the
        original per-leg get_status() polling", not as "no orders are
        open," which an empty dict (a real, successful "nothing resting
        right now" answer) means instead."""
        if self._tick_open_orders_cache is not None:
            return self._tick_open_orders_cache
        list_open_orders = getattr(self.broker, "list_open_orders", None)
        if list_open_orders is None:
            return None
        try:
            orders = list_open_orders()
        except Exception:
            logger.warning("list_open_orders failed this cycle; falling back to per-leg get_order_status polling.", exc_info=True)
            return None
        self._tick_open_orders_cache = {order.broker_order_id: order for order in orders if order.broker_order_id}
        return self._tick_open_orders_cache

    def _poll_broker_bracket(self, candidate: Candidate, position: Position, now: datetime) -> bool:
        """Checks whether this position's resting broker-side stop or
        target leg has filled since the last tick, finalizing the trade
        the same way a software-submitted exit would
        (_dispatch_exit_finalization) -- but without this loop ever having
        submitted the order itself; Webull filled it directly against the
        resting order placed by _attach_broker_bracket/
        _sync_broker_protective_orders. Returns True if a fill was found
        and handled this tick (caller should stop processing this
        candidate further this tick, mirroring _poll_pending_exit's early
        return), False otherwise (nothing filled yet, or a status check
        itself failed -- left for the next tick, same as any other broker
        poll failure in this loop).

        The target leg (when present) is always sized at half the position
        (see _attach_broker_bracket) and never re-armed after one partial,
        so a target-leg fill is unconditionally a SCALE_OUT, never a full
        EXIT -- unlike the pure-software path, there's no "too small to
        split, downgrade to a full exit" branch to replicate here since
        _attach_broker_bracket already made that same call before ever
        placing the leg.

        Checks _get_open_orders_for_tick's batched result first: a leg
        still listed there is confirmed still resting with NO individual
        get_order_status call needed at all -- the common case, every tick
        a resting order hasn't fired yet. Only a leg that's disappeared
        from that batch (or a broker that doesn't support the batch at
        all) falls back to the original one-call-per-leg polling below, to
        learn whether it specifically filled (vs. was cancelled/rejected)."""
        open_orders = self._get_open_orders_for_tick()

        for order_id, is_target in ((position.broker_stop_order_id, False), (position.broker_target_order_id, True)):
            if order_id is None:
                continue
            if open_orders is not None and order_id in open_orders:
                continue  # confirmed still resting via the batched fetch -- no individual call needed
            try:
                status_order = self.order_manager.get_status(order_id)
            except Exception:
                logger.warning(
                    "get_order_status failed for resting %s order %s on %s this cycle.",
                    "target" if is_target else "stop", order_id, candidate.symbol, exc_info=True,
                )
                continue
            self._notify_order_update(status_order)

            if status_order.status != OrderStatus.FILLED:
                continue

            exit_reason = ExitReason.PARTIAL_PROFIT_TARGET if is_target else ExitReason.STOP_LOSS
            exit_signal = Signal(
                symbol=candidate.symbol,
                action=SignalAction.SCALE_OUT if is_target else SignalAction.EXIT,
                generated_at=now,
                strategy_name=position.strategy_name,
                strategy_version="broker_bracket",
                reference_price=status_order.limit_price or status_order.stop_price or position.avg_entry_price,
                metadata={"exit_reason": exit_reason.value},
            )
            # The sibling leg (if any) was auto-cancelled by Webull's OCO
            # the instant this one filled (see
            # WebullBrokerClient.place_oco_bracket's docstring) -- clear
            # both ids up front so neither PositionManager's software
            # checks nor a concurrent _sync_broker_protective_orders call
            # try to act on now-stale ids.
            position.broker_stop_order_id = None
            position.broker_target_order_id = None
            position.broker_stop_price_synced = None
            position.broker_stop_is_trailing = False
            self._dispatch_exit_finalization(candidate, position, status_order, exit_signal, now)

            if exit_signal.action == SignalAction.SCALE_OUT:
                # The stop leg protecting the FULL original quantity was
                # just auto-cancelled along with this target fill -- the
                # remainder is naked until a fresh resting stop is placed
                # for it.
                self._attach_broker_bracket(candidate, position, now)
            return True
        return False

    def _manage_position(self, candidate: Candidate, snapshot: MarketSnapshot, now: datetime) -> None:
        pending = self._pending_exit_orders.get(candidate.symbol)
        if pending is not None:
            self._poll_pending_exit(candidate, snapshot, now)
            return

        position = self._positions.get(candidate.symbol)
        if position is None:
            logger.warning("%s is %s but has no tracked position; moving to COOLDOWN.", candidate.symbol, candidate.state.value)
            transition(candidate, CandidateState.EXITED, now=now, reason="position tracking lost")
            transition(candidate, CandidateState.COOLDOWN, now=now, reason="post-trade cooldown")
            return

        if position.broker_stop_order_id is not None or position.broker_target_order_id is not None:
            if self._poll_broker_bracket(candidate, position, now):
                return  # a resting leg filled and finalized this tick -- nothing else to do

        exit_signal = self.position_manager.check_exit(position, snapshot, now=now)
        if exit_signal is None:
            self._sync_broker_protective_orders(candidate, position, now)
            return

        if position.broker_stop_order_id is not None or position.broker_target_order_id is not None:
            # A software-side exit for a broker-managed position only ever
            # happens for VWAP failure / time limit (see
            # PositionManager.check_exit's broker_managed gate -- stop/
            # target price-crosses are the broker's own job once
            # bracketed). Cancel the resting order(s) first so nothing is
            # left resting against a position that's about to be fully
            # closed by the market order below.
            self._cancel_broker_protective_orders(candidate.symbol, position)

        try:
            order = self.order_manager.submit_signal(exit_signal, snapshot=snapshot, position=position)
        except OrderRejected:
            # Exits aren't supposed to be rejectable (see order_manager.py),
            # but don't crash the loop if something unexpected happens.
            logger.exception("Unexpected OrderRejected on an exit signal for %s.", candidate.symbol)
            return
        except Exception:
            # Mirrors _submit_entry's own catch-all below, added for the
            # same reason: a real incident where a stop-loss failed to fire
            # on a position sitting well past its stop, with the only trace
            # of why buried inside _process_all_candidates' generic
            # per-candidate "Unhandled error processing candidate" catch --
            # useful enough to notice something failed, not specific enough
            # to see AT WHICH STEP or WHY without re-reading this whole
            # method's call chain under time pressure. This still just
            # returns (check_exit re-evaluates fresh next tick, same as
            # the OrderRejected branch above -- no candidate/position state
            # needs to change here, unlike _submit_entry reverting TRIGGERED
            # back to ARMED), but now logs specifically that IT WAS THE
            # EXIT SUBMISSION that failed, for this symbol, with the real
            # traceback, the instant it happens.
            logger.exception(
                "broker.place_order raised submitting an exit (%s) for %s -- position remains open, "
                "will retry next tick.", exit_signal.action.value, candidate.symbol,
            )
            return
        self._notify_order_update(order)

        if order.status == OrderStatus.FILLED:
            self._dispatch_exit_finalization(candidate, position, order, exit_signal, now)
        else:
            self._pending_exit_orders[candidate.symbol] = (order, exit_signal)

    def _poll_pending_exit(self, candidate: Candidate, snapshot: MarketSnapshot, now: datetime) -> None:
        order, exit_signal = self._pending_exit_orders[candidate.symbol]
        try:
            status_order = self.order_manager.get_status(order.broker_order_id)
        except Exception:
            logger.warning("get_order_status failed for pending exit on %s.", candidate.symbol, exc_info=True)
            return
        self._notify_order_update(status_order)

        if status_order.status == OrderStatus.FILLED:
            self._pending_exit_orders.pop(candidate.symbol)
            position = self._positions[candidate.symbol]
            self._dispatch_exit_finalization(candidate, position, status_order, exit_signal, now)
        elif status_order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
            self._pending_exit_orders.pop(candidate.symbol)
            logger.warning("Exit order for %s did not fill (%s); will re-evaluate exit next tick.", candidate.symbol, status_order.status.value)
        # else still pending

    def _dispatch_exit_finalization(self, candidate: Candidate, position: Position, order: Order, exit_signal: Signal, now: datetime) -> None:
        """SCALE_OUT (a target hit -- see PositionManager.check_exit) closes
        only part of the position and leaves the candidate MANAGING so the
        remainder keeps being tracked; EXIT closes it entirely."""
        if exit_signal.action == SignalAction.SCALE_OUT:
            self._finalize_partial_exit(candidate, position, order, exit_signal, now)
        else:
            self._finalize_exit(candidate, position, order, exit_signal, now)

    def _build_trade_from_fill(self, candidate: Candidate, position: Position, order: Order, exit_signal: Signal, now: datetime) -> Trade:
        exit_price = None
        try:
            fills = [f for f in self.broker.poll_fills(since=order.created_at) if f.order_client_id == order.client_order_id]
            if fills:
                exit_price = fills[-1].price
        except Exception:
            logger.warning("poll_fills failed while finalizing exit for %s.", candidate.symbol, exc_info=True)

        if exit_price is None:
            # Fallback: fill lookup is best-effort (see WebullBrokerClient's
            # poll_fills docstring -- unverified response shape). Approximate
            # with the position's stop/target level rather than fabricating precision.
            exit_price = position.stop_price or position.target_price or position.avg_entry_price

        pnl = (exit_price - position.avg_entry_price) * order.quantity
        pnl_pct = (exit_price - position.avg_entry_price) / position.avg_entry_price * 100.0 if position.avg_entry_price else 0.0
        exit_reason = ExitReason(exit_signal.metadata.get("exit_reason", ExitReason.MANUAL.value))

        return Trade(
            symbol=candidate.symbol,
            strategy_name=position.strategy_name,
            side=position.side,
            entry_price=position.avg_entry_price,
            exit_price=exit_price,
            quantity=order.quantity,
            opened_at=position.opened_at,
            closed_at=now,
            exit_reason=exit_reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            max_favorable_excursion=position.max_favorable_excursion,
            max_adverse_excursion=position.max_adverse_excursion,
        )

    def _finalize_exit(self, candidate: Candidate, position: Position, order: Order, exit_signal: Signal, now: datetime) -> None:
        trade = self._build_trade_from_fill(candidate, position, order, exit_signal, now)

        self.risk_engine.record_trade_closed(candidate.symbol, trade.pnl, now=now)
        self._positions.pop(candidate.symbol, None)
        if self.on_trade_closed is not None:
            try:
                self.on_trade_closed(trade)
            except Exception:
                logger.exception("on_trade_closed callback raised for %s.", candidate.symbol)

        transition(candidate, CandidateState.EXITED, now=now, reason=trade.exit_reason.value)
        transition(candidate, CandidateState.COOLDOWN, now=now, reason="post-trade cooldown")

    def _finalize_partial_exit(self, candidate: Candidate, position: Position, order: Order, exit_signal: Signal, now: datetime) -> None:
        """A target hit sold only `order.quantity` shares (see
        PositionManager.check_exit/OrderManager.submit_signal's SCALE_OUT
        handling) -- unlike a full exit, the position stays open (reduced
        quantity) and the candidate stays MANAGING; only this realized
        slice becomes a Trade record. partial_exit_taken is set so the next
        tick's check_exit doesn't fire another partial while price remains
        above target -- the remainder is now governed purely by the
        stop/trailing-stop/breakeven/VWAP/time-limit checks."""
        trade = self._build_trade_from_fill(candidate, position, order, exit_signal, now)

        self.risk_engine.record_trade_closed(candidate.symbol, trade.pnl, now=now)
        position.quantity -= order.quantity
        position.partial_exit_taken = True
        if self.on_trade_closed is not None:
            try:
                self.on_trade_closed(trade)
            except Exception:
                logger.exception("on_trade_closed callback raised for %s.", candidate.symbol)

    # -- read-only accessors for external consumers (e.g. the dashboard) -----

    def get_candidates(self) -> dict[str, Candidate]:
        """Shallow copy of the tracked candidates dict -- safe to iterate
        without racing a concurrent rescan/run_once() mutating it (e.g. from
        a dashboard reading this loop's state from another thread)."""
        with self._candidates_lock:
            return dict(self.candidates)

    def get_open_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    # -- startup reconciliation --------------------------------------------

    def reconcile_positions_from_broker(self, now: Optional[datetime] = None) -> None:
        """Two-way sync between self._positions and whatever the broker
        actually reports, in both directions:

        1. Adopts any position the broker reports that this process
           doesn't already know about. Originally added to close a gap
           structurally identical in symptom to the get_positions()-parsing-failure
           incident documented in _confirm_entry_filled: self._positions is
           a plain in-memory dict with no persistence of its own, so EVERY
           process restart -- a deploy, a crash, a VPS reboot -- previously
           wiped tracking for a position that was genuinely open a moment
           before, with nothing to notice or recover it.
        2. Drops any position this process still thinks is open that the
           broker no longer reports at all. Also a real incident, found the
           same day as (1): scripts/list_and_close_positions.py closes a
           position by calling broker.place_order directly, entirely
           outside this running process -- there was nothing to ever tell
           the bot that happened, so the dashboard kept showing a position
           as open indefinitely after it was genuinely closed. The same gap
           applies to a manual close from the Webull app itself, or any
           other out-of-band close this codebase doesn't control.

        Called from _process_all_candidates, throttled by
        TradingLoopConfig.position_reconcile_interval_seconds, but firing
        immediately on that method's very first-ever call regardless
        (self._last_position_reconcile starts unset) -- both run_once and
        run_forever route through _process_all_candidates, so every real
        entrypoint gets an immediate reconcile before any candidate is
        processed, with no separate startup call needed. (2) in particular
        needs to run repeatedly, not just once, since an external close can
        happen at any time while this process keeps running.

        An adopted position (case 1) has no original strategy Signal to
        pull stop_price/target_price from (that only exists at the moment a
        Signal fires, and this position may have opened in a previous
        process's lifetime). Given a flat stop/target instead, from the
        same two risk settings a real signal's flat-% strategies (see
        RiskConfig.stop_loss_pct's docstring for which ones) use:

            stop_price = current_price -+ stop_loss_pct%      (long/short)
            target_price = current_price +- stop_loss_pct% * min_risk_reward_ratio

        Simplified 2026-08-11 back to this flat-%-of-price form (it briefly
        tried to reverse-engineer a stop from account_equity/quantity so an
        adopted position's realized loss at the stop would equal a fixed %
        of equity -- that only made sense while risk_per_trade_pct meant
        "% of equity to risk," and stopped being correct the moment that
        field was renamed to stop_loss_pct and repurposed as a genuine
        per-position stop distance instead; the equity-based version would
        have quietly gone back to computing the wrong thing). No
        get_account_equity() call needed at all now -- a flat % is
        well-defined regardless of share count, so there's no degenerate
        case to fall back from either."""
        now = now or datetime.utcnow()
        try:
            # _get_positions_for_tick, not broker.get_positions() directly:
            # this call and _maybe_verify_entry_via_positions' own can land
            # in the same _process_all_candidates pass (reconcile's own
            # throttle is independent of any candidate's entry-verify
            # timing), so sharing one result avoids a second, redundant
            # network round-trip for the exact same data within one tick.
            broker_positions = self._get_positions_for_tick()
        except Exception:
            logger.exception("reconcile_positions_from_broker: get_positions() failed; skipping this run.")
            return

        broker_symbols = {p.symbol for p in broker_positions}
        # Any symbol the broker DID report this pass has recovered (or was
        # never actually missing) -- forget its miss streak, if any, so a
        # later transient miss starts counting from zero again rather than
        # inheriting stale history.
        for symbol in broker_symbols & self._missing_from_broker_counts.keys():
            del self._missing_from_broker_counts[symbol]

        for symbol in set(self._positions.keys()) - broker_symbols:
            if symbol in self._pending_exit_orders:
                # This process's own exit is already in flight for this
                # symbol -- let _poll_pending_exit/_dispatch_exit_finalization
                # finish it normally instead of yanking it out from under
                # that machinery just because the broker-side quantity has
                # already dropped to zero ahead of this process noticing.
                # Not a "missing" observation at all -- don't let it count
                # toward the streak below.
                self._missing_from_broker_counts.pop(symbol, None)
                continue

            miss_count = self._missing_from_broker_counts.get(symbol, 0) + 1
            self._missing_from_broker_counts[symbol] = miss_count
            if miss_count < self.config.position_missing_confirmations_required:
                # Not yet confirmed -- see
                # TradingLoopConfig.position_missing_confirmations_required's
                # docstring: a single broker.get_positions() response
                # missing a symbol isn't trusted as "actually closed" on
                # its own, since a live account under rate-limit
                # contention isn't guaranteed to return a complete list
                # even on a 200. Left fully tracked/managed in the
                # meantime -- next tick's software/broker-side checks run
                # exactly as if this pass hadn't happened.
                logger.warning(
                    "%s missing from broker.get_positions() (%d/%d consecutive reconcile passes) "
                    "-- not yet treating as closed externally.", symbol,
                    miss_count, self.config.position_missing_confirmations_required,
                )
                continue

            stale_position = self._positions[symbol]
            if stale_position.broker_stop_order_id is not None or stale_position.broker_target_order_id is not None:
                # Whatever closed this position out-of-band (a manual close
                # in the Webull app, an external script) may not have gone
                # through the resting stop/target legs this process placed
                # -- best-effort cleanup so they don't sit orphaned at the
                # broker indefinitely.
                self._cancel_broker_protective_orders(symbol, stale_position)
            logger.warning(
                "%s no longer exists at the broker (closed outside this process -- a manual "
                "close, scripts/list_and_close_positions.py, etc.) -- removing from local "
                "tracking so the dashboard reflects reality. Confirmed missing across %d "
                "consecutive reconcile passes.", symbol, miss_count,
            )
            del self._positions[symbol]
            del self._missing_from_broker_counts[symbol]
            candidate = self.candidates.get(symbol)
            if candidate is not None and candidate.state in (CandidateState.ENTERED, CandidateState.MANAGING):
                transition(candidate, CandidateState.EXITED, now=now, reason="position closed externally (not by this process)")
                transition(candidate, CandidateState.COOLDOWN, now=now, reason="post-external-close cooldown")

        with self._candidates_lock:
            existing_candidates = dict(self.candidates)

        for position in broker_positions:
            symbol = position.symbol
            if symbol in self._positions:
                continue  # already tracked this process's own lifetime -- nothing to adopt

            try:
                snapshot = self.broker.get_snapshot(symbol)
            except Exception:
                logger.warning(
                    "reconcile_positions_from_broker: get_snapshot failed for %s; skipping adoption this run.",
                    symbol, exc_info=True,
                )
                continue

            is_short = position.side == OrderSide.SELL_SHORT
            stop_pct = self.risk_engine.config.stop_loss_pct / 100.0
            reward_risk_ratio = self.risk_engine.config.min_risk_reward_ratio
            if is_short:
                position.stop_price = snapshot.last_price * (1 + stop_pct)
                position.target_price = snapshot.last_price * (1 - stop_pct * reward_risk_ratio)
            else:
                position.stop_price = snapshot.last_price * (1 - stop_pct)
                position.target_price = snapshot.last_price * (1 + stop_pct * reward_risk_ratio)
            position.strategy_name = "reconciled_at_startup"
            self._positions[symbol] = position

            candidate = existing_candidates.get(symbol)
            if candidate is None or candidate.state not in (CandidateState.ENTERED, CandidateState.MANAGING):
                # A single-hop transition straight to MANAGING is only ever
                # legal from ENTERED (see state_machine._ALLOWED_TRANSITIONS)
                # -- an existing candidate sitting anywhere else (TRIGGERED
                # in particular: this is exactly the case where an entry
                # filled at the broker but this process never confirmed it,
                # which is the whole reason adoption exists) would raise
                # InvalidStateTransition here, aborting this entire
                # reconciliation pass for every symbol still left in the
                # loop -- confirmed live: candidates stuck in TRIGGERED
                # forever, since the exact fix meant to unstick them kept
                # crashing on the very attempt to do so. Simplest correct
                # fix: always rebuild a fresh Candidate through the full,
                # always-legal WATCHING->...->MANAGING chain (same as the
                # "no existing candidate at all" case) rather than trying to
                # advance whatever state the stale existing one happens to
                # be in -- that existing candidate's state already disagrees
                # with reality (the broker has a real fill; its state
                # machine says otherwise), so it isn't worth preserving.
                candidate = new_candidate(symbol, now=now)
                for state in (
                    CandidateState.WATCHING, CandidateState.HEATING_UP, CandidateState.ARMED,
                    CandidateState.TRIGGERED, CandidateState.ENTERED, CandidateState.MANAGING,
                ):
                    transition(candidate, state, now=now)
                with self._candidates_lock:
                    self.candidates[symbol] = candidate
                # The old candidate object (if any) may have already had
                # some of its transitions persisted under this count --
                # that object is now discarded, so persisting the fresh
                # one's full history from scratch (rather than skipping
                # entries _flush_state_transitions would otherwise think
                # are already covered) is the correct restart point.
                self._persisted_transition_counts.pop(symbol, None)

            # Adopted the same way a fresh entry fill would be: attach a
            # resting broker-side bracket as soon as this position is
            # locally tracked, rather than leaving it to ride purely on
            # the software-side check_exit until the next price-cross
            # tick -- this position may have opened in a previous
            # process's lifetime (a deploy, a crash, a VPS reboot), so
            # there is no earlier moment this process could have done it.
            # Unlike before this fix, position.target_price is now real
            # (see above), so this places a full stop+target OCO bracket,
            # not just a lone stop.
            self._attach_broker_bracket(candidate, position, now)
            self._ensure_streaming_subscribed([symbol])

            logger.warning(
                "Adopted untracked broker position at startup: %s qty=%s side=%s "
                "stop_price=%.4f target_price=%.4f (stop_loss_pct=%.1f%%, "
                "min_risk_reward_ratio=%.2f, current price %.4f).",
                symbol, position.quantity, position.side.value, position.stop_price,
                position.target_price, self.risk_engine.config.stop_loss_pct,
                self.risk_engine.config.min_risk_reward_ratio, snapshot.last_price,
            )

    # -- main loop -------------------------------------------------------------

    def run_once(self, now: Optional[datetime] = None) -> None:
        """Single-threaded, synchronous pass: rescan (if due) then process
        every candidate, all on the caller's thread. Kept exactly as it
        always behaved for callers (mainly tests) that call it directly and
        expect one deterministic pass -- run_forever() does NOT call this;
        it runs the rescan on a separate background thread instead (see this
        module's docstring's "Concurrency model" section)."""
        now = now or datetime.utcnow()
        if (
            self._last_universe_scan is None
            or (now - self._last_universe_scan) >= timedelta(seconds=self.config.universe_rescan_interval_seconds)
        ):
            self._rescan_universe(now)
            self._last_universe_scan = now

        self._process_all_candidates(now)

    def _process_all_candidates(self, now: datetime) -> None:
        # Fresh per pass -- see _get_positions_for_tick/self._tick_positions_cache's
        # docstrings: at most one real broker.get_positions() call gets
        # shared across every caller within THIS pass, never reused into
        # the next one.
        self._tick_positions_cache = None
        self._tick_open_orders_cache = None

        # Runs here (main thread) rather than acted on immediately by
        # whatever thread called engage_kill_switch_and_flatten -- keeps
        # all position-closing work on the single thread that already owns
        # mutating candidates/positions, with no new locking needed. A few
        # seconds of latency (at most one poll_interval_seconds) before
        # flattening actually starts is an acceptable cost for that safety;
        # new entries are already blocked immediately regardless, since
        # engage_kill_switch itself takes effect the instant it's called.
        #
        # Re-evaluated every tick (fixed 2026-08-11) rather than a one-shot
        # flag consumed once: the kill switch is an emergency stop, and a
        # single failed close attempt on any symbol -- a rate limit, a
        # get_snapshot hiccup, anything -- must not permanently abandon
        # the flatten for it. Mirrors the end-of-day auto-flatten's own
        # already-correct pattern just below. Naturally becomes a no-op
        # once either every position has actually closed
        # (`self._positions` empty) or the switch is disengaged
        # (`kill_switch_active` False) -- whichever happens first.
        if self._positions and self.risk_engine.kill_switch_active:
            try:
                self._close_all_positions_now(self._close_all_positions_reason or "Kill switch engaged", now)
            except Exception:
                logger.exception("Unhandled error force-closing all positions for kill switch.")

        # Per-position manual close (dashboard's "Close" button, see
        # request_manual_close) -- same retry-every-tick contract as the
        # kill switch above, scoped to just the requested symbol(s). A
        # symbol drops out of self._positions the moment its close
        # actually finalizes (_finalize_exit), which is this set's only
        # "done" signal -- there's no separate completion callback to wire
        # up, so self-cleaning against the live position dict here is
        # simpler and can't drift out of sync with it. Pruned both before
        # AND after the close attempt below (not just before): a close
        # that finalizes synchronously within this same tick would
        # otherwise leave its symbol dangling in the set for one extra
        # tick before the next call's leading prune caught it.
        self._manual_close_requests &= set(self._positions)
        if self._manual_close_requests:
            try:
                self._close_all_positions_now(
                    "Manual close requested from dashboard.", now,
                    exit_reason=ExitReason.MANUAL, symbols=self._manual_close_requests,
                )
            except Exception:
                logger.exception("Unhandled error force-closing manually-requested position(s).")
            self._manual_close_requests &= set(self._positions)

        # End-of-day auto-flatten: unlike the kill switch above, this
        # doesn't set risk_engine.kill_switch_active (that's a manual,
        # sticky halt meant to require a human to clear it -- see the
        # dashboard's kill-switch toggle) and isn't a one-shot flag either.
        # It just checks the clock every tick and calls the same
        # _close_all_positions_now this method already uses for the kill
        # switch, reusing its exact submit/pending/finalize path. Cheap to
        # call on every tick once inside the buffer window: RiskEngine.evaluate
        # already independently refuses any *new* entry outside core hours
        # (see market_hours.is_within_core_trading_hours there), so
        # self._positions only ever has something in it here if a position
        # was still open going into the close -- after the first successful
        # flatten each day, this is a no-op loop over an empty dict.
        #
        # Fires config.end_of_day_flatten_buffer_minutes BEFORE the actual
        # 4:00pm ET close (is_within_closing_buffer), NOT at/after it --
        # observed live 2026-08-11 that firing at the close itself left a
        # position open indefinitely, retried every tick with no visible
        # progress (leading diagnosis: the flatten's MARKET/CORE exit
        # order needs a still-live CORE session, already ended by then --
        # see is_within_closing_buffer's docstring for why this isn't
        # independently confirmed via a captured rejection message). See
        # that docstring for the full reasoning and why this still fires
        # every tick (not just once) so a position opened in the last
        # moments before the buffer window started is still caught.
        if self._positions and is_within_closing_buffer(now, self.config.end_of_day_flatten_buffer_minutes):
            try:
                self._close_all_positions_now(
                    "Auto-flattening open position(s) before end of core trading hours.",
                    now,
                    exit_reason=ExitReason.END_OF_CORE_HOURS,
                )
            except Exception:
                logger.exception("Unhandled error auto-flattening positions before end of core trading hours.")

        if (
            self._last_position_reconcile is None
            or (now - self._last_position_reconcile) >= timedelta(seconds=self.config.position_reconcile_interval_seconds)
        ):
            self._last_position_reconcile = now
            try:
                self.reconcile_positions_from_broker(now)
            except Exception:
                logger.exception("Unhandled error reconciling positions against the broker.")

        candidates = self._snapshot_candidates()

        # Keep every candidate in a streaming-eligible state subscribed to
        # live prices -- cheap to call every tick: _ensure_streaming_subscribed
        # already no-ops for a symbol already requested this process's
        # lifetime, so in steady state this is just a membership check per
        # candidate, not a real subscribe call. ENTERED/MANAGING positions
        # also get an eager first attempt right when they start being
        # tracked (see _confirm_entry_filled and
        # reconcile_positions_from_broker) so they don't wait a full tick
        # for their first subscription -- this sweep is what RETRIES that
        # attempt on every subsequent tick if it failed (subscribe_quotes
        # raising leaves a symbol out of self._streaming_requested_symbols,
        # so it's picked up again here rather than silently never
        # streaming for the rest of the process -- see
        # _ensure_streaming_subscribed's docstring).
        streaming_eligible_symbols = [c.symbol for c in candidates if c.state in self._STREAMING_ELIGIBLE_STATES]
        if streaming_eligible_symbols:
            self._ensure_streaming_subscribed(streaming_eligible_symbols)

        # Batch-fetch snapshots for every candidate that will actually need
        # one this cycle (mirrors _process_candidate_inner's own REJECTED/
        # COOLDOWN skip below) in as few Webull-paced round-trips as
        # possible, instead of each candidate making its own get_snapshot()
        # call -- see WebullBrokerClient.get_snapshots' docstring for why
        # this matters: every get_snapshot-family call shares the same
        # globally-paced rate limiter regardless of request size, so N
        # tracked candidates used to mean a real >=N-second floor on how
        # often any single one's tick refreshed. Not part of the
        # BrokerClient interface (paper/backtest has no rate limit to work
        # around), so checked via getattr like get_raw_bars/get_daily_volumes
        # elsewhere in this codebase -- a broker without it just means every
        # candidate below falls back to its own get_snapshot() call, exactly
        # this method's behavior before batching existed.
        batch_snapshots: dict[str, MarketSnapshot] = {}
        get_snapshots = getattr(self.broker, "get_snapshots", None)
        if get_snapshots is not None:
            symbols_needing_snapshot = [
                c.symbol for c in candidates
                if c.state not in (CandidateState.REJECTED, CandidateState.COOLDOWN)
                # A candidate in any _STREAMING_ELIGIBLE_STATES state with
                # a fresh live-streamed price (see _get_streaming_snapshot)
                # doesn't need this REST call at all --
                # _process_candidate_inner will use the streamed value
                # directly. Excluding it here (not just having
                # _process_candidate_inner prefer it once fetched) is what
                # actually realizes streaming's rate-limit savings; leaving
                # it in this batch would fetch it over REST anyway and
                # simply discard that fetch unused.
                and not (
                    c.state in self._STREAMING_ELIGIBLE_STATES
                    and self._get_streaming_snapshot(c.symbol, now) is not None
                )
            ]
            if symbols_needing_snapshot:
                try:
                    # CRITICAL priority: this batch includes every MANAGING
                    # position's price, which directly feeds
                    # PositionManager.check_exit's stop/target/VWAP
                    # decisions -- must win contention over
                    # BroadScanner's own (BACKGROUND-priority) discovery
                    # snapshot calls, not queue behind them. See
                    # WebullBrokerClient.get_snapshots' `priority`
                    # docstring note and retry.py's CallPriority docstring.
                    batch_snapshots = get_snapshots(symbols_needing_snapshot, priority=CallPriority.CRITICAL)
                except Exception:
                    logger.exception("Batch get_snapshots failed this cycle; falling back to per-candidate fetch.")

        for candidate in candidates:
            try:
                self._process_candidate(candidate, now, batch_snapshots.get(candidate.symbol))
            except Exception:
                logger.exception("Unhandled error processing candidate %s; continuing loop.", candidate.symbol)

    def _universe_rescan_loop(self, stop_flag: Optional[Callable[[], bool]]) -> None:
        """Runs on a background daemon thread from run_forever(): repeatedly
        rescans the universe back-to-back (the configured interval is a
        floor, not an idle wait -- see TradingLoopConfig's docstring), so
        the main thread's candidate/position processing never blocks on it."""
        while stop_flag is None or not stop_flag():
            now = datetime.utcnow()
            self._rescan_universe(now)
            self._last_universe_scan = now
            elapsed = (datetime.utcnow() - now).total_seconds()
            remaining = self.config.universe_rescan_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def run_forever(self, stop_flag: Optional[Callable[[], bool]] = None) -> None:
        """Runs candidate/position processing back-to-back on
        poll_interval_seconds, with universe rescanning decoupled onto its
        own background thread so a slow rescan can never delay exit/stop-
        loss management -- see this module's docstring's "Concurrency
        model" section. Runs until stop_flag() returns True (if provided).

        No explicit startup call to reconcile_positions_from_broker needed
        here: _process_all_candidates' own throttled check fires on its
        very first invocation regardless (self._last_position_reconcile
        starts unset), which happens before any candidate gets processed --
        see that method and reconcile_positions_from_broker's docstring."""
        rescan_thread = threading.Thread(
            target=self._universe_rescan_loop, args=(stop_flag,), daemon=True, name="universe-rescan",
        )
        rescan_thread.start()
        while stop_flag is None or not stop_flag():
            now = datetime.utcnow()
            self._process_all_candidates(now)
            time.sleep(self.config.poll_interval_seconds)
