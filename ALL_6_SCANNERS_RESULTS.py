
import csv
import json
from pathlib import Path
from datetime import datetime, timezone

# ============================================================
# ALL SCANNERS MASTER RESULTS
# 7 EXISTING SCANNERS + GOLD XAU + GOLD PAXG
# PAPER TRADING REPORT ONLY - NO LIVE ORDERS
# ============================================================

ROOT = Path(__file__).resolve().parent

OUTPUT_FILE = ROOT / "ALL_6_SCANNERS_RESULTS.csv"

SCANNERS = [
    {
        "name": "TOP100 ORIGINAL",
        "file": "TOP100_CRIPTO_AMC_POC_state.json",
        "kind": "top100",
    },
    {
        "name": "ASIAN LONDON NY ORIGINAL",
        "file": "ASIAN_LONDON_NY_PAPER_state.json",
        "kind": "asian",
    },
    {
        "name": "BTC SMC ORIGINAL",
        "file": "BTC_SMC_Paper_state.json",
        "kind": "btc",
    },
    {
        "name": "TOP100 NEWS IMPACT",
        "file": "TOP100_CRIPTO_AMC_POC_NEWS_IMPACT_state.json",
        "kind": "top100",
    },
    {
        "name": "ASIAN LONDON NY NEWS IMPACT",
        "file": "ASIAN_LONDON_NY_PAPER_NEWS_IMPACT_state.json",
        "kind": "asian",
    },
    {
        "name": "BTC SMC NEWS IMPACT",
        "file": "BTC_SMC_Paper_NEWS_IMPACT_state.json",
        "kind": "btc",
    },
    {
        "name": "TOP100 AMC POC V4 PAPER",
        "file": "TOP100_CRIPTO_AMC_POC_V4_state.json",
        "kind": "top100",
    },
    {
        "name": "GOLD XAU USDT",
        "file": "GOLD_COMBO_PAPER_state.json",
        "kind": "gold",
        "symbol": "XAU",
    },
    {
        "name": "GOLD PAXG USDT",
        "file": "GOLD_COMBO_PAPER_state.json",
        "kind": "gold",
        "symbol": "PAXG",
    },
]

HEADERS = [
    "Scanner",
    "State File",
    "Status",
    "Open Trades",
    "Open Legs",
    "Closed Trades",
    "Wins",
    "Losses",
    "Win Rate (%)",
    "Net Return Sum (%)",
    "Net PnL (USDT)",
    "Notes",
]


def number(value):
    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None

    return None


def show(value):
    if value is None:
        return "N/A"

    if isinstance(value, float):
        return round(value, 4)

    return value


