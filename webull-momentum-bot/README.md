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
- Data-collection scaffolding for recording momentum events (traded and
  not-traded) with forward-looking outcome windows
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
- Production run-loop (`main.py` only builds the object graph; no
  poll/react loop wired up yet)
- RVOL's historical intraday-volume-by-time-of-day baseline
- Real resistance/support level detection (currently just running HOD)
- Level 2 / order-flow features
- Alembic migrations (use `scripts/init_db.py` for now)

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

## Development order

Following the project outline: architecture -> Webull integration ->
streaming -> float data -> database -> calculations -> scanner -> MIS ->
state machine -> backtesting -> risk engine -> paper execution -> position
management -> dashboard -> data collection at scale -> strategy
optimization -> L2/order-flow -> production-readiness testing.

This repo currently covers architecture, database, calculations, scanner,
MIS, state machine, backtesting, risk engine, position management, real
free-float data (FMP), and a real Webull sandbox connection (account,
market data, order submission). Next up: confirm the sandbox streaming
host, re-verify the still-best-effort response shapes during market hours
with a real filled order, then build the production run-loop.
