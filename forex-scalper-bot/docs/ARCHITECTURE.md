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

## What's next

Phase 5 (local MT4/5 connector + relay protocol) is the next planned
increment -- the broker/bridge integration itself, per the approved
plan's hybrid deployment-model decision (a thin local connector talking
to the user's own MT5 terminal, relayed to the centrally-hosted dashboard/
strategy engine/AI assistant over an outbound-only connection). See the
approved plan for the full phase list.
