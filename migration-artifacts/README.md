# Migration Artifacts

These files are committed to GitHub so the repo carries the migration payload, not just the workspace source.

## Files

- `openclaw-portable-full-latest.tar.gz.part-00`
- `openclaw-portable-full-latest.tar.gz.part-01`
- `openclaw-portable-clean-latest.tar.gz.part-00`
- `openclaw-portable-clean-latest.tar.gz.part-01`
- `restore-portable-agent-bundle.sh`
- `workspace-history.bundle`
- `SHA256SUMS.txt`

## Which bundle to use

Use the **full** bundle for the new VPS unless you intentionally want a reduced, less secret-heavy transfer.

The full bundle includes as much portable state as possible, including:

- workspace files
- `.git` history
- OpenClaw config
- agent auth state
- session history
- WhatsApp auth state
- workspace-local runtime state like `.env`, generated files, approvals, and temp files

## Reassemble the full bundle

```bash
cat openclaw-portable-full-latest.tar.gz.part-00 \
    openclaw-portable-full-latest.tar.gz.part-01 \
  > openclaw-portable-full-latest.tar.gz
```

Optional, verify checksum after reassembly:

```bash
sha256sum openclaw-portable-full-latest.tar.gz
```

## Restore on the new VPS

```bash
chmod +x restore-portable-agent-bundle.sh
./restore-portable-agent-bundle.sh --bundle ./openclaw-portable-full-latest.tar.gz
```

## Important

This repo now carries sensitive migration material. Keep it **private**. If it is ever exposed, rotate credentials and relink channel auth immediately.
