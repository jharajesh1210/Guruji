
import csv
import json
from pathlib import Path

# ALL 6 SCANNERS RESULTS
# 7 existing scanners + 2 Gold pairs
# PAPER RESULTS ONLY - NO REAL ORDERS

SCANNERS = [
    (
        "TOP100 ORIGINAL",
        "TOP100_CRIPTO_AMC_POC_state.json",
        "top100",
    ),
    (
        "ASIAN LONDON NY ORIGINAL",
        "ASIAN_LONDON_NY_PAPER_state.json",
        "asian",
    ),
    (
        "BTC SMC ORIGINAL",
        "BTC_SMC_Paper_state.json",
        "btc",
    ),
    (
        "TOP100 NEWS IMPACT",
        "TOP100_CRIPTO_AMC_POC_NEWS_IMPACT_state.json",
        "top100",
    ),
    (
        "ASIAN LONDON NY NEWS IMPACT",
        "ASIAN_LONDON_NY_PAPER_NEWS_IMPACT_state.json",
        "asian",
    ),
    (
        "BTC SMC NEWS IMPACT",
        "BTC_SMC_Paper_NEWS_IMPACT_state.json",
        "btc",
    ),
    (
        "TOP100 AMC POC V4 PAPER",
        "TOP100_CRIPTO_AMC_POC_V4_state.json",
        "top100",
    ),
    (
        "GOLD XAU USDT",
        "GOLD_COMBO_PAPER_state.json",
        "gold_xau",
    ),
    (
        "GOLD PAXG USDT",
        "GOLD_COMBO_PAPER_state.json",
        "gold_paxg",
    ),
]

HEADERS = [
    "Scanner",
    "State File",
    "Status",
    "Open Trades",
    "Closed Trades",
    "Wins",
    "Losses",
    "Net Return Sum (%)",
    "Notes",
]


def count_positions(value):
    if isinstance(value, (list, dict)):
        return len(value)
    return "N/A"


def build_row(name, filename, kind):
    path = Path(filename)

    if not path.is_file():
        return [
            name,
            filename,
            "MISSING",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "State file missing",
        ]

    try:
        state = json.loads(
            path.read_text(encoding="utf-8")
        )

        if not isinstance(state, dict):
            raise ValueError("Expected JSON object")

    except (OSError, ValueError) as exc:
        return [
            name,
            filename,
            "ERROR",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            str(exc),
        ]

    if kind.startswith("gold_"):

        symbol = (
            "XAU"
            if kind == "gold_xau"
            else "PAXG"
        )

        record = state.get(symbol, {})

        if symbol not in state:
            open_count = "N/A"
        else:
            open_count = int(
                record.get("position") is not None
            )

        closed = record.get(
            "closed_count", "N/A"
        )

        wins = record.get(
            "wins", "N/A"
        )

        net = record.get(
            "net_return_sum_pct", "N/A"
        )

        net_usdt = record.get(
            "net_pnl_usdt", "N/A"
        )

        notes = (
            "Forward-only paper trading; "
            "Net P/L USDT = "
            + str(net_usdt)
        )

    elif kind == "top100":

        open_count = count_positions(
            state.get("open_trades")
        )

        closed = state.get(
            "closed_count", "N/A"
        )

        wins = state.get(
            "closed_wins", "N/A"
        )

        net = state.get(
            "net_return_sum_pct", "N/A"
        )

        notes = (
            "Cumulative net return sum; "
            "not account ROI"
        )

    elif kind == "btc":

        open_count = count_positions(
            state.get("positions")
        )

        closed = state.get(
            "closed", "N/A"
        )

        wins = state.get(
            "wins", "N/A"
        )

        net = state.get(
            "net_pct", "N/A"
        )

        notes = (
            "Cumulative net return sum; "
            "not account ROI"
        )

    else:

        open_count = count_positions(
            state.get("positions")
        )

        closed = "N/A"
        wins = "N/A"
        net = "N/A"

        entered = state.get(
            "entered_days", {}
        )

        if isinstance(entered, dict):
            entered_count = len(entered)
        else:
            entered_count = "unknown"

        notes = (
            "Closed P/L summary unavailable; "
            "entered_days = "
            + str(entered_count)
        )

    if (
        isinstance(closed, (int, float))
        and isinstance(wins, (int, float))
    ):
        losses = closed - wins
    else:
        losses = "N/A"

    return [
        name,
        filename,
        "OK",
        open_count,
        closed,
        wins,
        losses,
        net,
        notes,
    ]


def main():

    rows = [
        build_row(*scanner)
        for scanner in SCANNERS
    ]

    output = Path(
        "ALL_6_SCANNERS_RESULTS.csv"
    )

    with output.open(
        "w",
        encoding="utf-8-sig",
        newline=""
    ) as file:

        writer = csv.writer(file)

        writer.writerow(HEADERS)
        writer.writerows(rows)

    print(
        f"Created {output} "
        f"with {len(rows)} scanner summaries"
    )

    print("=" * 60)

    for row in rows:

        print(
            f"{row[0]} | "
            f"Status={row[2]} | "
            f"Open={row[3]} | "
            f"Closed={row[4]} | "
            f"Wins={row[5]} | "
            f"Losses={row[6]} | "
            f"Net%={row[7]}"
        )

    print("=" * 60)

    print(
        "N/A means data unavailable, "
        "not zero."
    )

    print(
        "Gold results come from "
        "GOLD_COMBO_PAPER_state.json"
    )

    print(
        "No real trading orders placed."
    )


if __name__ == "__main__":
    main()
