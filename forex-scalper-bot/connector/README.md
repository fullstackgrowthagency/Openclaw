# fx-connector

The local MT4/5 connector: a small Windows-only program that runs on a
user's own machine, talks to their already-running MetaTrader 5 terminal
via MetaQuotes' official `MetaTrader5` Python package, and relays
account/quote/order data to the `forex-scalper-bot` cloud backend over an
outbound-only WebSocket (see the main project's `docs/ARCHITECTURE.md`
for the full Phase 5 design this implements).

This is a **separate, standalone project** from `forex-scalper-bot`
itself -- it shares only `relay_protocol` (the dependency-free wire
format) with the cloud side, and never imports `fx_bot`. That's
deliberate: this is the one piece of the monorepo meant to eventually run
on an end-user's machine, packaged with PyInstaller (Phase 5e), so it
must never pull in the cloud backend's dependency closure (FastAPI,
SQLAlchemy, etc.).

## Setup

```bash
cd connector
python3 -m venv .venv && source .venv/bin/activate
pip install -e "../relay_protocol[dev]"
pip install -e ".[dev]"
cp .env.example .env
pytest -q
```

`relay_protocol` is a companion install, not a declared path dependency
in this project's own `pyproject.toml` -- see `docs/ARCHITECTURE.md`'s
Phase 5d section for why (a relative sibling-directory reference would
break the moment this project is packaged/distributed independently of
the monorepo it currently lives in).

`MetaTrader5` (MetaQuotes' official package) is declared with a
`sys_platform == 'win32'` marker, so `pip install` on Linux/CI simply
skips it -- nothing in this project imports it at module load time
regardless (see `main.py`'s `_import_real_mt5()`), so the whole package
stays importable and testable on any platform. Only real end-to-end
running (not testing) requires an actual Windows machine with MT5
installed.

## What's built so far

Phase 5d: the connector skeleton itself, with the `MetaTrader5` module
boundary mocked for unit tests -- no PyInstaller packaging yet (Phase
5e), and nothing here has been run against a real MT5 terminal yet
(Phase 5g). See the main project's `docs/ARCHITECTURE.md` for the full
write-up.