def read_state(filename):
    path = ROOT / filename

    if not path.exists():
        return None, "MISSING"

    try:
        with path.open("r", encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return None, "INVALID JSON"

        return state, "OK"

    except Exception as e:
        return None, "ERROR: " + str(e)


def positions_list(value):
    if isinstance(value, list):
        return [
            x for x in value
            if isinstance(x, dict)
        ]

    if isinstance(value, dict):
        return [
            x for x in value.values()
            if isinstance(x, dict)
        ]

    return []


def asian_position_counts(state):
    positions = positions_list(
        state.get("positions", [])
    )

    # Each entry may have separate 2R, 3R and 4R legs.
    # Count one entry as one trade.
    trade_keys = set()

    for index, position in enumerate(positions):
        symbol = str(
            position.get("symbol", "")
        )

        direction = str(
            position.get("direction", "")
        )

        entry = str(
            position.get("entry", "")
        )

        entry_time = str(
            position.get("entry_time_utc", "")
        )

        if symbol and entry_time:
            key = (
                symbol,
                direction,
                entry,
                entry_time,
            )
        else:
            key = ("unknown", index)

        trade_keys.add(key)

    return len(trade_keys), len(positions)


def extract_results(state, kind, symbol=None):

    result = {
        "open": "N/A",
        "legs": "N/A",
        "closed": "N/A",
        "wins": "N/A",
        "losses": "N/A",
        "win_rate": "N/A",
        "net_pct": "N/A",
        "net_usdt": "N/A",
        "notes": "",
    }

    if kind == "asian":

        open_trades, open_legs = (
            asian_position_counts(state)
        )

        result["open"] = open_trades
        result["legs"] = open_legs

        result["notes"] = (
            "Open entries grouped by "
            "symbol, direction, entry and time; "
            "2R/3R/4R are separate legs"
        )

    elif kind == "gold":

        pair = state.get(symbol)

        if not isinstance(pair, dict):
            result["notes"] = (
                "Pair data missing from Gold state"
            )
            return result

        position = pair.get("position")

        result["open"] = (
            1 if position is not None else 0
        )

        result["legs"] = "N/A"

        result["closed"] = pair.get(
            "closed_count", "N/A"
        )

        result["wins"] = pair.get(
            "wins", "N/A"
        )

        result["net_pct"] = pair.get(
            "net_return_sum_pct", "N/A"
        )

        result["net_usdt"] = pair.get(
            "net_pnl_usdt", "N/A"
        )

        result["notes"] = (
            "Forward-only Gold automatic paper trading"
        )

    elif kind == "top100":

        positions = state.get("open_trades")

        if isinstance(positions, (list, dict)):
            result["open"] = len(positions)

        result["closed"] = state.get(
            "closed_count", "N/A"
        )

        result["wins"] = state.get(
            "closed_wins", "N/A"
        )

        result["net_pct"] = state.get(
            "net_return_sum_pct", "N/A"
        )

        result["notes"] = (
            "Cumulative trade returns, not account ROI"
        )

    elif kind == "btc":

        positions = state.get("positions")

        if isinstance(positions, (list, dict)):
            result["open"] = len(positions)

        result["closed"] = state.get(
            "closed", "N/A"
        )

        result["wins"] = state.get(
            "wins", "N/A"
        )

        result["net_pct"] = state.get(
            "net_pct", "N/A"
        )

        result["notes"] = (
            "Cumulative trade returns, not account ROI"
        )

    closed = number(result["closed"])
    wins = number(result["wins"])

    if closed is not None and wins is not None:

        losses = closed - wins

        result["losses"] = int(losses)

        if closed > 0:
            result["win_rate"] = round(
                wins / closed * 100, 2
            )
        else:
            result["win_rate"] = 0.0

    return result


def make_row(scanner):

    name = scanner["name"]
    filename = scanner["file"]
    kind = scanner["kind"]
    symbol = scanner.get("symbol")

    state, status = read_state(filename)

    if state is None:
        return [
            name,
            filename,
            status,
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "State unavailable",
        ]

    result = extract_results(
        state,
        kind,
        symbol,
    )

    return [
        name,
        filename,
        status,
        show(result["open"]),
        show(result["legs"]),
        show(result["closed"]),
        show(result["wins"]),
        show(result["losses"]),
        show(result["win_rate"]),
        show(result["net_pct"]),
        show(result["net_usdt"]),
        result["notes"],
    ]


def main():

    print("=" * 70)
    print("ALL SCANNERS MASTER PAPER RESULTS")
    print(
        "UTC:",
        datetime.now(timezone.utc).isoformat()
    )
    print("=" * 70)

    rows = []

    for scanner in SCANNERS:
        row = make_row(scanner)
        rows.append(row)

    with OUTPUT_FILE.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as f:

        writer = csv.writer(f)

        writer.writerow(HEADERS)
        writer.writerows(rows)

    print(
        "Created:",
        OUTPUT_FILE.name
    )

    print(
        "Total scanner rows:",
        len(rows)
    )

    print("-" * 70)

    for row in rows:

        print(
            f"{row[0]} | "
            f"Status={row[2]} | "
            f"Open={row[3]} | "
            f"Legs={row[4]} | "
            f"Closed={row[5]} | "
            f"Wins={row[6]} | "
            f"Losses={row[7]} | "
            f"WinRate={row[8]}% | "
            f"NetReturn={row[9]}% | "
            f"NetUSDT={row[10]}"
        )

    print("-" * 70)

    print(
        "N/A = data not available in state file."
    )

    print(
        "Asian 2R/3R/4R legs are grouped "
        "as one entry trade."
    )

    print(
        "No real orders placed by this report."
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
