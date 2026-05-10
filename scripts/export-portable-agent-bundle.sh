#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="/data/.openclaw/workspace"
STATE_ROOT="/data/.openclaw"
OUT_DIR="$WORKSPACE/exports"
INCLUDE_SESSIONS=0
INCLUDE_WHATSAPP=0
INCLUDE_GIT=0
INCLUDE_WORKSPACE_RUNTIME=0

usage() {
  cat <<'EOF'
Usage:
  export-portable-agent-bundle.sh [--include-sessions] [--include-whatsapp] [--include-git] [--include-workspace-runtime] [--output <path>]

Creates a timestamped tarball with the workspace and selected OpenClaw state.

Flags:
  --include-sessions          Include /data/.openclaw/agents/main/sessions/
  --include-whatsapp          Include /data/.openclaw/credentials/whatsapp/default/
  --include-git               Include the workspace .git directory for full local repo history
  --include-workspace-runtime Include workspace-local secrets, generated files, temp files, and .openclaw/
  --output PATH               Write tarball to PATH instead of the default exports folder
  -h, --help                  Show help
EOF
}

OUTPUT_PATH=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --include-sessions)
      INCLUDE_SESSIONS=1
      shift
      ;;
    --include-whatsapp)
      INCLUDE_WHATSAPP=1
      shift
      ;;
    --include-git)
      INCLUDE_GIT=1
      shift
      ;;
    --include-workspace-runtime)
      INCLUDE_WORKSPACE_RUNTIME=1
      shift
      ;;
    --output)
      OUTPUT_PATH="${2:-}"
      if [[ -z "$OUTPUT_PATH" ]]; then
        echo "Missing value for --output" >&2
        exit 1
      fi
      shift 2
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

mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
if [[ -z "$OUTPUT_PATH" ]]; then
  OUTPUT_PATH="$OUT_DIR/openclaw-portable-bundle-$STAMP.tar.gz"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$TMP_DIR/bundle"

# Workspace copy, excluding known local-only artifacts unless explicitly requested.
TAR_ARGS=(
  --exclude='exports'
  --exclude='backups'
)
if [[ "$INCLUDE_WORKSPACE_RUNTIME" -ne 1 ]]; then
  TAR_ARGS+=(
    --exclude='.openclaw'
    --exclude='ghl-openclaw/.env'
    --exclude='ghl-openclaw/data/secrets'
    --exclude='ghl-openclaw/data/generated'
    --exclude='ghl-openclaw/data/taskpacks'
    --exclude='ghl-openclaw/data/approvals'
    --exclude='ghl-openclaw/tmp-*'
  )
fi
if [[ "$INCLUDE_GIT" -ne 1 ]]; then
  TAR_ARGS+=(--exclude='.git')
fi

tar "${TAR_ARGS[@]}" -C "$WORKSPACE" -cf - . | tar -C "$TMP_DIR/bundle" -xf -

mkdir -p "$TMP_DIR/bundle/_state/openclaw"
cp "$STATE_ROOT/openclaw.json" "$TMP_DIR/bundle/_state/openclaw/"
mkdir -p "$TMP_DIR/bundle/_state/openclaw/agents/main"
cp -R "$STATE_ROOT/agents/main/agent" "$TMP_DIR/bundle/_state/openclaw/agents/main/"

if [[ "$INCLUDE_SESSIONS" -eq 1 ]]; then
  mkdir -p "$TMP_DIR/bundle/_state/openclaw/agents/main"
  cp -R "$STATE_ROOT/agents/main/sessions" "$TMP_DIR/bundle/_state/openclaw/agents/main/"
fi

if [[ "$INCLUDE_WHATSAPP" -eq 1 ]]; then
  mkdir -p "$TMP_DIR/bundle/_state/openclaw/credentials/whatsapp"
  cp -R "$STATE_ROOT/credentials/whatsapp/default" "$TMP_DIR/bundle/_state/openclaw/credentials/whatsapp/"
fi

cat > "$TMP_DIR/bundle/_state/RESTORE_NOTES.txt" <<'EOF'
Restore notes:
- Copy workspace files into the target workspace path.
- Copy _state/openclaw/openclaw.json to /data/.openclaw/openclaw.json on the new VPS.
- Copy _state/openclaw/agents/main/agent/ to /data/.openclaw/agents/main/agent/.
- If included, copy _state/openclaw/agents/main/sessions/ to /data/.openclaw/agents/main/sessions/.
- If included, copy _state/openclaw/credentials/whatsapp/default/ to /data/.openclaw/credentials/whatsapp/default/.
- Start OpenClaw on the new VPS after restore.
EOF

tar -C "$TMP_DIR" -czf "$OUTPUT_PATH" bundle

echo "Created portable bundle: $OUTPUT_PATH"
