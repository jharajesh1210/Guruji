"""Build one honest six-scanner paper trading summary from GitHub state files.
Does not place orders or modify scanner state.
"""
import csv
import json
from pathlib import Path

SCANNERS = [
    ("TOP100 ORIGINAL", "TOP100_CRIPTO_AMC_POC_state.json", "top100"),
    ("ASIAN LONDON NY ORIGINAL", "ASIAN_LONDON_NY_PAPER_state.json", "asian"),
    ("BTC SMC ORIGINAL", "BTC_SMC_Paper_state.json", "btc"),
    ("TOP100 NEWS IMPACT", "TOP100_CRIPTO_AMC_POC_NEWS_IMPACT_state.json", "top100"),
    ("ASIAN LONDON NY NEWS IMPACT", "ASIAN_LONDON_NY_PAPER_NEWS_IMPACT_state.json", "asian"),
    ("BTC SMC NEWS IMPACT", "BTC_SMC_Paper_NEWS_IMPACT_state.json", "btc"),
]

HEADERS = ["Scanner", "State File", "Status", "Open Trades", "Closed Trades", "Wins", "Losses", "Net Return Sum (%)", "Notes"]

def count_positions(value):
    if isinstance(value, (list, dict)):
        return len(value)
    return "N/A"

def build_row(name, filename, kind):
    path = Path(filename)
    if not path.is_file():
        return [name, filename, "MISSING", "N/A", "N/A", "N/A", "N/A", "N/A", "State file missing"]
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Expected JSON object")
    except (OSError, ValueError) as exc:
        return [name, filename, "ERROR", "N/A", "N/A", "N/A", "N/A", "N/A", str(exc)]

    if kind == "top100":
        open_count = count_positions(state.get("open_trades"))
        closed = state.get("closed_count", "N/A")
        wins = state.get("closed_wins", "N/A")
        net = state.get("net_return_sum_pct", "N/A")
        notes = "Cumulative net return sum, not account ROI"
    elif kind == "btc":
        open_count = count_positions(state.get("positions"))
        closed = state.get("closed", "N/A")
        wins = state.get("wins", "N/A")
        net = state.get("net_pct", "N/A")
        notes = "Cumulative net return sum, not account ROI"
    else:
        open_count = count_positions(state.get("positions"))
        closed, wins, net = "N/A", "N/A", "N/A"
        entered = state.get("entered_days", {})
        notes = "State lacks closed P/L summary; entered_days=" + (str(len(entered)) if isinstance(entered, dict) else "unknown")

    losses = closed - wins if isinstance(closed, (int, float)) and isinstance(wins, (int, float)) else "N/A"
    return [name, filename, "OK", open_count, closed, wins, losses, net, notes]


def main():
    rows = [build_row(*scanner) for scanner in SCANNERS]
    output = Path("ALL_6_SCANNERS_RESULTS.csv")
    with output.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(HEADERS)
        writer.writerows(rows)
    print(f"Created {output} with {len(rows)} scanner summaries")
    for row in rows:
        print(f"{row[0]}: {row[2]}, open={row[3]}, closed={row[4]}, net={row[7]}")
    print("N/A means not available, not zero. No trades placed.")

if __name__ == "__main__":
    main()
