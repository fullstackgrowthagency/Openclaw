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
  `OrderManager` never routes exits through `evaluate()` at all. Any
  position still open is now automatically flattened shortly before core
  hours end (`TradingLoop`'s end-of-day auto-flatten, distinct from the
  manual kill switch -- it never halts the *next* day's trading, it just
  closes out today's) -- see `docs/ARCHITECTURE.md`'s "Core trading hours
  gate" and "End-of-day auto-flatten" sections.
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
  adopted position, it's given a flat one instead of being left
  unprotected: `stop_price = current_price -+ stop_loss_pct%` (long/short),
  with `target_price` derived from `min_risk_reward_ratio` the same way a
  fresh signal would. This briefly (for one turn) tried to be
  equity/quantity-based instead -- solving for the stop that would make the
  position's already-fixed share count risk exactly a configured % of
  account equity -- but that only made sense while the underlying config
  field (`risk_per_trade_pct`) meant "% of equity to risk on this trade."
  Once that field was renamed to `stop_loss_pct` and repurposed to mean a
  genuine per-position stop distance (see "Risk sizing" below), the
  equity-based version would have quietly started computing the wrong
  thing, so it was simplified back to the flat form above -- no
  `get_account_equity()` call and no degenerate-distance fallback needed at
  all now, since a flat % is well-defined regardless of share count.
  Tagged with `strategy_name="reconciled_at_startup"` so it's
  always distinguishable from a real signal-driven entry in the trade
  history. See `docs/ARCHITECTURE.md`'s "Risk sizing" section for the full
  formula. **Adoption always
  builds a fresh `Candidate` instead of advancing an existing one** -- an
  earlier version tried a direct single-hop jump to `MANAGING`, which is
  only ever legal from `ENTERED`; a candidate stuck in `TRIGGERED` (exactly
  the state adoption exists to fix) made that jump raise
  `InvalidStateTransition` and silently abort reconciliation for every
  other symbol in that pass too -- confirmed live as the reason candidates
  stayed stuck in `TRIGGERED` indefinitely. See `docs/ARCHITECTURE.md`'s
  "Adoption always rebuilds a fresh Candidate" note for the full fix.
- **Stops/targets are now broker-side (resting orders), not purely
  software-polled -- added 2026-08-11.** A software-only stop only gets
  enforced when this process is alive, awake, and error-free at the exact
  moment price crosses it -- confirmed as a real gap the same day a
  position (RDGT) sat well past its stop with a five-figure unrealized
  loss because the software-side exit submission silently failed with no
  retry. Live-verified that Webull's OpenAPI supports attaching a real
  `OCO` stop+target bracket to a position that's already open (no need to
  rearchitect the entry-fill pipeline itself), so `TradingLoop` now does
  exactly that right after every entry fill: a resting `STOP` order
  protects the full position and a resting `LIMIT` order (half the
  quantity, mirroring the existing partial-exit design) banks the first
  target hit, both enforced by Webull directly rather than by this
  process noticing a price cross on its own polling cadence. Breakeven/
  trailing-stop math, VWAP-failure, and the time limit all keep working
  exactly as before (see the position manager bullet below) -- VWAP/time
  aren't expressible as resting orders so this loop still submits those
  itself, and a stop-price change from breakeven/trailing gets pushed to
  the broker via cancel-then-place-again (`modify_order`'s effect on a
  resting order's price was live-tested and found inconclusive, so this
  never relies on it). Falls back automatically and PERMANENTLY to the
  pre-existing pure-software behavior only when the broker doesn't
  support resting orders at all (`PaperBrokerClient`, backtests). A
  placement call simply failing (rate limit, network error, anything) is
  only ever a TEMPORARY fallback (extended 2026-08-11): the position
  rides on software-only checks for a few ticks while
  `_sync_broker_protective_orders` retries attaching a real broker-side
  bracket every tick (~`poll_interval_seconds` apart, at `CRITICAL`
  rate-limiter priority, same as every other order call) until it
  actually succeeds -- giving up permanently after one failed call was
  exactly the gap the RDGT incident exposed in the first place. See
  `docs/ARCHITECTURE.md`'s "Broker-side (resting) stop/target management"
  section (especially "Retrying a failed attach") for the full lifecycle.
- **The post-partial-exit stop is now a native broker-side `TRAILING_STOP`
  order, not a plain `STOP` this process keeps cancelling and
  re-placing -- added 2026-08-11.** Once a position has taken its one
  partial exit (target hit, half sold), `_attach_broker_bracket` now
  places a real `TRAILING_STOP_LOSS` order (Webull trails it itself, via
  `trailing_type`/`trailing_stop_step`) instead of a plain `STOP` that
  `_sync_broker_protective_orders` would otherwise cancel+replace every
  time `PositionManager`'s own trailing-stop math ratchets it. Wired in at
  the account owner's explicit instruction that this order type is
  supported for US equities on this account (the SDK's own sample only
  demonstrates `TRAILING_STOP_LOSS` against the HK market) -- **confirmed
  working live in this account's real trading, 2026-08-12.** Unaffected: the pre-partial stop+target bracket (still plain
  `STOP`+`LIMIT`), and a too-small-to-split position that never takes a
  partial (rides on a plain `STOP` + breakeven for its whole lifetime, as
  before). Defensively cancels any leftover resting order before adding
  the trailing stop, since a `TRAILING_STOP` can't be added while another
  resting sell order still reserves the same shares. See
  `docs/ARCHITECTURE.md`'s "Broker-side (resting) stop/target management"
  section for the full lifecycle.
