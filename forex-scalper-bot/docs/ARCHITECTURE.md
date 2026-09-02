# Architecture

`forex-scalper-bot` is a sibling project to `webull-momentum-bot` in this
monorepo, reusing that project's proven layered design (`Strategy` ABC ->
`TriggerEngine` -> `RiskEngine` -> `OrderManager` -> `BrokerClient` ABC)
adapted for forex scalping. The full architecture, the deployment-model
decision (hybrid local connector + hosted dashboard/strategy engine/AI
assistant), the rule-builder schema, the AI authoring-assistant design,
and the phased build order were worked out in a planning session before
any code was written -- see that plan for the complete picture; this doc
tracks what's actually built, phase by phase, as it lands.

## Phase 0 -- scaffolding & core domain model (done)

- `config.py`: env-driven `Settings` (`TradingMode.DEMO`/`LIVE`,
  `Environment`), mirroring `webull_bot/config.py`'s shape. The
  live-trading safety gate (`is_live_trading_authorized`-equivalent) is
  intentionally NOT here yet -- it's Phase 1, built early per an explicit
  user decision to support both demo and live from early on rather than
  gating live to a late phase.
- `enums.py`: `OrderSide` (BUY/SELL only -- forex has no equities-style
  short-selling mechanic), `OrderType`, `TimeInForce`, `OrderStatus`,
  `SignalAction`, `ExitReason`. No `RiskEventType` yet (nothing raises
  those events until the risk-engine phase) and no state-machine enum
  (`CandidateState`-equivalent) -- whether scalping even needs one is an
  open question deferred to the rule-builder phase, not decided here.
- `pairs.py`: pip-size and base/quote-currency helpers, keyed off
  "BASE/QUOTE" pair strings (e.g. "EUR/USD"). Kept as lookup functions
  rather than fields duplicated on every model instance, since pip size
  is a property of the pair, not of any one snapshot/order/position.
