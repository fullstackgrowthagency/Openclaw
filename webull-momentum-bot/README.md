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
  alternate skeleton if you switch providers later.
- Momentum metrics (float turnover/velocity, RVOL, volume/price
  acceleration, VWAP, spread, dollar volume, etc.)
- Momentum Ignition Score with YAML-configurable weights/thresholds
  (`scoring/weights.yaml`) -- **not tuned**, meant to be improved from
  backtest/paper-trading data
- Three-tier scanner (BroadScanner -> CandidateWatcher -> TriggerEngine)
- Two entry strategies: Momentum Breakout and Breakout Pullback
- Deterministic Risk Engine enforcing per-trade risk, position/exposure
  caps, daily loss limit, per-ticker/per-day trade caps, spread/liquidity
  gates, cooldowns, and a kill switch
- `Strategy -> RiskEngine -> OrderManager -> Broker` enforced in code --
  strategies never hold a broker reference
- Position manager (stop/target/trailing/VWAP-failure/time exits)
- Event-driven backtest engine using the *same* strategy/risk/order-manager
  code as live trading, with a configurable slippage/fee model
- **Production run-loop** (`runtime/trading_loop.py`, wired up in `main.py`):
  polls `broker.get_snapshot()` per candidate (no streaming yet -- see
  below) and periodically rescans a symbol universe. Correctly handles
  `WebullBrokerClient.place_order` returning `SUBMITTED` rather than
  `FILLED` (confirmed live) via pending-order polling for both entries and
  exits, rather than assuming synchronous fills like the backtest engine
  can. Universe discovery in sandbox/live mode combines **four
  independent, verified Webull screener sources** (`data/universe.py`),
  unioned via `MultiSourceUniverseProvider` rather than a priority fallback
  chain -- a symbol only needs to show up on one list, and `BroadScanner`
  vets every symbol the same way regardless of which list(s) surfaced it:
  `get_most_active(rank_type="RELATIVE_VOLUME_10D")` (high relative
  volume), `get_most_active(rank_type="TURNOVER_RATE")` (% of float traded
  today -- directly analogous to the float_turnover metric the MIS already
  computes), `get_gainers_losers(rank_type="DAY_1")` (today's top % price
  movers), and `get_gainers_losers(rank_type="MIN_5")` (the last 5
  minutes' top % price movers -- Webull's real equivalent of "most active
  last 5 minutes" for price; there is no equivalent 5-minute *volume*
  ranking anywhere in the API, confirmed live). Each source paginates
  instead of taking a single fixed-size page, stopping at a data-driven
  threshold or a generous safety valve rather than an arbitrary result
  count -- see `docs/ARCHITECTURE.md` for the exact stopping rule. Price
  range is $0.40-$25.00. There is no cap on how many symbols get scanned
  per cycle -- every symbol every source returns gets checked, so
  full-scan duration scales with universe size rather than being bounded
  by a fixed number; see `docs/ARCHITECTURE.md` for the measured
  per-symbol timing this implies (noting those numbers predate this wider
  price range/pagination/4th source and now understate real scan time).
  Dollar volume and average volume are informational `Candidate` fields,
  not discovery gates -- see `docs/ARCHITECTURE.md`'s "Structural vs.
  temporary disqualification" section for why, and for the same
  distinction applied to `CandidateWatcher`'s spread/liquidity checks
  (`Candidate.trade_eligible`/`block_reasons`, which no longer permanently
  reject a candidate either).
- **Resistance detection via volume profile** (`metrics/volume_profile.py`):
  resistance is no longer just the running high of day. At discovery,
  `BroadScanner` fetches recent intraday bars and builds a volume-at-price
  histogram, keeping the biggest clusters ("high volume nodes") as static
  resistance levels -- a more general stand-in for hand-picked levels like
  prior-day-high or round numbers, since those usually show up as volume
  clusters anyway, plus this gives a real strength signal a flat list of
  price points can't. `CandidateWatcher` merges the nearest still-untested
  static level with the running high on every tick. See
  `docs/ARCHITECTURE.md`'s "Resistance detection" section for the full
  design and an important data-shape caveat (Webull's raw bars reach back
  as far as needed to find real data for illiquid names, which this
  deliberately bounds to a recent calendar window before building the
  profile).
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
  link, to go back.
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
- RVOL's historical intraday-volume-by-time-of-day baseline
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
falling back to fake data. Get a key at financialmodelingprep.com.

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
