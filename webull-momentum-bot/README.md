# Webull Low-Float Momentum Trading Bot

Automated scanner + paper/live trading system for low-free-float momentum
stocks, built on the Webull OpenAPI. See `docs/ARCHITECTURE.md` for the full
data-flow and module map.

## Status: Phase 1 (architecture) + Phase 2 (real Webull sandbox + FMP integrations)

What's implemented and tested:

- Config/safety layer with a hard-disabled-by-default live trading gate
- Candidate lifecycle state machine (DISCOVERED -> ... -> COOLDOWN)
- Broker abstraction with a fully working local `PaperBrokerClient` and a
  **real, sandbox-verified** `WebullBrokerClient` built on the official
  `webull-openapi-python-sdk` -- account balance/positions, order
  place/cancel/replace/detail, and market data (snapshot + historical bars)
  all confirmed live against `api.sandbox.webull.com`. See the module
  docstring in `brokers/webull/client.py` for exactly what was verified
  live vs. best-effort (a few response shapes -- populated positions, a
  successful order response, fill executions -- couldn't be confirmed
  because the sandbox account had no positions and testing happened on a
  weekend; re-verify those during market hours). Streaming
  (`subscribe_quotes`) is not implemented: no sandbox MQTT host was found.
- Free-float provider abstraction + local disk cache, backed by a **real,
  verified** Financial Modeling Prep (FMP) integration
  (`data/float_providers/fmp.py`, selected automatically by
  `get_float_provider()` when `FMP_API_KEY` is set). Massive remains an
  alternate skeleton if you switch providers later. Whichever primary is
  selected is automatically paired with a free Yahoo Finance fallback
  (`data/float_providers/yfinance_provider.py`, via the unofficial
  `yfinance` package -- no API key needed) through
  `FallbackFloatProvider` (`data/float_providers/fallback.py`): if the
  primary fails or rate-limits for a symbol, Yahoo's `floatShares` field
  (real float, not a shares-outstanding approximation) is tried before
  giving up on that symbol. On by default (`ENABLE_YFINANCE_FALLBACK=true`);
  set it to `false` to skip the extra round-trip. Since Yahoo's endpoint is
  unofficial/scraped and known to throttle datacenter IPs (which most VPS
  deployments look like), it's wired in strictly as a secondary -- a Yahoo
  block just means no fallback that day, not a new failure mode, since it's
  never the sole source.
- Momentum metrics (float turnover/velocity, RVOL, volume/price
  acceleration, VWAP, spread, dollar volume, etc.)
- Momentum Ignition Score with YAML-configurable weights/thresholds
  (`scoring/weights.yaml`) -- **not tuned**, meant to be improved from
  backtest/paper-trading data
