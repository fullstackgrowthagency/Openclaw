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

## What's next

Phase 1 (live-trading safety gate, built early) is the next planned
increment -- see the approved plan for the full phase list and the
deployment-model/broker-bridge decision (hybrid local MT4/5 connector +
centrally-hosted dashboard/strategy engine/AI assistant).
