# VPS Migration Guide

This is the shortest path to move this agent to another VPS and keep working with the same memory, persona, and session history.

## What to move

### 1) Workspace repo (the agent's brain)
Copy or clone this workspace to the new machine:

- `~/.openclaw/workspace` in general OpenClaw docs
- on this machine: `/data/.openclaw/workspace`

This preserves:

- `AGENTS.md`
- `SOUL.md`
- `USER.md`
- `IDENTITY.md`
- `TOOLS.md`
- `HEARTBEAT.md`
- `MEMORY.md`
- `memory/`
- project files and git history in the workspace repo

### 2) OpenClaw state (needed for continuity)
These are outside the workspace and must be copied separately:

- `/data/.openclaw/openclaw.json`
- `/data/.openclaw/agents/main/agent/auth-profiles.json`
- `/data/.openclaw/agents/main/sessions/`

This preserves:

- model/provider auth profiles
- main-session transcript history
- session metadata and continuity
- routing/config needed by the gateway

### 3) WhatsApp auth state (only if you want to keep the same linked account)
Copy:

- `/data/.openclaw/credentials/whatsapp/default/`

Important: treat this directory like a password vault. It contains live WhatsApp auth material.

## Recommended migration flow

1. Stop OpenClaw on the old VPS.
2. Copy the workspace repo to the new VPS.
3. Copy the state files listed above to the same relative paths under `/data/.openclaw/` on the new VPS.
4. Install the same OpenClaw version on the new VPS.
5. Start OpenClaw on the new VPS.
6. Verify the workspace path in config points at the migrated workspace.
7. Test with a direct message.

## Fastest safe path

If you want nearly full continuity, migrate all three layers:

- workspace
- OpenClaw state
- WhatsApp credentials

If you only migrate the workspace, you keep the persona/memory/work files but lose live session continuity and likely need to relink channels.

## Git recommendation

Keep the workspace in a private git repo, but do **not** commit:

- `.openclaw/`
- `.env` files
- credentials
- `ghl-openclaw/data/secrets/`

A root `.gitignore` now exists to help with that.

## One-command export helper

Use:

```bash
/data/.openclaw/workspace/scripts/export-portable-agent-bundle.sh
```

Optional flags:

- `--include-sessions`
- `--include-whatsapp`
- `--output <path>`

Example:

```bash
/data/.openclaw/workspace/scripts/export-portable-agent-bundle.sh --include-sessions --include-whatsapp
```

That creates a timestamped tarball containing the workspace plus the selected OpenClaw state.

## Notes

- Do not run the same WhatsApp account from two VPSes at once unless you are intentionally testing and ready for auth/state conflicts.
- If the new VPS uses a different workspace path, update `agents.defaults.workspace` accordingly in OpenClaw config.
- Session continuity depends on copying `/data/.openclaw/agents/main/sessions/`, not just the workspace repo.
