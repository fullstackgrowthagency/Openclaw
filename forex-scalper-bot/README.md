# forex-scalper-bot

A multi-tenant forex scalping bot: users control their own trade
parameters, build custom strategies through a schema-validated rule
builder, and can chat with an AI assistant to author a strategy in plain
English (the AI only ever produces a config for review/approval -- it is
never in the live trading path). Sibling project to `webull-momentum-bot`
in this monorepo, reusing its proven architecture where it transfers and
designing net-new pieces (rule builder, AI assistant, MT4/5 connector,
live-trade charting) where it doesn't.

See `docs/ARCHITECTURE.md` for what's actually built so far, phase by
phase.

## Setup (Phase 0)

```bash
cd forex-scalper-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pytest -q
```

There's no broker/connector, strategy engine, or dashboard yet -- Phase 0
is just the core domain model (config, enums, models, market-hours
calendar, and the `Strategy`/`BrokerClient` interfaces) that every later
phase builds on.
