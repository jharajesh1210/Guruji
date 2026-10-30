"""Guruji unified workflow and paper-trade report. Does not run trading strategies."""
import csv
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

REPO = os.getenv("GITHUB_REPOSITORY", "jharajesh1210/Guruji")
TOKEN = os.getenv("GITHUB_TOKEN", "")
EXCLUDE = ("ALL_6_SCANNERS_RESULTS", "ALL_BOTS_STATUS", "TELEGRAM_TEST")
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "Guruji-report"}
if TOKEN:
    HEADERS["Authorization"] = "Bearer " + TOKEN

def api(path):
    with urlopen(Request("https://api.github.com/repos/" + REPO + path,
                         headers=HEADERS), timeout=30) as response:
        return json.load(response)

def numeric(value):
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None

def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())

def trades_for(workflow):
    stem = Path(workflow.get("path", "")).stem
    key = normalize(stem)
    candidates = []
    for file in Path(".").glob("*trades.csv"):
        base = normalize(file.name[:-len("_trades.csv")])
        if base == key:
            candidates.append(file)
    # Some workflows are named without the _PAPER suffix while their data uses it.
    if not candidates:
        for file in Path(".").glob("*trades.csv"):
            base = normalize(file.name[:-len("_trades.csv")])
            if base == key + "paper" or key == base + "paper":
                candidates.append(file)
    return candidates

def performance(workflow):
    files = trades_for(workflow)
    if not files:
        return dict(total_trades="N/A", wins="N/A", losses="N/A",
                    breakeven="N/A", win_percent="N/A", net_pnl_usdt="N/A",
                    sum_trade_return_pct="N/A", trade_files="", data_note="No matching trade CSV")
    wins = losses = breakeven = 0
    pnl = 0.0
    return_pct = 0.0
    pnl_count = return_count = 0
    unclassified = 0
    for file in files:
        with file.open(encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                row = {normalize(k): v for k, v in raw.items() if k}
                if not row:
                    continue
                if "exittime" in row and not row["exittime"].strip():
                    continue
                status = str(row.get("result") or row.get("status") or row.get("outcome") or "").upper().strip()
                if status in ("OPEN", "ACTIVE", "PENDING", "RUNNING", "ENTRY"):
                    continue
                usdt = next((numeric(row[k]) for k in
                             ("netpnlusdt", "pnlusdt", "realizedpnl", "netpnl", "profitusdt", "pnl")
                             if k in row and numeric(row[k]) is not None), None)
                pct = next((numeric(row[k]) for k in
                            ("netreturnpctperleg", "netreturnpct", "returnpct", "profitpct")
                            if k in row and numeric(row[k]) is not None), None)
                if usdt is not None:
                    pnl += usdt
                    pnl_count += 1
                if pct is not None:
                    return_pct += pct
                    return_count += 1
                metric = usdt if usdt is not None else pct
                if metric is not None:
                    if metric > 0: wins += 1
                    elif metric < 0: losses += 1
                    else: breakeven += 1
                elif status in ("WIN", "WON", "TP", "TAKE_PROFIT", "TP_RR4"):
                    wins += 1
                elif status in ("LOSS", "LOST", "SL", "STOP_LOSS"):
                    losses += 1
                elif status in ("BE", "BREAKEVEN", "BE_SL"):
                    breakeven += 1
                else:
                    unclassified += 1
    decisive = wins + losses
    return dict(total_trades=decisive + breakeven, wins=wins, losses=losses,
                breakeven=breakeven,
                win_percent=round(100 * wins / decisive, 2) if decisive else "N/A",
                net_pnl_usdt=round(pnl, 4) if pnl_count else "N/A",
                sum_trade_return_pct=round(return_pct, 4) if return_count else "N/A",
                trade_files="; ".join(str(f) for f in files),
                data_note=("Unclassified rows: " + str(unclassified) if unclassified else
                           "Win rate excludes zero-P&L trades; percentage sum is not account return"))

def main():
    workflows = []
    page = 1
    while True:
        batch = api(f"/actions/workflows?per_page=100&page={page}").get("workflows", [])
        workflows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    rows = []
    for wf in workflows:
        if any(x in normalize(wf.get("name", "")).upper() or
               x in normalize(Path(wf.get("path", "")).stem).upper()
               for x in [normalize(n).upper() for n in EXCLUDE]):
            continue
        try:
            runs = api(f"/actions/workflows/{wf['id']}/runs?per_page=1").get("workflow_runs", [])
            run = runs[0] if runs else {}
            error = ""
        except Exception as exc:
            run = {}
            error = str(exc)
        started = run.get("run_started_at") or run.get("created_at")
        ist = (datetime.fromisoformat(started.replace("Z", "+00:00"))
               .astimezone(timezone(timedelta(hours=5, minutes=30)))
               .strftime("%Y-%m-%d %H:%M:%S") if started else "")
        rows.append(dict(scanner=wf.get("name", ""), workflow=wf.get("path", ""),
                         enabled=wf.get("state", ""), last_run_ist=ist,
                         run_status=run.get("status", "never_run"),
                         conclusion=run.get("conclusion") or "",
                         run_url=run.get("html_url", ""),
                         telegram="Not verified", **performance(wf),
                         api_error=error))
    fields = ["scanner", "workflow", "enabled", "last_run_ist", "run_status",
              "conclusion", "run_url", "telegram", "total_trades", "wins",
              "losses", "breakeven", "win_percent", "net_pnl_usdt",
              "sum_trade_return_pct", "trade_files", "data_note", "api_error"]
    with open("ALL_SCANNER_REPORT.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print("Report generated:", len(rows), "workflows")
    for row in rows:
        print(row["scanner"], ":", row["conclusion"], "win%:", row["win_percent"])

if __name__ == "__main__":
    main()