- Three-tier scanner (BroadScanner -> CandidateWatcher -> TriggerEngine)
- **Eight entry strategies**, registered together on `TriggerEngine` (most
  selective/confirmed first, most permissive last -- see
  `docs/ARCHITECTURE.md`'s "Entry strategies" section): Refined Breakout
  (breakout bounded to within 3% above resistance), Opening Range Breakout,
  VWAP Reclaim Continuation, Momentum Breakout, Breakout Pullback,
  Ignition Pullback, Volatility Contraction ("flag/pennant"), and Volume
  Ignition (volume/float-turnover surge, no resistance level needed).
  Every strategy computes its target from the *same live*
  `RiskConfig.min_risk_reward_ratio` (no more hardcoded per-strategy
  multiples) via an injected `reward_risk_ratio_fn`, so tuning that one
  setting in the dashboard changes what all 8 target on their next signal
- Deterministic Risk Engine enforcing per-trade risk sizing (5% of equity,
  entry-to-stop), a minimum 1:2 reward:risk ratio on any signal with a
  target, a max single-position size (100% of buying power), a fleet-wide
  cap on total assumed stop-loss risk (50% of equity, not just notional
  exposure), daily loss limit, per-ticker/per-day trade caps,
  spread/liquidity gates, cooldowns, and a kill switch -- the four sizing
  metrics above are live-adjustable from the dashboard's Settings button
  (top right), no restart required; see `docs/ARCHITECTURE.md`'s "Risk
  sizing" section for the full mechanics. Approving a signal optimistically
  increments the daily/per-ticker trade counters immediately, before the
  broker has confirmed anything -- `RiskEngine.record_entry_order_failed`
  rolls that back if the resulting order is later rejected/canceled/expired,
  or if placing it raises an unexpected error entirely, without ever
  filling, so a broker-side failure (e.g. outside trading hours, a
  network/API error) can't silently exhaust a symbol's daily entry budget
  with zero real positions ever opened, or leave a candidate stranded in
  `TRIGGERED` waiting on a fallback safety net to notice (both were real
  bugs this fixes, not hypotheticals -- see `docs/ARCHITECTURE.md`'s
  "Daily trade counters vs. actual trades" and "`_submit_entry`'s catch-all
  exception handler" notes)
- **A stop-loss can silently fail to fire -- fixed.** The exit-side
  counterpart to the entry catch-all above: `_manage_position`'s exit
  submission and the shared kill-switch/end-of-day flatten path
  (`_close_all_positions_now`) both only caught `OrderRejected`, same gap
  as the entry side used to have. Confirmed as a real incident, not just a
  theoretical one: a position sat well past its `stop_price` with the stop
  never firing, because `broker.place_order` raised something else, which
  surfaced only in a generic per-candidate catch-all with no specific
  indication of what failed. Both call sites now also catch `Exception`
  broadly and log specifically that the exit submission failed for that
  symbol -- `check_exit` still gets a fair retry every subsequent tick
  either way (that part already worked), this just makes it possible to
  actually diagnose a stuck exit instead of guessing -- see
  `docs/ARCHITECTURE.md`'s "The exit-submission side of the same gap"
  section.
- **Core trading hours entry gate**: `RiskEngine.evaluate` unconditionally
  refuses any new entry signal outside 9:30am-4:00pm ET, Monday-Friday
  (`market_hours.is_within_core_trading_hours`) -- added after a real
  production report of an entry filling *during* core hours whose resulting
  position then went untracked (see the position-tracking bullet below);
  this gate closes a separate gap found while investigating that report --
  there was no explicit guarantee anywhere that entries only ever happen in
  core hours in the first place. Exits are never affected (a stop-loss or
  the end-of-day auto-flatten below still fire any time) since
  `OrderManager` never routes exits through `evaluate()` at all. Any open
  position still open once core hours end is now automatically flattened
  (`TradingLoop`'s end-of-core-hours auto-flatten, distinct from the manual
  kill switch -- it never halts the *next* day's trading, it just closes
  out today's) -- see `docs/ARCHITECTURE.md`'s "Core trading hours gate"
  and "End-of-core-hours auto-flatten" sections.
- **Position tracking can be lost on a broker-side fill reconciliation
  failure -- fixed.** A real production incident: an entry filled during
  core hours, but a field-name mismatch in `WebullBrokerClient.get_positions()`
  (never verified against a real, populated position -- only an empty
  response existed during integration) raised an exception partway through
  recording the fill, so the position was open at the broker but the bot
  never knew: no stop-loss management, not shown as an open position
  anywhere, buying power silently consumed. Fixed at two layers: local
  position tracking in `TradingLoop._confirm_entry_filled` no longer lets
  *any* broker-lookup failure (not just "no matching position") block
  recording the fill locally, and `get_positions()` itself now parses each
  returned row independently, logging and skipping just the one row it
  can't understand instead of losing every real position at once -- see
  `docs/ARCHITECTURE.md`'s "Position tracking can be lost..." section for
  the full mechanics and what the fix's logging captures for next time.
- **`self._positions` can drift out of sync with the broker in either
  direction -- both fixed.** `self._positions` is a plain in-memory dict
  with no persistence or cross-checking of its own, and this bit twice in
  one day, in opposite directions: (1) any deploy, crash, or VPS reboot
  previously wiped tracking for a position that was genuinely open a
  moment before ("broker has it, bot doesn't"), and (2) closing a position
  with `scripts/list_and_close_positions.py` (or manually in the Webull
  app) -- entirely outside the running bot process -- left the dashboard
  showing it as open indefinitely afterward, confirmed live ("bot has it,
  broker doesn't"). `TradingLoop.reconcile_positions_from_broker()` now
  fixes both in one pass: adopts any broker position this process doesn't
  know about, and drops any locally-tracked position the broker no longer
  reports at all (skipping a symbol with an exit order already in flight,
  so it can finish through the normal path instead). Runs from
  `_process_all_candidates` -- immediately on the very first call, then
  every `position_reconcile_interval_seconds` (30s default) afterward,
  since an external close can happen at any time, not just before startup.
  Since there's no original strategy signal to pull a real stop from for an
  adopted position, it's given a conservative synthetic one instead of
  being left unprotected: `RiskConfig.risk_per_trade_pct` as a straight
  %-below-current-price line (long) or %-above (short), no target -- it
  rides on the breakeven/trailing-stop rules only. Tagged with
  `strategy_name="reconciled_at_startup"` so it's always distinguishable
  from a real signal-driven entry in the trade history.
- `Strategy -> RiskEngine -> OrderManager -> Broker` enforced in code --
  strategies never hold a broker reference
- Position manager: a universal breakeven-at-+5% rule (stop jumps to entry
  once price is up 5%) applies to every open position from tick one; the 3%
  trailing stop only takes over once a target has been hit (see below) --
  both only ever tighten the stop, plus VWAP-failure and time-limit
  backstops. A target hit doesn't fully close the position -- it sells half
  (partial exit) and lets the rest keep riding the breakeven/trailing
  rules, so gains aren't capped at a fixed R-multiple the way a full-exit
  target would. The strategy's own suggested stop/target still become the
  position's initial stop/target
  unchanged (RiskEngine's settings gate entry, they never move an open
  position's stop or target). Both the breakeven trigger and trailing-stop
  % are live-adjustable from the dashboard's Settings button alongside the
  risk-sizing metrics -- see `docs/ARCHITECTURE.md`'s "Position
  management" section for the exact exit-check order
- Event-driven backtest engine using the *same* strategy/risk/order-manager
  code as live trading, with a configurable slippage/fee model
- **Production run-loop** (`runtime/trading_loop.py`, wired up in `main.py`):
  polls a snapshot per candidate every cycle (no streaming yet -- see
  below) and periodically rescans a symbol universe. Snapshot fetching is
  **batched** (`WebullBrokerClient.get_snapshots`) rather than one
  `get_snapshot()` call per candidate: every such call shares the same
  globally-paced Webull rate limiter (~1 req/s sustained), so N tracked
  candidates used to mean a real >=N-second floor on how often any single
  one's tick refreshed -- tens of seconds of staleness for a fast-moving
  low-float mover once the candidate list grows past a handful of names.
  Batching (chunked at Webull's own 100-symbol-per-request cap) turns that
  into `ceil(N/100)` rate-limited calls per cycle instead of N. Falls back
  to the original one-call-per-candidate behavior automatically for a
  broker without batching support (paper/backtest mode) or if the batch
  call itself fails for a cycle. The same batching applies to universe
  discovery too (`BroadScanner.scan`), not just already-tracked candidates
  -- `check_symbol_verbose` calls `get_snapshot` for every symbol in the
  universe before any structural gate runs, so with the universe now
  routinely in the hundreds (seven discovery sources, unbounded pagination
  per source), that was the dominant cost of a full scan, not thread-pool
  concurrency (every Webull call queues on the same limiter regardless of
  how many threads are running). See `docs/ARCHITECTURE.md`'s "Batched
  snapshot fetching" section for the full design. Correctly handles
  `WebullBrokerClient.place_order` returning `SUBMITTED` rather than
  `FILLED` (confirmed live) via pending-order polling for both entries and
  exits, rather than assuming synchronous fills like the backtest engine
  can. Universe discovery in sandbox/live mode combines **seven
  independent Webull screener sources** (`data/universe.py`), unioned via
  `MultiSourceUniverseProvider` rather than a priority fallback chain -- a
  symbol only needs to show up on one list, and `BroadScanner` vets every
  symbol the same way regardless of which list(s) surfaced it. The first
  four are live-verified:
  `get_most_active(rank_type="RELATIVE_VOLUME_10D")` (high relative
  volume), `get_most_active(rank_type="TURNOVER_RATE")` (% of float traded
  today -- directly analogous to the float_turnover metric the MIS already
  computes), `get_gainers_losers(rank_type="DAY_1")` (today's top % price
  movers), and `get_gainers_losers(rank_type="MIN_5")` (the last 5
  minutes' top % price movers -- Webull's real equivalent of "most active
  last 5 minutes" for price; there is no equivalent 5-minute *volume*
  ranking anywhere in the API, confirmed live). Three more sources are
  wired in but **not yet live-verified** (their `rank_type` strings and
  the value field each pagination threshold checks are inferred, not
  confirmed against a real response -- see `docs/ARCHITECTURE.md`'s
  "Production run-loop" section):
  `get_gainers_losers(rank_type="PRE_MARKET")` and
  `get_gainers_losers(rank_type="AFTER_MARKET")` (today's top % movers
  during the pre-market and after-hours sessions), and
  `get_most_active(rank_type="AMPLITUDE")` (today's most active names by
  high-low price range). Each source paginates instead of taking a single
  fixed-size page, stopping at a data-driven threshold or a generous
  safety valve rather than an arbitrary result count -- see
  `docs/ARCHITECTURE.md` for the exact stopping rule. Price range is
  $0.40-$25.00. There is no cap on how many symbols get scanned per cycle
  -- every symbol every source returns gets checked, so full-scan duration
  scales with universe size rather than being bounded by a fixed number;
  see `docs/ARCHITECTURE.md` for the measured per-symbol timing this
  implies (noting those numbers predate this wider price
  range/pagination/more sources and now understate real scan time). Dollar
  volume is an informational `Candidate` field, not a discovery gate.
  Average-daily, previous-day, and current-day (today's volume-so-far)
  volume, however, ARE a structural gate again
  (`BroadScannerConfig.min_average_daily_volume`/`min_previous_day_volume`/
  `min_current_day_volume`, 500,000/750,000/500,000 by default) -- a
  symbol is rejected only when it misses ALL THREE, so clearing any one
  bar alone is enough to survive. See `docs/ARCHITECTURE.md`'s "Volume floor" and
  "Structural vs. temporary disqualification" sections for the full
  reasoning, and for the same temporary-not-permanent distinction still
  applied to `CandidateWatcher`'s spread/liquidity checks
  (`Candidate.trade_eligible`/`block_reasons`, which don't permanently
  reject a candidate).
- **Extended-hours pricing and volume**: `WebullBrokerClient.get_snapshot()`
  now requests extended-hours data (`extend_hour_required=True`) and
  prefers `ext_price`/`ext_volume` fields over the regular-session
  `price`/`volume` when present AND the quote's own timestamp falls
  outside 9:30am-4:00pm ET, so a candidate's displayed price and every
  volume-derived Momentum Ignition Score component (relative volume,
  volume/dollar-volume acceleration, float velocity/turnover) reflect real
  pre-market/after-hours activity instead of a stale price or a flat 0
  (the regular-session `volume` field is legitimately 0 before 9:30am ET
  even during active pre-market trading). The time gate exists because
  it's unconfirmed whether Webull actually zeroes `ext_price`/`ext_volume`
  out once the regular session opens, or keeps echoing that morning's last
  pre-market value all day -- gating on the quote's own timestamp avoids
  ever using a stale pre-market number during regular hours either way.
  The exact `ext_price`/`ext_volume` field names are inferred from the
  SDK's protobuf streaming schema, not confirmed against a real sandbox
  response -- see the module docstring in `brokers/webull/client.py` and
  `docs/ARCHITECTURE.md`'s "Webull integration" section. It fails soft: if
  a field name guess is wrong, or it's simply absent, pricing/volume just
  fall back to the regular-session fields as before.
- **`support_trading_session` is `"CORE"` -- `"ALL"` was tried and directly
  confirmed live to be rejected outright** (`OAUTH_OPENAPI_PARAM_ERR`,
  HTTP 417, "invalid support_trading_session, value: ALL"), despite being a
  documented value in Webull's own public API docs -- a live rejection
  overrides documentation, so this was reverted the moment it was observed,
  not left in place pending further research. `"ALL"` had briefly replaced
  `"CORE"` mid-session on a since-corrected diagnosis of a
  buying-power-reserved-with-no-position report (the actual cause,
  confirmed directly by the user, was the entry firing *during* core hours
  with the fill going untracked -- see the position-tracking bullet above).
  New entries don't need `"ALL"` regardless -- they're never attempted
  outside core hours in the first place now (see the entry-gate bullet
  above). Still open: whether a `"CORE"`-flagged order fired right at the
  4:00pm ET close by the end-of-core-hours auto-flatten executes cleanly --
  see `docs/ARCHITECTURE.md`'s "Webull integration" section for the full
  history and what to watch for.
- **Resistance detection via volume profile** (`metrics/volume_profile.py`):
  resistance is no longer just the running high of day. At discovery,
  `BroadScanner` fetches recent intraday bars -- including pre-market and
  after-hours, not just the regular 9:30am-4:00pm ET session, so a
  low-float mover's real resistance level is caught even when it formed
  entirely outside regular hours -- and builds a volume-at-price histogram,
  keeping the biggest clusters ("high volume nodes") as static resistance
  levels. This is a more general stand-in for hand-picked levels like
  prior-day-high or round numbers, since those usually show up as volume
  clusters anyway, plus this gives a real strength signal a flat list of
  price points can't. `CandidateWatcher` merges the nearest still-untested
  static level with the running high on every tick. These levels are also
  periodically **refreshed** on later universe rescans (every 5 minutes by
  default) for any candidate that hasn't entered a position yet, so a
  candidate discovered early in the session isn't stuck with a volume
  profile that's missing everything that formed afterward. The exact
  pre-market/after-hours request parameter is inferred from a sibling
  Webull SDK endpoint's docs, not confirmed live -- see
  `brokers/webull/client.py`'s `get_raw_bars` docstring. See
  `docs/ARCHITECTURE.md`'s "Resistance detection" section for the full
  design, the refresh mechanics, and an important data-shape caveat
  (Webull's raw bars reach back as far as needed to find real data for
  illiquid names, which this deliberately bounds to a recent calendar
  window before building the profile).
- **RVOL historical baseline** (`metrics/volume_baseline.py`): relative
  volume (`relative_volume`/`relative_volume_1m`/`relative_volume_5m`)
  needs a "what's typical for this symbol at this point in the session"
  reference to compare against; without one, it silently falls back to a
  neutral default and its Momentum Ignition Score components always read a
  flat 0 -- true before this existed, regardless of session. Built from the
  SAME raw bars already fetched for the resistance volume profile above (no
  extra network call), deliberately from Webull's own historical bars
  rather than accumulated from this bot's own tick history over time: a
  low-float mover is very often a symbol the bot has never watched before,
  and a baseline that only builds up after weeks of running would have
  nothing to compare against on exactly the day it matters most. Tracks
  three independently-reset cumulative curves -- pre-market, regular
  session, and after-hours -- since cumulative volume itself resets at
  each of those boundaries (see "Extended-hours pricing and volume" above);
  a live tick only ever compares against its own phase's curve, bucketed
  in 5-minute increments and averaged across every historical day the
  fetched bars cover, excluding today itself so a still-forming day never
  leaks into its own baseline. Computed once at discovery (not refreshed
  intraday, unlike resistance) since it only reflects days before today,
  which don't change once the day is over. See `docs/ARCHITECTURE.md`'s
  "RVOL historical baseline" section for the full design.
- **Seeded rolling history at discovery** (`metrics/rolling.seed_history_from_bars`):
  a low-float mover is discovered *because* it already made a move big
  enough to surface on a screener -- the move structurally happens before
  discovery, not after. `CandidateWatcher`'s rolling tick history used to
  start empty at discovery, so every window-diffed metric (float velocity,
  volume/dollar-volume acceleration, price acceleration, short-term
  relative volume) read 0 for several real minutes after discovery, blind
  to a move that may have already happened, while the cumulative-total
  metrics (relative volume, float turnover, breakout proximity) correctly
  showed the name already maxed out -- understating a candidate that's
  actually already extremely hot (confirmed against a real case: a
  low-float name already up over 100% in pre-market on heavy volume scored
  only 40.4, just barely HEATING_UP). Now backfills that rolling history
  from the SAME raw bars already fetched for resistance/the RVOL baseline
  above (no extra network call), anchored so the seeded cumulative-volume
  series lines up exactly with the live feed's real total -- no artificial
  jump when live ticks start arriving. See `docs/ARCHITECTURE.md`'s
  "Seeding rolling history at discovery" section for the full design.
- **Dashboard** (`dashboard/app.py` + `scripts/run_dashboard.py`): a FastAPI
  backend + self-contained HTML/JS frontend, run with
  `python scripts/run_dashboard.py`. Live panels (candidates, open
  positions with unrealized P&L, risk events, kill-switch state) read
  directly off the running `TradingLoop`/`RiskEngine` in-process -- no DB
  round-trip, so they reflect the current process exactly. Historical
  panels (trade history, win rate/P&L performance, and a Momentum Ignition
  Score weighting breakdown + per-symbol score history lookup for
  sanity-checking `scoring/weights.yaml` against real data) read from the
  database. The score breakdown panel defaults to a historical average
  across all candidates, but clicking a row in the live Candidates panel
  switches it to that exact candidate's own live component breakdown (no
  averaging) -- click the same row again, or the "show all candidates"
  link, to go back. The **Ticker Scanner** panel runs any entered symbol
  through `BroadScanner`'s structural gates (price range, free float,
  volume floor) on demand and adds it to the live candidate list if it
  passes, instead of waiting for the next full universe rescan (which can
  take many minutes) -- backed by `TradingLoop.scan_and_add_candidate` and
  `BroadScanner.check_symbol_verbose`, which explains *why* a rejected
  symbol was rejected rather than just dropping it silently the way the
  bulk per-cycle scan path does. An already-tracked symbol is shown as-is
  (its real current state) rather than being re-scanned. A **Chart** panel
  above Candidates embeds TradingView's Advanced Real-Time Chart widget for
  whichever candidate row is currently selected, collapsed by default
  behind a "Show Chart" toggle so the third-party widget (a live connection
  to TradingView's servers) isn't loaded on every dashboard visit --
  collapsing it again fully tears the widget down rather than just hiding
  it. Re-renders only when the selected symbol actually changes, not on
  the normal 5s poll cycle. The symbol is passed to TradingView as a bare
  ticker (e.g. `AAPL`, no exchange prefix) since `Candidate` doesn't track
  which exchange a symbol lists on; TradingView resolves that to the
  primary listing on its own for the NASDAQ/NYSE/AMEX names this bot
  trades, unconfirmed for anything OTC-only or otherwise ambiguous.
- **Full DB persistence** (`db/repository.py`, wired through `TradingLoop`'s
  `on_trade_closed` / `on_order_update` / `on_state_transition` /
  `on_score_computed` hooks and an optional `momentum_event_tracker`
  collaborator): trades, order status changes, candidate state transitions
  (`scanner_events`), every computed Momentum Ignition Score
  (`momentum_scores`), and momentum events -- traded AND non-traded, with
  forward-looking 30s-15m outcome windows -- via `DBBackedEventRecorder`
  (`momentum_events`). Previously nothing in the live loop persisted
  anywhere; `scripts/run_dashboard.py` wires all of it. Fixed a real bug
  found while wiring this up: `MomentumEventTracker._finalize()` (which
  sets the final CONTINUED/FAILED/CHOPPY label) used to run *after* the
  last `recorder.update()` call for an event, so that label was computed
  but never actually included in what got persisted -- invisible before
  since `update()` was a no-op with no real recorder behind it.
- SQLAlchemy schema for Postgres/Supabase covering the tables in the
  project outline

What's still a skeleton or unverified (explicitly, not silently):

- `brokers/webull/client.py` streaming -- MQTT-based `DataStreamingClient`
  needs a confirmed sandbox `mqtt_host`; only the production host
  (`data-api.webull.com`) is documented. Poll `get_snapshot()`/`get_bars()`
  until this is confirmed.
- A few Webull response shapes are best-effort, not verified live: populated
  `get_account_position()` rows (sandbox account had zero positions), a
  successful `place_order()` response body (every live attempt was
  correctly rejected for being outside market hours -- weekend testing),
  and `get_order_executions()` fill rows. Re-verify during market hours
  with an actual position open.
- `data/float_providers/massive.py` -- unused now that FMP is wired up; kept
  only as an alternate skeleton
- Level 2 / order-flow features
- Alembic migrations (use `scripts/init_db.py` for now)
- `market_observations` (raw-ish sampled quote features, distinct from the
  derived `momentum_scores`/`momentum_events` tables that already persist)
  is still unused -- the schema exists but nothing writes to it yet.
- No throttling on `momentum_scores` writes -- one row is written per tick
  per actively-watched candidate, which is intentional (dense history for
  offline MIS-formula comparison) but can grow the table quickly with many
  candidates and a short poll interval. Add sampling if that becomes a
  problem for your database.

## Safety model

Live trading is refused unless **all three** are true:

1. `TRADING_MODE=live`
2. `LIVE_TRADING_ENABLED=true`
3. `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING_RISK`

This is checked in three independent places: `Settings.require_non_live_or_authorized()`
(called by the broker factory and `main.py`), `WebullBrokerClient.__init__`,
and again by `OrderManager` before every order. See `src/webull_bot/config.py`.

**Kill switch**: the dashboard's header has a "Safety" button (top right)
that toggles `RiskEngine`'s kill switch, gated behind a confirmation
dialog. Engaging it blocks every new trade instantly and force-closes all
currently open positions at market -- the position-closing runs on the
trading loop's own thread within one poll cycle (not synchronously in the
browser request), so there's a few seconds of latency for that half, but
new-entry blocking is immediate. Disengaging just resumes normal trading;
it never touches positions on the way out. See `docs/ARCHITECTURE.md`'s
"Safety" section for the full mechanics.

`TRADING_MODE=sandbox` now runs end-to-end against a real Webull sandbox
account (fake money) -- verified live for account balance/positions,
snapshots, historical bars, and order submission (rejected only for being
outside market hours during testing, which confirmed the request schema is
correct). **Important:** a Webull app key/secret is tied to one specific
environment -- a production-issued key will authenticate fine but gets
silently rejected by the sandbox host with a generic "invalid credentials"
error, and vice versa. If sandbox auth ever starts failing, check that
first before assuming the code broke. `TRADING_MODE=paper` remains fully
local/synthetic with zero external calls.

## Setup

```bash
cd webull-momentum-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in FMP_API_KEY now; Webull creds once you have them
pytest -q
```

`FMP_API_KEY` (Financial Modeling Prep) is required for real free-float
data; without it, `get_float_provider()` raises rather than silently
falling back to fake data. Get a key at financialmodelingprep.com. A free
Yahoo Finance fallback (via `yfinance`, no key needed) is wired in
automatically behind FMP -- see `ENABLE_YFINANCE_FALLBACK` in
`.env.example` if you want to turn it off.

Create the database schema once you have a Postgres/Supabase connection
string in `DATABASE_URL`:

```bash
python scripts/init_db.py
```

Run the bot without a UI (defaults to whatever `TRADING_MODE` is set to in `.env`):

```bash
python -m webull_bot.main
```

Or run it with the dashboard (same run loop, plus a web UI + DB persistence
of trades/orders as they happen):

```bash
python scripts/run_dashboard.py   # http://127.0.0.1:8000
```

In `sandbox` mode this polls the real Webull sandbox account and screener
on a timer (`Ctrl+C` to stop) -- fake money only, per the safety model
above. In `paper` mode there's no live market data source, so it polls a
placeholder watchlist (`main.py`'s `_PAPER_MODE_PLACEHOLDER_WATCHLIST`)
that won't do anything useful until you feed it snapshots yourself via
`PaperBrokerClient.feed_snapshot()`.

## Development order

Following the project outline: architecture -> Webull integration ->
streaming -> float data -> database -> calculations -> scanner -> MIS ->
state machine -> backtesting -> risk engine -> paper execution -> position
management -> dashboard -> data collection at scale -> strategy
optimization -> L2/order-flow -> production-readiness testing.

This repo currently covers architecture, database, calculations, scanner,
MIS, state machine, backtesting, risk engine, position management, real
free-float data (FMP), a real Webull sandbox connection (account, market
data, order submission), a production poll-based run-loop, DB persistence
of trades/orders, and a dashboard. Next up: confirm the sandbox streaming
host so the loop can react to pushed ticks instead of polling, re-verify
the still-best-effort response shapes during market hours with a real
filled order, and expand persistence to scanner events/momentum
scores/momentum events for large-scale data collection.
