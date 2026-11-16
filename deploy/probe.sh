#!/usr/bin/env bash
# Read-only check of the CoinDCX keys (no orders). Usage: bash ~/Guruji/deploy/probe.sh
cd "$(dirname "$0")/.."
git pull -q --ff-only 2>/dev/null || true
set -a; eval "$(sudo cat /etc/rd_live.env)"; set +a
./venv/bin/python rd_live.py --probe