- **A `TRIGGERED` entry gets a second, independent fill check -- added
  2026-08-11.** Order-status polling (`get_order_status`) was the only way
  this loop noticed an entry had filled, and this project has already
  found that endpoint's populated-response field mapping unverified in
  this exact spot, plus a real incident where `get_positions()` (a
  different endpoint) silently lost a fill to a field-name mismatch. Now,
  ~10 seconds after an entry order is submitted
  (`TradingLoopConfig.entry_position_verify_delay_seconds`), if
  `get_order_status` still hasn't reported a terminal status (or failed
  outright), `_poll_pending_entry` also queries `broker.get_positions()`
  directly and self-heals into a tracked position immediately if Webull
  already shows one open -- see `docs/ARCHITECTURE.md`'s "Extra
  position-based confirmation for a TRIGGERED entry" section.
- **Performance/rate-limit rehaul -- 2026-08-11.** Every Webull endpoint
  this bot calls shares one real, measured ~1 req/s account-wide budget --
  but only `market_data.*` calls were ever paced or retried; `place_order`/
  `cancel_order`/`get_order_status`/`get_positions`/`get_account_balance`
  had zero protection despite drawing from the same budget. Every Webull
  call now goes through one shared, **priority-aware** rate limiter
  (`CallPriority.CRITICAL/NORMAL/BACKGROUND` -- exit/stop-loss management
  always wins contention over discovery/resistance-refresh traffic instead
  of queuing behind it). Also added: hysteresis on the broker-side stop
  cancel+replace (a fast-moving trailing stop no longer hammers the API on
  every tick for sub-0.25% changes), batched broker-bracket status polling
  (`list_open_orders` -- one call covers every resting order instead of up
  to 2 `get_order_status` calls per managed position per tick), and a
  per-tick `get_positions()` cache (several candidates needing it in the
  same pass now share one real call). See `docs/ARCHITECTURE.md`'s
  "Performance/rate-limit rehaul" section for the full breakdown.
- **Streaming market data -- confirmed live and wired into production,
  now covering pre-entry monitoring too (2026-08-11).**
  `WebullBrokerClient.subscribe_quotes` is a real implementation, not a
  stub: it connects to the confirmed sandbox MQTT host
  **`data-api.sandbox.webull.com`** (found by ruling out a timing-race
  theory and a cross-environment host mismatch -- see
  `docs/ARCHITECTURE.md`'s "Streaming market data" section for the full
  writeup) and subscribes to **both** the `SNAPSHOT` sub_type (price,
  OHLC, volume, extended-hours variants) and the `QUOTE` sub_type
  (top-of-book bid/ask), merging the latest of each into one complete
  snapshot per symbol before it ever reaches `TradingLoop` -- a symbol
  with only one of the two cached never gets pushed at all, so a caller
  can never see a fabricated `bid=0`/`ask=0` (which would otherwise read
  as a fake zero spread to the entry-eligibility gate). `TradingLoop`
  uses the merged stream for every pre-entry and exit-management state --
  `WATCHING`/`HEATING_UP`/`ARMED` as well as `ENTERED`/`MANAGING` -- with
  `DISCOVERED`/`TRIGGERED` excluded. A position's symbol is subscribed
  the moment its broker-side bracket is attached; a watch-stage
  candidate's symbol is (re-)subscribed once per tick (cheap once already
  requested). Either way, it falls back to REST polling automatically if
  nothing has streamed in the last 10s, and is excluded from that tick's
  batched REST `get_snapshots` call once it's actively streaming. Known
  open tradeoff: there's still no unsubscribe path for a symbol that
  leaves every streaming-eligible state, so subscriptions only grow for
  the life of the process -- now covering a much larger population than
  just open positions, whether Webull's subscription count/rate has a
  practical ceiling this could approach is not yet confirmed. One other
  still-open loose end from the original verification: a "Protocol not
  supported" MQTT error observed during the verify script's own shutdown
  sequence on every test run, not yet understood.
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
- **`support_trading_session` defaults to `"CORE"` -- `"ALL"` was tried and directly
  confirmed live to be rejected outright** (`OAUTH_OPENAPI_PARAM_ERR`,
  HTTP 417, "invalid support_trading_session, value: ALL"), despite being a
  documented value in Webull's own public API docs -- a live rejection
  overrides documentation, so this was reverted the moment it was observed,
  not left in place pending further research. `"ALL"` had briefly replaced
  `"CORE"` mid-session on a since-corrected diagnosis of a
  buying-power-reserved-with-no-position report (the actual cause,
  confirmed directly by the user, was the entry firing *during* core hours
  with the fill going untracked -- see the position-tracking bullet above).
  A `"CORE"`-flagged order fired right at the 4:00pm ET close turned out to
  matter after all, just for the auto-flatten's own exit order rather than
  entries -- observed live 2026-08-11 that a position still open at the
  close never actually flattened. Rather than chase down the exact
  rejection (the leading diagnosis: that order needs a still-live CORE
  session it no longer has by then, not independently confirmed via a
  captured error), the auto-flatten now fires 2 minutes *before* the close
  instead (see the "End-of-day auto-flatten" bullet above) -- see
  `docs/ARCHITECTURE.md`'s "Webull integration" section for the full
  history.
