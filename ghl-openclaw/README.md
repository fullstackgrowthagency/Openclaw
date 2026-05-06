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

## Output files

Generated files are written to `data/generated/`.

- `capability-registry.json`
- `agent-manifests.json`
- `taskpacks.json`
- `status.json`
