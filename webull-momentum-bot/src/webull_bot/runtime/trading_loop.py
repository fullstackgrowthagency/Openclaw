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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

from ..brokers.webull.retry import CallPriority
from ..collection.event_recorder import MomentumEventTracker
from ..enums import CandidateState, ExitReason, MomentumPhase, OrderSide, OrderStatus, RiskEventType, SignalAction
from ..execution.order_manager import BracketEntryRejected, BracketSubmissionResult, OrderManager, OrderRejected
from ..interfaces.broker import BrokerClient
from ..data.universe import SymbolUniverseProvider
from ..market_hours import is_within_closing_buffer, is_within_core_trading_hours
from ..metrics.volume_profile import compute_runway_consumed_pct, evaluate_target_clearance
from ..models import Candidate, MarketSnapshot, MomentumEvent, MomentumScore, MomentumState, Order, Position, Signal, Trade
from ..position.position_manager import PositionManager
from ..risk.risk_engine import RiskEngine
from ..scanner.broad_scanner import BroadScanner
from ..scanner.candidate_watcher import CandidateWatcher
from ..scanner.momentum_qualification import MomentumQualificationEngine, TriggerDecision
from ..scanner.momentum_structure import momentum_structure_intact
from ..scanner.trigger_engine import TriggerEngine
from ..scoring.strategy_quality import strategy_quality_score
from ..state_machine import new_candidate, transition

logger = logging.getLogger(__name__)


