#!/usr/bin/env python3
"""Replay results ko jodkar Telegram par report bhejta hai.
Usage: python replay/replay_report.py RESULTS_DIR DATE_LABEL
"""
import csv
import glob
import json
import os
import sys
import urllib.request
from pathlib import Path

ENTRY_WORDS = ("ENTRY", "BUY", "SELL", "LONG", "SHORT")
EXIT_WORDS = ("EXIT", "STOP", "SL HIT", "TARGET", "TP1", "TP2", "CLOSED", "EOD", "PROFIT", "LOSS")
PNL_COLS = ("net_inr", "paper_pnl_usdt", "net_pnl_usdt", "net_return_pct_per_leg", "net_pct", "pnl", "net")


def send(text):
    token, chat = os.environ.get("BOT_TOKEN"), os.environ.get("CHAT_ID")
    print(text)
    print("-" * 40)
    if not token or not chat:
        return
    data = json.dumps({"chat_id": chat, "text": text[:4000]}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def kind(msg):
    up = msg.upper()
    first = up.splitlines()[0] if up else ""
    if any(w in first for w in ("EXIT",)) or any(w in up for w in ("SL HIT", "TARGET HIT", "STOP HIT")):
        return "exit"
    if any(w in first for w in ENTRY_WORDS) or "ENTRY" in up:
        return "entry"
    if any(w in up for w in EXIT_WORDS):
        return "exit"
    return "other"


def trades_summary(folder):
    rows, pnl, col_used = 0, 0.0, None
    for f in glob.glob(os.path.join(folder, "*trades*.csv")) + glob.glob(os.path.join(folder, "*TRADES*.csv")):
        try:
            with open(f, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    rows += 1
                    for c in PNL_COLS:
                        if c in r and r[c] not in ("", None):
                            try:
                                pnl += float(r[c])
                                col_used = c
                            except ValueError:
                                pass
                            break
        except OSError:
            pass
    return rows, pnl, col_used


def main():
    res_dir, label = sys.argv[1], sys.argv[2]
    bots = []
    for jf in sorted(glob.glob(os.path.join(res_dir, "*", "replay_*.json"))):
        d = json.load(open(jf))
        folder = os.path.dirname(jf)
        msgs = d.get("telegram", [])
        ent = [m for m in msgs if kind(m["text"]) == "entry"]
        ex = [m for m in msgs if kind(m["text"]) == "exit"]
        rows, pnl, col = trades_summary(folder)
        bots.append((d, msgs, ent, ex, rows, pnl, col))

    expected = os.environ.get("EXPECTED_BOTS", "").split()
    got = {b[0]["bot"] for b in bots}
    missing = [b for b in expected if b not in got]

    lines = [f"🔁 GURUJI REPLAY TEST - {label}",
             "Aaj ki candles par har bot dobara chalaya (paper, koi asli order nahi)", ""]
    for d, msgs, ent, ex, rows, pnl, col in bots:
        status = "⚠️" if d["errors"] else "✅"
        unit = "INR" if col == "net_inr" else ("%" if col and "pct" in col else "USDT")
        pnl_txt = f" | band trade: {rows} | P&L: {pnl:+.2f} {unit}" if rows else ""
        err = f" | error steps: {d['errors']}/{d['steps']}" if d["errors"] else ""
        lines.append(f"{status} {d['bot']}: entry {len(ent)}, exit {len(ex)}{pnl_txt}{err}")
    for m in missing:
        lines.append(f"❌ {m}: replay nahi chal paya")
    lines += ["", "Har bot ke alert alag message me aa rahe hain (jinke kuch bane)."]
    send("\n".join(lines))

    for d, msgs, ent, ex, rows, pnl, col in bots:
        if d["errors"] and d["errors"] == d["steps"]:
            send(f"⚠️ {d['bot']} replay me har step par error:\n{d['last_error'][-1500:]}")
            continue
        important = [m for m in msgs if kind(m["text"]) in ("entry", "exit")]
        if not important:
            continue
        chunk = f"📋 {d['bot']} - aaj ke alerts (replay)\n"
        for m in important:
            line = f"\n[{m['time_ist']}] " + m["text"].strip().replace("\n", " | ")[:300] + "\n"
            if len(chunk) + len(line) > 3800:
                send(chunk)
                chunk = f"📋 {d['bot']} (aage)\n"
            chunk += line
        send(chunk)

    # GitHub page summary
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
