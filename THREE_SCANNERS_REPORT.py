
#!/usr/bin/env python3
# THREE_SCANNERS_REPORT.py
# COMBINED NSE EQUITY PAPER TRADING REPORT
# Read-only: Does not modify trading scanner files.

import csv
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape

ROOT = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")

SUMMARY_FILE = ROOT / "THREE_SCANNERS_SUMMARY.csv"
TRADES_FILE = ROOT / "THREE_SCANNERS_ALL_TRADES.csv"
HTML_FILE = ROOT / "THREE_SCANNERS_REPORT.html"

SCANNERS = {
    "NIFTY50 EQUITY INTRADAY": {
        "states": [
            "NIFTY50_EQUITY_PAPER_STATE.json",
            "NIFTY50_EQUITY_STATE.json",
        ],
        "trades": [
            "NIFTY50_EQUITY_PAPER_TRADES.csv",
            "NIFTY50_EQUITY_TRADES.csv",
        ],
    },
    "RELIANCE 5MIN": {
        "states": [
            "RELIANCE_PAPER_STATE.json",
        ],
        "trades": [
            "RELIANCE_PAPER_TRADES.csv",
        ],
    },
    "RIL CANDLESTICK BIBLE": {
        "states": [
            "RIL_CANDLESTICK_BIBLE_state.json",
            "RIL_CANDLESTICK_BIBLE_STATE.json",
        ],
        "trades": [
            "RIL_CANDLESTICK_BIBLE_trades.csv",
            "RIL_CANDLESTICK_BIBLE_TRADES.csv",
        ],
    },
}


def find_file(names):
    for name in names:
        path = ROOT / name
        if path.is_file():
            return path
    return None


def to_number(value):
    if value is None or value == "":
        return None

    try:
        return float(
            str(value)
            .replace(",", "")
            .replace("₹", "")
            .strip()
        )
    except (ValueError, TypeError):
        return None


def get_value(row, names):
    if not isinstance(row, dict):
        return None

    lookup = {
        str(key).strip().lower(): value
        for key, value in row.items()
        if key is not None
    }

    for name in names:
        value = lookup.get(name.lower())

        if value is not None and str(value).strip():
            return value

    return None


