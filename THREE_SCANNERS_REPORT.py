#!/usr/bin/env python3
"""Read-only consolidated report for three NSE equity paper scanners.
Never alters source scanners, states, or trade CSVs.
"""
import csv
import os
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "THREE_SCANNERS_SUMMARY.csv"
DETAIL = ROOT / "THREE_SCANNERS_ALL_TRADES.csv"
HTML = ROOT / "THREE_SCANNERS_REPORT.html"
IST = ZoneInfo("Asia/Kolkata")

SCANNERS = {
    "NIFTY50 EQUITY INTRADAY": {
        "files": ["NIFTY50_EQUITY_PAPER_TRADES.csv"],
        "keywords": ["NIFTY50", "TRADES"],
    },
    "RELIANCE 5MIN": {
        "files": ["RELIANCE_PAPER_TRADES.csv"],
        "keywords": ["RELIANCE", "PAPER", "TRADES"],
    },
    "RIL CANDLESTICK BIBLE": {
        "files": [
            "RIL_CANDLESTICK_BIBLE_trades.csv",
            "RIL_CANDLESTICK_BIBLE_TRADES.csv",
        ],
        "keywords": ["RIL_CANDLESTICK_BIBLE", "TRADES"],
    },
}
# Note: only explicitly named paper trade files are read.
# Historical backtest CSVs are intentionally excluded.

def number(value):
    try:
        return float(str(value).replace(",", "").replace("₹", "").strip())
    except (ValueError, TypeError):
        return None

def pick(row, names):
    lower = {str(k).strip().lower(): v for k, v in row.items() if k}
    for name in names:
        value = lower.get(name.lower())
        if value is not None and str(value).strip():
            return value
    return None

def find_file(config):
    for filename in config["files"]:
        path = ROOT / filename
        if path.is_file():
            return path
    return None

def closed(row):
    status = str(pick(row, ["status", "trade_status"]) or "").upper()
    if status in {"OPEN", "ACTIVE", "PENDING", "ENTRY"}:
        return False
    exit_time = pick(row, ["exit_time", "closed_at", "exit_timestamp"])
    exit_price = pick(row, ["exit", "exit_price"])
    if status in {"CLOSED", "EXITED", "COMPLETE", "COMPLETED"}:
        return True
    return bool(exit_time or exit_price)

def read_trades(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def summarize():
    summaries, details = [], []
    for name, config in SCANNERS.items():
        path = find_file(config)
        if path is None:
            summaries.append({
                "scanner": name, "source_file": "NOT FOUND",
                "status": "NO PAPER CSV FOUND", "closed_trades": 0,
                "wins": 0, "losses": 0, "win_rate_pct": "",
                "net_pnl_inr": "", "unknown_pnl_trades": 0,
            })
            continue
        try:
            rows = read_trades(path)
        except Exception as exc:
            summaries.append({
                "scanner": name, "source_file": path.name,
                "status": f"READ ERROR: {exc}", "closed_trades": 0,
                "wins": 0, "losses": 0, "win_rate_pct": "",
                "net_pnl_inr": "", "unknown_pnl_trades": 0,
            })
            continue
        complete = [r for r in rows if closed(r)]
        pnl_values = []
        wins = losses = unknown = 0
        for r in complete:
            pnl = number(pick(r, [
                "net", "net_pnl", "net_pnl_inr", "net_profit",
                "net_profit_inr", "pnl_inr", "profit_loss",
                "net_profit_loss", "realized_pnl", "realised_pnl",
            ]))
            if pnl is None:
                unknown += 1
            else:
                pnl_values.append(pnl)
                wins += pnl > 0
                losses += pnl < 0
            details.append({
                "scanner": name,
                "symbol": pick(r, ["symbol", "stock", "ticker", "pair"]) or "",
                "strategy": pick(r, ["strategy", "setup"]) or "",
                "side": pick(r, ["side", "direction"]) or "",
                "entry_time": pick(r, ["entry_time", "entry_at"]) or "",
                "exit_time": pick(r, ["exit_time", "closed_at"]) or "",
                "net_pnl_inr": "" if pnl is None else round(pnl, 2),
                "source_file": path.name,
            })
        decisive = wins + losses
        summaries.append({
            "scanner": name, "source_file": path.name,
            "status": "OK" if not unknown else "PARTIAL PNL",
            "closed_trades": len(complete),
            "wins": wins, "losses": losses,
            "win_rate_pct": round(100 * wins / decisive, 2) if decisive else "",
            "net_pnl_inr": round(sum(pnl_values), 2) if not unknown else "",
            "unknown_pnl_trades": unknown,
        })
    return summaries, details

def write_csv(path, rows, columns):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)

