#!/usr/bin/env bash
set -euo pipefail

cat openclaw-portable-full-latest.tar.gz.part-00 \
    openclaw-portable-full-latest.tar.gz.part-01 \
  > openclaw-portable-full-latest.tar.gz

cat openclaw-portable-clean-latest.tar.gz.part-00 \
    openclaw-portable-clean-latest.tar.gz.part-01 \
  > openclaw-portable-clean-latest.tar.gz

echo "Reassembled:"
ls -lh openclaw-portable-full-latest.tar.gz openclaw-portable-clean-latest.tar.gz

echo
echo "Expected checksums for reassembled files:"
cat SHA256SUMS.reassembled.txt