- `models.py`: `MarketSnapshot` (bid/ask-based, no cumulative-volume/VWAP
  equivalent -- those are exchange/tape concepts that don't exist in a
  decentralized OTC market fed by one broker's quotes), `Signal`,
  `RiskDecision` (`max_units`, not `max_shares`), `Order` (carries
  `stop_loss_price`/`take_profit_price` directly, matching MT4/5's
  bracket-on-open convention), `Fill`, `Position`/`Trade` (both carry
  `swap`, a real forex concept with no equities-bot equivalent).
- `market_hours.py`: named session windows (Sydney/Tokyo/London/New York,
  UTC-band approximations) plus the London/New York overlap and
  Sunday-22:00-to-Friday-22:00-UTC market-open calendar -- forex has no
  single "core hours" window the way equities do, so this replaces
  `webull_bot`'s single-window model with an allowlist-of-named-windows
  approach (consumed by `RiskConfig.session_windows` once the risk engine
  exists).
- `interfaces/strategy.py`, `interfaces/broker.py`: the `Strategy` and
  `BrokerClient` ABCs, same contract shape as `webull_bot`'s. `Strategy
  .on_snapshot`'s signature is provisional (`symbol`, `snapshot`, raw
  `history` list) pending the state-machine question above.
  `BrokerClient.get_free_margin` replaces `get_buying_power` --
  forex/MT4-5 terminology, not a different concept.

## Phase 1 -- live-trading safety gate (done)

`config.py`'s `Settings` gained the deployment-wide half of the
three-condition live-trading gate, verbatim in shape from `webull_bot`'s
own (same confirmation phrase, same reasoning): `TRADING_MODE=live` AND
`LIVE_TRADING_ENABLED=true` AND `LIVE_TRADING_CONFIRMATION` set to the
exact phrase `is_live_trading_authorized()` checks for. All three are
required so a single stray env var can never enable live trading by
accident. `require_non_live_or_authorized()` raises at startup if a
deployment claims `TRADING_MODE=live` without actually being authorized.

Built now (not deferred to a late phase) per an explicit user decision:
both demo and live accounts should be supported per-user from early on,
not gated behind a "live" phase at the end. This is only the
deployment-wide half of the gate, though -- a per-user opt-in toggle
(mirroring `webull_bot`'s `BrokerCredential.live_trading_enabled`) is
added later, once there's a user/auth system for it to belong to
(multi-tenant hardening phase), exactly the same two-phase order the
equities bot built these in.

## Phase 2 -- paper broker + backtest skeleton (done)

Proves the full `Strategy -> RiskEngine -> OrderManager -> BrokerClient`
pipeline runs end to end, the same architectural guarantee the equities
bot's own backtest engine gives (a strategy that only works in a
bespoke test-only path proves nothing about live behavior). Since
`RiskEngine`/`OrderManager` didn't exist before this phase, it also
introduced deliberately minimal versions of both -- NOT the full field
set the approved plan describes for Phase 4 ("forex risk engine +
position management"); designing that now would mean building for
parameters nothing yet sets.

- `risk/risk_engine.py`: `RiskConfig` has exactly three fields
  (`stop_loss_required`, `max_simultaneous_positions`,
  `default_quantity` -- a flat per-trade unit size, not yet the real
  risk-%-of-equity/stop-distance lot sizing the plan calls for).
  `RiskEngine.evaluate()` only ever gates ENTER_LONG/ENTER_SHORT signals;
  everything else (EXIT, SCALE_IN/OUT) returns an explicit "not gated
  here" rejection if called directly -- callers must not route those
  through it. `OrderManager` doesn't.
- `execution/order_manager.py`: `OrderManager.submit_signal` routes
  entries through `RiskEngine.evaluate` (building a MARKET order with
  the signal's stop/target attached as MT4/5-style bracket-on-open
  fields) and exits straight to a closing order with NO risk check at
  all -- an exit must never be blockable by risk logic, the same
  reasoning the equities bot's own kill-switch/manual-close paths rely
  on. `SCALE_IN`/`SCALE_OUT` (partial exits) return `None` -- not
  implemented yet, matching how the equities bot itself deferred partial-
  exit support until well after its own initial skeleton.
- `brokers/paper/client.py`: `PaperBrokerClient` fills MARKET orders
  synchronously by crossing the spread (buy at ask, sell at bid) plus an
  optional configurable `slippage_pips` -- forex's spread-based
  equivalent of the equities bot's bps-of-price paper-fill model. Only
  MARKET orders and full (not partial) position closes are supported so
  far. Every close is currently recorded as `ExitReason.MANUAL`, which is
  accurate today (there's no automatic stop-loss/target-triggering
  machinery yet -- that's position management, a later phase) but will
  need a real per-order exit-reason field once that exists.
- `backtest/engine.py`: `BacktestEngine.run(bars)` feeds a chronologically
  sorted bar list through the pipeline one snapshot at a time and returns
  the resulting `Trade` list. No cross-symbol no-lookahead guarantees yet
  (the equities bot only needed that once it tracked multiple symbols
  concurrently) -- add it here once this bot does too.

## Phase 3 -- indicators + rule-builder schema + compiler (done)

**State-machine question, resolved.** The equities bot's multi-state
discovery pipeline (WATCHING -> ... -> COOLDOWN) exists to filter noise
while scanning thousands of constantly-changing tickers. This bot has no
such discovery problem -- a `StrategyConfig` names exactly one pair, so
there's nothing to scan or narrow down. `Strategy.on_snapshot`'s signature
changed instead to take an explicit `position: Optional[Position]`
parameter (the currently-open position on that pair, or None) -- the one
piece of state a strategy genuinely needs (am I looking for an entry, or
managing an exit?) without a separate Candidate/state-machine object.
`BacktestEngine` now looks this up from `broker.get_positions()` each
tick and passes it through.

- `indicators/`: `sma`/`ema`/`rsi`, each returning a series the same
  length as its input price list (leading `None`s where there isn't
  enough history yet) -- this is what lets crossover conditions compare
  "current vs previous" uniformly for any indicator. `registry.py` is the
  single whitelist every layer reads from (validator, compiler, and
  eventually the AI assistant's tool-use schema). ATR/Bollinger/MACD/
  Stochastic are deliberately NOT here yet -- they need true OHLC bars
  (high/low/close per period), which don't exist: `MarketSnapshot` is a
  single bid/ask point, not an aggregated bar. Faking them off inadequate
  data would produce numbers that look plausible but aren't the real
  indicator; add them once a bar-aggregation module exists.
- `strategy_builder/schema.py`: a Pydantic `StrategyConfig` -- pair,
  named `IndicatorRef`s, an entry `ConditionGroup` (AND/OR/NOT tree of
  `gt`/`lt`/`gte`/`lte`/`eq`/`crosses_above`/`crosses_below` comparisons
  between indicator values/price/constants), an optional rule-based
  `exit_conditions` group, and `stop_loss`/`take_profit` (fixed-pips only
  for the stop; the target also supports `risk_reward_ratio`, scaling off
  the stop distance). Trimmed to exactly what's real right now -- no
  `timeframe` (meaningless without bar aggregation), no `position_sizing`
  (RiskEngine.evaluate still decides `max_units`), no `filters` section
  (max_spread_pips/session_windows/etc. are RiskEngine-level concerns per
  the approved plan's field-mapping table, Phase 4). Add each back once
  the infrastructure it depends on exists.
- `strategy_builder/validator.py`: `validate_strategy_config` wraps BOTH
  Pydantic's structural errors and semantic checks (unknown indicator
  type, missing required params, a condition referencing an indicator_id
  never declared in `indicators[]`) into one `StrategyConfigError` with a
  flat message list -- the shape the approved plan's AI-assistant flow
  needs (feed validation errors back to Claude for a repair attempt,
  without the caller needing to handle two different exception types).
- `strategy_builder/rule_based_strategy.py` + `compiler.py`: `compile()`
  turns any validated `StrategyConfig` into a `RuleBasedStrategy` --
  ONE `Strategy` ABC implementation every config becomes, whether
  hand-authored, a built-in template, or (later) AI-authored. Indicator
  series are recomputed fresh from `history` on every call (simple,
  correct, revisit only if profiling ever shows it matters).

**Parity proof** (`tests/test_rule_builder_parity.py`): a hand-coded
EMA-crossover `Strategy` subclass and an equivalent compiled
`StrategyConfig`, run through the identical `BacktestEngine` pipeline
over identical bars, produce byte-for-byte identical `Trade` records
(price, side, quantity, pnl, AND timestamps). This is the load-bearing
guarantee for the whole rule-builder idea: if a declaratively-authored
strategy behaved differently from its hand-coded equivalent, letting
users (or the AI assistant) define strategies this way wouldn't be
trustworthy.

**Real bug found and fixed while writing that proof**: `PaperBrokerClient
.place_order` was stamping fill/position timestamps with wall-clock
`datetime.utcnow()` instead of the snapshot's own (simulated) timestamp
-- harmless for live paper trading, but wrong for backtesting: two
otherwise-identical backtest runs a few milliseconds apart in real time
produced non-identical `Trade.opened_at`/`closed_at`, and a backtest
replaying 2020 data would have recorded trades as happening today. Fixed
to use `snapshot.timestamp`; regression test in
`tests/test_paper_broker_client.py`.

## Phase 4 -- forex risk engine + position management (done)

`RiskConfig`/`RiskEngine` expanded from the Phase 2 skeleton (3 fields) to
the real field set from the approved plan's forex risk mapping, with two
documented simplifications spelled out in `risk/risk_engine.py`'s module
docstring:

1. **Position sizing assumes the account's currency equals the pair's
   quote currency** (e.g. a USD account trading EUR/USD). `risk_percent`
   sizing (`max_units = risk_amount / stop_distance`) needs no currency
   conversion under that assumption; a pair whose quote currency differs
   from the account currency isn't handled correctly yet.
2. **Correlated-pair exposure is approximated by shared currency**, not a
   real historical correlation coefficient -- EUR/USD and GBP/USD both
   count as "correlated" because they share USD exposure. The standard
   practical proxy, not a substitute for real correlation data this bot
   doesn't fetch/store.

New checks in `evaluate()`: session-window filtering (replaces the
equities bot's single core-hours window -- forex has several named
liquidity windows instead, see `market_hours.py`'s `SESSION_WINDOWS` plus
the tighter `"london_new_york_overlap"` special case), daily-loss limit,
max trades per day/per pair, post-loss cooldown (mirrors `webull_bot`'s
`_DailyState`/`_last_loss_at` pattern), per-pair and correlated-currency
exposure caps (in addition to the existing overall
`max_simultaneous_positions`), max spread in pips, and min risk:reward
ratio. Two sizing methods: `fixed_units` (flat, from Phase 2) or
`risk_percent` (real risk-%-of-equity/stop-distance sizing).
`record_trade_closed` feeds a position's realized pnl back in once it
closes -- called from `OrderManager._submit_exit`, which finds the
matching `Fill` via the `BrokerClient` ABC's own `poll_fills()` (by
`broker_order_id`, not a broker-specific attribute) rather than reaching
into `PaperBrokerClient` internals.

**Position management, built for the first time this phase**: nothing
before Phase 4 auto-enforced a position's stop/target at all -- only an
explicit strategy `EXIT` signal ever closed anything (documented as a gap
since Phase 2). `position/position_manager.py`'s `PositionManager.manage`
now runs every tick, for every open position, BEFORE the strategy gets a
say: it applies breakeven (moves the stop to entry once triggered, never
backward) and trailing-stop (only ever tightens) adjustments, then checks
the position's current (possibly just-adjusted) stop/target against the
price it would actually exit at right now (bid for a long, ask for a
short -- not an optimistic mid). `BacktestEngine` wires this in ahead of
`strategy.on_snapshot`, and re-fetches open positions afterward so the
strategy always sees the POST-auto-close reality that same tick, never a
stale about-to-be-closed position.

**Exit-reason tagging, fixed at the source.** Every close used to be
recorded as `ExitReason.MANUAL` regardless of why it actually happened
(documented as a known gap needing "a real per-order exit-reason field"
once auto-triggering existed -- see the Phase 3 write-up above). `Order`
now carries an `exit_reason` field; `OrderManager._submit_exit` reads it
from `Signal.metadata["exit_reason"]` (set by `BacktestEngine`'s
position-management path to `STOP_LOSS`/`PROFIT_TARGET`, absent/defaulted
to `MANUAL` for a strategy's own rule-based `EXIT` signal -- same
metadata-key convention `webull_bot` uses for its own exit-tagged
Signals), and `PaperBrokerClient` tags the resulting `Trade` with it
instead of hardcoding `MANUAL`.

## Phase 5a -- shared `relay_protocol` package (done)

The first slice of the local-connector/relay-protocol work (see the
approved Phase 5 design for the full picture): a brand-new, standalone
top-level package at `forex-scalper-bot/relay_protocol/` (its own
`pyproject.toml`, its own `src/relay_protocol/`, its own `tests/`) --
**not** a subpackage of `fx_bot`. This is deliberate: `relay_protocol` is
imported by both sides of the relay, including the Windows-only connector
process (built in a later sub-phase), which must never need `fx_bot`'s
dependency closure (or `fx_bot` itself) just to speak the wire format its
own process uses. Its only dependency is `pydantic>=2.0`.

- **`envelope.py`**: the one frame shape every message on the connector's
  WebSocket takes -- `Envelope{v, id, kind, method, payload, sent_at}`,
  JSON-encoded via `to_wire()`/`from_wire()`. `kind` is one of
  `request`/`response`/`error`/`event`/`auth` (`EnvelopeKind`).
  `request`/`response`/`error` correlate via `id`; `event` frames are
  one-way (connector-pushed) and carry no `id`. Convenience constructors
  (`Envelope.make_request/make_response/make_error/make_event/make_auth`)
  exist so callers never hand-assemble a frame and risk a malformed one
  reaching the wire. `v` is a schema version for detecting a mismatched
  connector build early, distinct from ordinary payload growth.
- **`methods.py`**: `RequestMethod`/`EventMethod` string-constant classes
  -- one name per non-streaming `BrokerClient` method
  (`get_account_equity`, `get_positions`, `place_order`, etc.) plus the
  connector-pushed event names (`quote`, `heartbeat`,
  `mt5_disconnected`/`mt5_reconnected`, ...). A single source of truth so
  a typo on one side of the relay can't silently create a request the
  other side never recognizes.
- **`wire_models.py`**: `WireMarketSnapshot`/`WireOrder`/`WireFill`/
  `WirePosition` -- Pydantic mirrors of `fx_bot.models`' dataclasses,
  field-for-field. Enum-valued fields (`side`, `order_type`, `status`,
  `exit_reason`) are plain `str` holding exactly the `.value` a matching
  `fx_bot` enum member would produce (`"buy"`, never `"OrderSide.BUY"`),
  **not** `fx_bot`'s actual Enum classes -- keeping this package
  independent of `fx_bot` means its whitelist of legal values never has
  to be kept in sync with `fx_bot`'s as those enums grow. Validating a
  wire string against the real enum (`OrderSide(wire_order.side)`, which
  raises `ValueError` on garbage) is left to the Phase 5b conversion code
  in `fx_bot/brokers/local_connector/`, not to this package.

**Deliberately out of scope for 5a** (left for 5b): the actual
`RelayConnection`/`LocalConnectorBroker` that sends these envelopes over
a real socket, the fake-relay-peer test double, and the `Wire* <->`
`fx_bot.models` conversion functions. This phase only proves the wire
format itself is sound -- both directions of every `Envelope` kind and
every `Wire*` model round-trip through real JSON (`model_dump_json`/
`model_validate_json`) with no data loss, verified in
`relay_protocol/tests/test_envelope.py` and `test_wire_models.py` (16
tests). Installed editable (`pip install -e ./relay_protocol[dev]`) into
the same shared `.venv` `fx_bot` uses; the two test suites run
independently (`pytest relay_protocol/tests`, `pytest tests`) and neither
depends on the other.

## Phase 5b -- cloud-side relay server + `LocalConnectorBroker` (done)

The second `BrokerClient` implementation, alongside `PaperBrokerClient`
-- talks to a user's local MT4/5 connector over the relay protocol built
in Phase 5a. Scope is exactly sub-phase 5b: no auth/pairing yet (that's
5c) and no real MT5 (5d+); a `RelayConnection` is assumed to already have
a connected socket, a plain localhost one in every test here.

- **Concurrency model**: async internals (asyncio + the `websockets`
  library) behind a fully synchronous, blocking facade, so
  `LocalConnectorBroker` stays a drop-in `BrokerClient` for entirely
  synchronous callers (`RiskEngine`, `OrderManager`, `BacktestEngine`).
  One shared background event-loop thread per process
  (`brokers/local_connector/relay_server.py`'s `RelayServer`) hosts every
  accepted connector socket as one `RelayConnection`
  (`relay_connection.py`), scaling to many simultaneous connectors later
  (Phase 10) with no rewrite. `LocalConnectorBroker`'s public methods
  block the *calling* thread on `send_request(...)`, which schedules a
  coroutine onto the shared loop and waits on its result.
- **Three exceptions, no more** (`exceptions.py`): `ConnectorOfflineError`
  (socket not connected, or dropped mid-request -- fail fast),
  `ConnectorTimeoutError` (socket fine, no response within the per-call
  deadline), `BrokerRejectedError(error_type, message)` (a genuine MT5
  trading rejection off an `error`-kind envelope -- a real business
  outcome, not a connectivity fault). A malformed wire enum value raises
  a bare `ValueError` instead of a fourth wrapped type, deliberately --
  see `exceptions.py`'s docstring.
- **`wire_convert.py`**: conversion functions between `relay_protocol`'s
  `Wire*` Pydantic models and `fx_bot.models`' dataclasses, living here
  rather than in `relay_protocol` -- validating a wire string against a
  real `fx_bot` enum (`OrderSide(wire.side)`, raising on garbage) is this
  project's job specifically, so `relay_protocol` never needs `fx_bot`'s
  enum vocabulary kept in sync as it grows.
- **`get_last_known_positions()`**: a deliberately minimal display/
  alerting accessor -- set on every successful `get_positions()` call,
  raises `RuntimeError` before the first one succeeds. Never consumed by
  `RiskEngine`/`PositionManager` (both only ever see a live read or an
  explicit failure). The full two-tier staleness-alerting system is
  Phase 5f; this is just the cache half of it, proven by test now so 5f
  has something to build alerting logic on top of.
- **`is_live`** is a plain constructor flag today, proven-by-test to not
  be hardcoded -- `relay_protocol` has no `hello`/auth-ack frame yet
  carrying MT5's real account-type field, so wiring it to a real
  handshake value is deferred to Phase 5c, not guessed at now.
- **Testing, no real MT5 needed**: `tests/fakes/fake_relay_peer.py`'s
  `FakeRelayPeer` is a real `websockets` client dialing a real
  `RelayServer` over localhost TCP, playing the connector role -- an
  in-process mock was considered and rejected, since it would leave
  `RelayServer` itself unexercised and defeat the point of catching real
  framing bugs. Scripted per method: `script_response`/`script_delay`/
  `script_drop`/`script_error`, covering the three exception types plus
  normal round trips. `tests/test_relay_connection.py` (transport-level,
  8 tests) and `tests/test_local_connector_broker.py` (connector-specific
  behavior -- quote dispatch, `get_last_known_positions`, `is_live`, the
  three exceptions surfaced through real `BrokerClient` methods, 9 tests)
  cover it directly. `tests/test_broker_client_contract.py` is the new
  shared ABC-contract suite: the same 7 behavioral cases (order placement
  shape, `get_positions`/`poll_fills` reflecting a fill, snapshot/equity/
  order-status reads, `is_live`'s type) run against both
  `PaperBrokerClient` and `LocalConnectorBroker` via a parametrized
  fixture, proving the two backends are actually interchangeable, not
  just independently self-consistent. `poll_fills(since=...)`'s
  time-filtering behavior stays paper-only (already covered in
  `test_paper_broker_client.py`) -- real time-based filtering for a
  connector will live on the MT5/connector side itself once that's built
  (5d+), not something worth faking here.
- 31 new tests, full suite (191 across `fx_bot` + `relay_protocol`) green
  and fast (~1.5s) -- an early version of the test double had a teardown
  race (closing a peer's own event loop before a concurrently-scheduled
  shutdown coroutine could report back) that added a spurious 5-second
  hang per test; fixed by giving `FakeRelayPeer` the same
  `run_forever`-until-explicitly-stopped loop lifecycle `RelayServer`
  already used, plus idempotent `stop()` on both.

New dependency: `websockets>=13.0,<15.0` added to this project's
`pyproject.toml` (not `relay_protocol`'s, which stays pydantic+stdlib
only). `relay-protocol` itself is a required companion `pip install -e`
step, documented in `README.md`, rather than an inline path dependency
inside `pyproject.toml` -- see that section for why.

## Phase 5c -- pairing flow backend routes (done)

The minimal standalone auth flow a connector uses to pair itself to a
cloud account, built specifically because it lands before Phase 10's
real multi-tenant auth/database system exists -- not a preview of that
system, a deliberately small, self-contained piece.

- **New package `brokers/local_connector/pairing/`**: `codes.py`
  (`generate_pairing_code()` -- 8 chars from a no-ambiguous-character
  32-symbol alphabet, `"XXXX-XXXX"`, ~1.1x10^12 combinations),
  `tokens.py` (`generate_token()` -- `secrets.token_urlsafe(32)`, 256
  bits; `hash_token()` -- plain `hashlib.sha256`, deliberately NOT
  `webull_bot`'s `Fernet`/reversible-encryption pattern, since a bearer
  token is never read back in plaintext, and NOT bcrypt either, since
  bcrypt's deliberate slow/salted hashing exists to resist brute-forcing
  *low-entropy human-guessable* secrets -- a 256-bit token has no such
  weakness for bcrypt to defend against, so it would only add latency to
  every relay-connection handshake for nothing), `store.py`
  (`PairingStore` -- bare stdlib `sqlite3`, WAL mode, `threading.Lock`
  since it's called from both FastAPI's threadpool and `RelayServer`'s
  event-loop thread; deliberately not SQLAlchemy, to avoid guessing at
  Phase 10's real multi-tenant schema under no pressure to do so yet),
  `routes.py` (`build_pairing_router(store, *, settings) -> APIRouter`,
  the same DI-factory shape `webull_bot`'s own `auth/routes.py` uses, so
  Phase 6's real dashboard just does `app.include_router(...)` with zero
  rework), `app.py` (`create_pairing_app` -- thin, Phase-6-absorbable).
  Both tables (`pairing_codes`, `connector_tokens`) carry an `account_id`
  column from day one, mirroring how `relay_protocol`'s
  `Envelope.make_auth` already carries one ahead of real multi-tenancy.
- **Two routes**: `POST /connector/pairing-codes` (201) and
  `POST /connector/pair` (200 with `{token, account_id}`; 404 unknown
  code, 400 expired, 409 already used -- the same 404/400/409 convention
  `webull_bot`'s own auth routes use). Re-pairing revokes the account's
  previous token (single active token per account, v1's "old one
  invalidated" rule). New dependency: `fastapi` (+ `httpx`/`uvicorn`,
  dev-only for now) -- pulled in now rather than deferred to Phase 6
  specifically so these two routes aren't thrown away and rebuilt later.
- **AUTH wired end to end for the first time**: `RelayConnection` gains
  `_authenticate(authenticator, grace_seconds)`, run by
  `RelayServer._handle_connection` BEFORE a connection is ever queued for
  `accept()` -- `accept()` therefore only ever returns an authenticated
  connection, never a bare one waiting to be authed later. A connector
  that sends nothing is caught by an `asyncio.wait_for` grace-period
  timeout (default 10s, `connector_auth_grace_seconds`); a non-`auth`
  first frame, a malformed frame, or an unrecognized token all close the
  socket with a distinguishing WebSocket code (`4401`,
  `AUTH_FAILURE_CLOSE_CODE`) so a real connector knows to stop
  auto-reconnecting and prompt for re-pairing rather than hot-looping. A
  late/duplicate `auth` frame arriving after a successful handshake is
  logged and ignored (`_handle_frame`'s dead Phase-5b comment is now a
  real branch) rather than silently vanished or mistaken for a
  re-authentication attempt. `RelayServer.__init__` now takes a
  **required** keyword-only `authenticator: Callable[[str],
  Optional[str]]` (no silent-bypass default) --
  `pairing.tokens.make_authenticator(store)` builds it, keeping
  `relay_server.py`/`relay_connection.py` decoupled from `PairingStore`
  and the hashing scheme entirely.
- **Testing**: `tests/test_pairing_store.py` (store/codes/tokens, no
  FastAPI, `tmp_path` sqlite, 10 tests) and `tests/test_pairing_routes.py`
  (FastAPI `TestClient`, 6 tests) cover the HTTP flow and persistence in
  isolation. `tests/test_relay_auth.py` (6 tests) deliberately bypasses
  the now-auto-authenticating `relay_pair`/`local_connector_broker`
  fixtures to exercise every failure path directly: valid token queued
  with `account_id` set, invalid token closes with 4401, a non-auth first
  frame closes with 4401, a malformed first frame closes with 4401, no
  auth frame within the grace period times out and closes, and a late
  duplicate auth frame is ignored without corrupting an otherwise-usable
  connection. `FakeRelayPeer` gained `send_auth`/`send_raw`/
  `wait_for_close` to support this. Both existing `RelayServer(...)`
  construction sites (`tests/conftest.py`'s `relay_pair` fixture,
  `tests/test_broker_client_contract.py`'s `_LocalConnectorHarness`) were
  updated to authenticate before their first `accept()` call -- no other
  existing test needed to change. Along the way, fixed a latent
  `FakeRelayPeer` teardown race (its own `_connect_and_serve` task could
  be torn down by the garbage collector mid-suspension if the event loop
  stopped before that task finished unwinding, surfacing as an
  intermittent `PytestUnraisableExceptionWarning` on an unrelated later
  test) by waiting for that task to actually finish before stopping the
  loop.
- 22 new tests; full suite (`fx_bot` 182 + `relay_protocol` 16 = 198)
  green, verified clean across 5 repeated runs (no flakiness).

**Deliberately out of scope for 5c** (see the approved design's own
flagged deferrals): per-IP/connection-count throttling of unauthenticated
sockets and HTTP rate-limiting on the two pairing routes (both real
multi-tenant infrastructure, Phase 10's job, and neither is needed yet --
the pairing-code keyspace is already brute-force-infeasible unthrottled
within its TTL); `is_live` still isn't wired to a real MT5 account-type
value (`relay_protocol`'s `auth` payload carries no such field yet);
production co-process wiring (one real entrypoint running both the
pairing HTTP app and `RelayServer` against the same `PairingStore`) --
`make_authenticator(store)` makes that a one-line closure whenever a real
entrypoint is actually built (5d+).

## Phase 5d -- local connector project skeleton (done)

The actual local MT4/5 connector program, as a **new standalone
project** at `forex-scalper-bot/connector/` -- sharing only
`relay_protocol` with `fx_bot` (never imports `fx_bot` itself), since
this is the one piece of the monorepo meant to eventually run on an
end-user's Windows machine (PyInstaller-packaged in Phase 5e). Grounded
directly against MetaQuotes' official MQL5 docs for the real
`MetaTrader5` Python package's API surface, not assumed.

- **`AUTH_FAILURE_CLOSE_CODE` hoisted into `relay_protocol`** (next to
  `WIRE_PROTOCOL_VERSION`) as a small addendum to Phase 5b/5c code,
  decided at the start of this phase rather than left as two
  independently-maintained `4401` literals -- the connector can't import
  `fx_bot.brokers.local_connector.relay_connection` to get it, so both
  sides now read one shared source of truth (`relay_connection.py`
  re-exports it, so no existing test import broke).
- **`symbols.py`**: `wire_pair_to_mt5_symbol`/`mt5_symbol_to_wire_pair`
  translate between fx_bot's `"EUR/USD"` convention and MT5's confirmed
  no-separator `"EURUSD"` format -- deliberately duplicated in miniature
  rather than importing `fx_bot.pairs` (this project must never depend
  on `fx_bot`). Per-broker suffixes threaded through but never
  auto-detected, flagged for Phase 5g.
- **`mt5_client.py`**: `MT5Client` takes the real-or-fake `mt5` module
  via constructor injection (never a bare top-level `import
  MetaTrader5`), translating between MT5's native shapes and
  `relay_protocol`'s `Wire*` models. The tricky parts, each explicitly
  stated rather than hand-waved: an in-memory `_order_registry` maps
  MT5's order/position/deal ticket trio onto this project's
  `broker_order_id`/`strategy_name` (falling back to `"external"` on a
  miss, consistent with `fx_bot.ExitReason.EXTERNAL_CLOSE` already
  anticipating exactly this gap) -- lost on a connector restart, a
  stated limitation, not a bug; `order_send`'s retcode **raises**
  `MT5OrderRejectedError` rather than ever returning a "rejected"-status
  `WireOrder`, since the cloud's `BrokerRejectedError` only ever fires
  off an `error`-kind envelope, never a normal response; `get_bars` maps
  OHLC bars to synthetic zero-spread snapshots (no OHLC field exists on
  `WireMarketSnapshot`); `poll_fills` synthesizes fills directly from
  `order_send`'s own result rather than reaching for the unverified
  `history_deals_get` API, so it only ever reflects fills this connector
  itself produced within its current process lifetime.
- **`mt5_executor.py`**: every blocking `mt5.*` call -- from both
  `relay_client.py`'s request handlers and `main.py`'s polling loops --
  routes through one shared **single-worker** executor
  (`run_in_executor`, the same idiom already used cloud-side for the
  pairing sqlite lookup during auth). `max_workers=1` deliberately:
  whether MT5's IPC channel is safe under concurrent calls is
  unverified, so serializing removes that risk regardless.
- **`relay_client.py`**: the connector-side counterpart to
  `RelayConnection`/`RelayServer`, as a WebSocket client with real MT5
  dispatch instead of test scripting. Auth mirrors the **cloud's** own
  ordering (connect -> send `auth` -> one direct `recv()` for the ack ->
  only then start the read loop) rather than `FakeRelayPeer`'s
  id-correlation pattern, since nothing else is reading the socket yet
  at that point. `AuthFailure` (a rejected token, or a `4401` close)
  propagates out of `run_forever()` rather than being retried --
  retrying a rejected token can never succeed. Reconnect-with-backoff
  (`backoff.py`) uses **proportional** jitter (not `webull_bot`'s flat
  jitter, which does nothing to spread out many connectors reconnecting
  at exactly the same cap after a shared outage) and only resets the
  attempt counter after a connection has been authenticated and stable
  for 10+ seconds, not on every successful auth.
- **`pairing.py`**: the HTTP client side of Phase 5c's pairing routes --
  `pair()`/`save_credentials()`/`load_credentials()`/`prompt_and_pair()`,
  mapping 404/400/409 to a `PairingError` with a retry-with-fresh-code
  message. Token file gets `chmod 0600` on POSIX; Windows ACL-based
  protection is explicitly deferred to installer work (5e+).
- **`main.py`**: wires it all together on one event loop
  (`asyncio.gather` of the relay-with-re-pairing loop, the quote-polling
  loop, and the heartbeat loop) -- `_import_real_mt5()` is the *only*
  place `import MetaTrader5` ever executes anywhere in this project,
  inside a function, never at module load time, which is what keeps the
  whole package importable and testable on Linux.
- **Testing, no real MT5 or real cloud server needed**:
  `tests/fakes/fake_mt5_module.py` (a plain scriptable object exposing
  exactly the `mt5.*` surface `MT5Client` calls) and
  `tests/fakes/fake_cloud_peer.py` (a real `websockets` **server**, the
  inverse of `fake_relay_peer.py`, playing the cloud's role for testing
  `relay_client.py` end to end over a real socket). 52 tests across
  `test_symbols.py`, `test_backoff.py`, `test_mt5_client.py` (21),
  `test_relay_client.py` (10, including auth success/rejection/4401,
  order-rejection -> `error`-not-`response`, reconnect backoff timing,
  no-reconnect-after-auth-failure), `test_pairing.py`, and
  `test_main_wiring.py` -- verified clean across repeated runs. Along
  the way, fixed the same class of async-teardown race caught twice
  already on the cloud side (a cancelled `run_forever` task needs a real
  chance to unwind, including draining `websockets`' own internal
  supporting tasks, before the test harness stops its event loop).

**Deliberately out of scope for 5d** (see the approved design's own
flagged deferrals): PyInstaller packaging (5e); the cloud-side
consumption/alerting half of health/staleness (5f -- 5d only builds the
connector-side event-*pushing* mechanism); any real-hardware/real-broker
verification -- per-broker symbol suffixes, real `type_filling` support,
real `ACCOUNT_TRADE_MODE_*` integer values, whether
`history_deals_get`/`history_orders_get` behave as expected (5g); a
GUI/installer config replacing the env-var `ConnectorSettings` shape and
Windows ACL token protection (5e+); wiring `is_live_account()` through
the wire protocol (no field exists yet, an already-flagged deferral).

## What's next

Phase 5e (PyInstaller packaging -- freezing `connector/` into a
standalone signed `.exe` so end users don't need Python installed) is
next. See the approved plan's Phase 5 design for the full remaining
sub-phase list (5f health/staleness wiring on the cloud side, 5g manual
verification checkpoint once real Windows/MT5 access exists, resolving
every flagged assumption from Phase 5d).
