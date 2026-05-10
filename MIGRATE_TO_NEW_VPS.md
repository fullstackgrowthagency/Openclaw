# Migrate OpenClaw to a New VPS

This repo contains both the workspace source and GitHub-safe split migration bundles.

## What to use

For the closest possible continuity, use:

- `migration-artifacts/openclaw-portable-full-latest.tar.gz.part-00`
- `migration-artifacts/openclaw-portable-full-latest.tar.gz.part-01`
- `migration-artifacts/restore-portable-agent-bundle.sh`
- `migration-artifacts/SHA256SUMS.reassembled.txt`

## What the full bundle contains

As much portable state as possible from the current VPS, including:

- workspace files
- persona and memory files
- workspace git history
- OpenClaw config under `/data/.openclaw/openclaw.json`
- agent auth state under `/data/.openclaw/agents/main/agent/`
- session history under `/data/.openclaw/agents/main/sessions/`
- WhatsApp auth state under `/data/.openclaw/credentials/whatsapp/default/`
- workspace-local runtime state such as `.env`, generated assets, approvals, and temp files

## On the new VPS

### 1) Install OpenClaw
Install the same or a compatible OpenClaw version on the new VPS.

### 2) Clone this repo
```bash
git clone <this-repo-url>
cd Openclaw
```

### 3) Reassemble the full migration bundle
```bash
cd migration-artifacts
chmod +x reassemble-bundles.sh restore-portable-agent-bundle.sh
./reassemble-bundles.sh
sha256sum openclaw-portable-full-latest.tar.gz
cat SHA256SUMS.reassembled.txt
```

The checksum printed for `openclaw-portable-full-latest.tar.gz` should match the value in `SHA256SUMS.reassembled.txt`.

### 4) Restore the bundle
```bash
./restore-portable-agent-bundle.sh --bundle ./openclaw-portable-full-latest.tar.gz
```

By default this restores into:

- workspace: `/data/.openclaw/workspace`
- OpenClaw state root: `/data/.openclaw`

### 5) Start OpenClaw
Start or restart OpenClaw on the new VPS.

### 6) Keep the old VPS offline
Do not run the old and new VPS against the same WhatsApp auth state at the same time.

## What may still need rebuilding

These items are outside the migration bundle or can still be environment-sensitive:

- OpenClaw itself on the new VPS
- OS-level packages and runtime libraries
- project dependency installs if they are not already present there
- host-level SSH keys and Git auth
- reverse proxy, firewall, and DNS setup
- Tailscale or other network overlay setup
- any external databases or services not stored in this repo or bundle
- possibly WhatsApp relink, if WhatsApp rejects the restored auth state after the move

## Safety note

This repo contains sensitive migration material. Keep it private. If it ever becomes public, rotate credentials and relink channels immediately.
