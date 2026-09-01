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
pip install -e "./relay_protocol[dev]"
cp .env.example .env
pytest -q
```

`relay_protocol/` is a separate, standalone package (its own
`pyproject.toml`) used by the local MT4/5 connector's wire protocol
(Phase 5+) -- it's deliberately not declared as a path dependency inside
this project's own `pyproject.toml` (see `docs/ARCHITECTURE.md`'s Phase
5a section for why), so it needs this second, explicit `pip install -e`
step. Its own test suite runs separately: `pytest relay_protocol/tests`.

There's no dashboard yet -- the domain model, safety gate, paper
broker/backtest engine, rule-builder, risk engine/position manager, and
(as of Phase 5) the local-connector broker are what's built so far, phase
by phase -- see `docs/ARCHITECTURE.md`.
