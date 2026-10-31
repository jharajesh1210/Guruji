#!/usr/bin/env python3
"""Guruji crypto scanner inventory and safe backtest orchestration.
Discovers all .py files recursively, without changing or executing original strategies.
Use --list to inventory; --run SCRIPT --args '...' to execute a verified backtest.
Each run saves stdout/stderr and status under master_reports/.
"""
import argparse
import csv
import datetime as dt
import json
import pathlib
import re
import shlex
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
KIND = "crypto"
SELF = {"SHARE_MASTER.py", "CRYPTO_MASTER.py"}
SHARE = re.compile(r"(reliance|\\bril\\b|nifty|banknifty|equity|stock|share|arvind|nse|bse|vwap_pullback|candlestick_bible)", re.I)
CRYPTO = re.compile(r"(crypto|cripto|coin|btc|eth|smc|top100|poc|amc|gold|paxg|xau|futures|binance)", re.I)
IGNORE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "master_reports"}

def inventory():
    entries = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if p.name in SELF or any(part in IGNORE_DIRS for part in rel.parts):
            continue
        name = str(rel)
        share = bool(SHARE.search(name))
        crypto = bool(CRYPTO.search(name))
        # Ambiguous or unclassified scripts appear in BOTH reports for manual review;
        # never silently omit an original scanner.
        category = "both-review" if share == crypto else ("share" if share else "crypto")
        if category == KIND or category == "both-review":
            entries.append({"script": name, "category": category,
                            "backtest_status": "NOT_VERIFIED", "net_profit": "",
                            "win_rate": "", "profit_factor": "", "max_drawdown": ""})
    return entries

def write_inventory(entries):
    out = ROOT / "master_reports"
    out.mkdir(exist_ok=True)
    dest = out / (KIND + "_scanner_inventory.csv")
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["script", "category", "backtest_status",
                                          "net_profit", "win_rate", "profit_factor", "max_drawdown"])
        w.writeheader()
        w.writerows(entries)
    print("Inventory saved:", dest)

def main():
    ap = argparse.ArgumentParser(description="Guruji " + KIND + " master inventory")
    ap.add_argument("--list", action="store_true", help="Discover scanners and create inventory CSV")
    ap.add_argument("--run", metavar="SCRIPT", help="Run one reviewed scanner by exact relative path")
    ap.add_argument("--args", default="", help="Explicit, verified backtest CLI arguments")
    ap.add_argument("--timeout", type=int, default=1200)
    opts = ap.parse_args()
    entries = inventory()
    write_inventory(entries)
    for item in entries:
        print(item["category"], item["script"], item["backtest_status"], sep=" | ")
    print("Total discovered for", KIND, len(entries))
    if not opts.run:
        print("No trades placed and no backtests assumed. Use --run after verifying CLI options.")
        return 0
    allowed = {e["script"] for e in entries}
    if opts.run not in allowed:
        ap.error("Script not in discovered " + KIND + " inventory")
    script = (ROOT / opts.run).resolve()
    if not script.is_relative_to(ROOT) or not script.is_file():
        ap.error("Invalid script path")
    if not opts.args.strip():
        ap.error("Provide explicit backtest arguments; never execute live scanner by default")
    args = shlex.split(opts.args)
    if not any(x in args for x in ("--backtest", "--days", "--synthetic", "--historical")):
        ap.error("Backtest marker required (--backtest / --days / --historical / --synthetic)")
    command = [sys.executable, str(script), *args]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = ROOT / "master_reports" / (KIND + "_" + script.stem + "_" + stamp + ".json")
    print("Executing reviewed command:", command, flush=True)
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                timeout=opts.timeout, check=False)
        record = {"script": opts.run, "args": args, "returncode": result.returncode,
                  "stdout": result.stdout[-30000:], "stderr": result.stderr[-30000:],
                  "note": "Raw output only; profit metrics are NOT validated"}
    except subprocess.TimeoutExpired:
        record = {"script": opts.run, "args": args, "returncode": 124,
                  "error": "Timed out; results not validated"}
    report.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Raw run log:", report)
    return record["returncode"]

if __name__ == "__main__":
    sys.exit(main())