@dataclass
class PendingConfirmation:
    """Tracks a strategy trigger through TradingLoop's confirmation window --
    see enums.CandidateState.CONFIRMING's docstring and _poll_confirmation.
    `signal` starts as the ORIGINAL Signal the strategy produced at trigger
    time; _poll_confirmation replaces it with a recomputed one (reference_price/
    suggested_stop/suggested_target recentered on the actual confirmed price)
    once the window elapses cleanly -- see that method for why the stale
    trigger-time price is never used directly for order submission.
    `snapshot` is refreshed every tick (whatever _poll_confirmation last saw)
    so _submit_ranked_entries always has a fresh one to hand to _submit_entry
    regardless of how many ticks a confirmed-but-unslotted candidate waits."""
    signal: Signal
    momentum_event: Optional[MomentumEvent]
    started_at: datetime
    reference_price: float
    snapshot: MarketSnapshot


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
    # How long a candidate can sit in a PRE-ENTRY, NOT-currently-active state
    # (WATCHING/HEATING_UP/REJECTED/COOLDOWN -- see _PRUNABLE_STATES) with no
    # state transition at all before _prune_stale_candidates drops it from
    # self.candidates entirely and unsubscribes its symbol from streaming.
    # Real incident (2026-08-14): self.candidates has never had a removal
    # path since this codebase's very first version -- every symbol
    # BroadScanner ever surfaced stayed tracked, and re-processed every
    # single tick, for the rest of the process's life. By mid-day this had
    # grown to 184 tracked candidates, which (1) blew past Webull's
    # confirmed cumulative-per-session streaming-subscribe cap (see
    # brokers/webull/client.py's _STREAMING_SUBSCRIBE_BATCH_SIZE comment
    # and _reconcile_streaming_subscriptions below) so most of them fell
    # back to REST polling, and (2) that REST-polling volume saturated the
    # shared account-wide rate limiter badly enough that BroadScanner's
    # own BACKGROUND-priority discovery calls appear to have been starved
    # out completely (zero "passed broad scanner filters" log lines for
    # the entire day) -- the bot effectively stopped finding NEW winners
    # while it kept re-polling a growing pile of old, cold ones. A
    # candidate's last_updated_at only moves on an actual state transition
    # (see state_machine.transition), not on every tick's score recompute
    # -- so this correctly measures "how long has nothing interesting
    # happened for this symbol," not "how long since it was last ticked."
    # Deliberately excludes ARMED/CONFIRMING/TRIGGERED (actively working
    # toward an entry) and ENTERED/MANAGING (an open position) -- those
    # must never be dropped out from under the entry/exit logic still
    # actively managing them. 1 hour is a starting value, not backtested:
    # long enough that a name which cools off and reheats within a normal
    # low-float move's timeframe isn't lost, short enough that a full
    # session's worth of one-off cold discoveries doesn't accumulate
    # unbounded the way it did today.
    candidate_stale_after_seconds: float = 3600.0
    # How often reconcile_positions_from_broker re-runs after its initial
    # run_forever-startup call -- see that method's docstring for why this
    # needs to run more than once: a position can be closed OUTSIDE this
    # process entirely (e.g. scripts/list_and_close_positions.py, or a
    # manual close in the Webull app itself), and nothing else in this
    # codebase ever notices that on its own. One extra broker.get_positions()
    # call per interval (not per-candidate, unlike get_snapshots) is cheap
    # enough to run this fairly often.
    position_reconcile_interval_seconds: float = 30.0
    # How often TradingLoop refreshes its own cached account equity/buying
    # power in the background -- see get_account_summary's docstring. Real
    # incident (2026-08-12): the dashboard's /api/status called
    # broker.get_account_equity()/get_buying_power() live on EVERY HTTP
    # request, through the same shared, priority-queued, occasionally-
    # exclusive (place_order/place_oco_bracket) webull_limiter used for
    # order placement -- under real trading load a dashboard request could
    # queue behind CRITICAL trading traffic (or a whole exclusive() hold)
    # for tens of seconds, past nginx's proxy_read_timeout, producing 504s.
    # This value only needs to be "fresh enough for a human glancing at a
    # dashboard," not "fresh for a trading decision" (nothing in this
    # codebase's own trading logic reads the cache this populates -- see
    # order_manager.py's own direct, per-signal get_account_equity/
    # get_buying_power calls, which are unaffected by this and still
    # always live).
    account_summary_refresh_interval_seconds: float = 30.0
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
    # Exponential backoff base/ceiling for retrying a failed exit-order
    # submission -- see Position.exit_submission_failures' docstring for
    # the real incident (CYCU/SCKT, 2026-08-12) this fixes: with no
    # backoff at all, a stuck exit retried every single
    # poll_interval_seconds tick regardless of how many times it had
    # already failed, adding to the exact rate-limit contention blocking
    # it. Delay is exit_submission_backoff_base_seconds *
    # 2^(failures - 1), capped at exit_submission_backoff_max_seconds --
    # 5s, 10s, 20s, 40s, 60s(capped), 60s, ... with the defaults below.
    # Never gives up entirely (see that field's docstring for why an exit
    # must not use the same give-up-after-N pattern as broker bracket
    # attach) -- only ever slows down how often it's retried.
    exit_submission_backoff_base_seconds: float = 5.0
    exit_submission_backoff_max_seconds: float = 60.0
    # How long a MANAGING position can go with no broker-side protective
    # bracket (position.broker_stop_order_id still None) before TradingLoop
    # raises a RiskEventType.POSITION_UNPROTECTED_TOO_LONG event -- see
    # _manage_position's check and RiskEngine.record_operational_event.
    # _attach_broker_bracket/_sync_broker_protective_orders keep retrying
    # every tick regardless of this value (this is visibility, not a
    # circuit breaker -- see Position.broker_bracket_attach_failures'
    # deliberate absence from this codebase, referenced in several nearby
    # docstrings, for why a give-up-after-N mechanism was rejected here).
    # The gap this closes: ANY structurally broken order payload (a
    # future Webull API change, a new order type added without live
    # verification, an account-level restriction) would otherwise make
    # this retry loop fail forever with NOTHING surfaced anywhere a human
    # would actually see it -- the position just quietly rides on
    # software-only management for its whole lifetime. Not needed for
    # this codebase's current bracket/trailing-stop payload fields --
    # both confirmed working live (see _order_payload's stop_price
    # comment and _ORDER_TYPE_TO_WEBULL's TRAILING_STOP entry in
    # brokers/webull/client.py) -- this is defense-in-depth against a
    # FUTURE regression, not a currently-known problem.
    # 60s (12 ticks at the 5s poll_interval_seconds default) is long enough
    # that a single transient 429/rate-limit blip self-healing within a
    # few ticks (the normal case) never fires this, but short enough that
    # a genuinely stuck position is flagged within about a minute rather
    # than being discovered by accident hours later.
    unprotected_position_alert_seconds: float = 60.0
    # How long an exit order can sit in self._pending_exit_orders with
    # neither a terminal status (FILLED/REJECTED/CANCELED/EXPIRED) nor
    # being cleared, before _poll_pending_exit treats it as stuck: cancels
    # it, drops it from tracking, and raises a
    # RiskEventType.PENDING_EXIT_ORDER_STUCK event, letting the very next
    # tick's PositionManager.check_exit fire a completely fresh exit
    # attempt instead of polling the same never-resolving order forever.
    # Real incident (2026-08-13, sandbox): a software-managed exit order
    # for a position well past its stop-loss got submitted, entered
    # self._pending_exit_orders, and then simply never resolved -- not
    # filled, not rejected, not cancelled, not expired -- for many hours
    # straight. _manage_position's very first check
    # (`if pending is not None: self._poll_pending_exit(...); return`)
    # means this doesn't just fail to protect the position -- it
    # PERMANENTLY skips PositionManager.check_exit entirely for that
    # symbol from then on (check_exit is never even called again), and
    # _poll_pending_exit's own "still pending" branch was a bare
    # `# else: still pending` with NO logging at all, so this failure
    # mode was completely silent: no error, no warning, nothing in the
    # logs to find, while the position rode on, unprotected, indefinitely.
    # It ALSO silently defeated the dashboard's own manual "Close"
    # button -- _close_all_positions_now explicitly skips any symbol
    # already in self._pending_exit_orders (correctly so, for a order
    # that's genuinely still in flight -- but wrongly so for one that's
    # actually stuck forever). 180s (3 minutes -- well beyond the ~1s
    # this bot's own MARKET/marketable-LIMIT exit orders should
    # realistically take to fill under normal conditions) is long enough
    # to never fire on a legitimate brief delay, short enough that a
    # position can never again ride unprotected in this specific way for
    # more than a few minutes, let alone hours.
    pending_exit_stuck_timeout_seconds: float = 180.0
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
    # How long a subscribed symbol's stream can go completely quiet (no
    # pushed message at all, per _live_snapshots' own received_at
    # timestamp) before _reconcile_streaming_subscriptions proactively
    # unsubscribes and immediately resubscribes it -- an attempt at
    # recovering the "known-but-not-yet-understood reconnect issue" noted
    # in WebullBrokerClient.subscribe_quotes' docstring, where a single
    # symbol's subscription can apparently go silently dead without the
    # whole MQTT connection dropping. Deliberately looser than
    # streaming_staleness_seconds (10s, which only governs whether THIS
    # tick trusts a cached message enough to use it, falling back to REST
    # meanwhile) -- this is a much rarer, heavier-handed recovery action,
    # not the everyday fallback path, so it needs a longer quiet spell
    # before firing to avoid needlessly resubscribing a symbol that's
    # just genuinely trading quietly for a few seconds. Does NOT help a
    # symbol Webull never sends ANY data for in the first place (a
    # cold-start gap, not a died-mid-stream one) -- that case has no
    # "was receiving, then stopped" signal to key off; it's still only
    # covered by the REST fallback every tick already provides. Added
    # 2026-08-21 at the user's explicit request ("ensure the data is
    # live... for all candidates and open positions") -- detection/
    # alerting alone can't fix a genuinely broken feed, but this
    # suspected class of bug is worth an active recovery attempt, not
    # just a louder alert.
    stream_stale_resubscribe_seconds: float = 30.0
    # How long since THIS PROCESS last successfully cached a snapshot for
    # a tracked candidate (open position or pre-entry) -- i.e. age against
    # _last_known_snapshots' own received_at, NOT the cached snapshot's
    # quote_time -- before dashboard/app.py's /api/positions AND
    # /api/candidates should flag its displayed price as stale rather
    # than presenting it as live (2026-08-19, real incident: BTCT/BTOG
    # showed a frozen price that no longer matched the market).
    # get_last_known_price itself has no staleness check at all -- see
    # get_last_known_price_age_seconds' docstring, including its
    # 2026-08-21 correction, for why received_at (not quote_time) is the
    # right clock here -- this is deliberately looser than
    # streaming_staleness_seconds (10s), which governs whether THIS loop
    # trusts a streamed snapshot enough to use it for its own tick
    # processing; this value instead governs whether the DASHBOARD should
    # keep presenting an old cached number as if it's current.
    last_known_price_stale_after_seconds: float = 30.0
    # How long since THIS PROCESS last successfully cached a snapshot for
    # a tracked candidate (open position or pre-entry) can get before
    # _maybe_raise_stale_market_data_alert raises a
    # RiskEventType.MARKET_DATA_STALE event -- turning
    # last_known_price_stale_after_seconds' passive dashboard badge above
    # into an actual entry on the Risk Events panel a human is more
    # likely to notice. Real incident (2026-08-20, positions only): a
    # position's price feed died silently (both the stream and the REST
    # fallback in _process_candidate_inner failing every cycle) for
    # several minutes with nothing surfacing why; broadened (2026-08-21)
    # to cover pre-entry candidates too, since the same silent failure
    # mode existed there and was simply never surfaced. Deliberately the
    # same 60.0 default as unprotected_position_alert_seconds above (same
    # "long enough that an ordinary transient blip self-heals, short
    # enough that a genuinely dead feed gets flagged within about a
    # minute" reasoning) -- these are two independent config knobs, not
    # the same value reused, so tune them separately if experience says
    # otherwise.
    stale_market_data_alert_seconds: float = 60.0
    # Maximum number of symbols _reconcile_streaming_subscriptions will
    # keep actively subscribed to live streaming at once -- see that
    # method and brokers/webull/client.py's corrected
    # _STREAMING_SUBSCRIBE_BATCH_SIZE comment for the 2026-08-14 incident:
    # Webull's DataStreamingClient.subscribe() enforces a cumulative,
    # per-MQTT-session cap of 100 ACTIVE subscriptions (confirmed live --
    # a batch of just ~36 NEW symbols was still rejected wholesale with
    # TOO_MANY_SYMBOLS once the session's running total was already near
    # 100), not a per-call argument-count cap the way chunking alone
    # (already in place since 2026-08-13) assumed. Before this fix,
    # subscriptions only ever grew for the life of the process -- once
    # total tracked candidates passed the real cap, EVERY subscribe
    # attempt for any new symbol failed wholesale, forever, with the
    # affected symbols silently and permanently stuck on REST polling.
    # 90 rather than the full confirmed 100: headroom against the eager,
    # immediate _ensure_streaming_subscribed calls made outside this
    # method's own reconcile pass (_confirm_entry_filled/
    # reconcile_positions_from_broker's position-adoption path), which
    # could otherwise transiently push the session's real total a few
    # symbols past the last reconcile's computed budget before the next
    # reconcile tick evicts back down to it.
    streaming_subscription_budget: int = 90
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

    # -- entry-selectivity rework (2026-08-13, see docs/ARCHITECTURE.md) ------
    # Real motivation: the bot was losing more trades than it won, and the
    # user identified two concrete causes -- (1) a trigger fired off a
    # single snapshot, with no check the move actually held even a second,
    # and (2) when multiple candidates wanted a scarce position slot at
    # once, whichever triggered first in iteration order won it, not the
    # best one available.
    #
    # How long a CONFIRMING candidate must keep holding above its trigger
    # reference price before its signal is even considered for submission --
    # see enums.CandidateState.CONFIRMING's docstring for the single-tick-
    # noise problem this closes. 10s is a starting value, not backtested --
    # short enough that a genuine fast breakout on a low-float name isn't
    # badly chased, long enough to filter an instant reversal.
    confirmation_window_seconds: float = 10.0
    # How far price is allowed to pull back below the trigger reference
    # price during the confirmation window before it's treated as a
    # reversal (RiskEventType.CONFIRMATION_FAILED), not ordinary tick-to-
    # tick noise.
    confirmation_max_pullback_pct: float = 1.5
    # Extra time a candidate that ALREADY passed confirmation is allowed to
    # keep waiting for a position slot to open up (see
    # _submit_ranked_entries) before giving up and reverting to ARMED --
    # avoids holding a stale confirmed price open indefinitely if slots
    # never free up. Total time a candidate can spend CONFIRMING before
    # being given up on is therefore confirmation_window_seconds + this.
    confirmation_ready_max_wait_seconds: float = 60.0
    # Hard reject threshold for how much of the original trigger->resistance
    # runway the confirmed entry price has already consumed (see
    # metrics/volume_profile.py's compute_runway_consumed_pct/
    # resistance_runway_score) -- 0.40 means a confirmed entry that already
    # used up 40%+ of the room between the trigger and the next known
    # resistance level is rejected as too extended, independent of target
    # clearance (see _poll_confirmation). Starting value from the spec this
    # rework is based on, not backtested.
    max_runway_consumed_pct: float = 0.40

    # -- TICK-derived order flow (2026-08-14, see docs/ARCHITECTURE.md) -------
    # Real motivation: every existing confirmation-window check (price
    # reversal, MIS fading, spread widening) measures the SAME thing every
    # other component in this pipeline already sees -- price and volume
    # moving. None of them can tell a genuine buyer-driven breakout apart
    # from a volume spike caused by someone dumping into a thin book at a
    # price that briefly ticks up anyway. TICK's `side` field (aggressor
    # classification) is the one signal that can. A hard gate here mirrors
    # max_runway_consumed_pct above: a soft MIS-scoring signal
    # (order_flow_score in weights.yaml) coexists with a stricter,
    # independently-configured threshold for the stronger action of
    # actually blocking an entry outright.
    #
    # Imbalance ((buy-sell)/(buy+sell)) at or below this during CONFIRMING
    # fails the window, same failure family as reversed_past_tolerance/
    # mis_faded/spread_too_wide (RiskEventType.CONFIRMATION_FAILED) -- see
    # _poll_confirmation. -0.4 means net sell-side volume is at least 70%
    # of classified flow (buy=0.15, sell=0.85 -> imbalance=-0.70... at
    # exactly -0.4, a 30/70 buy/sell split). Starting value, not
    # backtested.
    order_flow_sell_pressure_threshold: float = -0.4
    # Minimum classified (BUY or SELL, never UNKNOWN) TICK prints required
    # in the trailing window before the sell-pressure gate above is even
    # evaluated -- deliberately a HIGHER bar than weights.yaml's
    # min_order_flow_sample_count (8), since blocking a trade outright is
    # a stronger action than nudging a ranking score, and CONFIRMING
    # candidates get subscribed to TICK data right when they trigger, so
    # a symbol with too few classified prints this early just hasn't had
    # enough real trading yet to trust a directional read. Below this,
    # candidate.latest_metrics.order_flow_imbalance_1m may still be a real
    # (if noisy) number, but _poll_confirmation ignores it entirely rather
    # than blocking an entry on a thin sample.
    order_flow_min_sample_count_for_gate: int = 10

    # -- atomic bracket entry retry (2026-08-13, see docs/ARCHITECTURE.md's
    # "Atomic bracket entry" section) -----------------------------------
    # How many additional attempts a rejected atomic bracket entry gets
    # before TradingLoop._submit_entry finally gives up (2 = 3 total
    # attempts: the original plus 2 retries). A rejection isn't always
    # permanent -- call_with_retry already retries Webull's own rate-limit
    # errors internally (see brokers/webull/retry.py), so a
    # BracketEntryRejected reaching this loop is either a genuine
    # structural rejection (an unsupported combo for this instrument --
    # retrying won't help, but 3 attempts is a small, bounded cost) or a
    # transient failure call_with_retry's own classification didn't catch
    # (a network blip, a momentary 5xx) -- worth a couple more tries
    # spaced roughly a tick apart. Each retry re-recomputes stop/target
    # from the then-current price (reuses _poll_confirmation's own
    # recompute-and-gate machinery -- see _submit_entry's
    # BracketEntryRejected handling) rather than blindly resubmitting the
    # exact same stale request. Explicit instruction still holds once
    # retries are exhausted: the trade does not go through, no fallback to
    # an unprotected entry.
    bracket_entry_max_retries: int = 2


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
    # CONFIRMING needs a live price every tick just as much as ARMED does --
    # see _poll_confirmation, which is what actually reads it.
    _STREAMING_ELIGIBLE_STATES = (
        CandidateState.WATCHING, CandidateState.HEATING_UP, CandidateState.ARMED, CandidateState.CONFIRMING,
        CandidateState.ENTERED, CandidateState.MANAGING,
    )

    # States eligible for _prune_stale_candidates to drop entirely -- see
    # TradingLoopConfig.candidate_stale_after_seconds' docstring. Everything
    # NOT in this tuple (ARMED, CONFIRMING, TRIGGERED, ENTERED, MANAGING) is
    # either actively working toward an entry or an open position, and must
    # never be pruned regardless of how long it's been sitting there.
    _PRUNABLE_STATES = (
        CandidateState.WATCHING, CandidateState.HEATING_UP, CandidateState.REJECTED, CandidateState.COOLDOWN,
    )

    # Priority tier for _reconcile_streaming_subscriptions -- lower wins a
    # scarce streaming slot first when demand exceeds
    # TradingLoopConfig.streaming_subscription_budget. An open position
    # (ENTERED/MANAGING) must never lose its live price, and a candidate
    # actively working toward an entry (ARMED/CONFIRMING, where poll
    # cadence directly affects confirmation-window accuracy) comes next --
    # both always outrank the long tail of WATCHING/HEATING_UP candidates,
    # for which streaming is a latency optimization on top of a REST-
    # polling fallback that's otherwise fully correct, just slower. Any
    # state not listed here (TRIGGERED, DISCOVERED, REJECTED, COOLDOWN)
    # never reaches this ranking at all -- see _STREAMING_ELIGIBLE_STATES.
    _STREAMING_PRIORITY_TIERS = {
        CandidateState.ENTERED: 0,
        CandidateState.MANAGING: 0,
        CandidateState.ARMED: 1,
        CandidateState.CONFIRMING: 1,
        CandidateState.WATCHING: 2,
        CandidateState.HEATING_UP: 2,
    }

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
        on_position_snapshot_upsert: Optional[Callable[[Position], None]] = None,
        on_position_snapshot_delete: Optional[Callable[[str], None]] = None,
        load_position_snapshot: Optional[Callable[[], list[Position]]] = None,
        momentum_event_tracker: Optional[MomentumEventTracker] = None,
        momentum_engine: Optional[MomentumQualificationEngine] = None,
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
        # Write-through persistence for the "currently open positions"
        # snapshot (db/models.py's PositionRecord, 2026-08-20
        # restart-blind-spot fix) -- upsert called whenever self._positions
        # gains a symbol or that symbol's quantity changes, delete called
        # whenever a symbol leaves self._positions. NOT called on plain
        # per-tick price/MFE updates -- see PositionRecord's docstring for
        # why this stays off the hot tick path. load_position_snapshot is
        # read exactly once, at reconcile_positions_from_broker's very
        # first call, to recover visibility into a position that closed at
        # the broker WHILE this process was down for a restart -- see that
        # method's docstring for the incident this closes (BTCT/BTOG,
        # 2026-08-19/20: two positions closed at the broker during a
        # restart with zero trace -- no warning, no Trade record -- because
        # self._positions starts empty on every restart by design).
        self.on_position_snapshot_upsert = on_position_snapshot_upsert
        self.on_position_snapshot_delete = on_position_snapshot_delete
        self._load_position_snapshot = load_position_snapshot
        # Optional collaborator (not a callback) since momentum-event
        # tracking needs ongoing state across many ticks (filling forward-
        # looking outcome windows over up to 15 minutes) -- see
        # collection/event_recorder.py.
        self.momentum_event_tracker = momentum_event_tracker
        # Real-Time Momentum Qualification Layer (2026-08-17, see
        # scanner/momentum_qualification.py) -- the Tier 2.5 gate between
        # trigger_engine's strategy match and CONFIRMING. None (the
        # default) falls back to a fresh default-config engine rather than
        # skipping the gate entirely -- there is no "off" switch by design,
        # matching every other stage of this pipeline (MIS, resistance
        # checks, ...), none of which are optional either.
        self.momentum_engine = momentum_engine or MomentumQualificationEngine()

        self.candidates: dict[str, Candidate] = {}
        # Guards structural access (insert/copy) to self.candidates only --
        # see this module's docstring's "Concurrency model" section. Held
        # only briefly, never across a network call or a full processing pass.
        self._candidates_lock = threading.Lock()
        self._entry_signals: dict[str, Signal] = {}       # symbol -> signal that triggered a pending entry
        self._pending_entry_orders: dict[str, Order] = {}  # symbol -> submitted-but-not-yet-filled entry order
        # symbol -> the stop/target Order objects an atomic bracket entry
        # (OrderManager.submit_entry_signal) already placed alongside this
        # pending entry, if any -- see enums.RiskEventType
        # .BRACKET_ENTRY_REJECTED's docstring and _confirm_entry_filled's
        # docstring for why this needs to survive until the entry leg's
        # fill is confirmed (only then does _confirm_entry_filled have a
        # real Position to attach broker_stop_order_id/broker_target_order_id
        # to). Popped alongside _entry_signals at every one of that dict's
        # own pop sites; entries never present in this dict at all (as
        # opposed to a real (None, None) result) fall back to
        # _attach_broker_bracket exactly as before this feature existed.
        self._pending_entry_brackets: dict[str, BracketSubmissionResult] = {}
        # symbol -> number of BracketEntryRejected retries already spent
        # this arm cycle (see TradingLoopConfig.bracket_entry_max_retries
        # and _submit_entry's handling). Reset to 0/removed whenever an
        # entry actually fills (_confirm_entry_filled) or a genuinely
        # fresh trigger starts a new confirmation window
        # (_start_confirmation) -- a brand new trigger must never inherit
        # a stale retry count left over from an earlier, unrelated
        # rejection episode.
        self._bracket_entry_retry_counts: dict[str, int] = {}
        # symbol -> PendingConfirmation for a candidate currently CONFIRMING
        # (see enums.CandidateState.CONFIRMING's docstring). Populated by
        # _start_confirmation, consumed by _poll_confirmation/
        # _submit_ranked_entries.
        self._pending_confirmations: dict[str, PendingConfirmation] = {}
        # Candidates that finished their confirmation window and cleared
        # target-clearance/runway this tick -- reset at the start of every
        # _process_all_candidates pass and consumed once, at the end of
        # that same pass, by _submit_ranked_entries. See that method's
        # docstring for why ranking happens once per tick across every
        # ready candidate instead of submitting each one the instant it
        # individually clears, and PendingConfirmation's docstring for why
        # a candidate that doesn't get a slot this tick isn't lost -- it's
        # simply re-added here again next tick by _poll_confirmation.
        self._ready_to_enter: list[Candidate] = []
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
        # symbol -> Position reconstructed from the persisted open-positions
        # snapshot (db/repository.get_open_positions_snapshot), for a symbol
        # not yet in self._positions this process's own lifetime. Populated
        # exactly once, at reconcile_positions_from_broker's very first
        # call (see that method), and ONLY used to extend the
        # missing-from-broker comparison there -- deliberately never merged
        # directly into self._positions, which would make the adoption loop
        # further down wrongly skip bracket-attach/streaming-resubscribe
        # for a symbol that turns out to still be genuinely open at the
        # broker. Entries are removed the moment they're resolved either
        # way: found again at the broker (falls through to normal
        # adoption) or confirmed closed (a Trade gets built and recorded).
        self._recovered_snapshot_positions: dict[str, Position] = {}
        # Guards the one-time load above -- deliberately NOT keyed off
        # self._last_position_reconcile, which _process_all_candidates
        # already sets to `now` (see that method) BEFORE calling
        # reconcile_positions_from_broker, so it's never None from inside
        # that method's own body even on its first-ever call.
        self._position_snapshot_load_attempted = False
        # symbol -> consecutive reconcile_positions_from_broker passes it's
        # been absent from broker.get_positions() -- see
        # TradingLoopConfig.position_missing_confirmations_required's
        # docstring for the incident this guards against (a single
        # degraded/rate-limited-adjacent poll abandoning a still-open
        # position). Only ever holds entries for symbols currently in
        # self._positions; cleared the moment a symbol reappears in a
        # broker response or is actually declared closed.
        self._missing_from_broker_counts: dict[str, int] = {}
        # symbol -> (most recent MarketSnapshot _process_candidate_inner
        # saw for this symbol, the wall-clock `now` at which THIS PROCESS
        # cached it) -- see get_last_known_price's docstring. The second
        # element is deliberately this process's own receipt/fetch time,
        # NOT snapshot.timestamp (Webull's reported quote_time) -- same
        # "received_at, not the payload's own timestamp" idea as
        # _live_snapshots below, and for the same reason: quote_time only
        # advances when the symbol actually prints a new trade/quote, so a
        # genuinely quiet-but-still-being-fetched-every-tick WATCHING/
        # HEATING_UP candidate would look falsely "stale" for tens of
        # seconds at a stretch if staleness were measured against it
        # instead (real incident, 2026-08-21: nearly every non-actively-
        # trading candidate on the dashboard showed the stale-price
        # warning, even though _process_all_candidates was successfully
        # refreshing every one of them on every single tick -- the metric
        # was measuring "how long since this symbol last traded," not
        # "how long since this process last fetched it"). Deliberately
        # never cleared when a position closes/a candidate stops being
        # tracked (a harmless, small, bounded-by-universe-size dict entry
        # left behind -- simpler than adding cleanup for a value nothing
        # reads once its symbol is gone).
        self._last_known_snapshots: dict[str, tuple[MarketSnapshot, datetime]] = {}
        # symbol -> (cumulative_pv, last_seen_cumulative_volume, last_seen_price),
        # this process's own running approximation of REAL session VWAP --
        # see _update_session_vwap's docstring for the 2026-08-14 ONFO
        # incident this fixes and the boundary-price approximation used to
        # keep accumulating it tick to tick. Deliberately never cleared for
        # a symbol that stops being tracked, same harmless/bounded-by-
        # universe-size tradeoff as _last_known_snapshots just above.
        self._vwap_state: dict[str, tuple[float, float, float]] = {}
        self._last_universe_scan: Optional[datetime] = None
        self._last_position_reconcile: Optional[datetime] = None
        self._last_account_summary_refresh: Optional[datetime] = None
        # See get_account_summary's docstring -- refreshed periodically by
        # _process_all_candidates (account_summary_refresh_interval_seconds),
        # never by a dashboard request itself.
        self._cached_equity: Optional[float] = None
        self._cached_buying_power: Optional[float] = None
        self._cached_account_summary_error: Optional[str] = None
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
        # Symbols CURRENTLY subscribed for live streaming (not, since
        # 2026-08-14, "ever requested this process's lifetime" -- see
        # _reconcile_streaming_subscriptions, which now removes a symbol
        # from here via broker.unsubscribe_quotes when it's pruned
        # (_prune_stale_candidates) or evicted to make room for a
        # higher-priority one under streaming_subscription_budget).
        # _ensure_streaming_subscribed still only subscribes a symbol not
        # already in this set, so a symbol dropped here is a genuine future
        # re-subscribe candidate, not a permanent no-op.
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

    def _prune_stale_candidates(self, now: datetime) -> None:
        """Drops candidates in a _PRUNABLE_STATES state that have gone
        candidate_stale_after_seconds with no state transition at all --
        see that config field's docstring for the 2026-08-14 incident this
        fixes (self.candidates growing unboundedly for the life of the
        process). Runs on the main thread from _process_all_candidates,
        same as every other structural self.candidates mutation site
        (_rescan_universe's insert) -- deletion is guarded by the same
        _candidates_lock for the same reason: the rescan thread reads/
        inserts into this dict concurrently.

        Best-effort cleanup only, mirroring _ensure_streaming_subscribed's
        own fallback contract: a symbol dropped here also has its streaming
        subscription released (freeing a slot in
        _reconcile_streaming_subscriptions' cap-limited budget for a
        genuinely active candidate to use instead) and its bookkeeping
        cleared, but a failure to unsubscribe it at the broker is only
        logged, never allowed to block the candidate drop itself -- an
        orphaned subscription self-corrects the next time
        _reconcile_streaming_subscriptions runs short on budget and evicts
        it anyway."""
        with self._candidates_lock:
            stale_symbols = [
                symbol for symbol, candidate in self.candidates.items()
                if candidate.state in self._PRUNABLE_STATES
                and now - candidate.last_updated_at >= timedelta(seconds=self.config.candidate_stale_after_seconds)
            ]
            for symbol in stale_symbols:
                del self.candidates[symbol]
        if not stale_symbols:
            return
        logger.info(
            "Pruned %d stale candidate(s) with no activity for over %.0fs: %s",
            len(stale_symbols), self.config.candidate_stale_after_seconds, stale_symbols,
        )
        for symbol in stale_symbols:
            self._streaming_requested_symbols.discard(symbol)
            with self._live_snapshots_lock:
                self._live_snapshots.pop(symbol, None)
        try:
            self.broker.unsubscribe_quotes(stale_symbols)
        except Exception:
            logger.warning(
                "unsubscribe_quotes failed for pruned candidates %s; harmless -- an orphaned "
                "subscription self-corrects the next time the streaming budget runs short.",
                stale_symbols, exc_info=True,
            )

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
        _reconcile_streaming_subscriptions once per tick with the
        budget-ranked add-list it computed (both an eventual first attempt
        for a watch-stage candidate and a RETRY of the eager attempt above
        if it failed -- see the "retried automatically" paragraph below).

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

        Only ever subscribes symbols not already in
        self._streaming_requested_symbols -- callers that want a bounded
        total subscription count (i.e. every caller except the two eager
        adoption call sites) go through _reconcile_streaming_subscriptions
        instead, which computes the add-list this method receives AND
        handles evicting (broker.unsubscribe_quotes) whatever fell out of
        the top streaming_subscription_budget first. This method itself
        stays a pure "add these, best-effort" primitive with no eviction
        logic of its own -- see TradingLoopConfig.streaming_subscription_budget's
        docstring for the 2026-08-14 incident that made eviction necessary
        at all (subscriptions used to only ever grow for the life of the
        process, one entry per symbol BroadScanner had ever surfaced,
        which silently overflowed Webull's real cumulative-per-session
        cap once total tracked candidates passed it)."""
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

    def _reconcile_streaming_subscriptions(self, candidates: list[Candidate], now: datetime) -> None:
        """Keeps the broker's live-streamed subscription set within
        TradingLoopConfig.streaming_subscription_budget, evicting the
        lowest-priority currently-subscribed symbols (broker.unsubscribe_quotes)
        before subscribing new, higher-priority ones -- see that config
        field's docstring and brokers/webull/client.py's corrected
        _STREAMING_SUBSCRIBE_BATCH_SIZE comment for the 2026-08-14 incident
        this replaces (streaming subscriptions only ever grew, with no cap
        enforcement at all, silently overflowing Webull's real cumulative-
        per-session limit once total tracked candidates passed it).

        Ranking: see _STREAMING_PRIORITY_TIERS' docstring for the tier
        order. Within a tier, the most recently updated candidates keep
        their slot over stale ones -- an approximation of "hottest first"
        using data already on hand (Candidate.last_updated_at) rather than
        adding a new ranking metric just for this.

        A no-op in steady state once the desired top-N set stops changing
        tick to tick: only the diff (symbols newly in the top N, symbols
        that fell out of it) results in any real subscribe/unsubscribe
        call, exactly like _ensure_streaming_subscribed's own existing
        membership-check no-op for symbols already subscribed.

        Also recovers a stream that's gone silently dead (2026-08-21, see
        TradingLoopConfig.stream_stale_resubscribe_seconds' docstring):
        any symbol we're keeping subscribed anyway (still in `desired`,
        not part of this tick's ordinary budget eviction) whose last
        streamed message is older than that threshold gets force-
        unsubscribed and immediately resubscribed, an active recovery
        attempt for the suspected per-symbol reconnect bug rather than
        just waiting on the REST fallback forever."""
        eligible = [c for c in candidates if c.state in self._STREAMING_ELIGIBLE_STATES]
        if not eligible:
            return
        ranked = sorted(
            eligible,
            key=lambda c: (self._STREAMING_PRIORITY_TIERS.get(c.state, 9), -c.last_updated_at.timestamp()),
        )
        desired = {c.symbol for c in ranked[: self.config.streaming_subscription_budget]}
        currently_subscribed = set(self._streaming_requested_symbols)
        to_drop = currently_subscribed - desired
        to_add = [symbol for symbol in (c.symbol for c in ranked) if symbol in desired and symbol not in currently_subscribed]

        if to_drop:
            dropped_sorted = sorted(to_drop)
            try:
                self.broker.unsubscribe_quotes(dropped_sorted)
            except Exception:
                logger.warning(
                    "unsubscribe_quotes failed while making room in the streaming budget for %s; "
                    "those symbols stay counted as subscribed for now and will be retried next tick.",
                    dropped_sorted, exc_info=True,
                )
            else:
                self._streaming_requested_symbols -= to_drop
                with self._live_snapshots_lock:
                    for symbol in to_drop:
                        self._live_snapshots.pop(symbol, None)

        still_subscribed = (desired & currently_subscribed) - to_drop
        stale_streams = []
        with self._live_snapshots_lock:
            for symbol in still_subscribed:
                entry = self._live_snapshots.get(symbol)
                if entry is None:
                    continue  # never received anything at all -- a cold-start gap, not this fix's job
                _, received_at = entry
                if now - received_at > timedelta(seconds=self.config.stream_stale_resubscribe_seconds):
                    stale_streams.append(symbol)
        if stale_streams:
            stale_streams_sorted = sorted(stale_streams)
            logger.info(
                "Resubscribing %d symbol(s) whose stream has gone quiet for over %.0fs: %s",
                len(stale_streams_sorted), self.config.stream_stale_resubscribe_seconds, stale_streams_sorted,
            )
            try:
                self.broker.unsubscribe_quotes(stale_streams_sorted)
            except Exception:
                logger.warning(
                    "unsubscribe_quotes failed while resubscribing stale streams for %s; will retry next tick.",
                    stale_streams_sorted, exc_info=True,
                )
            else:
                self._streaming_requested_symbols -= set(stale_streams_sorted)
                with self._live_snapshots_lock:
                    for symbol in stale_streams_sorted:
                        self._live_snapshots.pop(symbol, None)
                for symbol in stale_streams_sorted:
                    if symbol not in to_add:
                        to_add.append(symbol)

        if to_add:
            self._ensure_streaming_subscribed(to_add)

    # -- per-candidate processing ---------------------------------------------

    def _update_session_vwap(self, candidate: Candidate, snapshot: MarketSnapshot) -> None:
        """Overwrites `snapshot.vwap` IN PLACE with a real, running,
        whole-regular-session VWAP -- see metrics/session_vwap.py's module
        docstring for the full 2026-08-14 ONFO incident this fixes:
        WebullBrokerClient's live snapshot paths (REST and streaming both)
        hardcode `vwap = last_price` because Webull's own snapshot/
        streaming APIs don't return VWAP at all, which silently made
        `distance_from_vwap_pct` exactly 0.0 on every live tick --
        breaking scoring/momentum_ignition_score.py's trend_quality_score
        (a no-signal constant, removed 2026-08-20 -- see
        scoring/weights.yaml's v2.7 changelog), strategy/vwap_reclaim.py and
        strategy/ignition_pullback.py (structurally unable to ever trigger
        live), and position/position_manager.py's exit_on_vwap_failure
        safety backstop (structurally unable to ever fire), all at once.

        Called from _process_candidate_inner for EVERY tracked
        state -- not just pre-entry ones -- specifically so this reaches
        BOTH CandidateWatcher.update() (MIS scoring, vwap_reclaim/
        ignition_pullback's trigger checks) AND _manage_position's
        PositionManager.check_exit call (the VWAP-failure backstop) from
        one place, since those two are otherwise completely separate code
        paths that never both see the same fix applied once.

        Maintains a running (cumulative_pv, last_seen_cumulative_volume,
        last_seen_price) tuple per symbol in self._vwap_state, seeded ONCE
        per symbol from candidate.vwap_anchor_pv/vwap_anchor_volume
        (BroadScanner's discovery-time bars-derived starting point -- see
        metrics/session_vwap.py) or from scratch (0, 0) if no anchor
        exists (e.g. a position TradingLoop adopted via
        reconcile_positions_from_broker rather than discovering through
        the normal BroadScanner pipeline -- same "start cold, self-heals
        as real ticks accumulate" tolerance seed_history_from_bars already
        applies elsewhere in this codebase).

        Every tick AFTER the first for a symbol, the volume that traded
        since the last tick (snapshot.cumulative_volume, Webull's own
        real, trustworthy running total -- unlike vwap, this field is NOT
        faked) is priced at the AVERAGE of the previous and current tick's
        price, the same boundary-price approximation
        metrics.calculations.dollar_volume_from_avg_price already relies
        on elsewhere in this codebase: a snapshot only tells us this
        tick's price and the cumulative volume since the last one, never
        the true distribution of trades within that gap. A no-op (leaves
        snapshot.vwap untouched, i.e. still last_price) only when
        cumulative volume is 0 or unavailable -- nothing to divide by yet."""
        symbol = candidate.symbol
        state = self._vwap_state.get(symbol)
        if state is None:
            cumulative_pv = candidate.vwap_anchor_pv or 0.0
        else:
            cumulative_pv, last_volume, last_price = state
            volume_delta = max(0.0, snapshot.cumulative_volume - last_volume)
            if volume_delta > 0:
                avg_price = (last_price + snapshot.last_price) / 2.0
                cumulative_pv += avg_price * volume_delta
        self._vwap_state[symbol] = (cumulative_pv, snapshot.cumulative_volume, snapshot.last_price)
        if snapshot.cumulative_volume > 0:
            snapshot.vwap = cumulative_pv / snapshot.cumulative_volume

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
                # Real-Time Momentum Qualification Layer (2026-08-17): the
                # other genuine "starting fresh" seam (see
                # MomentumState's docstring for the other one,
                # ARMED->HEATING_UP in candidate_watcher.py) -- a candidate
                # coming out of cooldown must not inherit stale impulse/
                # pullback/phase state from a completely different,
                # already-closed-out momentum episode.
                candidate.momentum = MomentumState()
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
                if candidate.state in self._STREAMING_ELIGIBLE_STATES:
                    self._maybe_raise_stale_market_data_alert(candidate, now)
                return

        try:
            self._update_session_vwap(candidate, snapshot)
        except Exception:
            logger.exception("_update_session_vwap failed for %s this cycle; snapshot.vwap left as-is.", candidate.symbol)

        if candidate.state in self._STREAMING_ELIGIBLE_STATES:
            # Cache this tick's (VWAP-corrected) price for the dashboard's
            # /api/positions and /api/candidates to read (see
            # get_last_known_price) -- covers both open positions and
            # pre-entry candidates now (2026-08-21, broadened from
            # positions-only at the user's explicit request) since a dead
            # feed is exactly as invisible pre-entry as it is post-entry.
            # Also clears any prior dead-feed episode -- this tick just
            # proved the feed is live again -- so a LATER stretch without
            # live data raises its own fresh alert instead of staying
            # suppressed.
            self._last_known_snapshots[candidate.symbol] = (snapshot, now)
            candidate.market_data_stale_alert_logged = False

        if self.momentum_event_tracker is not None:
            try:
                self.momentum_event_tracker.on_snapshot(candidate.symbol, snapshot)
            except Exception:
                logger.exception("momentum_event_tracker.on_snapshot failed for %s.", candidate.symbol)

        if candidate.state == CandidateState.CONFIRMING:
            self._poll_confirmation(candidate, snapshot, now)
            return

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

        if candidate.state == CandidateState.ARMED:
            # Real-Time Momentum Qualification Layer (2026-08-17, see
            # scanner/momentum_qualification.py): runs every ARMED tick,
            # not just when a strategy fires -- this is what lets
            # phase/RTMS/impulse-pullback tracking stay current between a
            # strategy's own re-fires, which aren't guaranteed every tick.
            self.momentum_engine.on_snapshot(candidate, signal, snapshot, now)

        if signal is None:
            return

        # trigger_engine.on_snapshot is a pure "which strategy matches"
        # function now (2026-08-17) -- it no longer transitions the
        # candidate to CONFIRMING itself. That decision belongs to the
        # momentum-qualification gate below: a signal firing below the
        # momentum regime, or during an unhealthy pullback, must leave the
        # candidate ARMED, not move it to CONFIRMING and immediately fail
        # it. Every fired signal is logged via _register_momentum_event
        # regardless of outcome, satisfying the "log every trigger, entered
        # or not" requirement.
        decision = self.momentum_engine.evaluate_trigger(candidate, signal, snapshot, now)
        momentum_event = self._register_momentum_event(candidate, signal, now, decision=decision)
        if decision.outcome != "start_confirmation":
            return
        self._start_confirmation(candidate, signal, snapshot, now, momentum_event=momentum_event)

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

    def _notify_position_snapshot_upsert(self, position: Position) -> None:
        if self.on_position_snapshot_upsert is None:
            return
        try:
            self.on_position_snapshot_upsert(position)
        except Exception:
            logger.exception("on_position_snapshot_upsert callback raised for %s.", position.symbol)

    def _notify_position_snapshot_delete(self, symbol: str) -> None:
        if self.on_position_snapshot_delete is None:
            return
        try:
            self.on_position_snapshot_delete(symbol)
        except Exception:
            logger.exception("on_position_snapshot_delete callback raised for %s.", symbol)

    def _register_momentum_event(
        self, candidate: Candidate, signal: Signal, now: datetime,
        decision: Optional[TriggerDecision] = None,
    ) -> Optional[MomentumEvent]:
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
            momentum_qualification_at_event=self._momentum_qualification_snapshot(candidate, decision),
        )
        try:
            self.momentum_event_tracker.register(event)
        except Exception:
            logger.exception("momentum_event_tracker.register failed for %s.", candidate.symbol)
            return None
        return event

    def _momentum_qualification_snapshot(
        self, candidate: Candidate, decision: Optional[TriggerDecision],
    ) -> dict:
        """Real-Time Momentum Qualification Layer (2026-08-17): dump of
        candidate.momentum (phase, RTMS + components, impulse/pullback
        state, active_strategy_name, structure_intact) plus this specific
        trigger's accept/reject decision, if any -- stashed onto
        MomentumEvent.momentum_qualification_at_event so every logged
        trigger (entered or not) carries full qualification context for
        later offline analysis. `confirmation_price`/`actual_entry_price`
        are added later, in place, by _poll_confirmation's success path and
        _submit_entry respectively -- see MomentumEvent's docstring."""
        data = asdict(candidate.momentum)
        for key, value in list(data.items()):
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        data["phase"] = candidate.momentum.phase.value
        if decision is not None:
            data["decision_outcome"] = decision.outcome
            data["decision_reason"] = decision.reason
        return data

    def _notify_order_update(self, order: Order) -> None:
        if self.on_order_update is not None:
            try:
                self.on_order_update(order)
            except Exception:
                logger.exception("on_order_update callback raised for order %s.", order.client_order_id)

    def _start_confirmation(
        self, candidate: Candidate, signal: Signal, snapshot: MarketSnapshot, now: datetime,
        momentum_event: Optional[MomentumEvent] = None,
    ) -> None:
        """Moves `candidate` to CONFIRMING and records what _poll_confirmation
        needs to evaluate the window on later ticks. Does NOT submit an
        order; see enums.CandidateState.CONFIRMING's docstring for why.

        (2026-08-17) Previously trigger_engine.on_snapshot performed the
        ARMED->CONFIRMING transition itself as a side effect before this was
        ever called. It's a pure function now (see that module's docstring)
        -- the momentum-qualification gate (momentum_engine.evaluate_trigger,
        called in _process_candidate_inner) decides whether a fired signal
        ever reaches this method at all, so the transition happens here,
        the sole remaining place ARMED->CONFIRMING actually occurs."""
        transition(candidate, CandidateState.CONFIRMING, now=now, reason=f"{signal.strategy_name} triggered, momentum qualified")
        # A genuinely fresh trigger (this is only ever called from
        # trigger_engine's real trigger path in _process_candidate_inner,
        # never from a bracket-entry retry -- see that handling in
        # _submit_entry) must not inherit a stale retry count left over
        # from an earlier, unrelated BracketEntryRejected episode.
        self._bracket_entry_retry_counts.pop(candidate.symbol, None)
        candidate.confirmation_started_at = now
        candidate.confirmation_expires_at = now + timedelta(seconds=self.config.confirmation_window_seconds)
        candidate.entry_block_reason = None
        self._pending_confirmations[candidate.symbol] = PendingConfirmation(
            signal=signal, momentum_event=momentum_event, started_at=now,
            reference_price=signal.reference_price, snapshot=snapshot,
        )

    def _poll_confirmation(self, candidate: Candidate, snapshot: MarketSnapshot, now: datetime) -> None:
        """Runs every tick while `candidate` is CONFIRMING -- see
        enums.CandidateState.CONFIRMING's docstring for why this state
        exists at all. Keeps candidate.latest_metrics/latest_score fresh via
        watcher.update (needed for the MIS-still-qualifies check below);
        this can never move candidate.state out from under this method,
        since CandidateWatcher.update()'s transition logic has no branch
        that matches CONFIRMING.

        Three ways this ends (rtms-v3, 2026-08-19: simplified from four --
        see scanner/momentum_qualification.py's evaluate_trigger docstring
        for the incident writeup; candidate.momentum.phase is still
        updated every tick below for dashboard display/ranking, but no
        longer changes which of these branches this method takes):
        1. FAILS -- price reversed past confirmation_max_pullback_pct,
           (2026-08-14) TICK-derived order flow shows net sell-side
           pressure past order_flow_sell_pressure_threshold with enough
           classified volume to trust it, or the 5m momentum regime itself
           failed -- RiskEventType.CONFIRMATION_FAILED, revert to ARMED,
           must re-trigger fresh (not resume this same clock).
           (2026-08-19: a momentary MIS dip or spread widen during the
           window no longer cancels here -- see the note further down
           where those checks used to live.)
        2. Window elapses clean (nothing above failed) -- recompute stop/
           target from the ACTUAL current price, not signal.reference_price
           (which by now is confirmation_window_seconds stale), preserving
           the ORIGINAL strategy's risk/reward shape rather than
           recomputing generically. Then run the target-clearance/
           resistance-runway hard gates; a failure here reverts to ARMED
           with RiskEventType.RESISTANCE_BEFORE_TARGET. Passing queues the
           candidate onto self._ready_to_enter for this tick's batch-
           ranking pass (_submit_ranked_entries) instead of submitting
           immediately -- see that method for why.
        3. Already cleared confirmation on an earlier tick but never won a
           slot -- keeps re-queuing onto self._ready_to_enter (recomputed
           fresh off the current price every tick) until either it wins a
           slot or confirmation_ready_max_wait_seconds' extra grace period
           runs out, at which point it gives up and reverts to ARMED.
        """
        pending = self._pending_confirmations.get(candidate.symbol)
        if pending is None:
            # Shouldn't happen, but don't get stuck in CONFIRMING forever --
            # same safety-net pattern as _poll_pending_entry.
            transition(candidate, CandidateState.ARMED, now=now, reason="no pending confirmation found for CONFIRMING candidate")
            return
        pending.snapshot = snapshot

        self.watcher.update(candidate, snapshot)
        # Real-Time Momentum Qualification Layer (2026-08-17): keep
        # phase/RTMS/impulse-pullback tracking current while CONFIRMING
        # too, not just while ARMED -- a pullback that starts
        # mid-confirmation needs the same tracking one starting
        # pre-trigger would get (see scanner/momentum_qualification.py).
        self.momentum_engine.on_snapshot(candidate, pending.signal, snapshot, now)
        self._notify_score(candidate)

        elapsed_seconds = (now - pending.started_at).total_seconds()
        total_timeout = self.config.confirmation_window_seconds + self.config.confirmation_ready_max_wait_seconds

        def _timing_str() -> str:
            # Distinguish "still inside the base confirmation window" from
            # "already cleared it and is now waiting for a slot" (see this
            # method's own docstring, case 3) -- these checks run every
            # tick across the WHOLE total_timeout span (window + wait), not
            # just the first confirmation_window_seconds, so elapsed_seconds
            # routinely exceeds the window on a candidate that's simply
            # queued for a slot. Always phrasing it as "Ns into a Ms
            # window" when N > M read as if the window itself had somehow
            # run over, which it never does -- this makes the wait-phase
            # case explicit instead.
            if elapsed_seconds <= self.config.confirmation_window_seconds:
                return f"{elapsed_seconds:.0f}s into a {self.config.confirmation_window_seconds:.0f}s window"
            wait_elapsed = elapsed_seconds - self.config.confirmation_window_seconds
            return (
                f"cleared the {self.config.confirmation_window_seconds:.0f}s window, "
                f"{wait_elapsed:.0f}s into waiting for a position slot"
            )

        def _cancel(failure: str, event_type: RiskEventType = RiskEventType.CONFIRMATION_FAILED) -> None:
            reason = f"{candidate.symbol} failed confirmation ({failure}, {_timing_str()})"
            candidate.entry_block_reason = reason
            self._pending_confirmations.pop(candidate.symbol, None)
            self.risk_engine.record_operational_event(event_type, candidate.symbol, reason, now)
            transition(candidate, CandidateState.ARMED, now=now, reason=reason)

        reversed_past_tolerance = snapshot.last_price < pending.reference_price * (
            1 - self.config.confirmation_max_pullback_pct / 100.0
        )
        # rtms-v3-follow-up (2026-08-19, real incidents: BTTC cancelled 9s
        # into its 10s window on "spread widened"; BTOG cancelled right at
        # the 10s mark on "MIS faded" -- both otherwise-good setups killed
        # by a momentary dip in a signal that isn't the move itself). Per
        # explicit user decision, a MIS dip below armed_score_threshold or
        # a spread widening past max_spread_pct during the confirmation
        # window are no longer hard fails here -- removed, following the
        # same reasoning as rtms-v3's regime-only entry gate above. The
        # checks that stayed (order flow, regime, and the price-reversal
        # check below) all measure something about the move ITSELF; MIS
        # and spread are secondary signals that can move around during a
        # short window without the breakout having stopped being real.
        # TICK-derived order flow (2026-08-14, see
        # TradingLoopConfig.order_flow_sell_pressure_threshold's
        # docstring): net sell-side volume during the confirmation window
        # is a real reason to distrust the trigger even when price hasn't
        # reversed and MIS/spread still look fine -- those three checks
        # above all measure price/volume moving, never which side is
        # driving it. Requires order_flow_min_sample_count_for_gate
        # classified prints before trusting the ratio enough to block an
        # entry on it; below that, order_flow_imbalance_1m may still be a
        # real number but is ignored here as too thin a sample.
        sell_pressure_detected = (
            candidate.latest_metrics is not None
            and candidate.latest_metrics.order_flow_imbalance_1m is not None
            and candidate.latest_metrics.order_flow_sample_count_1m >= self.config.order_flow_min_sample_count_for_gate
            and candidate.latest_metrics.order_flow_imbalance_1m <= self.config.order_flow_sell_pressure_threshold
        )
        # Real-Time Momentum Qualification Layer: the 5m momentum regime
        # itself materially failing during confirmation is just as hard a
        # failure as the four checks above -- no amount of "still
        # technically confirming" matters once the underlying premise
        # (this is a real momentum runner) no longer holds.
        regime_failed = (
            candidate.latest_metrics is not None and candidate.latest_metrics.return_5m is not None
            and candidate.latest_metrics.return_5m < self.momentum_engine.config.thresholds["min_return_5m_pct"]
        )

        # These two are ALWAYS hard failures -- never excused by a
        # pullback classification (see the spec's own "structural failure"
        # list: sustained sell pressure or the regime itself failing both
        # remain hard fails regardless of what phase tracking currently
        # reads).
        if sell_pressure_detected or regime_failed:
            failure = "5m momentum regime failed" if regime_failed else "sell-side order flow"
            _cancel(failure)
            return

        # rtms-v3 (2026-08-19): a raw price reversal past
        # confirmation_max_pullback_pct now hard-cancels unconditionally --
        # the momentum engine's phase classification (PULLING_BACK etc.)
        # is display/ranking-only now, not an excuse to keep waiting
        # through a real reversal. See scanner/momentum_qualification.py's
        # evaluate_trigger docstring for the incident writeup.
        if reversed_past_tolerance:
            _cancel("price reversed")
            return

        # Cosmetic only (rtms-v3): still surface "why is this waiting"
        # text on the dashboard while PULLING_BACK, purely informational --
        # doesn't affect whether this method cancels, waits, or succeeds.
        if candidate.momentum.phase == MomentumPhase.PULLING_BACK:
            candidate.entry_block_reason = (
                f"{candidate.symbol}: pulling back during confirmation "
                f"(retracement {candidate.momentum.current_retracement_pct:.1f}%), structure intact -- "
                "waiting for reacceleration, not cancelled"
            )

        if elapsed_seconds > total_timeout:
            reason = (
                f"{candidate.symbol} confirmed but never won a position slot within {total_timeout:.0f}s -- "
                "giving up and reverting to ARMED"
            )
            candidate.entry_block_reason = reason
            self._pending_confirmations.pop(candidate.symbol, None)
            transition(candidate, CandidateState.ARMED, now=now, reason=reason)
            return

        if elapsed_seconds < self.config.confirmation_window_seconds:
            # Still inside the base window with nothing having failed yet.
            return

        original = pending.signal
        confirmed_entry = snapshot.last_price
        stop_pct = (
            (original.reference_price - original.suggested_stop) / original.reference_price
            if original.suggested_stop else None
        )
        target_pct = (
            (original.suggested_target - original.reference_price) / original.reference_price
            if original.suggested_target else None
        )
        confirmed_stop = confirmed_entry * (1 - stop_pct) if stop_pct is not None else original.suggested_stop
        confirmed_target = confirmed_entry * (1 + target_pct) if target_pct is not None else original.suggested_target

        target_clearance = None
        if confirmed_target is not None:
            target_clearance = evaluate_target_clearance(confirmed_entry, confirmed_target, candidate.static_resistance_levels)
            candidate.next_resistance_price = target_clearance.next_resistance
            candidate.target_clear = target_clearance.target_clear

        runway_consumed = None
        if target_clearance is not None and target_clearance.next_resistance is not None:
            runway_consumed = compute_runway_consumed_pct(
                pending.reference_price, confirmed_entry, target_clearance.next_resistance,
            )
        candidate.runway_consumed_pct = runway_consumed

        if target_clearance is not None and not target_clearance.target_clear:
            reason = (
                f"{candidate.symbol}: target {confirmed_target:.4f} sits behind known resistance "
                f"{target_clearance.next_resistance:.4f} -- rejecting entry"
            )
            candidate.entry_block_reason = reason
            self._pending_confirmations.pop(candidate.symbol, None)
            self.risk_engine.record_operational_event(RiskEventType.RESISTANCE_BEFORE_TARGET, candidate.symbol, reason, now)
            transition(candidate, CandidateState.ARMED, now=now, reason=reason)
            return

        if runway_consumed is not None and runway_consumed > self.config.max_runway_consumed_pct:
            reason = (
                f"{candidate.symbol}: confirmed entry already consumed {runway_consumed:.0%} of the "
                f"trigger-to-resistance runway (max {self.config.max_runway_consumed_pct:.0%}) -- rejecting entry"
            )
            candidate.entry_block_reason = reason
            self._pending_confirmations.pop(candidate.symbol, None)
            self.risk_engine.record_operational_event(RiskEventType.RESISTANCE_BEFORE_TARGET, candidate.symbol, reason, now)
            transition(candidate, CandidateState.ARMED, now=now, reason=reason)
            return

        # Confirmed and clear -- queue for this tick's batch-ranking pass
        # instead of submitting immediately, so a simultaneously-confirming
        # BETTER candidate this same tick can win a scarce slot instead of
        # losing purely to iteration order (see _submit_ranked_entries).
        if pending.momentum_event is not None and pending.momentum_event.momentum_qualification_at_event is not None:
            pending.momentum_event.momentum_qualification_at_event["confirmation_price"] = confirmed_entry
        pending.signal = Signal(
            symbol=original.symbol,
            action=original.action,
            generated_at=now,
            strategy_name=original.strategy_name,
            strategy_version=original.strategy_version,
            reference_price=confirmed_entry,
            suggested_stop=confirmed_stop,
            suggested_target=confirmed_target,
            score_at_signal=candidate.latest_score.score if candidate.latest_score else original.score_at_signal,
            metadata=original.metadata,
        )
        if candidate not in self._ready_to_enter:
            self._ready_to_enter.append(candidate)

    def _final_entry_rank(self, candidate: Candidate, signal: Signal) -> float:
        """0.65*RTMS + 0.20*MIS + 0.15*strategy_quality -- see
        scoring/rtms_weights.yaml's ranking_weights and
        scoring/strategy_quality.py. Replaces the plain-MIS sort key
        _submit_ranked_entries used before the Real-Time Momentum
        Qualification Layer (2026-08-17) existed: RTMS ("is this stock
        moving right now") dominates the ranking on purpose, since two
        candidates can carry similar MIS while one is actively impulsing
        and the other has already gone quiet."""
        weights = self.momentum_engine.config.ranking_weights
        rtms = candidate.momentum.rtms if candidate.momentum.rtms is not None else 0.0
        mis = candidate.latest_score.score if candidate.latest_score else 0.0
        quality = strategy_quality_score(
            signal, candidate, min_risk_reward_ratio=self.risk_engine.config.min_risk_reward_ratio,
        )
        return weights["rtms"] * rtms + weights["mis"] * mis + weights["strategy_quality"] * quality

    def _final_pretrade_recheck(
        self, candidate: Candidate, pending: "PendingConfirmation", now: datetime,
    ) -> Optional[str]:
        """Runs immediately before a ranked, slot-winning candidate actually
        submits an order -- the spec's own final pre-submission recheck.
        Freshness matters here specifically: a candidate can win a slot in
        _submit_ranked_entries' ranking and then still sit queued for
        several ticks (case 4 of _poll_confirmation's docstring) behind a
        faster-confirming rival before a slot actually opens up, by which
        point its setup may no longer hold. Returns None if every check
        passes, otherwise a short human-readable reason -- never raises.

        rtms-v3 (2026-08-19): dropped the momentum-phase and RTMS-floor
        checks that used to live here -- they duplicated the exact
        compound gate just removed from evaluate_trigger, and would have
        silently killed a candidate at this final step for the same
        reason BIVI/LGHL never entered in the first place. See
        scanner/momentum_qualification.py's evaluate_trigger docstring
        for the incident writeup. `state` is still read below for
        active_strategy_name; phase/RTMS remain fully populated for the
        dashboard and for _final_entry_rank's ranking weight, just not
        checked here anymore.

        rtms-v3-follow-up (2026-08-19): also dropped the MIS-faded and
        spread-too-wide checks that used to live here, for the same
        BTTC/BTOG-incident reasoning as _poll_confirmation's matching
        note -- see that method's docstring."""
        metrics = candidate.latest_metrics
        state = candidate.momentum
        th = self.momentum_engine.config.thresholds

        if metrics is None or metrics.return_5m is None or metrics.return_5m < th["min_return_5m_pct"]:
            return f"5m return no longer clears the {th['min_return_5m_pct']:.1f}% regime gate"

        active_strategy = state.active_strategy_name or pending.signal.strategy_name
        if not momentum_structure_intact(candidate, active_strategy, pending.snapshot):
            return "structural level no longer held"

        if pending.signal.suggested_target is not None:
            clearance = evaluate_target_clearance(
                pending.signal.reference_price, pending.signal.suggested_target, candidate.static_resistance_levels,
            )
            if not clearance.target_clear:
                return "target no longer clear of resistance"

        return None

    def _submit_ranked_entries(self, now: datetime) -> None:
        """Runs once per _process_all_candidates pass, after every candidate
        has already been processed this tick -- see self._ready_to_enter's
        docstring. Real motivation (2026-08-13, user-reported): the bot was
        taking whichever candidate happened to trigger/confirm FIRST in
        iteration order, not the best one available, whenever multiple
        candidates wanted a scarce position slot at the same time.

        Ranks by _final_entry_rank (2026-08-17: RTMS/MIS/strategy_quality
        blend, replacing the old plain-MIS sort key) descending, then walks
        the full ranked list -- not just the top `available_slots` -- since
        a candidate can now fail _final_pretrade_recheck and be skipped
        WITHOUT consuming a slot, letting the next-ranked candidate still
        get a chance this same tick. Zero candidates passing the recheck
        means zero trades this tick, per the spec's explicit "never fill a
        slot just because one is open." A candidate that fails the recheck
        reverts to ARMED (RiskEventType.MOMENTUM_QUALIFICATION_LOST), not
        left dangling in CONFIRMING/_ready_to_enter. Any candidate that
        neither wins a slot nor fails the recheck is simply left CONFIRMING
        -- _poll_confirmation re-queues it next tick exactly as before."""
        if not self._ready_to_enter:
            return

        max_positions = self.risk_engine.config.max_simultaneous_positions
        available_slots = (
            max(0, max_positions - len(self._positions)) if max_positions else len(self._ready_to_enter)
        )
        if available_slots <= 0:
            return

        def _rank_key(candidate: Candidate) -> float:
            pending = self._pending_confirmations.get(candidate.symbol)
            return self._final_entry_rank(candidate, pending.signal) if pending is not None else 0.0

        ranked = sorted(self._ready_to_enter, key=_rank_key, reverse=True)
        slots_filled = 0
        for candidate in ranked:
            if slots_filled >= available_slots:
                break
            pending = self._pending_confirmations.get(candidate.symbol)
            if pending is None:
                continue  # shouldn't happen -- defensive only

            failure_reason = self._final_pretrade_recheck(candidate, pending, now)
            if failure_reason is not None:
                self._pending_confirmations.pop(candidate.symbol, None)
                reason = f"{candidate.symbol}: failed final pre-submission momentum recheck ({failure_reason})"
                candidate.entry_block_reason = reason
                self.risk_engine.record_operational_event(
                    RiskEventType.MOMENTUM_QUALIFICATION_LOST, candidate.symbol, reason, now,
                )
                transition(candidate, CandidateState.ARMED, now=now, reason=reason)
                self._flush_state_transitions(candidate)
                continue

            self._pending_confirmations.pop(candidate.symbol, None)
            transition(candidate, CandidateState.TRIGGERED, now=now, reason="confirmed and ranked for entry")
            self._submit_entry(candidate, pending.signal, pending.snapshot, now, momentum_event=pending.momentum_event)
            self._flush_state_transitions(candidate)
            slots_filled += 1

    def _submit_entry(
        self, candidate: Candidate, signal: Signal, snapshot: MarketSnapshot, now: datetime,
        momentum_event: Optional[MomentumEvent] = None,
    ) -> None:
        # Defense-in-depth against a real incident (BIVI, 2026-08-12): a
        # position wrongly dropped from self._positions by a since-fixed
        # reconcile_positions_from_broker bug went to COOLDOWN, then back
        # to WATCHING once the cooldown timer expired, and this loop
        # fired a genuine SECOND entry on top of a position that was
        # still very much open at the broker the entire time -- ballooning
        # it to Webull's own 200k-share order ceiling and a ~$250k
        # unrealized loss before anyone noticed. Fixing the false-drop
        # trigger (see reconcile_positions_from_broker's docstring) closes
        # THAT specific path, but local candidate/position tracking could
        # still theoretically get corrupted some other way (a crash mid-
        # tick, a future bug) -- this check doesn't trust local tracking
        # at all: it asks the broker directly, right before ANY new entry
        # order goes out, whether it already reports an open position for
        # this exact symbol, independent of self._positions or the
        # candidate's own state. Deliberately calls broker.get_positions()
        # directly, NOT _get_positions_for_tick() -- that method caches
        # its result for the rest of the current _process_all_candidates
        # pass (self._tick_positions_cache), and this check runs BEFORE
        # the entry order below is placed. Populating the tick cache with
        # a pre-entry (position-not-yet-open) snapshot here would poison
        # it for every later same-tick caller that needs to see the
        # position this call is about to create -- confirmed while adding
        # this check: it broke _maybe_verify_entry_via_positions' self-
        # heal path, which stopped seeing its own just-placed fill
        # because it kept reusing this call's stale empty cache instead
        # of fetching fresh. One extra broker call per entry attempt (not
        # per tick, not per candidate) is a fine price for correctness.
        try:
            broker_positions = self.broker.get_positions()
        except Exception:
            logger.warning(
                "Could not verify with the broker whether %s already has an open position "
                "before submitting a new entry (get_positions failed) -- proceeding without "
                "this extra check this tick.", candidate.symbol, exc_info=True,
            )
        else:
            existing = next(
                (p for p in broker_positions if p.symbol == candidate.symbol and p.quantity), None,
            )
            if existing is not None:
                logger.error(
                    "Refusing to submit a new entry for %s -- the broker already reports an "
                    "open position (quantity=%s) even though it isn't in this process's own "
                    "local tracking. Reverting to ARMED instead of risking a duplicate entry "
                    "on top of it.", candidate.symbol, existing.quantity,
                )
                transition(
                    candidate, CandidateState.ARMED, now=now,
                    reason="broker already reports an open position for this symbol",
                )
                return

        try:
            # open_positions=list(self._positions.values()), NOT
            # self.broker.get_positions() -- see submit_entry_signal's
            # docstring: only this process's own locally-tracked positions
            # carry a real stop_price, which RiskEngine.evaluate's
            # max_total_risk_pct gate needs to compute actual assumed risk.
            bracket_result = self.order_manager.submit_entry_signal(
                signal, snapshot=snapshot, open_positions=list(self._positions.values()), now=now,
            )
        except OrderRejected as exc:
            transition(candidate, CandidateState.ARMED, now=now, reason=f"risk engine rejected entry: {exc.decision.reason}")
            return
        except BracketEntryRejected as exc:
            # Atomic bracket entry (2026-08-13, see docs/ARCHITECTURE.md's
            # "Atomic bracket entry" section) -- the broker supports
            # place_bracket_entry but rejected this specific combo request.
            # No order was ever accepted, so this must not count against
            # this symbol's daily entry budget, same as the other
            # record_entry_order_failed call sites in this method --
            # applies whether this attempt gets retried below or not.
            self.risk_engine.record_entry_order_failed(candidate.symbol, now)

            retry_count = self._bracket_entry_retry_counts.get(candidate.symbol, 0) + 1
            if retry_count <= self.config.bracket_entry_max_retries:
                # Retry (added 2026-08-13 at explicit request): re-queue
                # this SAME confirmed signal for another attempt next tick
                # by reusing _poll_confirmation's own recompute-and-gate
                # machinery instead of blindly resubmitting the exact same
                # stale request -- transition back through ARMED->CONFIRMING
                # (both legal single hops; TRIGGERED cannot jump straight
                # to CONFIRMING) with a PendingConfirmation backdated by a
                # full confirmation_window_seconds, so _poll_confirmation
                # treats the window as already-elapsed on its very next
                # tick: it re-runs the reversal/MIS/spread checks (a real
                # reversal since the original trigger correctly cancels
                # the retry too, not just a rejection retry), recomputes
                # stop/target off the then-current price, and re-validates
                # target-clearance/runway fresh, exactly as a normal
                # confirmation would.
                self._bracket_entry_retry_counts[candidate.symbol] = retry_count
                logger.warning(
                    "Atomic bracket entry for %s was rejected by the broker (attempt %d/%d) -- "
                    "retrying next tick rather than giving up. Reason: %s",
                    candidate.symbol, retry_count, self.config.bracket_entry_max_retries + 1, exc.reason,
                )
                transition(candidate, CandidateState.ARMED, now=now, reason=f"bracket entry rejected, retrying: {exc.reason}")
                transition(candidate, CandidateState.CONFIRMING, now=now, reason="retrying atomic bracket entry")
                candidate.confirmation_started_at = now - timedelta(seconds=self.config.confirmation_window_seconds)
                candidate.confirmation_expires_at = now
                candidate.entry_block_reason = f"retrying after a rejected bracket entry ({retry_count}/{self.config.bracket_entry_max_retries}): {exc.reason}"
                self._pending_confirmations[candidate.symbol] = PendingConfirmation(
                    signal=signal, momentum_event=momentum_event,
                    started_at=now - timedelta(seconds=self.config.confirmation_window_seconds),
                    reference_price=signal.reference_price, snapshot=snapshot,
                )
                return

            # Retries exhausted -- give up for real. Per explicit
            # instruction: the trade does NOT go through at all, no
            # fallback to a plain unprotected entry.
            logger.error(
                "Atomic bracket entry for %s was rejected by the broker after %d attempt(s) -- giving up; "
                "the trade will NOT go through (no fallback to an unprotected plain entry). Reason: %s",
                candidate.symbol, retry_count, exc.reason,
            )
            self._bracket_entry_retry_counts.pop(candidate.symbol, None)
            reason = (
                f"{candidate.symbol}: atomic bracket entry rejected by the broker after {retry_count} "
                f"attempt(s) -- trade did not go through ({exc.reason})"
            )
            self.risk_engine.record_operational_event(RiskEventType.BRACKET_ENTRY_REJECTED, candidate.symbol, reason, now)
            transition(candidate, CandidateState.ARMED, now=now, reason=reason)
            return
        except Exception:
            # The caller (_submit_ranked_entries) already transitioned this
            # candidate to TRIGGERED right before calling here -- if
            # order_manager.submit_entry_signal raises anything other than
            # the expected OrderRejected/BracketEntryRejected (a real
            # broker/network error, a bug), that leaves the candidate stuck
            # in TRIGGERED with no order ever recorded in
            # _pending_entry_orders, since we never got past this call.
            # Without this handler, that exception propagates up to
            # _process_all_candidates' generic catch-all, which just logs
            # "Unhandled error processing candidate" and moves on -- the
            # candidate then sits in TRIGGERED until _poll_pending_entry's
            # "no pending order found for TRIGGERED candidate" safety net
            # eventually notices and reverts it, which can take a while if
            # compounded by other transient failures (e.g. get_snapshot
            # also failing for this symbol on subsequent cycles). Confirmed
            # as a real production case: a candidate sat TRIGGERED for over
            # a minute with zero orders ever submitted before that fallback
            # finally caught it. Log the real traceback here (the generic
            # catch-all above logs a much less specific message) and revert
            # immediately instead of relying on that fallback to eventually
            # clean it up.
            logger.exception(
                "Unexpected error submitting entry order for %s; reverting to ARMED.", candidate.symbol
            )
            # risk_engine.evaluate() already ran (and approved/incremented
            # the counters) inside order_manager.submit_entry_signal BEFORE
            # the broker call that just failed -- roll that back too, same
            # as the other two record_entry_order_failed call sites, or an
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
            if momentum_event.momentum_qualification_at_event is not None:
                momentum_event.momentum_qualification_at_event["actual_entry_price"] = signal.reference_price
        order = bracket_result.entry_order
        self._notify_order_update(order)
        if bracket_result.stop_order is not None:
            self._notify_order_update(bracket_result.stop_order)
        if bracket_result.target_order is not None:
            self._notify_order_update(bracket_result.target_order)

        if order.status == OrderStatus.FILLED:
            self._confirm_entry_filled(candidate, signal, order, now, bracket_result=bracket_result)
        elif order.status in (OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
            self._entry_signals[candidate.symbol] = signal
            self._pending_entry_orders[candidate.symbol] = order
            # Only stashed when an atomic bracket was actually placed
            # (stop_order is None otherwise, e.g. paper/backtest) -- see
            # this dict's own docstring in __init__ for why an absent
            # entry here means _confirm_entry_filled falls back to
            # _attach_broker_bracket exactly as before this feature
            # existed, not "atomic bracket with zero legs."
            if bracket_result.stop_order is not None:
                self._pending_entry_brackets[candidate.symbol] = bracket_result
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
                bracket_result = self._pending_entry_brackets.pop(candidate.symbol, None)
                self._confirm_entry_filled(candidate, signal, status_order, now, bracket_result=bracket_result)
                return
            elif status_order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
                self._entry_signals.pop(candidate.symbol, None)
                self._pending_entry_orders.pop(candidate.symbol, None)
                self._pending_entry_position_checked.discard(candidate.symbol)
                self._pending_entry_brackets.pop(candidate.symbol, None)
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
        bracket_result = self._pending_entry_brackets.pop(candidate.symbol, None)
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
        self._confirm_entry_filled(candidate, signal, filled_order, now, bracket_result=bracket_result)

    def _confirm_entry_filled(
        self, candidate: Candidate, signal: Signal, order: Order, now: datetime,
        bracket_result: Optional[BracketSubmissionResult] = None,
    ) -> None:
        # A successful fill means any earlier BracketEntryRejected retry
        # count for this symbol is no longer relevant -- see
        # _submit_entry's handling and _start_confirmation's matching
        # clear for the "fresh trigger" side of this same contract.
        self._bracket_entry_retry_counts.pop(candidate.symbol, None)
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
        # Atomic bracket entry (2026-08-13, see docs/ARCHITECTURE.md's
        # "Atomic bracket entry" section): if OrderManager.submit_entry_signal
        # already placed the stop/target as part of the SAME broker call as
        # this entry (bracket_result.stop_order is not None), that resting
        # bracket already exists -- record it directly instead of calling
        # _attach_broker_bracket, which would place a SECOND, duplicate
        # bracket on top of the one already resting at the broker. Only
        # falls through to _attach_broker_bracket when no atomic bracket
        # was attempted at all (bracket_result is None or its stop_order is
        # None -- PaperBrokerClient/backtests, or a signal with no
        # suggested_stop/suggested_target), preserving that path's existing
        # behavior exactly.
        #
        # Known residual gap, not fully closed by this: the bracket's
        # resting quantity was sized off decision.max_shares at submission
        # time, before this fill was confirmed -- if the broker's actual
        # fill quantity above ends up different (a genuine partial fill on
        # a MARKET order), the resting bracket and the locally-tracked
        # position.quantity could briefly disagree. _sync_broker_protective_orders'
        # existing every-tick reconciliation is what would eventually
        # correct this, same defense-in-depth this loop already relies on
        # for other broker-response uncertainties.
        if bracket_result is not None and bracket_result.stop_order is not None:
            position.broker_stop_order_id = bracket_result.stop_order.broker_order_id
            position.broker_target_order_id = (
                bracket_result.target_order.broker_order_id if bracket_result.target_order is not None else None
            )
            position.broker_stop_price_synced = position.stop_price
            position.broker_stop_is_trailing = False
            position.unprotected_alert_logged = False
        else:
            self._attach_broker_bracket(candidate, position, now)
        self._notify_position_snapshot_upsert(position)
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
        # A later unprotected stretch (e.g. after a future cancel+replace
        # cycle) should be able to raise its own fresh alert rather than
        # staying silently suppressed by a flag left over from this
        # earlier, now-resolved episode -- see
        # Position.unprotected_alert_logged's docstring.
        position.unprotected_alert_logged = False

    def _maybe_raise_unprotected_position_alert(self, candidate: Candidate, position: Position, now: datetime) -> None:
        """Visibility, not a circuit breaker: _attach_broker_bracket/
        _sync_broker_protective_orders already retry attaching a real
        broker-side bracket every tick, unconditionally, forever (see
        those methods' docstrings) -- this doesn't change that. What it
        adds is a single, one-time RiskEventType.POSITION_UNPROTECTED_TOO_LONG
        event (surfaced on the dashboard's existing Risk Events panel,
        RiskEngine.events) once a position has gone
        TradingLoopConfig.unprotected_position_alert_seconds or longer
        riding on software-only management, so a structurally broken
        order payload that can NEVER succeed (a future Webull API
        change, a new order type added without live verification) doesn't
        fail completely silently for the rest of the position's
        lifetime -- a human watching the dashboard actually sees it.
        Defense-in-depth against a FUTURE regression, not a currently-
        known problem: this codebase's bracket/trailing-stop payload
        fields are both confirmed working live as of this writing (see
        _order_payload's stop_price comment and
        WebullBrokerClient._ORDER_TYPE_TO_WEBULL's TRAILING_STOP entry).

        Uses position.opened_at as the start of the unprotected clock
        (not a separate "first attach attempt" timestamp): a fresh
        position always has _attach_broker_bracket attempted synchronously
        in the same tick it's created (_confirm_entry_filled), so the two
        are effectively the same moment in practice, and reusing opened_at
        avoids adding another timestamp field just for this. No-op if
        already broker-managed (broker_stop_order_id is not None) or if
        this exact unprotected stretch already raised its one alert
        (position.unprotected_alert_logged, reset by _attach_broker_bracket
        the moment it next succeeds)."""
        if position.broker_stop_order_id is not None:
            return
        if position.unprotected_alert_logged:
            return
        if now - position.opened_at < timedelta(seconds=self.config.unprotected_position_alert_seconds):
            return
        position.unprotected_alert_logged = True
        self.risk_engine.record_operational_event(
            RiskEventType.POSITION_UNPROTECTED_TOO_LONG,
            candidate.symbol,
            (
                f"{candidate.symbol} has had no broker-side protective bracket for at least "
                f"{self.config.unprotected_position_alert_seconds:.0f}s -- riding on software-"
                f"only position management. The bot keeps retrying every tick, but this is worth "
                f"checking (broker rejection reason in the logs, sustained rate-limit contention, "
                f"or a genuinely unsupported order payload)."
            ),
            now,
        )

    def _maybe_raise_stale_market_data_alert(self, candidate: Candidate, now: datetime) -> None:
        """Visibility, not a new fetch mechanism: streaming/REST both keep
        being retried every tick regardless (this doesn't change that) --
        what it adds is a single, one-time RiskEventType.MARKET_DATA_STALE
        event (surfaced on the dashboard's existing Risk Events panel,
        same as _maybe_raise_unprotected_position_alert above) once a
        tracked candidate's cached price (get_last_known_price_age_seconds
        -- the exact same age the dashboard's /api/positions AND
        /api/candidates staleness badges already read) has gone
        TradingLoopConfig.stale_market_data_alert_seconds or longer
        without a fresh snapshot. Called from _process_candidate_inner's
        REST-fallback exception handler for any _STREAMING_ELIGIBLE_STATES
        candidate (2026-08-21: broadened from ENTERED/MANAGING-only, since
        a dead feed is exactly as invisible pre-entry as it is post-entry)
        -- that's the only place a snapshot fetch can fail, so it's the
        only place this stretch can start growing.

        No-op if this candidate hasn't had a single tick processed yet
        (get_last_known_price_age_seconds returns None -- nothing to
        measure an age against, e.g. the instant after discovery/adoption)
        or if this exact dead-feed stretch already raised its one alert
        (candidate.market_data_stale_alert_logged, reset by
        _process_candidate_inner the moment a fresh snapshot is cached
        again)."""
        if candidate.market_data_stale_alert_logged:
            return
        age_seconds = self.get_last_known_price_age_seconds(candidate.symbol, now)
        if age_seconds is None or age_seconds < self.config.stale_market_data_alert_seconds:
            return
        candidate.market_data_stale_alert_logged = True
        self.risk_engine.record_operational_event(
            RiskEventType.MARKET_DATA_STALE,
            candidate.symbol,
            (
                f"{candidate.symbol}'s price feed has had no fresh snapshot (streaming or REST) "
                f"for at least {age_seconds:.0f}s -- the displayed price is stale and may not "
                f"reflect the current market. The bot keeps retrying every tick, but this is worth "
                f"checking (a halted/delisted symbol, a Webull data gap for this ticker, or "
                f"sustained rate-limit contention)."
            ),
            now,
        )

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

        The target leg's size varies by which call placed it (2026-08-20):
        _attach_broker_bracket (every re-arm after the first) always sizes
        it at half the position, so a fill there is a SCALE_OUT -- but
        OrderManager.submit_entry_signal's very first, entry-time atomic
        bracket sizes BOTH legs at the FULL entry quantity (see that
        method's docstring for why: Webull's atomic MASTER+STOP_LOSS+
        STOP_PROFIT combo rejects a half/full mismatch between those two
        legs with OAUTH_OPENAPI_ERROR_STOP_LOSS_QUANTITY -- confirmed live
        2026-08-20, HUIZ/ZSTK). So a target-leg fill is only a SCALE_OUT
        when it covers LESS than the currently-tracked position.quantity;
        a fill covering the full remaining quantity (the common case for
        every position's very first bracket) is a genuine full EXIT, not
        a partial -- routing that through _finalize_partial_exit instead
        would leave position.quantity at 0 without ever actually removing
        the position from tracking.

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

            # A target-leg fill is only a partial SCALE_OUT when it covers
            # LESS than the position's full tracked quantity (a
            # half-sized re-arm from _attach_broker_bracket) -- a fill
            # covering the full quantity (the entry-time atomic bracket's
            # target leg, sized full per submit_entry_signal's docstring)
            # is a genuine full EXIT. See this method's own docstring.
            is_partial_target = is_target and status_order.quantity < position.quantity
            exit_reason = (
                ExitReason.PARTIAL_PROFIT_TARGET if is_partial_target
                else ExitReason.PROFIT_TARGET if is_target
                else ExitReason.STOP_LOSS
            )
            exit_signal = Signal(
                symbol=candidate.symbol,
                action=SignalAction.SCALE_OUT if is_partial_target else SignalAction.EXIT,
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
        # `snapshot` is already cached into self._last_known_snapshots (for
        # the dashboard's /api/positions to read -- see get_last_known_price)
        # and candidate.market_data_stale_alert_logged already reset by
        # _process_candidate_inner just before this call, for every
        # _STREAMING_ELIGIBLE_STATES candidate -- both used to happen here
        # directly, centralized upstream (2026-08-21) so the same tick's
        # snapshot is cached once for candidates and positions alike.
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
            self._maybe_raise_unprotected_position_alert(candidate, position, now)
            return

        if position.exit_submission_failures > 0 and position.last_exit_submission_attempt_at is not None:
            # Backing off after a previous submission failure -- see
            # Position.exit_submission_failures' docstring for the real
            # incident (CYCU/SCKT, 2026-08-12) this guards against:
            # retrying a failed exit unconditionally every single tick
            # only added to the rate-limit contention that was blocking
            # it in the first place. Skip this tick's attempt entirely
            # (no network call at all) until the backoff window clears --
            # check_exit will simply fire the same signal again next tick
            # once it does, same as if this tick had never run.
            delay = min(
                self.config.exit_submission_backoff_base_seconds * (2 ** (position.exit_submission_failures - 1)),
                self.config.exit_submission_backoff_max_seconds,
            )
            if now - position.last_exit_submission_attempt_at < timedelta(seconds=delay):
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
            position.exit_submission_failures += 1
            position.last_exit_submission_attempt_at = now
            next_delay = min(
                self.config.exit_submission_backoff_base_seconds * (2 ** (position.exit_submission_failures - 1)),
                self.config.exit_submission_backoff_max_seconds,
            )
            logger.exception(
                "broker.place_order raised submitting an exit (%s) for %s -- position remains open, "
                "will retry in %.0fs (%d consecutive failures).", exit_signal.action.value, candidate.symbol,
                next_delay, position.exit_submission_failures,
            )
            return
        position.exit_submission_failures = 0
        self._notify_order_update(order)

        if order.status == OrderStatus.FILLED:
            self._dispatch_exit_finalization(candidate, position, order, exit_signal, now)
        else:
            self._pending_exit_orders[candidate.symbol] = (order, exit_signal)

    def _poll_pending_exit(self, candidate: Candidate, snapshot: MarketSnapshot, now: datetime) -> None:
        """While a symbol has an entry in self._pending_exit_orders,
        _manage_position defers to this method EXCLUSIVELY -- check_exit
        is never called again for it until this pops the entry (see
        _manage_position's `if pending is not None: ...; return`). That
        makes a still-pending order that never reaches a terminal status
        a real trap: real incident (2026-08-13, sandbox), an exit order
        for a position well past its stop-loss simply never resolved --
        not filled, not rejected, not cancelled, not expired -- for many
        hours, and this method used to just silently return every tick
        with no logging at all in that case ("else: pass"). Nothing else
        in this codebase noticed either: the dashboard's own manual
        "Close" button (_close_all_positions_now) correctly refuses to
        touch a symbol already in self._pending_exit_orders (it assumes
        the order is still genuinely in flight), so it silently no-opped
        too. The position rode on, completely unprotected, indefinitely,
        with zero visibility anywhere.

        Fix: once the order has been outstanding for
        TradingLoopConfig.pending_exit_stuck_timeout_seconds (180s
        default) with no terminal status, treat it as stuck -- cancel it
        (best-effort; if this itself fails, still proceed to drop
        tracking below, since leaving a possibly-already-filled or
        already-dead order in self._pending_exit_orders forever is
        strictly worse than the rare case of a cancel racing a real late
        fill), raise a loud, visible RiskEventType.PENDING_EXIT_ORDER_STUCK
        event, and drop it from self._pending_exit_orders so the very
        next tick's _manage_position call falls through to a completely
        fresh PositionManager.check_exit/exit-submission attempt instead
        of polling the same dead order forever."""
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
        elif now - order.created_at >= timedelta(seconds=self.config.pending_exit_stuck_timeout_seconds):
            try:
                self.order_manager.cancel(order.broker_order_id)
            except Exception:
                logger.warning(
                    "Failed to cancel a stuck pending exit order for %s -- dropping it from "
                    "tracking anyway so a fresh exit attempt can run next tick.", candidate.symbol,
                    exc_info=True,
                )
            self._pending_exit_orders.pop(candidate.symbol)
            logger.error(
                "Exit order for %s has been pending for over %.0fs with no terminal status -- "
                "treating it as stuck, cancelling it, and will attempt a fresh exit next tick.",
                candidate.symbol, self.config.pending_exit_stuck_timeout_seconds,
            )
            self.risk_engine.record_operational_event(
                RiskEventType.PENDING_EXIT_ORDER_STUCK,
                candidate.symbol,
                (
                    f"{candidate.symbol}'s exit order ({exit_signal.action.value}) has been pending "
                    f"for over {self.config.pending_exit_stuck_timeout_seconds:.0f}s with no fill, "
                    "rejection, cancellation, or expiration -- cancelled and will retry fresh."
                ),
                now,
            )
        # else still pending, within the timeout -- normal, no action needed

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

    def _build_trade_for_external_close(self, symbol: str, position: Position, now: datetime) -> Trade:
        """Counterpart to _build_trade_from_fill for a position
        reconcile_positions_from_broker confirmed closed externally (see
        that method's ExitReason.EXTERNAL_CLOSE branch) -- this process
        never submitted or saw fill for the exit itself, so there's no
        Order/Signal to build from. Without this, an external close
        (manual close in the Webull app, scripts/list_and_close_positions.py,
        or any other out-of-band close) previously vanished from tracking
        with NO Trade record at all -- confirmed live 2026-08-12: BIVI was
        correctly detected as closed and removed from the dashboard, but
        never showed up in trade history/performance, because
        record_trade()/on_trade_closed are only ever called from
        _finalize_exit's own internal fill-confirmation path.

        exit_price here is a genuine best-effort approximation, not a
        confirmed fill price -- tries broker.poll_fills() first (an exit-
        side fill for this symbol at/after the position's opened_at is
        almost certainly the real closing fill), then the position's own
        stop/target price, then the last live-STREAMED price for this
        symbol (self._get_streaming_snapshot -- an already-in-memory value
        from the existing quote stream, NOT a new network call), and only
        falls all the way back to avg_entry_price -- which fabricates an
        exact $0 P&L regardless of what actually happened -- as an
        absolute last resort when every other source is unavailable.
        Confirmed live 2026-08-12: WCT had neither a matched fill nor a
        stop_price/target_price set, so this chain used to land straight
        on avg_entry_price and record a misleadingly exact break-even
        trade no matter what the real outcome was.

        A same-day earlier version of this fallback used a fresh
        broker.get_snapshot() REST call here instead of the streaming
        cache -- reverted after the user reported it was contributing to
        renewed rate-limit pressure. That call runs synchronously inside
        reconcile_positions_from_broker's drop loop (one per externally-
        closed symbol found in a single pass, with call_with_retry's own
        up to 4 paced attempts each on a 429) at exactly the moments this
        codebase has repeatedly seen sustained rate-limit contention
        already in progress -- see the CYCU/SCKT/BIVI incidents elsewhere
        in this file's history. The streaming cache costs nothing extra:
        MANAGING positions are already streaming-subscribed for
        stop/target management (_ensure_streaming_subscribed), so this
        just reads a value already being kept warm for another purpose
        instead of placing a dedicated request. The tradeoff is a
        narrower window (only helps when streaming has a fresh price for
        this exact symbol, per streaming_staleness_seconds) -- accepted
        deliberately: this is a best-effort historical record, not
        something worth spending scarce account-wide request budget on
        during exactly the conditions most likely to need that budget
        elsewhere (a genuine stuck exit retry, a real entry)."""
        exit_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY_TO_COVER
        exit_price = None
        try:
            fills = [
                f for f in self.broker.poll_fills(since=position.opened_at)
                if f.symbol == symbol and f.side == exit_side
            ]
            if fills:
                exit_price = max(fills, key=lambda f: f.filled_at).price
        except Exception:
            logger.warning("poll_fills failed while building an external-close Trade for %s.", symbol, exc_info=True)

        if exit_price is None:
            exit_price = position.stop_price or position.target_price

        if exit_price is None:
            streaming_snapshot = self._get_streaming_snapshot(symbol, now)
            if streaming_snapshot is not None:
                exit_price = streaming_snapshot.last_price

        if exit_price is None:
            exit_price = position.avg_entry_price

        pnl = (exit_price - position.avg_entry_price) * position.quantity
        pnl_pct = (
            (exit_price - position.avg_entry_price) / position.avg_entry_price * 100.0
            if position.avg_entry_price else 0.0
        )

        return Trade(
            symbol=symbol,
            strategy_name=position.strategy_name,
            side=position.side,
            entry_price=position.avg_entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            opened_at=position.opened_at,
            closed_at=now,
            exit_reason=ExitReason.EXTERNAL_CLOSE,
            pnl=pnl,
            pnl_pct=pnl_pct,
            max_favorable_excursion=position.max_favorable_excursion,
            max_adverse_excursion=position.max_adverse_excursion,
        )

    def _finalize_exit(self, candidate: Candidate, position: Position, order: Order, exit_signal: Signal, now: datetime) -> None:
        trade = self._build_trade_from_fill(candidate, position, order, exit_signal, now)

        self.risk_engine.record_trade_closed(candidate.symbol, trade.pnl, now=now)
        self._positions.pop(candidate.symbol, None)
        self._notify_position_snapshot_delete(candidate.symbol)
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
        self._notify_position_snapshot_upsert(position)
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

    def get_last_known_price(self, symbol: str) -> Optional[float]:
        """Dashboard-facing (see dashboard/app.py's /api/positions AND
        /api/candidates): the most recent price _process_candidate_inner
        saw for `symbol`, for any _STREAMING_ELIGIBLE_STATES candidate --
        open position or pre-entry -- already fetched as part of that
        candidate's own tick processing (streaming, batch REST, or a
        per-candidate fallback call) -- NEVER a new broker call of its
        own. Real incident (2026-08-12): /api/positions used to call
        broker.get_snapshot() directly, once per open position,
        sequentially, on every single HTTP request -- through the same
        shared, priority-queued, occasionally-exclusive (place_order/
        place_oco_bracket) webull_limiter order placement uses. Under real
        trading load that could queue behind CRITICAL trading traffic (or
        a whole exclusive() hold) for tens of seconds per position, well
        past nginx's proxy_read_timeout, producing 504s on a page that's
        supposed to be a cheap read. Returns None if this candidate hasn't
        had a tick processed yet (e.g. the instant after discovery, or
        after a position was adopted/opened) -- callers should treat that
        exactly like the old get_snapshot()-failed case: no current
        price/unrealized P&L available this refresh, nothing more
        alarming than that."""
        entry = self._last_known_snapshots.get(symbol)
        if entry is None:
            return None
        snapshot, _ = entry
        return snapshot.last_price

    def get_last_known_price_age_seconds(self, symbol: str, now: datetime) -> Optional[float]:
        """Dashboard-facing companion to get_last_known_price (see that
        method's docstring for why /api/positions and /api/candidates read
        a cache instead of calling the broker directly): how long ago THIS
        PROCESS last successfully cached a snapshot for `symbol` -- i.e.
        `now` minus `_last_known_snapshots[symbol]`'s own `received_at`,
        NOT the cached snapshot's `timestamp` field (Webull's reported
        quote_time). get_last_known_price itself has NO staleness check --
        unlike _get_streaming_snapshot, which already compares age against
        streaming_staleness_seconds before trusting a streamed snapshot
        for this loop's own tick processing, nothing ever stopped a
        dashboard endpoint from presenting an arbitrarily old cached price
        as if it were current (2026-08-19, real incident: BTCT/BTOG showed
        a frozen price that no longer matched the market -- the underlying
        per-tick fetch for that symbol had started failing every cycle,
        per _process_candidate_inner's early-return on a failed
        get_snapshot, and _last_known_snapshots simply stops being written
        from that point on with nothing flagging it).

        Deliberately measures against `received_at`, not `snapshot.timestamp`
        (2026-08-21, real incident: after broadening this cache to cover
        every tracked candidate, not just open positions, nearly every
        WATCHING/HEATING_UP row on the dashboard showed a false "stale"
        warning even though _process_all_candidates was successfully
        refreshing every one of them on every single tick -- comparing
        against quote_time was measuring "how long since this symbol last
        actually traded," which is routinely 30+ seconds for a genuinely
        quiet-but-still-being-fetched candidate, not "how long since this
        process last fetched it." Same fix `_live_snapshots`' own
        `received_at` field already applied to the streaming path -- see
        that dict's docstring). Returns None if this candidate hasn't had
        a tick processed yet, same "nothing to report" contract as
        get_last_known_price's own None case."""
        entry = self._last_known_snapshots.get(symbol)
        if entry is None:
            return None
        _, received_at = entry
        return (now - received_at).total_seconds()

    def get_account_summary(self) -> dict:
        """Dashboard-facing (see dashboard/app.py's /api/status): cached
        equity/buying_power, refreshed in the background by
        _process_all_candidates every
        TradingLoopConfig.account_summary_refresh_interval_seconds --
        NEVER a live broker call made from the request thread. Same
        real incident as get_last_known_price's docstring: /api/status
        used to call broker.get_account_equity()/get_buying_power() live
        on every single HTTP request, exposed to the exact same rate-
        limiter contention. Values are None (with equity_error explaining
        why) until the first background refresh completes -- same
        shape/meaning /api/status already returned for a failed live
        call, so this is a drop-in swap for the dashboard frontend, not a
        breaking change. If a LATER refresh fails after an earlier one
        succeeded, equity/buying_power deliberately keep their last
        good values (stale, not None) while equity_error still reports
        the failure -- a brief broker hiccup shouldn't blank out a number
        that was correct 30 seconds ago; the frontend can use
        equity_error's presence to show a "may be stale" indicator
        without losing the last known figure entirely."""
        return {
            "equity": self._cached_equity,
            "buying_power": self._cached_buying_power,
            "equity_error": self._cached_account_summary_error,
        }

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
        case to fall back from either.

        One-time, first-call-only load of the persisted open-positions
        snapshot (see PositionRecord / TradingLoop's on_position_snapshot_*
        hooks) happens right below, before the broker.get_positions() call
        -- self._position_snapshot_load_attempted is only ever False on
        this method's very first invocation for this process's lifetime,
        which is exactly the moment self._positions is guaranteed to still
        be empty regardless of what was genuinely open at the broker a
        moment before this process started. Without this, a position that
        closed at the broker WHILE this process was down/restarting is
        completely invisible: set(self._positions.keys()) is empty, so the
        missing-symbol diff below has nothing to compare against and never
        fires at all -- confirmed live 2026-08-19/20 (BTCT, a second
        BTOG): no warning log, no Trade record, no trace the position ever
        closed. Loaded into a SEPARATE dict (_recovered_snapshot_positions),
        never directly into self._positions -- see that field's docstring
        for why merging it in here would break the adoption loop below for
        a symbol that turns out to still be genuinely open."""
        now = now or datetime.utcnow()
        if not self._position_snapshot_load_attempted:
            self._position_snapshot_load_attempted = True
            if self._load_position_snapshot is not None:
                try:
                    for snap_position in self._load_position_snapshot():
                        if snap_position.symbol not in self._positions:
                            self._recovered_snapshot_positions[snap_position.symbol] = snap_position
                except Exception:
                    logger.exception(
                        "reconcile_positions_from_broker: failed to load the persisted position "
                        "snapshot; continuing without it -- any position that closed at the broker "
                        "during this restart may go undetected this pass."
                    )
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
        # A recovered-snapshot symbol the broker still confirms open just
        # needs ordinary adoption (the loop further down already triggers
        # on "not in self._positions") -- drop it from recovery
        # bookkeeping here so it isn't checked twice below.
        for symbol in broker_symbols & self._recovered_snapshot_positions.keys():
            del self._recovered_snapshot_positions[symbol]

        for symbol in (set(self._positions.keys()) | set(self._recovered_snapshot_positions.keys())) - broker_symbols:
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

            stale_position = self._positions.get(symbol) or self._recovered_snapshot_positions[symbol]
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
            trade = self._build_trade_for_external_close(symbol, stale_position, now)
            self.risk_engine.record_trade_closed(symbol, trade.pnl, now=now)
            if self.on_trade_closed is not None:
                try:
                    self.on_trade_closed(trade)
                except Exception:
                    logger.exception("on_trade_closed callback raised for %s (external close).", symbol)
            self._positions.pop(symbol, None)
            self._recovered_snapshot_positions.pop(symbol, None)
            self._notify_position_snapshot_delete(symbol)
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
            self._notify_position_snapshot_upsert(position)

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
                    CandidateState.CONFIRMING, CandidateState.TRIGGERED, CandidateState.ENTERED, CandidateState.MANAGING,
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
        # Reset every pass -- see this attribute's docstring in __init__.
        self._ready_to_enter = []

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

        if (
            self._last_account_summary_refresh is None
            or (now - self._last_account_summary_refresh) >= timedelta(seconds=self.config.account_summary_refresh_interval_seconds)
        ):
            self._last_account_summary_refresh = now
            try:
                self._cached_equity = self.broker.get_account_equity()
                self._cached_buying_power = self.broker.get_buying_power()
                self._cached_account_summary_error = None
            except Exception as exc:
                self._cached_account_summary_error = str(exc)
                logger.warning("Failed to refresh cached account equity/buying power this cycle.", exc_info=True)

        # Drop candidates that have gone stale (no state transition for
        # candidate_stale_after_seconds) BEFORE taking this tick's
        # candidates snapshot, so a just-pruned symbol isn't processed
        # again this pass -- see _prune_stale_candidates' docstring for
        # the 2026-08-14 incident this fixes.
        try:
            self._prune_stale_candidates(now)
        except Exception:
            logger.exception("Unhandled error pruning stale candidates this cycle.")

        candidates = self._snapshot_candidates()

        # Keep every candidate in a streaming-eligible state subscribed to
        # live prices, within a bounded budget -- cheap to call every tick
        # in steady state (a no-op once the desired top-N set stops
        # changing). ENTERED/MANAGING positions also get an eager first
        # attempt right when they start being tracked (see
        # _confirm_entry_filled and reconcile_positions_from_broker) so
        # they don't wait a full tick for their first subscription -- this
        # sweep is what RETRIES that attempt on every subsequent tick if it
        # failed, and also what EVICTS lower-priority symbols to make room
        # once the budget is full -- see
        # _reconcile_streaming_subscriptions' docstring.
        try:
            self._reconcile_streaming_subscriptions(candidates, now)
        except Exception:
            logger.exception("Unhandled error reconciling streaming subscriptions this cycle.")

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

        # After every candidate this tick has had a chance to finish
        # confirmation and queue onto self._ready_to_enter -- see
        # _submit_ranked_entries' docstring for why ranking/submission
        # happens once here rather than inline per-candidate above.
        try:
            self._submit_ranked_entries(now)
        except Exception:
            logger.exception("Unhandled error submitting ranked entries this cycle.")

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