def make_html(summaries, details):
    from html import escape
    now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    cols = ["scanner", "status", "closed_trades", "wins", "losses",
            "win_rate_pct", "net_pnl_inr", "source_file"]
    headers = ["Scanner", "Data status", "Closed", "Wins", "Losses",
               "Win rate %", "Net P/L ₹", "Source CSV"]
    def cell(v):
        return escape(str(v)) if v != "" else "—"
    rows = "\n".join("<tr>" + "".join(f"<td>{cell(r.get(k,''))}</td>" for k in cols) + "</tr>" for r in summaries)
    known = all(r["status"] == "OK" for r in summaries)
    total = sum(r["net_pnl_inr"] for r in summaries) if known else None
    total_text = f"₹{total:,.2f}" if total is not None else "Not available (missing/partial data)"
    HTML.write_text(f"""<!doctype html><html lang="en"><meta charset="utf-8">
<title>3 NSE Equity Paper Scanners — Consolidated Report</title>
<style>body{{font:16px system-ui;margin:35px;background:#f6f8fb;color:#17202a}}
main{{max-width:1200px;margin:auto}}h1{{margin-bottom:5px}}
small{{color:#596579}}table{{border-collapse:collapse;width:100%;background:white}}
td,th{{padding:14px;text-align:left;border-bottom:1px solid #e3e7ee}}
th{{background:#123b69;color:white}}.total{{font-size:23px;font-weight:bold;margin:25px 0}}
.note{{background:#fff4d8;padding:15px;border-radius:8px}}</style>
<main><h1>3 NSE Equity Intraday Paper Scanners</h1><small>Generated {escape(now)}</small>
<p class="total">Combined net P/L: {escape(total_text)}</p>
<table><thead><tr>{''.join(f'<th>{h}</th>' for h in headers)}</tr></thead>
<tbody>{rows}</tbody></table>
<p class="note">Only recorded CLOSED paper trades are counted. Missing CSVs or missing
net P/L values prevent a reliable combined total. Backtest results are excluded.
This is delayed Yahoo paper testing, not Zerodha-executed profit.</p>
<p>Closed trade rows found: {len(details)}. Download CSV reports from GitHub Actions artifacts.</p></main></html>""", encoding="utf-8")

def main():
    summaries, details = summarize()
    write_csv(REPORT, summaries, [
        "scanner", "source_file", "status", "closed_trades", "wins", "losses",
        "win_rate_pct", "net_pnl_inr", "unknown_pnl_trades"
    ])
    write_csv(DETAIL, details, [
        "scanner", "symbol", "strategy", "side", "entry_time", "exit_time",
        "net_pnl_inr", "source_file"
    ])
    make_html(summaries, details)
    for r in summaries:
        print(f'{r["scanner"]}: {r["status"]} | closed={r["closed_trades"]} | net INR={r["net_pnl_inr"]}')
    print("Reports:", REPORT.name, DETAIL.name, HTML.name)
    if any(r["status"] != "OK" for r in summaries):
        print("IMPORTANT: Missing or incomplete source paper CSV; do not treat combined P/L as zero.")

if __name__ == "__main__":
    main()
