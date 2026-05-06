# GHL OpenClaw

Implementation scaffold for a GoHighLevel / HighLevel multi-agent control plane inside OpenClaw.

## What this scaffold includes

- live capability registry builder with doc snapshot fallback
- `ghl-agency-lead` and location/specialist agent template rendering
- RBAC policy model with location isolation
- credential broker skeleton for OAuth and PIT handling
- modular task pack definitions
- webhook routing and dedupe skeleton
- real OAuth exchange, refresh, and agency-to-location token exchange plumbing
- first live API adapters for locations, contacts, conversations, and opportunities
- expanded adapter set for users, calendars, invoices, payments, products, snapshots, social planner, voice AI, and workflows
- webhook ingestion server with signature verification, persistent dedupe store, and queue processor skeleton
- task-pack execution engine with persistent run history and dry-run by default before credential hookup

## Quick start

1. Copy `.env.example` to `.env` and fill in credentials.
2. Refresh the capability registry:

```bash
npm run capability:refresh
```

3. Render agent manifests:

```bash
npm run agents:render
```

4. Render task pack manifests:

```bash
npm run taskpacks:render
```

## Current state

This is the first build pass. It is implementation scaffolding, not yet a production deployment.

It already enforces these core rules:

- use official HighLevel docs as source of truth
- block undocumented features with `not_publicly_api_supported`
- separate agency and location capability ownership
- keep destructive actions approval-gated
- keep raw credentials out of agent manifests and logs

## Auth commands

Check auth readiness:

```bash
npm run auth:status
```

You can also call the CLI directly:

```bash
node src/cli/index.js auth:exchange-code --code=AUTH_CODE --user-type=Company
node src/cli/index.js auth:refresh --credential-ref=agency-oauth
node src/cli/index.js auth:location-token --credential-ref=agency-oauth --company-id=COMPANY_ID --location-id=LOCATION_ID
```

These commands only work after `.env` is configured.

## Webhook commands

```bash
npm run webhook:status
npm run webhook:serve
node src/cli/index.js webhook:test-event --type=ContactCreate --location-id=demo-location
node src/cli/index.js webhook:drain --limit=10
```

Webhook server behavior:

- listens on `/webhooks/ghl`
- verifies `X-GHL-Signature` and legacy `X-WH-Signature`
- stores receipts persistently
- deduplicates by `webhookId` or payload hash
- queues work and ACKs fast
- exposes `/health` and `/webhooks/ghl/status`

## Task-pack execution commands

```bash
npm run taskpack:status
node src/cli/index.js taskpack:runs --limit=20
node src/cli/index.js taskpack:run --name=lead_management_pack --event-type=ContactCreate --location-id=demo-location
```

Task-pack engine behavior:

- converts webhook-routed events into task-pack runs
- persists run history under `data/taskpacks/`
- executes in `dry_run` mode unless a real stored credential is available or `--mode=live` is used
- performs safe adapter reads live when credentials exist
- leaves high-risk or mutation steps as planned intents until final hookup and policy approval

## Output files

Generated files are written to `data/generated/`.

- `capability-registry.json`
- `agent-manifests.json`
- `taskpacks.json`
- `status.json`

Persistent webhook state is written under `data/webhooks/`.
Persistent task-pack runs are written under `data/taskpacks/`.

## Adapter coverage in this pass

Implemented adapters are intentionally limited to documented and verified paths already captured by the capability registry.

- Agency-oriented: locations, users, snapshots
- Location-oriented: contacts, conversations, opportunities, calendars, invoices, payments, products, social planner, voice AI, workflows

Examples live in:

- `src/ghl/api/index.js`
- `src/ghl/api/example-usage.js`
