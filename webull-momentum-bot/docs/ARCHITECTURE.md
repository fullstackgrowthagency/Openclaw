# Architecture

## Data flow

```
BroadScanner            (cheap filters: price range, float ceiling, $ volume)
      |
      v  Candidate(DISCOVERED -> WATCHING)
CandidateWatcher         (recomputes MomentumMetrics + Momentum Ignition
      |                   Score on every snapshot; drives WATCHING ->
      |                   HEATING_UP -> ARMED; can REJECT on liquidity fail)
      v
TriggerEngine             (only looks at ARMED candidates; asks each
      |                    Strategy for an entry Signal on real-time data)
      v  Signal
RiskEngine.evaluate()      (deterministic: risk-per-trade sizing, exposure/
      |                     position/trade-count caps, spread/liquidity
      |                     gates, cooldowns, kill switch)
      v  RiskDecision(approved, max_shares)
OrderManager                (the ONLY thing allowed to call the broker)
      |
      v
BrokerClient                (PaperBrokerClient for fully local sim, or
                              WebullBrokerClient against the real sandbox --
                              picked by TradingMode via brokers/__init__.py)
```

Exits follow a parallel, shorter path: `PositionManager.check_exit` emits an
EXIT/SCALE_OUT `Signal`, which `OrderManager` routes straight to the broker
**without** the entry-sizing risk gates (spread/liquidity checks exist to
control new exposure, not to trap the bot in a losing position).

**Hard rule enforced in code, not just convention:** `Strategy` classes hold
no broker reference. If new code calls `broker.place_order` from anywhere
outside `execution/order_manager.py`, that's a bypass of the architecture --
don't do it, even for "just a quick script."

## Production run-loop (polling, not streaming)

`runtime/trading_loop.py`'s `TradingLoop` is what actually drives the data
flow above outside of a backtest -- `main.py` constructs one and calls
`run_forever()`. Since Webull streaming isn't implemented, it polls
`broker.get_snapshot()` per tracked candidate on a timer
(`TradingLoopConfig.poll_interval_seconds`) instead of reacting to pushed
ticks, and periodically re-runs `BroadScanner.scan()` against a
`SymbolUniverseProvider` to discover new candidates
(`universe_rescan_interval_seconds`).

The one thing this loop has to handle that the backtest engine doesn't:
**`WebullBrokerClient.place_order` returns `status=SUBMITTED`, not
`FILLED`** (confirmed live -- a 2xx response means Webull accepted the
order for processing, not that it has executed). `PaperBrokerClient`, by
contrast, fills synchronously. So every entry and exit order goes through a
pending-order tracking step (`_pending_entry_orders` / `_pending_exit_orders`)
that polls `OrderManager.get_status()` on subsequent ticks until it
resolves to `FILLED` or a terminal failure, rather than assuming the order
in front of it already executed.

The other non-obvious bit: `TradingLoop` keeps its own `_positions` dict as
the source of truth for an open position's stop/target/trailing-stop/MFE/MAE
state, seeded once (with a real `avg_entry_price` from `broker.get_positions()`)
right when an entry fill is confirmed. It deliberately does NOT re-fetch
`broker.get_positions()` on every tick to read that state back, because
`PositionManager.check_exit` mutates the Position object it's given
in place, and the broker returns a fresh object on every call -- refetching
every tick would silently discard the running trailing-stop/MFE/MAE state
computed on prior ticks.

`data/universe.py` feeds the scanner from three independent, live-verified
Webull screener sources, combined by `MultiSourceUniverseProvider`:

- `WebullUniverseProvider` with `rank_type="RELATIVE_VOLUME_10D"` -- the
  project's "high relative volume" criterion directly.
