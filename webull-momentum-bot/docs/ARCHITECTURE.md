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
BrokerClient                (PaperBrokerClient today; WebullBrokerClient
                              once Phase 2 wires up the real SDK)
```

Exits follow a parallel, shorter path: `PositionManager.check_exit` emits an
EXIT/SCALE_OUT `Signal`, which `OrderManager` routes straight to the broker
**without** the entry-sizing risk gates (spread/liquidity checks exist to
control new exposure, not to trap the bot in a losing position).

**Hard rule enforced in code, not just convention:** `Strategy` classes hold
no broker reference. If new code calls `broker.place_order` from anywhere
outside `execution/order_manager.py`, that's a bypass of the architecture --
don't do it, even for "just a quick script."

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
