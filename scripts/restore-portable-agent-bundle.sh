#!/usr/bin/env bash
set -euo pipefail

BUNDLE=""
WORKSPACE="/data/.openclaw/workspace"
STATE_ROOT="/data/.openclaw"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage:
  restore-portable-agent-bundle.sh --bundle <tar.gz> [--workspace <path>] [--state-root <path>] [--dry-run]

Restores a portable OpenClaw bundle onto a target machine.

Flags:
  --bundle PATH       Path to exported tar.gz bundle
  --workspace PATH    Target workspace path (default: /data/.openclaw/workspace)
  --state-root PATH   Target OpenClaw state root (default: /data/.openclaw)
  --dry-run           Print planned actions only
  -h, --help          Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)
      BUNDLE="${2:-}"
      shift 2
      ;;
    --workspace)
      WORKSPACE="${2:-}"
      shift 2
      ;;
    --state-root)
      STATE_ROOT="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$BUNDLE" ]]; then
  echo "Missing --bundle <tar.gz>" >&2
  usage >&2
  exit 1
fi
if [[ ! -f "$BUNDLE" ]]; then
  echo "Bundle not found: $BUNDLE" >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

tar -C "$TMP_DIR" -xzf "$BUNDLE"
SRC="$TMP_DIR/bundle"

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run] %s\n' "$*"
  else
    eval "$@"
  fi
}

run "mkdir -p '$WORKSPACE'"
run "mkdir -p '$STATE_ROOT'"
run "rsync -a '$SRC/' '$WORKSPACE/'"

if [[ -d "$SRC/_state/openclaw" ]]; then
  run "mkdir -p '$STATE_ROOT'"
  run "rsync -a '$SRC/_state/openclaw/' '$STATE_ROOT/'"
fi

cat <<EOF
Restore complete.

Next steps:
1. Verify OpenClaw is installed on this VPS.
2. Verify config at $STATE_ROOT/openclaw.json points to the workspace at $WORKSPACE.
3. Start or restart OpenClaw.
4. If WhatsApp credentials were restored, keep the old VPS offline to avoid account-state conflicts.
EOF
