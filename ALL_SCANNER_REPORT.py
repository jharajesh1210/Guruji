"""Guruji unified workflow and paper-trade report. Does not run trading strategies."""
import csv
import io
import zipfile
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from urllib.parse import urlsplit

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

TRADE_FILE_MAP = {
    "TOP100_ORIGINAL": ["TOP100_CRIPTO_AMC_POC_trades.csv"],
    "TOP100_NEWS_IMPACT": ["TOP100_CRIPTO_AMC_POC_NEWS_IMPACT_trades.csv"],
    "ASIAN_LONDON_NY_ORIGINAL": ["ASIAN_LONDON_NY_PAPER_trades.csv"],
    "ASIAN_LONDON_NY_NEWS_IMPACT": ["ASIAN_LONDON_NY_PAPER_NEWS_IMPACT_trades.csv"],
    "BTC_SMC_ORIGINAL": ["BTC_SMC_Paper_trades.csv"],
    "BTC_SMC_NEWS_IMPACT": ["BTC_SMC_Paper_NEWS_IMPACT_trades.csv"],
    "TOP100_CRIPTO_AMC_POC_V4": ["TOP100_CRIPTO_AMC_POC_V4_trades.csv"],
    "GOLD_COMBO_PAPER": ["GOLD_COMBO_PAPER_trades.csv"],
    "NIFTY50_EQUITY_INTRADAY": ["NIFTY50_EQUITY_PAPER_TRADES.csv"],
    "RIL_TRADING_SYSTEM": ["RELIANCE_PAPER_TRADES.csv", "RELIANCE_ALL_TRADES.csv"],
    "RIL_CANDLESTICK_BIBLE": ["RIL_CANDLESTICK_BIBLE_trades.csv"],
    "Arvind_vwap_pullback_short": ["arvind_vwap_rr4_trades.csv"],
    "Arvind_strategy_1d1h15m": ["BTC_strategy_trades.csv", "PAXG_strategy_trades.csv"],
    "GOLD_COMBO_STRATEGY": ["XAUUSDT_1YEAR_TRADES.csv", "PAXGUSDT_1YEAR_TRADES.csv"],
}

def trades_for(workflow):
    stem = Path(workflow.get("path", "")).stem
    if stem in TRADE_FILE_MAP:
        return [Path(n) for n in TRADE_FILE_MAP[stem] if Path(n).is_file()]
    key = normalize(stem)
    matches = []
    for file in Path(".").glob("*trades.csv"):
        base = normalize(file.name[:-len("_trades.csv")])
        if base in (key, key + "paper") or key == base + "paper":
            matches.append(file)
    return matches


class SafeArtifactRedirect(HTTPRedirectHandler):
    """GitHub's signed blob URL must not receive the GitHub bearer token."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            redirected.headers.pop("Authorization", None)
            redirected.unredirected_hdrs.pop("Authorization", None)
        return redirected

ARTIFACT_OPENER = build_opener(SafeArtifactRedirect())

def restore_artifact_trades(wf):
    """Use newest available artifact snapshot; never combine repeated run snapshots."""
    stem = Path(wf.get("path", "")).stem
    wanted = TRADE_FILE_MAP.get(stem, [])
    missing = {n.lower(): n for n in wanted if not Path(n).is_file()}
    if not missing or not TOKEN:
        return []
    restored = []
    try:
        runs = api(f"/actions/workflows/{wf['id']}/runs?per_page=8").get("workflow_runs", [])
        for run in runs:
            if not missing:
                break
            if run.get("conclusion") != "success":
                continue
            artifacts = api(f"/actions/runs/{run['id']}/artifacts?per_page=100").get("artifacts", [])
            for artifact in artifacts:
                if not missing:
                    break
                if artifact.get("expired"):
                    continue
                url = artifact.get("archive_download_url")
                if not url:
                    continue
                try:
                    with ARTIFACT_OPENER.open(Request(url, headers=HEADERS), timeout=45) as response:
                        payload = response.read(30 * 1024 * 1024 + 1)
                    if len(payload) > 30 * 1024 * 1024:
                        print("Skipping oversized artifact", artifact.get("name"))
                        continue
                    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                        for member in archive.infolist():
                            name = Path(member.filename).name.lower()
                            if name not in missing or member.is_dir() or member.file_size > 10 * 1024 * 1024:
                                continue
                            destination = Path(missing.pop(name))
                            destination.write_bytes(archive.read(member))
                            restored.append(str(destination))
                            print("Restored trade CSV", destination, "from run", run["id"])
                except Exception as exc:
                    print("Artifact unavailable", artifact.get("name"), str(exc)[:160])
    except Exception as exc:
        print("Artifact lookup failed for", stem, str(exc)[:200])
    return restored

def performance(workflow):
    files = trades_for(workflow)
    if not files:
        stem = Path(workflow.get("path", "")).stem
        expected = TRADE_FILE_MAP.get(stem, [])
        reason = ("Expected trade CSV not in repository checkout: " + ", ".join(expected) + ". Check workflow artifact or scanner trade export" if expected else "No mapped trade CSV; this may be a signal-only scanner or diagnostic workflow")
        return dict(total_trades="-", wins="-", losses="-",
                    breakeven="-", win_percent="-", net_pnl_usdt="-",
                    sum_trade_return_pct="-", trade_files="", data_note=reason)
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
                if any(k in row and not str(row[k]).strip() for k in ("exittime", "exittimeutc")):
                    continue
                status = str(row.get("result") or row.get("status") or row.get("outcome") or "").upper().strip()
                if status in ("OPEN", "ACTIVE", "PENDING", "RUNNING", "ENTRY"):
                    continue
                usdt = next((numeric(row[k]) for k in
                             ("netpnlusdt", "paperpnlusdt", "pnlusdt", "realizedpnl", "netpnl", "profitusdt", "pnl")
                             if k in row and numeric(row[k]) is not None), None)
                pct = next((numeric(row[k]) for k in
                            ("netreturnpctperleg", "netreturnpct", "netpct", "returnpct", "profitpct", "returnpctofequity", "pnlpct")
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
                win_percent=round(100 * wins / decisive, 2) if decisive else "-",
                net_pnl_usdt=round(pnl, 4) if pnl_count else "-",
                sum_trade_return_pct=round(return_pct, 4) if return_count else "-",
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
        restore_artifact_trades(wf)
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
