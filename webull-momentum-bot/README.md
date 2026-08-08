# Webull Low-Float Momentum Trading Bot

Automated scanner + paper/live trading system for low-free-float momentum
stocks, built on the Webull OpenAPI. See `docs/ARCHITECTURE.md` for the full
data-flow and module map.

## Status: Phase 1 (architecture) + core Phase 2 interfaces

What's implemented and tested:

- Config/safety layer with a hard-disabled-by-default live trading gate
- Candidate lifecycle state machine (DISCOVERED -> ... -> COOLDOWN)
- Broker abstraction with a fully working local `PaperBrokerClient` and a
  `WebullBrokerClient` skeleton (methods raise `NotImplementedError` until
  wired to the real SDK against current docs)
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

What's still a skeleton (explicitly, not silently):

- `brokers/webull/client.py` -- real Webull OpenAPI/SDK calls. **Blocked on
  Webull OpenAPI developer credentials** (sandbox app key/secret); wire this
  up as soon as those exist, against Webull's current official docs.
- `data/float_providers/massive.py` -- unused now that FMP is wired up; kept
  only as an alternate skeleton
- Real-time streaming wiring / production run-loop (`main.py` only builds
  the object graph)
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

Until Webull sandbox credentials are wired up, `TRADING_MODE=paper` is the
only mode that runs end-to-end for execution (fully local simulated broker,
no external calls). Free-float *data* is real (FMP) regardless of trading
mode -- only order execution is simulated in paper mode.

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
MIS, state machine, backtesting, risk engine, position management, and real
free-float data (FMP). Next up: real Webull OpenAPI + streaming integration
(against current official docs, not guessed) once sandbox developer
credentials are available, then paper-mode end-to-end runs against live
sandbox market data.