- The same `WebullUniverseProvider` class again with `rank_type="TURNOVER_RATE"`
  (% of a stock's float traded today) -- confirmed live to surface a
  meaningfully different, generally more extreme set of names than relative
  volume, and conceptually the same thing as the `float_turnover` metric the
  MIS already computes, just used as a discovery filter instead of a score
  input.
- `WebullGainersLosersUniverseProvider` wrapping `screener.get_gainers_losers(rank_type="DAY_1", sort_by="CHANGE_RATIO", direction="DESC")`
  -- today's top % price movers. Note `get_gainers_losers`'s `rank_type` is
  a **time period** (`DAY_1`, `MIN_5`, `WEEK_52`, etc.), unrelated to
  `get_most_active`'s `rank_type` despite the shared parameter name --
  confirmed against the live SDK, not assumed from the naming.

`MultiSourceUniverseProvider` is a plain union (every source queried every
cycle, not a priority fallback chain) with per-source failure isolation --
one source raising is logged and skipped rather than aborting the scan.
Critically, it **interleaves results round-robin** across sources rather
than concatenating them: `TradingLoop._rescan_universe` truncates the
combined list to `max_universe_size` before scanning, and concatenation
would let whichever source is listed first fill that entire cap before the
others contributed a single symbol -- exactly the "one list dominates"
outcome three independent sources are meant to avoid. A symbol only needs
to appear on one list to reach `BroadScanner`, which vets every symbol
identically (price, dollar volume, real free float via FMP) regardless of
which list(s) surfaced it.

**Webull's sandbox has a real sustained rate limit, confirmed live
(2026-08-08) across several rounds of testing, each round correcting the
last:**
1. A burst test (10 concurrent `get_snapshot` calls) only tripped 2 429s
   and looked like a non-issue.
2. A follow-up sustained-interval test showed the real ceiling is close to
   a flat **~1 request/second regardless of concurrency** (0.5s spacing =
   0/20 errors, 0.3s spacing = 6/20 errors).
3. A real 149-symbol scan using reactive retry alone (no pacing) hit 101
   rate-limit errors and didn't finish inside a 60s timeout -- retry-with-
   backoff can't paper over sustained overload once retries themselves add
   load back into an already-saturated window.
4. A first `RateLimiter` fix paced only the *first* attempt of each call
   (call sites did `limiter.wait()` once, then `call_with_retry(fn)`). At
   both 0.6s and 1.0s spacing with 10 concurrent workers (`BroadScanner`'s
   real config), 9 of 60 calls still failed outright -- raising the
   interval made no difference, because retries triggered inside
   `call_with_retry` ran on their own backoff timer, entirely outside the
   pacer, and could still stack up across threads and re-trigger each
   other's 429s.
5. The actual fix: `call_with_retry` now calls the shared
   `webull_market_data_limiter` before *every* attempt, not just the
   first, so a retry queues on the same global pacer as any other call.
   Re-verified live end-to-end afterward: 100 real symbols from
   `MultiSourceUniverseProvider`, 10 concurrent workers, against the actual
   sandbox -- **zero hard failures** (a handful of individual 429s, all
   recovered by the now-paced retry), completing in 124.9s (~1.25s/symbol
   including retry overhead, vs. the limiter's own 1.0s interval).

This pacing is why `max_universe_size` was capped at 100 (measured:
`BroadScanner` issues one paced Webull call per universe symbol, so
scanning N symbols takes roughly `N * 1.25s` of Webull-bound time no
matter how many worker threads are checking symbols concurrently -- more
workers only let FMP float lookups overlap with that, they can't make
Webull itself go faster) -- **before** `_passes_average_volume_filter`
(added later, see below) introduced a *second* per-symbol Webull call and
changed that math again.

**Average-volume filter** (`BroadScanner._passes_average_volume_filter`):
excludes a symbol trading under `min_average_volume` (1,000,000) shares/day
on average, unless its previous trading day alone already cleared that bar
-- a previously-quiet float that just had one big volume day is exactly
the pattern this bot targets, and shouldn't be excluded just because a
longer average hasn't caught up to it. Backed by
`WebullBrokerClient.get_daily_volumes`, which deliberately does **not**
reuse `get_bars()`/`_snapshots_from_bars()`: that method accumulates
volume across every fetched bar for intraday VWAP, which is correct for
minute bars within one session but wrong for daily bars spanning multiple
days (it would sum several days' volume together instead of reporting
each day's own total). Confirmed live (2026-08-09) against raw daily bars
that each day's `volume` field is already a clean, distinct per-day total,
most-recent-first.

Two live findings from that verification are worth flagging:
- **Sandbox historical data quality varies by symbol liquidity.** Mega-caps
  (AAPL, TSLA, NVDA) returned consistent, plausible volume across all 10
  days requested. Every low-float/micro-cap symbol tested (the bot's actual
  target universe) returned a real-looking value for only the *most
  recent* day, with the other 9 showing near-zero placeholder-looking
  figures. This means the "average" side of the filter is not meaningfully
  exercised in sandbox testing for this bot's real target names -- it
  effectively falls back to the previous-day-volume exception path almost
  always. This appears to be a sandbox data-population limitation, not a
  code bug, and should be re-verified once trading against real production
  data.
- **This filter roughly doubled Webull-bound scan time, not exactly 2x**:
  a live 60-symbol re-measurement came back at 171.9s (~2.86s/symbol) vs.
  the single-call figure of ~1.25s/symbol -- more than double, since
  occasional retries apply per call, not per symbol. `max_universe_size`
  was cut from 100 to 45 (roughly `100 * 1.25/2.86`) to preserve the same
  rescan cadence rather than let scans stretch to 4-5 minutes; for a
  momentum bot, missing a fast mover for that long defeats the point of
  frequent rescanning, so breadth was traded down in favor of cadence.
  `universe_rescan_interval_seconds` is 190s -- ~45% margin over 45
  symbols' measured ~129s, consistent with the margin used before this
  filter existed rather than sizing to a best case.

Separately (found during this same live testing, unrelated to the filter
itself): the configured FMP API key was returning `429 Limit Reach` on
every endpoint tested, meaning `FloatDataProvider.get_float_data` fails
for every symbol and `BroadScanner` silently rejects everything at that
step (`_check_symbol`'s `except Exception: return None`) regardless of any
other filter. Until that plan/quota issue is resolved, **no candidates
will be discovered at all**, independent of price, volume, or float
settings.

`PaperBrokerClient` has no live screener of its own, so paper mode falls
back to `StaticUniverseProvider` with a placeholder watchlist -- paper mode
is for exercising the pipeline against manually-fed snapshots, not
autonomous discovery.

## State machine

```
DISCOVERED -> WATCHING -> HEATING_UP -> ARMED -> TRIGGERED -> ENTERED -> MANAGING -> EXITED -> COOLDOWN
                  \           \           \          \
                   -----------------> REJECTED (terminal, from most states)
```

A high Momentum Ignition Score can only push a candidate towards ARMED. It
never creates an order by itself -- see `state_machine.py` and
`scanner/candidate_watcher.py`.

## Resistance tracking gotcha (read before touching candidate_watcher.py)

`CandidateWatcher.update()` computes metrics/scores using the resistance
level as it stood **before** the current snapshot, and deliberately does
NOT fold the current bar's high into `candidate.resistance_level` until
`update_resistance()` is called afterward. A bar's `high_of_day` always
includes its own `last_price`, so if resistance were updated first, a
breakout check (`price > resistance`) could never fire on the bar that
actually breaks out. Call order for every snapshot must be:

```python
watcher.update(candidate, snapshot)
signal = trigger_engine.on_snapshot(candidate, snapshot)
watcher.update_resistance(candidate, snapshot)
```

`backtest/engine.py` follows this order; any new live run-loop must too.

## Momentum Ignition Score

`scoring/weights.yaml` holds component weights and normalization
thresholds, versioned via the `version` field. `scoring/momentum_ignition_score.py`
normalizes weights to sum to 1.0 at load time and produces a 0-100 score
plus its component breakdown (`MomentumScoreComponents`) so individual
factors can be analyzed later. Nothing about the formula is assumed
correct -- it exists to be replaced once backtest/paper data says otherwise.

## Data collection

`collection/event_recorder.py`'s `MomentumEventTracker` records a
`MomentumEvent` for any notable momentum occurrence -- traded or not -- and
fills in forward-looking outcome snapshots at 30s/1m/3m/5m/10m/15m as
subsequent snapshots arrive, plus MFE/MAE and HOD/VWAP-break flags. This is
the raw material for improving the MIS formula and strategies offline; it
persists via `EventRecorder`, which is a thin seam meant to be backed by
`db.models.MomentumEventRecord` in a real deployment.

## Dashboard

`dashboard/app.py`'s `create_app(trading_loop, session_factory, trading_mode)`
takes both as explicit arguments rather than importing globals, specifically
so tests (`tests/test_dashboard.py`) can pass a real `TradingLoop` wired to
`PaperBrokerClient` plus an in-memory SQLite session factory, with no live
bot or real database required.

Two different data paths, deliberately:
- **Live panels** (candidates, open positions, risk events, kill-switch
  state) read directly off `trading_loop.get_candidates()` /
  `get_open_positions()` / `risk_engine.events` -- no DB round-trip, so they
  reflect the current process's actual in-memory state, not a snapshot that
  might be stale or never got persisted. `get_candidates()`/`get_open_positions()`
  return shallow copies specifically so a dashboard request from another
  thread can't race a concurrent `run_once()` mutating the underlying dicts.
- **Historical panels** (trade history, performance/win-rate) read from the
  database via `db/repository.py`.

`scripts/run_dashboard.py` is what makes the historical panels have
anything to show: it wires `TradingLoop`'s `on_trade_closed`/`on_order_update`
callbacks to `record_trade()`/`record_order()`, runs the loop in a
background thread, and serves the FastAPI app in the foreground. Before
this, nothing in the live loop persisted anywhere -- `on_trade_closed` just
printed. `record_order()` upserts by `client_order_id` rather than
inserting a new row per call, since one order is reported multiple times as
its status changes (`SUBMITTED` -> `FILLED`, see the Webull integration
section above) and `OrderRecord.client_order_id` has a uniqueness
constraint.

The frontend (`dashboard/static/`) is plain HTML/CSS/JS with no build step
and no external dependencies (no CDN scripts) -- it polls the REST
endpoints every 5s. Keep it that way unless there's a real reason to add a
frontend toolchain; a monitoring dashboard for a single operator doesn't
need one.

## Full persistence: state transitions, MIS scores, momentum events

Beyond trades/orders, `TradingLoop` exposes three more hooks so
`scripts/run_dashboard.py` can persist everything the data-collection goals
in the project outline need, without `TradingLoop` itself importing the DB
layer:

- **`on_state_transition(symbol, from_state, to_state, timestamp)`** --
  fired for every state-machine transition, from wherever it happened
  (`CandidateWatcher`, `TriggerEngine`, or `TradingLoop` itself). Rather
  than threading a callback through every module that can call
  `state_machine.transition()`, `_flush_state_transitions()` diffs
  `Candidate.state_history` (an append-only list every `transition()` call
  already appends to) against a per-symbol "already reported" count kept in
  `TradingLoop`, and reports whatever's new. This runs inside a `finally`
  block wrapping `_process_candidate` specifically so it still fires no
  matter which early `return` branch was taken that tick.
- **`on_score_computed(symbol, score)`** -- fired with `candidate.latest_score`
  immediately after `CandidateWatcher.update()` produces one. Writes one row
  per tick per watched candidate on purpose (see the README's note on this)
  -- comparing MIS formulas offline needs a dense history, not samples only
  at trigger time.
- **`momentum_event_tracker`** -- unlike the other two, this is a
  collaborator object (`MomentumEventTracker`, `collection/event_recorder.py`),
  not a simple callback, because tracking a momentum event has ongoing state
  across many ticks (filling forward-looking 30s-15m outcome windows).
  `TradingLoop` registers a new `MomentumEvent` whenever `TriggerEngine`
  fires a signal (`_register_momentum_event`), flips `was_traded` to `True`
  in `_submit_entry` once the order actually gets submitted (not just
  attempted -- a risk-engine rejection leaves it `False`), and calls
  `momentum_event_tracker.on_snapshot()` unconditionally every tick a
  snapshot is available so outcome windows keep filling regardless of the
  candidate's state. `db/repository.py`'s `DBBackedEventRecorder` extends
  the base in-memory `EventRecorder` to also write through to the database
  on every `save()`/`update()`, keyed by the DB row id (momentum events have
  no natural unique key the way orders have `client_order_id`).

**Bug found and fixed while wiring this up:** `MomentumEventTracker._finalize()`
(which sets the final `CONTINUED`/`FAILED`/`CHOPPY` label once all outcome
windows are filled) used to run *after* the last `recorder.update()` call
for that event, so the finalized label was computed but never actually
included in what got persisted -- silent before because the original
`EventRecorder.update()` was a no-op with nothing behind it to expose the
bug. Fixed by finalizing before that last `update()` call; see
`tests/test_event_recorder.py::test_final_update_call_includes_the_finalized_outcome_label`,
which fails without the fix.

## Free-float data (FMP)

`data/float_providers/fmp.py` is a real, network-verified integration
against Financial Modeling Prep's `stable` API namespace (`/stable/shares-float`
and `/stable/profile`) -- confirmed live on 2026-08-08, including that FMP's
older `/api/v4/shares_float` now hard-fails with a "Legacy Endpoint" error.
`get_float_provider(settings)` (`data/float_providers/__init__.py`) is the
only place that should construct a float provider; it picks FMP when
`FMP_API_KEY` is set and wraps it in `CachedFloatProvider`. `MassiveFloatProvider`
remains as an alternate skeleton, unused unless FMP is unconfigured.

`FMPFloatProvider` takes an injectable `http_get` callable specifically so
`tests/test_fmp_float_provider.py` can run hermetically against canned
responses shaped like the real API, without a network call or API key.

## Webull integration

`brokers/webull/client.py` wraps the official `webull-openapi-python-sdk`.
Read its module docstring before touching it -- it lists exactly which
field mappings were confirmed against live sandbox responses (auth,
account balance, market snapshot/bars, order request schema) versus which
are best-effort guesses pending re-verification (populated position rows,
a successful order response body, fill executions), and why: the sandbox
account had zero positions and every live order test happened on a weekend
market close, so those specific shapes couldn't be observed.

Two non-obvious things worth knowing if you're debugging this client:

1. **App key/secret are environment-locked.** A key issued for production
   authenticates fine against `api.webull.com` but gets a generic
   `401 UNAUTHORIZED: ... ensure you are connecting to the correct
   environment` from the sandbox host, and vice versa -- confirmed by
   testing the exact same credentials against both hosts. If sandbox auth
   ever breaks, test against production first (read-only calls like
   `get_account_list` are enough) before assuming the integration code
   regressed.
2. **`instrument_type` for orders is the string `"EQUITY"`**, not
   `webull.trade.common.instrument_type.InstrumentType.STOCK.name` (which
   is `"STOCK"` and gets rejected with `INVALID_PARAMETER: Instrument type
   invalid.`) -- that Python enum apparently isn't what the order-placement
   endpoint expects, despite looking like the obvious match. Confirmed by
   testing both live.

Streaming (`subscribe_quotes`) intentionally raises `NotImplementedError`:
`DataStreamingClient` needs an `mqtt_host`, and only the production value
(`data-api.webull.com`) is documented anywhere found so far. Don't guess a
sandbox equivalent -- confirm it live (or from support/docs) first.

## Database

See `db/models.py`. Deliberately does not store a raw tick log --
`market_observations` holds sampled/derived features, not the full tape.
Run `scripts/init_db.py` against `DATABASE_URL` to create tables; introduce
Alembic once the schema needs to evolve without losing data you care about.

## Backtesting

`backtest/engine.py` reuses the live `Strategy` / `RiskEngine` /
`OrderManager` / `CandidateWatcher` / `TriggerEngine` / `PositionManager`
code, swapping only the broker for `PaperBrokerClient`. Snapshots across all
symbols are merged into one chronological timeline and processed one at a
time so no symbol can see a future bar (its own or another symbol's).

Known gaps, tracked rather than silently ignored: no trading-halt modeling,
no order latency, no partial fills, no L2/order-flow-aware fills.

## Safety

See `config.py` docstring and the README's "Safety model" section. The
three-condition live-trading gate is checked independently in the broker
factory, `WebullBrokerClient.__init__`, and `OrderManager.submit_signal` --
redundant on purpose.
