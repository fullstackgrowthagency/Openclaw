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

## What's next

Phase 3 (indicators + rule-builder schema + compiler) is the next planned
increment, including the open question of whether a state-machine/
Candidate-equivalent is actually needed for scalping -- see the approved
plan for the full phase list and the deployment-model/broker-bridge
decision (hybrid local MT4/5 connector + centrally-hosted dashboard/
strategy engine/AI assistant).