def load_state(path):
    if path is None:
        return None, "STATE NOT FOUND"

    try:
        with path.open(encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            return None, "STATE FORMAT UNKNOWN"

        return state, "STATE FOUND"

    except Exception as error:
        return None, "STATE ERROR: " + str(error)


def position_info(state):
    if state is None:
        return "UNKNOWN"

    for key in [
        "position",
        "open_position",
        "positions",
        "open_positions",
    ]:
        if key not in state:
            continue

        value = state[key]

        if value is None or value == [] or value == {}:
            return "NONE"

        if isinstance(value, list):
            return str(len(value))

        if isinstance(value, dict):
            if not value:
                return "NONE"

            # Some scanners store positions by symbol.
            if "side" in value or "entry" in value:
                return "1"

            return str(len(value))

        if isinstance(value, bool):
            return "1" if value else "NONE"

        return str(value)

    return "UNKNOWN"


def state_equity(state):
    if state is None:
        return ""

    value = get_value(
        state,
        ["equity", "capital", "balance"]
    )

    result = to_number(value)

    return "" if result is None else round(result, 2)


def load_trades(path):
    if path is None:
        return None, "TRADES CSV NOT FOUND"

    try:
        with path.open(
            encoding="utf-8-sig",
            newline=""
        ) as file:
            reader = csv.DictReader(file)
            rows = list(reader)

        return rows, "CSV FOUND"

    except Exception as error:
        return None, "CSV ERROR: " + str(error)


def is_closed(row):
    status = str(
        get_value(
            row,
            ["status", "trade_status"]
        ) or ""
    ).upper()

    if status in [
        "OPEN",
        "ACTIVE",
        "PENDING",
        "ENTRY",
    ]:
        return False

    if status in [
        "CLOSED",
        "EXITED",
        "COMPLETE",
        "COMPLETED",
    ]:
        return True

    exit_time = get_value(
        row,
        ["exit_time", "closed_at", "exit_timestamp"]
    )

    exit_price = get_value(
        row,
        ["exit", "exit_price"]
    )

    return bool(exit_time or exit_price)


def trade_pnl(row):
    return to_number(
        get_value(
            row,
            [
                "net",
                "net_pnl",
                "net_pnl_inr",
                "net_profit",
                "net_profit_inr",
                "pnl_inr",
                "profit_loss",
                "net_profit_loss",
                "realized_pnl",
                "realised_pnl",
            ]
        )
    )


def build_report():
    summary = []
    all_trades = []

    for scanner, config in SCANNERS.items():

        state_path = find_file(config["states"])
        trades_path = find_file(config["trades"])

        state, state_status = load_state(state_path)
        rows, csv_status = load_trades(trades_path)

        open_position = position_info(state)
        equity = state_equity(state)

        closed = []
        wins = 0
        losses = 0
        unknown_pnl = 0
        pnl_values = []

        if rows is not None:
            closed = [
                row for row in rows
                if is_closed(row)
            ]

            for row in closed:

                pnl = trade_pnl(row)

                if pnl is None:
                    unknown_pnl += 1
                else:
                    pnl_values.append(pnl)

                    if pnl > 0:
                        wins += 1
                    elif pnl < 0:
                        losses += 1

                all_trades.append({
                    "scanner": scanner,
                    "symbol": get_value(
                        row,
                        ["symbol", "stock", "ticker", "pair"]
                    ) or "",
                    "strategy": get_value(
                        row,
                        ["strategy", "setup"]
                    ) or "",
                    "side": get_value(
                        row,
                        ["side", "direction"]
                    ) or "",
                    "entry_time": get_value(
                        row,
                        ["entry_time", "entry_at"]
                    ) or "",
                    "exit_time": get_value(
                        row,
                        ["exit_time", "closed_at"]
                    ) or "",
                    "net_pnl_inr": (
                        ""
                        if pnl is None
                        else round(pnl, 2)
                    ),
                })

        if rows is None:
            closed_count = ""
            net_pnl = ""
            win_rate = ""
            data_status = csv_status

        else:
            closed_count = len(closed)

            if unknown_pnl:
                net_pnl = ""
                data_status = "PARTIAL PNL"
            else:
                net_pnl = round(sum(pnl_values), 2)
                data_status = "CSV FOUND"

            decisive = wins + losses

            win_rate = (
                round(wins / decisive * 100, 2)
                if decisive
                else ""
            )

        summary.append({
            "scanner": scanner,
            "state_status": state_status,
            "trade_data_status": data_status,
            "open_position": open_position,
            "closed_trades": closed_count,
            "wins": wins if rows is not None else "",
            "losses": losses if rows is not None else "",
            "win_rate_pct": win_rate,
            "net_pnl_inr": net_pnl,
            "paper_equity_inr": equity,
            "state_file": (
                state_path.name
                if state_path else ""
            ),
            "trades_file": (
                trades_path.name
                if trades_path else ""
            ),
        })

        print("--------------------------------")
        print("SCANNER:", scanner)
        print("STATE:", state_status)
        print("TRADE DATA:", data_status)
        print("OPEN POSITION:", open_position)
        print("CLOSED TRADES:", closed_count)
        print("NET P/L INR:", net_pnl)

    return summary, all_trades


def save_csv(path, rows, columns):
    with path.open(
        "w",
        encoding="utf-8-sig",
        newline=""
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=columns
        )

        writer.writeheader()
        writer.writerows(rows)


def create_html(summary):
    now = datetime.now(IST).strftime(
        "%d-%m-%Y %I:%M %p IST"
    )

    columns = [
        "scanner",
        "state_status",
        "trade_data_status",
        "open_position",
        "closed_trades",
        "wins",
        "losses",
        "win_rate_pct",
        "net_pnl_inr",
    ]

    headers = [
        "Scanner",
        "State",
        "Trade CSV",
        "Open Position",
        "Closed",
        "Wins",
        "Losses",
        "Win Rate %",
        "Net P/L INR",
    ]

    def display(value):
        if value == "" or value is None:
            return "UNKNOWN"

        return escape(str(value))

    table_rows = ""

    for item in summary:
        table_rows += "<tr>"

        for key in columns:
            table_rows += (
                "<td>"
                + display(item.get(key))
                + "</td>"
            )

        table_rows += "</tr>"

    all_complete = all(
        item["net_pnl_inr"] != ""
        for item in summary
    )

    if all_complete:
        combined = round(
            sum(
                item["net_pnl_inr"]
                for item in summary
            ),
            2
        )

        combined_text = f"INR {combined:,.2f}"
    else:
        combined_text = (
            "UNKNOWN - Some trade CSVs "
            "are missing or incomplete"
        )

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Three NSE Equity Scanners Report</title>

<style>
body {{
    font-family: Arial, sans-serif;
    background: #f4f6fa;
    color: #17212b;
    margin: 35px;
}}

h1 {{
    color: #173c70;
}}

table {{
    width: 100%;
    border-collapse: collapse;
    background: white;
}}

th {{
    background: #173c70;
    color: white;
    padding: 12px;
    text-align: left;
}}

td {{
    padding: 12px;
    border-bottom: 1px solid #ddd;
}}

.total {{
    font-size: 24px;
    font-weight: bold;
    margin: 25px 0;
}}

.note {{
    background: #fff2d6;
    padding: 15px;
    margin-top: 25px;
}}
</style>
</head>

<body>

<h1>THREE NSE EQUITY PAPER SCANNERS</h1>

<p>Report Generated: {escape(now)}</p>

<div class="total">
Combined Net P/L: {escape(combined_text)}
</div>

<table>
<tr>
{''.join("<th>" + h + "</th>" for h in headers)}
</tr>
{table_rows}
</table>

<div class="note">
State file found does not prove scanner is
currently running.

Missing trade CSV does not mean zero trades
or zero profit.

Only recorded closed paper trades are counted.

Yahoo Finance paper trading is not actual
Zerodha execution.
</div>

</body>
</html>
"""

    HTML_FILE.write_text(
        html,
        encoding="utf-8"
    )


def main():
    print("=" * 55)
    print("THREE NSE EQUITY SCANNERS REPORT")
    print("=" * 55)

    summary, trades = build_report()

    save_csv(
        SUMMARY_FILE,
        summary,
        [
            "scanner",
            "state_status",
            "trade_data_status",
            "open_position",
            "closed_trades",
            "wins",
            "losses",
            "win_rate_pct",
            "net_pnl_inr",
            "paper_equity_inr",
            "state_file",
            "trades_file",
        ]
    )

    save_csv(
        TRADES_FILE,
        trades,
        [
            "scanner",
            "symbol",
            "strategy",
            "side",
            "entry_time",
            "exit_time",
            "net_pnl_inr",
        ]
    )

    create_html(summary)

    print("=" * 55)
    print("REPORT FILES CREATED")
    print(SUMMARY_FILE.name)
    print(TRADES_FILE.name)
    print(HTML_FILE.name)
    print("=" * 55)


if __name__ == "__main__":
    main()
