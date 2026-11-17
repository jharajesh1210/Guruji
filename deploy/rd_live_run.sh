#!/usr/bin/env bash
# Runs rd_live.py one scan every 5 minutes + fast lock watch every minute (aligned to :00 :05 :10 ...). Keep it running with systemd.
set -u
cd "$(dirname "$0")/.."
exec 9>/tmp/rd_live.lock
flock -n 9 || { echo "rd_live is already running"; exit 1; }
while true; do
  START=$(date +%s)
  echo "=== rd_live cycle @ $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  timeout 280 ./venv/bin/python -u rd_live.py --alert || echo "WARN: cycle failed or timed out; continuing."
  NEXT=$(( (START / 300 + 1) * 300 ))
  # fast profit-lock watcher every minute until the next scan
  while [ $(( $(date +%s) + 60 )) -lt "$NEXT" ]; do
    W=$(date +%s)
    timeout 50 ./venv/bin/python -u rd_live.py --watch || echo "WARN: watch failed; continuing."
    S=$(( W + 60 - $(date +%s) )); [ "$S" -gt 0 ] && sleep "$S"
  done
  NOW=$(date +%s)
  if [ "$NEXT" -gt "$NOW" ]; then sleep $((NEXT - NOW)); fi
done
