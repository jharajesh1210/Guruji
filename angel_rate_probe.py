#!/usr/bin/env python3
"""Read-only diagnostic: how fast does Angel One's getCandleData accept requests from THIS runner?
Run it when the other Angel bots are idle (after 15:30 IST). It never places orders and prints no secrets.
Phase A: 20 calls spaced 4s apart (what the ORB bot does). Phase B: 15 calls spaced 1s apart.
If calls are rejected ("Too many requests") even though no other bot is running, the limit is account- or IP-level."""
import datetime as dt
import json
import os
import sys
import time

import pyotp
import requests
from SmartApi import SmartConnect

SCRIP = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
SYMS = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC", "LT", "AXISBANK",
        "KOTAKBANK", "HINDUNILVR", "MARUTI", "SUNPHARMA", "TITAN", "NTPC", "ONGC", "WIPRO", "TATASTEEL",
        "ULTRACEMCO", "BAJFINANCE", "ASIANPAINT", "COALINDIA", "CIPLA", "DRREDDY", "TECHM", "HCLTECH",
        "POWERGRID", "JSWSTEEL", "GRASIM", "ADANIPORTS", "ADANIENT", "EICHERMOT", "HEROMOTOCO", "BPCL",
        "INDUSINDBK", "NESTLEIND", "BRITANNIA", "APOLLOHOSP"]


def main():
    try:
        print("runner egress IP:", requests.get("https://api.ipify.org", timeout=10).text)
    except Exception as exc:  # noqa: BLE001
        print("egress IP lookup failed:", exc)
    need = ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        sys.exit("Missing secrets: " + ", ".join(missing))
    api = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
    res = api.generateSession(os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_PIN"],
                              pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now())
    print("login ok:", bool(res and res.get("status")))
    if not (res and res.get("status")):
        sys.exit("login failed")
    tokens = {x["symbol"]: x["token"] for x in requests.get(SCRIP, timeout=60).json() if x.get("exch_seg") == "NSE"}
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).replace(tzinfo=None)
    frm, to = now.strftime("%Y-%m-%d 09:15"), now.strftime("%Y-%m-%d %H:%M")
    queue = [(s, tokens.get(f"{s}-EQ")) for s in SYMS if tokens.get(f"{s}-EQ")]

    def phase(name, count, gap, offset):
        ok = limited = other = 0
        print(f"\n--- {name}: {count} calls, {gap}s apart ---")
        for i in range(count):
            sym, tok = queue[(offset + i) % len(queue)]
            t0 = time.monotonic()
            try:
                r = api.getCandleData({"exchange": "NSE", "symboltoken": tok, "interval": "FIVE_MINUTE",
                                       "fromdate": frm, "todate": to})
                good = bool(r and r.get("status") and r.get("data") is not None)
                msg = "ok" if good else str(r)[:90]
            except Exception as exc:  # noqa: BLE001
                good, msg = False, str(exc)[:90]
            low = msg.lower()
            lim = any(k in low for k in ("too many", "exceeding access rate", "access denied", "rate limit"))
            ok += good
            limited += (not good and lim)
            other += (not good and not lim)
            print(f"{i + 1:2d} {sym:12s} {'OK     ' if good else ('LIMITED' if lim else 'ERROR  ')} {time.monotonic() - t0:5.1f}s {'' if good else msg}")
            time.sleep(gap)
        print(f"{name} summary: ok={ok} limited={limited} other_errors={other} of {count}")
        return ok, limited

    a = phase("Phase A (4s gap, like the ORB bot)", 20, 4.0, 0)
    b = phase("Phase B (1s gap)", 15, 1.0, 20)
    print("\nVERDICT HINT:", "throttled even when idle -> account/IP-level limit" if a[1] >= 5 else
          "mostly accepted when idle -> the limit is caused by other bots/usage running at the same time")


if __name__ == "__main__":
    main()