- **Extended-hours (pre-market/after-hours) trading -- LIMIT orders only,
  software-managed positions, core hours unchanged.** `"ALL"` was
  confirmed live to work for `support_trading_session` (2026-08-12,
  sandbox, reversing the 2026-08-10 rejection -- likely an account
  entitlement enabled in between), but a same-morning follow-up found
  it's not that simple: a real resting OCO stop+target bracket (its
  `STOP_LOSS` leg) was rejected pre-market with the EXACT same error as
  the original 2026-08-10 finding, even though a plain LIMIT order tested
  clean 49 minutes earlier. Working theory (the user's diagnosis, fits
  both observations): Webull only accepts **LIMIT orders** outside core
  hours -- a common brokerage restriction -- so `MARKET` and `STOP_LOSS`
  order types are rejected regardless of `support_trading_session`.
  Compounding it: the bracket-attach retry loop (see "Broker-side resting
  stop/target management" below) kept re-attempting and re-failing that
  exact call every ~5s, burning rate-limiter budget and starving
  candidate discovery behind it.

  **Resulting design:** `RiskConfig.allow_extended_hours_trading`
  (dashboard-adjustable, **off by default**) still gates whether a signal
  is allowed outside 9:30am-4:00pm ET at all. Once let through,
  `OrderManager` now places a **marketable LIMIT** order instead of
  MARKET for both entries and exits outside core hours (priced
  `OrderManager.EXTENDED_HOURS_LIMIT_BUFFER_PCT`, 0.5% default, through
  the current bid/ask), and **no broker-side resting stop/target bracket
  is attempted at all** outside core hours -- the position is protected
  purely by `PositionManager`'s existing software-side stop/target/VWAP-
  failure/time-limit checks instead, same fallback path used for any
  broker without resting-order support. The moment core hours resume,
  broker-side brackets resume normally. Core-hours behavior (MARKET
  orders, broker-side OCO brackets) is completely unchanged.
  `Settings.webull_support_trading_session` (env var
  `WEBULL_SUPPORT_TRADING_SESSION`, still defaults to `"CORE"` in code)
  must be set to `"ALL"` for extended-hours orders to go out at all.
  **Important caveat:** only verified in `TRADING_MODE=sandbox` -- re-verify
  before assuming a live account has the same entitlement.
- **`support_trading_session="ALL"` is only accepted OUTSIDE core hours --
  a `WEBULL_SUPPORT_TRADING_SESSION=ALL` order now automatically downgrades
  to `"CORE"` for any order submitted during core hours.** Confirmed live
  2026-08-12 (~9:49am ET, right after a sandbox account reset, genuinely
  inside core hours): with `WEBULL_SUPPORT_TRADING_SESSION=ALL` deployed,
  every entry order failed with the exact same `OAUTH_OPENAPI_PARAM_ERR`
  (HTTP 417, "invalid support_trading_session, value: ALL") as the original
  2026-08-10 finding above -- but the same value was independently
  confirmed working for a pre-market order that same day. So `"ALL"`
  isn't a blanket account entitlement toggle after all -- it's a
  session-scoped value that Webull only accepts outside core hours, and a
  deployment left at `"ALL"` all day would fail every single core-hours
  entry/exit. `WebullBrokerClient._order_payload` now computes the actual
  `support_trading_session` sent per order from `order.created_at` via
  `is_within_core_trading_hours`, forcing `"CORE"` whenever the order is
  submitted during core hours regardless of the configured value, and only
  using the configured value (typically `"ALL"`) outside core hours where
  it's actually accepted -- so a deployment can leave
  `WEBULL_SUPPORT_TRADING_SESSION=ALL` set permanently and get correct
  behavior in both windows, rather than needing a manual `.env` flip and
  restart at each session boundary.
- **Position sizing is clamped to Webull's hard 200,000-share-per-order
  ceiling.** Confirmed live 2026-08-12: a cheap/penny-priced signal (DOGZ)
  combined with `max_position_size_pct=100.0` and a large buying-power
  balance computed a share count Webull itself rejected outright
  (`OAUTH_OPENAPI_ORDER_QUANTITY_EXCEED_LIMIT`, HTTP 417, "Order quantity
  must be below 200,000") -- every entry attempt on the symbol then failed
  and reverted to `ARMED`, never actually opening a position, no matter how
  many times it re-triggered. `RiskEngine.evaluate`'s sizing step now
  clamps `max_shares` to this ceiling unconditionally, independent of
  whatever `max_position_size_pct` is configured to -- a broker-side
  constraint the bot should never depend on risk-settings tuning alone to
  avoid.
- **Every order price is rounded to a valid tick size before being sent to
  Webull.** Confirmed live 2026-08-12: a resting OCO bracket's target/LIMIT
  leg for BIVI was rejected
  (`OAUTH_OPENAPI_STOCK_ORDER_PRICE_PRECISION_EXCEED`, HTTP 417, "Price
  increment should be 0.01 when price is equal to or greater than
  0.9999") because `target_price` -- computed as `entry_price +
  risk_per_share * reward_risk_ratio` by every strategy, none of which
  round the result -- came out as `3.4667600000000003`, an unrounded
  float with far more than 2 decimal digits. The position still opened
  fine (a plain `MARKET` entry has no price to round), but every
  subsequent attempt to attach its broker-side stop+target bracket failed
  outright, leaving it on software-only management indefinitely.
  `WebullBrokerClient._order_payload` now rounds both `limit_price` and
  `stop_price` (`_round_to_valid_price_increment`: 2 decimals at/above
  $1, 4 decimals below, matching the standard SEC Rule 612 sub-penny
  convention Webull's own message implies) at the single point every
  order's price passes through before serialization -- catching this for
  every price-computing call site (strategy target math, position stop
  math, extended-hours marketable-limit pricing) rather than requiring
  each one to remember to round itself.
- **A single reconcile pass can no longer abandon a still-open position.**
  Confirmed live 2026-08-12: `reconcile_positions_from_broker` (runs every
  `position_reconcile_interval_seconds`, 30s default) dropped a genuinely
  open position (BIVI) from local tracking and pushed its candidate
  straight to `COOLDOWN` -- ending ALL further management, software-side
  included -- after a SINGLE pass came back without it in
  `broker.get_positions()`'s response, immediately following a 429 on an
  unrelated call in the same tick. `get_positions()` itself never raised
  (that failure mode was already handled); it returned an ordinary 200
  whose body simply didn't include a position this bot's own fill records
  confirmed was still open. A live account under the kind of sustained
  rate-limit contention this bot can generate isn't guaranteed to return a
  complete positions list on every single request just because it
  responded 200. `reconcile_positions_from_broker` now requires a symbol
  to be missing across `TradingLoopConfig.position_missing_confirmations_
  required` (2 default) CONSECUTIVE passes before treating it as closed
  externally, via a new `self._missing_from_broker_counts` streak counter
  that resets the moment the symbol reappears in any pass. Trades a little
  detection latency for a genuine external close (up to one extra
  `position_reconcile_interval_seconds`) for never again silently walking
  away from an open, unprotected position on one flaky poll.
- **An externally-closed position now gets a `Trade` record too, not just
  removal from the dashboard.** Confirmed live 2026-08-12: BIVI was
  correctly detected as closed (once confirmed missing across two
  reconcile passes, per the fix above) and disappeared from the Open
  Positions table -- but never showed up in Trade History or Performance,
  because `record_trade()`/`on_trade_closed` were only ever called from
  `_finalize_exit`'s own internal fill-confirmation path, never from
  `reconcile_positions_from_broker`'s drop branch. A new
  `TradingLoop._build_trade_for_external_close` builds a best-effort
  `Trade` for this case: it first tries `broker.poll_fills()` for a real
  matching exit-side fill (almost certainly the actual closing trade),
  falling back to the position's own stop/target/entry price -- same
  fallback chain `_build_trade_from_fill` already uses -- when no fill is
  found. Tagged with a new `ExitReason.EXTERNAL_CLOSE` so it's
  distinguishable in history from a close this process actually executed
  itself. **This is still an approximation, not a confirmed fill record.**
  A more accurate fix -- backfilling `trades` directly from Webull's own
  order-history endpoint (`order_v3.get_order_history`), which would also
  recover trades that already slipped through before this fix existed --
  was explored the same day but not yet completed: a live sandbox query
  came back an empty list despite orders known to exist for the account,
  which needs to be understood (a request-parameter issue vs. a genuine
  sandbox limitation on that endpoint) before it can be relied on. See
  `docs/ARCHITECTURE.md`'s "Webull integration" section for the current
  state of that investigation.
- **The false external-close drop above went on to cause a real duplicate
  entry before its fix was deployed -- a new broker-side check now guards
  against re-entering a symbol independent of local tracking entirely.**
  Confirmed live: before the reconcile-debounce fix landed, BIVI was
  wrongly dropped, its candidate cycled `COOLDOWN -> WATCHING` once the
  cooldown timer expired, and the bot fired a genuine SECOND entry on a
  symbol that was never actually closed at the broker -- ballooning the
  position to Webull's own 200,000-share order ceiling and a roughly
  **$250,000 unrealized loss** before anyone noticed, since nothing in
  this codebase's own local state thought there was already a position
  open. `TradingLoop._submit_entry` now calls `broker.get_positions()`
  directly (a fresh, uncached call -- deliberately NOT the same
  `_tick_positions_cache` reconcile uses, since populating that cache
  before the entry's own fill would poison it for other same-tick callers
  needing to see the fresh position) immediately before any new entry
  order goes out, and refuses the entry outright if the broker already
  reports a nonzero-quantity position for that exact symbol -- reverting
  to `ARMED` instead. This is deliberately independent of
  `self._positions`/the candidate's own state: it exists specifically so
  a *different* future local-tracking bug can't reproduce this same
  failure mode. A `get_positions()` failure during this check doesn't
  block the entry (logged and proceeds) -- this is defense-in-depth on
  top of `RiskEngine`'s own gating, not a replacement for it, and
  shouldn't turn a transient broker hiccup into a missed legitimate
  entry.
- **The dashboard could 504 under real trading load -- `/api/status` and
  `/api/positions` no longer touch the broker at all.** Real incident
  reported by the user (2026-08-12): `GET /api/status` and `GET
  /api/positions` intermittently returned HTTP 504 from nginx. Root
  cause: both endpoints called the live Webull broker synchronously, on
  EVERY single HTTP request -- `/api/status` via
  `broker.get_account_equity()`/`get_buying_power()`, `/api/positions`
  via one `broker.get_snapshot()` call per open position, sequentially --
  through the exact same shared, priority-queued `webull_limiter`
  (`retry.py`) that order placement uses, including its `exclusive()`
  hold during a real `place_order`/`place_oco_bracket` call (see the
  "exclusive access to the rate-limit budget" entry above), where every
  OTHER thread's call is fully blocked, not just deprioritized. Under
  real trading load -- many candidates/positions, several entries/exits
  in flight, or a stuck retry -- these NORMAL-priority dashboard reads
  could queue behind tens of seconds of CRITICAL trading traffic (a real
  20+ second CRITICAL-vs-CRITICAL wait is already documented elsewhere in
  this codebase's incident history), comfortably exceeding nginx's
  `proxy_read_timeout` and producing a 504 on what should be a cheap
  read. Since the dashboard frontend polls both endpoints every 5
  seconds, this wasn't a rare edge case -- it was exposed on essentially
  every refresh cycle during exactly the periods when the bot is doing
  the most (i.e., when a user most wants the dashboard to be responsive).
  **Fix:** both endpoints now read from small in-memory caches on
  `TradingLoop` instead, populated as a side effect of work the main loop
  is already doing every tick -- zero new broker calls, and these two
  endpoints can no longer contend for the rate limiter at all:
  - `TradingLoop.get_last_known_price(symbol)` (`/api/positions`) reads
    `self._last_known_snapshots`, populated by `_manage_position` from
    the snapshot it already fetches every tick for that position's own
    stop/target check (streaming, batched REST, or a per-candidate
    fallback -- whichever `_process_candidate_inner` resolved this tick).
    Returns `None` (same as the old code's exception fallback) if a
    position hasn't had a tick processed yet.
  - `TradingLoop.get_account_summary()` (`/api/status`) reads
    `self._cached_equity`/`self._cached_buying_power`, refreshed in the
    background by `_process_all_candidates` on its own schedule
    (`TradingLoopConfig.account_summary_refresh_interval_seconds`, 30s
    default -- same throttle pattern as `position_reconcile_interval_seconds`),
    not by the request thread. This also cuts total equity/buying-power
    call volume: previously 1 call pair per dashboard poll (every 5s, ×N
    concurrent browser tabs); now capped at 1 pair per 30s regardless of
    how many dashboard clients are watching.

  Both caches are read-only from the dashboard's perspective -- nothing
  about order placement, risk sizing, or `OrderManager.submit_signal`'s
  own (still fully live, still fully correct) equity/buying-power reads
  changed. See `tests/test_trading_loop.py`'s
  `test_manage_position_caches_the_ticks_price_for_the_dashboard_to_read`,
  `test_get_account_summary_is_populated_by_the_periodic_background_refresh`,
  `test_get_account_summary_refresh_is_throttled_to_the_configured_interval`,
  `test_get_account_summary_reports_the_error_when_the_broker_refresh_fails`,
  and `tests/test_dashboard.py`'s updated `/api/status`/`/api/positions`
  tests.
- **`POST /api/scan-symbol` hardened the same day, same underlying
  exposure.** It still calls the broker live (there's no sensible cached
  value for an arbitrary just-typed symbol), but now bounds the wait
  with a hard deadline instead of blocking indefinitely: the call runs in
  a small dedicated `_scan_symbol_executor`
  (`ThreadPoolExecutor(max_workers=4)`) and the request thread waits at
  most `_SCAN_SYMBOL_TIMEOUT_SECONDS` (12s default) for it, returning
  `"state": "pending"` if that deadline passes rather than hanging until
  nginx would kill it. The scan itself isn't cancelled -- it keeps
  running in the background and still adds the candidate (picked up by
  the next `/api/candidates` poll) if it eventually succeeds. See
  `tests/test_dashboard.py::test_scan_symbol_returns_pending_when_the_broker_call_is_too_slow`
  and `docs/ARCHITECTURE.md`'s "Dashboard 504s" section for the fuller
  writeup and other optimization options considered (including why
  batching wasn't the right tool here, and nginx's `proxy_read_timeout`
  as a secondary, infra-level mitigation).
- **`_build_trade_for_external_close`'s exit-price fallback chain used to
  land on `avg_entry_price` far too easily, fabricating an exact $0.00
  P&L.** Confirmed live 2026-08-12 via a dashboard screenshot: WCT closed
  with neither a matched `poll_fills` result nor a `stop_price`/
  `target_price` set (both `None` by the time the position was confirmed
  externally closed), so the chain fell all the way to
  `position.avg_entry_price` -- recording entry=exit=1.04, an exact
  $0.00/0.00% trade regardless of what actually happened to the position.
  This wasn't a display/formatting bug (negative P&L already renders
  correctly elsewhere, e.g. a real -$55,979.72 loss shown in red) -- the
  fallback price itself was wrong. First fixed by inserting a fresh
  `broker.get_snapshot(symbol).last_price` REST call ahead of the
  `avg_entry_price` last resort -- **reverted the same day** after the
  user reported it was contributing to renewed rate-limit pressure: that
  call runs synchronously inside `reconcile_positions_from_broker`'s drop
  loop (one call per externally-closed symbol found in a single pass,
  each with `call_with_retry`'s own up to 4 paced attempts on a 429) at
  exactly the moments this codebase has repeatedly seen sustained
  rate-limit contention already in progress -- see the CYCU/SCKT/BIVI
  incidents below. **Fixed properly** by reading
  `self._get_streaming_snapshot(symbol, now)` instead -- the last live-
  STREAMED price for this symbol, already sitting in memory because
  MANAGING positions are streaming-subscribed for their own stop/target
  management anyway, so this costs no extra request at all. Only helps
  when streaming has a fresh price for that exact symbol (per
  `streaming_staleness_seconds`); `avg_entry_price` is still the final
  fallback when it doesn't, deliberately accepted rather than spending
  scarce account-wide request budget on a best-effort historical record.
  See `tests/test_trading_loop.py::test_reconcile_external_close_falls_back_to_the_streaming_cache_not_entry_price`
  and `::test_reconcile_external_close_falls_back_to_entry_price_when_streaming_cache_is_stale_or_empty`.
- **A stuck exit-order retry now backs off instead of hammering the
  broker every tick.** Real incident (CYCU/SCKT, 2026-08-12): a genuine
  stop-loss exit signal kept firing every `poll_interval_seconds` tick,
  and `broker.place_order` kept raising on sustained
  `TOO_MANY_REQUESTS` -- with no backoff of its own, `_manage_position`
  retried the exact same call again next tick regardless of how many
  times it had already failed, adding to (not easing) the very
  rate-limit contention blocking it, for two positions simultaneously,
  for many consecutive minutes, while the unrealized loss kept growing.
  `Position.exit_submission_failures`/`last_exit_submission_attempt_at`
  now drive an exponential backoff between retries
  (`exit_submission_backoff_base_seconds` \* 2^(failures-1), capped at
  `exit_submission_backoff_max_seconds` -- 5s/10s/20s/40s/60s(capped) by
  default) -- unlike `broker_bracket_attach_failures`, this never gives
  up entirely (an exit can't be allowed to just stop retrying), it only
  ever slows the retry cadence down. Resets to zero on the next
  successful submission.
- **Placing an order now gets exclusive access to the rate-limit budget,
  not just priority.** The CYCU/SCKT/BIVI incidents above all shared one
  root cause the user identified directly: `CallPriority.CRITICAL`
  already wins contention against `BACKGROUND` traffic, but does nothing
  when SEVERAL genuinely `CRITICAL` calls are simultaneously in flight (a
  stuck exit retry, a bracket-attach retry, `reconcile`'s
  `get_positions()`, ...) -- they still compete with each other for the
  same ~1 req/s account-wide ceiling. `RateLimiter.exclusive()`
  (`retry.py`) is a new, stronger mechanism for the single highest-stakes
  moment of all: while held by one thread, EVERY other thread's
  `wait()` call blocks outright, at any priority, until the holder is
  done -- so an order submission (including all of its own internal
  `call_with_retry` attempts) gets the account-wide budget entirely to
  itself instead of splitting it with concurrent discovery/reconcile/
  other-order traffic. `WebullBrokerClient.place_order` and
  `place_oco_bracket` (every code path that submits a new order --
  entries, exits, and broker-side brackets alike) now wrap their
  `call_with_retry` call in `webull_limiter.exclusive()`. Reentrant-safe
  for the holder's own thread (its own paced retries proceed normally);
  every other thread queues until it's released, even if it exits via an
  exception. **This also means orders for two or more different symbols
  can never be placed at the same time**, confirmed directly (not just
  at the abstract `RateLimiter` level) by
  `tests/test_webull_broker_client.py::test_place_order_never_overlaps_across_different_symbols`
  -- `webull_limiter` is one process-wide singleton, not scoped per
  symbol/client instance/thread, so any two `place_order` calls anywhere
  in the process serialize regardless of which stock they're for.
- **A position that's gone too long without a broker-side bracket now
  raises a visible alert instead of failing silently forever.** The user
  asked directly whether anything more could be done to guarantee a
  position is broker-managed every time during core hours.
  `_attach_broker_bracket`/`_sync_broker_protective_orders` already retry
  unconditionally, every tick, forever, at `CallPriority.CRITICAL` -- an
  audit confirmed that design is already about as strong as it can be
  made without reintroducing the reverted `broker_bracket_attach_failures`
  circuit breaker (see above), and found the honest limit isn't the retry
  logic, it's that a retry loop can only succeed against a call CAPABLE
  of succeeding. The initial pass at this audit flagged two
  `_order_payload` fields (`stop_price`'s field name, the entire
  `TRAILING_STOP_LOSS` order type) as still-UNVERIFIED per a stale code
  comment -- the account owner corrected this directly: both have
  already worked live in this account's real trading (in hindsight,
  `place_oco_bracket`'s own docstring already said `stop_price` was
  confirmed live 2026-08-11 via `scripts/verify_bracket_orders.py`; the
  comment on `_order_payload` itself just never got updated). Both
  comments in `client.py` are now corrected. **Fix, not a new retry
  mechanism -- kept anyway as general defense-in-depth:**
  `TradingLoop._maybe_raise_unprotected_position_alert` raises one
  `RiskEventType.POSITION_UNPROTECTED_TOO_LONG` event (new
  `RiskEngine.record_operational_event`, surfaced on the dashboard's
  existing Risk Events panel) once a `MANAGING` position has gone
  `unprotected_position_alert_seconds` (60s default) with no
  `broker_stop_order_id`. Not needed for the two fields above anymore,
  but still useful against any FUTURE structurally-broken payload (a
  Webull API change, a new order type added later without the same live
  verification) that would otherwise fail completely silently for a
  position's entire lifetime. Fires once per unprotected episode, resets
  the moment `_attach_broker_bracket` next succeeds. See
  `docs/ARCHITECTURE.md`'s "Visibility for a broker bracket that can
  never attach" section for the full audit, including one remaining open
  item: whether `market_hours.py`'s core-hours check is worth hardening
  against market holidays/early closes (currently a pure weekday+time-
  window check with no calendar awareness).
- **A stuck pending exit order could silently block all future
  protection forever -- fixed 2026-08-13, root-caused with the user
  directly.** Real sandbox incident: an exit order for a position well
  past its stop-loss got submitted and then simply never resolved --
  not filled, not rejected, not cancelled, not expired -- for hours.
  `_manage_position`'s very first check defers ENTIRELY to
  `_poll_pending_exit` while a symbol has a pending exit (`check_exit` is
  never called again for it), and `_poll_pending_exit`'s "still pending"
  branch used to be a bare `# else: pass` with zero logging -- so this
  failure mode produced no error, no warning, nothing to grep for. It
  also silently defeated the dashboard's manual "Close" button
  (`_close_all_positions_now` correctly skips a symbol already in
  `_pending_exit_orders`, assuming it's still genuinely in flight).
  Diagnosed live: confirmed the tick loop itself was healthy (the
  dashboard's "Last" price for the affected symbols was still updating)
  while zero exit-related log lines existed for those symbols at all.
  **Fix:** `TradingLoopConfig.pending_exit_stuck_timeout_seconds` (180s
  default) -- once a pending exit has been outstanding that long with no
  terminal status, it's cancelled, dropped from tracking, and a new
  `RiskEventType.PENDING_EXIT_ORDER_STUCK` event is raised (same
  dashboard-visible mechanism as the unprotected-position alert above),
  so the very next tick gets a completely fresh exit attempt instead of
  polling a dead order forever. See `docs/ARCHITECTURE.md`'s "A stuck
  pending exit order can silently block all future protection forever"
  section and `tests/test_trading_loop.py`'s
  `test_poll_pending_exit_cancels_and_drops_a_stuck_order_past_the_timeout`.
- **Streaming subscribe silently failed wholesale past 100 tracked
  symbols -- found and fixed alongside the above.** `subscribe_quotes`
  called Webull's streaming subscribe with every not-yet-subscribed
  symbol in ONE uncapped call; confirmed live that Webull rejects the
  WHOLE call with `TOO_MANY_SYMBOLS` ("Maximum number of symbols: 100")
  past that limit -- unlike `get_snapshots`' sibling REST endpoint, which
  already chunks correctly. Since symbols only get marked subscribed on
  success, the wholesale failure meant NONE of them ever did, so the
  exact same (or larger) list got retried every single tick, forever,
  burning real time and log volume on a call that could never succeed as
  written. Fixed with a new `_subscribe_symbols_in_batches` helper that
  chunks to `_STREAMING_SUBSCRIBE_BATCH_SIZE` (100), best-effort per
  chunk so one bad batch can't take down every other symbol in the same
  call. See `docs/ARCHITECTURE.md`'s "Streaming subscribe silently
  failed wholesale past 100 symbols" section.
- **Total P&L as a percentage, added to the Performance panel --
  2026-08-13, at the user's request.** The panel already showed
  `avg_pnl_pct`, an EQUAL-WEIGHTED average of each trade's own `pnl_pct`
  (a $100 trade and a $100,000 trade count the same). `get_performance_summary`
  (`db/repository.py`) now also returns `total_pnl_pct`: total dollar P&L
  as a percentage of total capital actually deployed (`Σ pnl / Σ
  (entry_price * quantity)` across every trade) -- a genuine
  capital-weighted "return on capital put to work" figure, where a large
  position's result dominates a small one's, same as it would in
  reality. The two figures can diverge significantly and both are shown
  side by side. Guards against a zero-cost-basis division (returns
  `0.0`). See `docs/ARCHITECTURE.md`'s "Two distinct 'P&L as a
  percentage' figures on the Performance panel" section and
  `tests/test_repository.py::test_get_performance_summary_total_pnl_pct_is_capital_weighted_not_averaged`.
- **Entry selectivity rework -- 2026-08-13, at the user's explicit request
  after reporting the bot was losing more trades than it won.** Two root
  causes confirmed against the actual code before building anything: (1)
  `TradingLoop` submitted an entry the instant ANY candidate triggered,
  with no comparison against other candidates competing for the same
  scarce position slot in the same tick -- first to trigger won, not the
  best one available; (2) every strategy (confirmed in
  `strategy/volume_ignition.py`) could fire off a single noisy snapshot,
  with nothing requiring the move to actually hold before an order went
  out. Four changes, built together:
  1. **Confirmation window** -- new `CandidateState.CONFIRMING` between
     `ARMED` and `TRIGGERED`. A strategy trigger no longer submits an
     order immediately; it has to hold above its reference price (within
     `TradingLoopConfig.confirmation_max_pullback_pct`, 0.5% default) for
     `confirmation_window_seconds` (10s default) with MIS still above the
     armed threshold, or it's rejected (`RiskEventType
     .CONFIRMATION_FAILED`) and must re-trigger fresh. Stop/target get
     recomputed from the actual confirmed price once the window holds
     clean, not the stale trigger-time price. Mirrored in
     `backtest/engine.py` too (not just live `TradingLoop`), since a
     backtest that doesn't wait the same way would stop predicting real
     behavior.
  2. **Resistance-runway / target-clearance** (added at the user's
     explicit request) -- reuses the existing volume-profile resistance
     infrastructure (see below), no new resistance engine. A confirmed
     entry is hard-rejected if a known static resistance level sits at or
     before the fixed target (`RiskEventType.RESISTANCE_BEFORE_TARGET`),
     or if it's already consumed more than
     `TradingLoopConfig.max_runway_consumed_pct` (40% default) of the
     room between the original trigger and that resistance level. No
     resistance found at all means automatically clear -- no invented
     arbitrary distance limit standing in for "we don't know."
  3. **`room_to_target_score`** (added at the user's explicit request) --
     new MIS component measuring room to the fixed target before a known
     resistance level. `weights.yaml` bumped to
     `v2.2-selectivity-rework`: every existing weight scaled by 0.93
     (preserving relative weighting) to free 7% for this one. Can be
     `None` (unavailable, not price-context-dependent callers) --
     `compute_score` renormalizes over the active weights rather than
     treating a missing component as a 0.
  4. **Batch ranking when slots are scarce** -- confirmed-and-cleared
     candidates queue onto a per-tick ready list instead of submitting
     immediately; once every candidate has had a chance to confirm this
     tick, the best-scoring ones (by current MIS) win the available
     position slots, not whichever confirmed first. Anyone who loses out
     isn't discarded -- they stay `CONFIRMING` and get re-ranked again
     next tick.
  Deliberately scoped down from a much larger proposed spec (full
  resistance strength-scoring/clustering, per-strategy setup/trigger/
  confirmation splitting across all 8 strategies, MIS persistence/decay
  detection, opportunity ranking via a full Entry Quality Score, shadow-
  mode rollout) -- this targets the two confirmed root causes plus the two
  pieces explicitly requested on top, not the whole thing at once. A
  backup branch (`backup/pre-selectivity-rework-2026-08-13`) preserves the
  exact commit before this rework, pushed to the remote, in case it needs
  reverting. See `docs/ARCHITECTURE.md`'s "Entry selectivity rework"
  section for the full design (including what was deliberately left out)
  and `tests/test_trading_loop.py`, `tests/test_state_machine.py`,
  `tests/test_volume_profile.py`, `tests/test_momentum_score.py` for the
  new coverage.
- **Atomic bracket entry -- 2026-08-13, at the user's explicit request,
  reversing a prior deliberate architectural decision.** Entry and stop/
  target used to be two separate broker calls: place the entry, wait for
  it to fill, then separately attach a resting stop+target bracket. That
  left a real unprotected window during core hours -- not a bounded few
  hundred milliseconds, but literally unbounded on failure
  (`_attach_broker_bracket` retries forever with no circuit breaker) and,
  outside core hours, the ENTIRE holding duration (the broker-side bracket
  is never even attempted there). User's own words after reviewing
  Webull's OpenAPI spec: "No Trade should be software managed during core
  hours... When a purchase is made it should include the stop loss and
  take profit order in it so its all executed at one." Now it is:
  `WebullBrokerClient.place_bracket_entry` submits the entry, stop-loss,
  and take-profit (half the entry quantity, mirroring the existing
  partial-exit design) as ONE atomic `MASTER`+`STOP_LOSS`+`STOP_PROFIT`
  combo, in the exact shape Webull's own docs show. Explicit instruction
  on the failure case, followed exactly: if the broker rejects that combo
  request, **the trade does not go through at all** -- no fallback to an
  unprotected plain entry. `OrderManager.submit_entry_signal` raises
  `BracketEntryRejected`, `TradingLoop._submit_entry` reverts the
  candidate to ARMED with no order and no position ever created, and a
  new `RiskEventType.BRACKET_ENTRY_REJECTED` event pops up automatically
  on the dashboard (not just another Risk Events row -- a real modal, the
  only system-triggered one among this dashboard's modals, since a trade
  that silently didn't happen is exactly the kind of thing that must not
  be missable). A broker that simply lacks this capability at all (paper
  trading, backtests) isn't a rejection -- falls back to a plain entry
  exactly as before, unchanged. See `docs/ARCHITECTURE.md`'s "Atomic
  bracket entry" section and `tests/test_webull_broker_client.py`,
  `tests/test_order_manager.py`, `tests/test_trading_loop.py` for the new
  coverage.
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
- **The dashboard's Performance/Trade History cards can now actually show
  data -- real bug fixed 2026-08-11.** Both cards were fully built
  end-to-end (HTML, `app.js`'s `refreshPerformance()`/`refreshTrades()`,
  the `/api/performance`/`/api/trades` endpoints, `record_trade()`) and
  stayed permanently empty anyway: `Base.metadata.create_all()` only
  creates a table that doesn't exist yet, it never alters one that's
  already there, and the VPS's long-lived `trades` table predated
  `TradeRecord.max_favorable_excursion`/`max_adverse_excursion`/
  `trading_mode` -- every `record_trade()` call had been failing (silently
  swallowed by `run_dashboard.py`'s `except Exception:
  logger.exception(...)`) since those columns were added, despite real
  trades actually closing. `db/session.py`'s `create_all()` now also runs
  a new `sync_schema()` step that diffs each already-existing table's live
  columns against the models here and `ALTER TABLE ... ADD COLUMN`s in
  whatever's missing (additive-only -- see that function's docstring for
  exactly what it does and doesn't cover) -- closes this specific failure
  mode for every table in this schema, not just `trades`, and for any
  future column added to any of them.
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
- Alembic migrations (use `scripts/init_db.py` for now -- `db/session.py`'s
  `sync_schema()`, added 2026-08-11, covers the specific "a table already
  exists without a column the model has since gained" failure mode, but
  it's additive-only: no dropped/renamed/retyped columns, no constraints,
  no data backfill. Reach for real Alembic the moment schema evolution
  needs any of that.)
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
trading loop's own thread, not synchronously in the browser request, and
**retries every poll cycle until every position is actually closed or the
switch is disengaged** (fixed 2026-08-11: reported live that clicking
"Engage & Close All Positions" during core hours, on several separate
occasions, appeared to do nothing at all -- root cause was a one-shot
request flag, where a single failed close attempt on any symbol, for any
reason, permanently abandoned the flatten for it with no further retry
ever). Disengaging resumes normal trading and also stops that retry; any
position still open at that point is left exactly as it is. See
`docs/ARCHITECTURE.md`'s "Safety" section for the full mechanics,
including the related double-submit guard added at the same time.

**Per-position "Close" button** (added 2026-08-12): each row in the Open
Positions table has its own "Close" button -- force-closes just that one
position, unlike the kill switch above (all-or-nothing, and stays engaged
blocking every new entry until manually disengaged). It also briefly
pauses new entries (20s default, `TradingLoopConfig.manual_close_entry_pause_seconds`)
so the close isn't left competing for Webull's account-wide rate limit
against a flood of *other* order-placement calls -- added after a real
incident where a stop-loss exit kept losing that rate-limit race for over
20 seconds per attempt while many simultaneous entries were in flight
(`max_simultaneous_positions` was unlimited at the time). The pause clears
itself automatically; no dashboard action needed to release it. See
`docs/ARCHITECTURE.md`'s "Safety" section for the full mechanics.

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
