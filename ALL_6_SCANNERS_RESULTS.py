
import csv
import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

# ============================================================
# ALL SCANNERS MASTER PAPER RESULTS
# 7 OLD SCANNERS + GOLD XAU + GOLD PAXG
# NO REAL TRADING ORDERS
# ============================================================

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "ALL_6_SCANNERS_RESULTS.csv"

SCANNERS = [
    {
        "name": "TOP100 ORIGINAL",
        "state": "TOP100_CRIPTO_AMC_POC_state.json",
        "kind": "top100",
    },
    {
        "name": "ASIAN LONDON NY ORIGINAL",
        "state": "ASIAN_LONDON_NY_PAPER_state.json",
        "trades": "ASIAN_LONDON_NY_PAPER_trades.csv",
        "kind": "asian",
    },
    {
        "name": "BTC SMC ORIGINAL",
        "state": "BTC_SMC_Paper_state.json",
        "kind": "btc",
    },
    {
        "name": "TOP100 NEWS IMPACT",
        "state": "TOP100_CRIPTO_AMC_POC_NEWS_IMPACT_state.json",
        "kind": "top100",
    },
    {
        "name": "ASIAN LONDON NY NEWS IMPACT",
        "state": "ASIAN_LONDON_NY_PAPER_NEWS_IMPACT_state.json",
        "trades": "ASIAN_LONDON_NY_PAPER_NEWS_IMPACT_trades.csv",
        "kind": "asian",
    },
    {
        "name": "BTC SMC NEWS IMPACT",
        "state": "BTC_SMC_Paper_NEWS_IMPACT_state.json",
        "kind": "btc",
    },
    {
        "name": "TOP100 AMC POC V4 PAPER",
        "state": "TOP100_CRIPTO_AMC_POC_V4_state.json",
        "kind": "top100",
    },
    {
        "name": "GOLD XAU USDT",
        "state": "GOLD_COMBO_PAPER_state.json",
        "kind": "gold",
        "symbol": "XAU",
    },
    {
        "name": "GOLD PAXG USDT",
        "state": "GOLD_COMBO_PAPER_state.json",
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


def numeric(value):
    if isinstance(value, bool):
        return None

    try:
        if value is None or value == "":
            return None
        return float(value)
    except (ValueError, TypeError):
        return None


def display(value):
    if value is None:
        return "N/A"

    if isinstance(value, float):
        return round(value, 4)

    return value


def read_json(filename):
    path = ROOT / filename

    if not path.exists():
        return None, "MISSING"

    try:
        with path.open(
            "r", encoding="utf-8"
        ) as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return None, "INVALID JSON"

        return state, "OK"

    except (OSError, ValueError) as exc:
        return None, "ERROR: " + str(exc)


def read_csv(filename):
    path = ROOT / filename

    if not path.exists():
        return None, "MISSING"

    try:
        with path.open(
            "r", encoding="utf-8-sig", newline=""
        ) as f:
            rows = list(csv.DictReader(f))

        return rows, "OK"

    except (OSError, csv.Error) as exc:
        return None, "ERROR: " + str(exc)


def get_positions(state):
    positions = state.get("positions", [])

    if isinstance(positions, list):
        return [
            p for p in positions
            if isinstance(p, dict)
        ]

    if isinstance(positions, dict):
        return [
            p for p in positions.values()
            if isinstance(p, dict)
        ]

    return []


def asian_trade_key(row):
    return (
        str(row.get("symbol", "")),
        str(row.get("direction", "")),
        str(row.get("entry_time_utc", "")),
        str(
            row.get(
                "entry_price",
                row.get("entry", "")
            )
        ),
    )


def asian_open_key(row):
    return (
        str(row.get("symbol", "")),
        str(row.get("direction", "")),
        str(row.get("entry_time_utc", "")),
        str(row.get("entry", "")),
    )


def asian_results(state, csv_filename):
    result = {
        "open": 0,
        "legs": 0,
        "closed": "N/A",
        "wins": "N/A",
        "losses": "N/A",
        "win_rate": "N/A",
        "net_pct": "N/A",
        "net_usdt": "N/A",
        "notes": "",
    }

    positions = get_positions(state)

    open_keys = set()

    for index, pos in enumerate(positions):
        key = asian_open_key(pos)

        if key[0] and key[2]:
            open_keys.add(key)
        else:
            open_keys.add(("unknown", index))

    result["open"] = len(open_keys)
    result["legs"] = len(positions)

    rows, csv_status = read_csv(csv_filename)

    if rows is None:
        result["notes"] = (
            "Open trades from JSON; "
            "closed trade CSV " + csv_status
        )
        return result

    if not rows:
        result.update({
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "net_pct": 0.0,
            "net_usdt": 0.0,
            "notes": "Trade CSV empty",
        })
        return result

    groups = defaultdict(dict)

    for row in rows:
        key = asian_trade_key(row)
        leg = str(row.get("leg", "")).upper()

        if not all(key[:3]):
            continue

        if leg not in ("2R", "3R", "4R"):
            continue

        # Prevent duplicate exit records for same leg.
        groups[key][leg] = row

    closed_trades = 0
    wins = 0
    losses = 0

    total_pct = 0.0
    total_usdt = 0.0

    partial_trades = 0
    invalid_trades = 0

    for key, legs in groups.items():
        if not all(
            leg in legs
            for leg in ("2R", "3R", "4R")
        ):
            partial_trades += 1
            continue

        leg_pct = []
        leg_usdt = []

        valid = True

        for leg in ("2R", "3R", "4R"):
            row = legs[leg]

            pct = numeric(
                row.get("net_return_pct_per_leg")
            )

            pnl = numeric(
                row.get("paper_pnl_usdt")
            )

            if pct is None or pnl is None:
                valid = False
                break

            leg_pct.append(pct)
            leg_usdt.append(pnl)

        if not valid:
            invalid_trades += 1
            continue

        # 3 equal legs make 1 complete trade.
        # Percentage is the average of the three legs.
        trade_pct = sum(leg_pct) / 3.0
        trade_usdt = sum(leg_usdt)

        closed_trades += 1
        total_pct += trade_pct
        total_usdt += trade_usdt

        if trade_usdt > 0:
            wins += 1
        elif trade_usdt < 0:
            losses += 1

    result["closed"] = closed_trades
    result["wins"] = wins
    result["losses"] = losses

    result["net_pct"] = round(total_pct, 6)
    result["net_usdt"] = round(total_usdt, 2)

    if closed_trades > 0:
        result["win_rate"] = round(
            wins / closed_trades * 100, 2
        )
    else:
        result["win_rate"] = 0.0

    result["notes"] = (
        "Asian CSV checked; "
        f"partial trades={partial_trades}; "
        f"invalid trades={invalid_trades}; "
        "3 legs = 1 complete trade"
    )

    return result


def gold_results(state, symbol):
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

    pair = state.get(symbol)

    if not isinstance(pair, dict):
        result["notes"] = (
            "Gold pair state unavailable"
        )
        return result

    result["open"] = (
        1 if pair.get("position") is not None else 0
    )

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

    closed = numeric(result["closed"])
    wins = numeric(result["wins"])

    if closed is not None and wins is not None:
        result["losses"] = int(
            max(0, closed - wins)
        )

        result["win_rate"] = (
            round(wins / closed * 100, 2)
            if closed > 0 else 0.0
        )

    result["notes"] = (
        "Gold forward-only automatic paper trading"
    )

    return result


def standard_results(state, kind):
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

    if kind == "top100":
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

    closed = numeric(result["closed"])
    wins = numeric(result["wins"])

    if closed is not None and wins is not None:
        result["losses"] = int(
            max(0, closed - wins)
        )

        result["win_rate"] = (
            round(wins / closed * 100, 2)
            if closed > 0 else 0.0
        )

    result["notes"] = (
        "Cumulative trade returns; "
        "not account ROI"
    )

    return result


def build_row(scanner):
    name = scanner["name"]
    filename = scanner["state"]
    kind = scanner["kind"]

    state, status = read_json(filename)

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

    if kind == "asian":
        result = asian_results(
            state,
            scanner["trades"]
        )

    elif kind == "gold":
        result = gold_results(
            state,
            scanner["symbol"]
        )

    else:
        result = standard_results(
            state,
            kind
        )

    return [
        name,
        filename,
        status,
        display(result["open"]),
        display(result["legs"]),
        display(result["closed"]),
        display(result["wins"]),
        display(result["losses"]),
        display(result["win_rate"]),
        display(result["net_pct"]),
        display(result["net_usdt"]),
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
        row = build_row(scanner)
        rows.append(row)

    with OUTPUT.open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)
        writer.writerows(rows)

    print(
        "Created:",
        OUTPUT.name,
        "| Scanner rows:",
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
            f"NetUSDT={row[10]} | "
            f"Notes={row[11]}"
        )

    print("-" * 70)
    print("N/A = source result not available.")
    print("Asian 2R/3R/4R = one completed trade.")
    print("Only fully closed Asian trades count.")
    print("No real orders placed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
