#!/usr/bin/env python3
"""Paper-only Telegram attribution adapter. Original strategies are unchanged."""
import functools

_SENT_BY_STRATEGY = {}
import SIX_STRATEGIES_IN_ONE_PAPER as combined

_original_loader = combined.load_original

class _TelegramRequestsProxy:
    """Intercept only Telegram API messages, leaving market-data requests intact."""
    def __init__(self, original, scanner):
        self._original = original
        self._scanner = scanner

    def __getattr__(self, name):
        return getattr(self._original, name)

    def _send(self, method, url, *args, **kwargs):
        if isinstance(url, str) and "api.telegram.org/" in url and (
            "/sendMessage" in url or "/sendPhoto" in url or "/sendDocument" in url
        ):
            for field in ("data", "json", "params"):
                payload = kwargs.get(field)
                if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                    payload = payload.copy()
                    payload["text"] = self._prefix(payload["text"])
                    kwargs[field] = payload
            # Telegram text may also be passed as a keyword argument.
            if isinstance(kwargs.get("text"), str):
                kwargs["text"] = self._prefix(kwargs["text"])
        response = getattr(self._original, method)(url, *args, **kwargs)
        if isinstance(url, str) and "api.telegram.org/" in url and "/sendMessage" in url:
            payload = kwargs.get("data") or kwargs.get("json") or kwargs.get("params") or {}
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                try:
                    result = response.json()
                    if response.status_code == 200 and result.get("ok"):
                        _SENT_BY_STRATEGY.setdefault(self._scanner, []).append(payload["text"])
                        print("TELEGRAM ORIGINAL DELIVERY OK | scanner=" + self._scanner, flush=True)
                    else:
                        print("TELEGRAM ORIGINAL DELIVERY FAILED | scanner=" + self._scanner + " | HTTP=" + str(response.status_code), flush=True)
                except (ValueError, AttributeError):
                    print("TELEGRAM ORIGINAL DELIVERY UNVERIFIED | scanner=" + self._scanner, flush=True)
        return response

    def _prefix(self, message):
        tag = "[SCANNER: " + self._scanner + "]"
        if tag in message:
            return message
        return tag + "\n" + message

    def post(self, url, *args, **kwargs):
        return self._send("post", url, *args, **kwargs)

    def get(self, url, *args, **kwargs):
        return self._send("get", url, *args, **kwargs)

def load_original_with_scanner_name(name):
    module = _original_loader(name)
    # Keep the existing periodic BOT CHECK suppression.
    if name == "BTC_EMA_ADX" and callable(getattr(module, "telegram", None)):
        original_telegram = module.telegram
        @functools.wraps(original_telegram)
        def signal_only_telegram(message, *args, **kwargs):
            if isinstance(message, str) and message.startswith("COINDCX BTC FUTURES BOT CHECK"):
                return None
            return original_telegram(message, *args, **kwargs)
        module.telegram = signal_only_telegram

    # Intercept Telegram requests in this strategy's own module only.
    # Do not alter entries, exits, SL, TP, state, or position sizing.
    requests_obj = getattr(module, "requests", None)
    if requests_obj is not None:
        module.requests = _TelegramRequestsProxy(requests_obj, name)
    return module

combined.load_original = load_original_with_scanner_name


# Notification-only audit: compare paper records before and after each successful cycle.
# No strategy code, thresholds, position sizing, or trade-state files are modified.
import csv
import json
import os
from pathlib import Path
import requests as _notification_requests

_RECORDS = {
    "BTC_SMC_Paper": ("BTC_SMC_Paper_trades.csv", "BTC_SMC_Paper_state.json"),
    "BTC_EMA_ADX": ("coindcx_futures_paper_trades.csv", "coindcx_futures_paper_state.json"),
    "BTC_SMC_VP": ("coindcx_futures_smc_vp_trades.csv", "coindcx_futures_smc_vp_state.json"),
    "GOLD_COMBO": ("GOLD_COMBO_PAPER_trades.csv", "GOLD_COMBO_PAPER_state.json"),
    "CRYPTO_TOP100": ("paper_trades.csv", "paper_state.json"),
    "TOP100_AMC_POC": ("TOP100_CRIPTO_AMC_POC_V4_trades.csv", "TOP100_CRIPTO_AMC_POC_V4_state.json"),
}

def _read_rows(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))
    except FileNotFoundError:
        return []

def _read_positions(path):
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (FileNotFoundError, ValueError):
        return {}
    positions = state.get("positions", state.get("open_trades", []))
    if isinstance(positions, dict):
        return positions
    if isinstance(positions, list):
        return {str(p.get("symbol", p.get("pair", i))): p for i, p in enumerate(positions) if isinstance(p, dict)}
    return {}

def _telegram_event(scanner, event):
    token = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM DELIVERY ERROR: token or chat ID missing (secret value not logged)")
    message = "[SCANNER: " + scanner + "]\n" + event
    response = _notification_requests.post(
        "https://api.telegram.org/bot" + token + "/sendMessage",
        data={"chat_id": chat_id, "text": message}, timeout=20,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 200 or not payload.get("ok"):
        raise RuntimeError("TELEGRAM DELIVERY ERROR: HTTP " + str(response.status_code) + "; Telegram ok=" + str(payload.get("ok")))
    print("TELEGRAM DELIVERY OK | scanner=" + scanner, flush=True)

def _report_new_events(scanner, old_rows, old_positions):
    csv_path, state_path = _RECORDS[scanner]
    new_rows = _read_rows(csv_path)
    if len(new_rows) < len(old_rows) or new_rows[:len(old_rows)] != old_rows:
        raise RuntimeError("Paper trade history changed unexpectedly; notification audit stopped")
    events = []
    for row in new_rows[len(old_rows):]:
        symbol = row.get("symbol") or row.get("pair") or row.get("coin") or "UNKNOWN"
        side = row.get("side", "")
        pnl = row.get("pnl") or row.get("net_usdt") or row.get("net_pnl_usdt") or ""
        reason = row.get("reason", "")
        events.append("PAPER EXIT | " + str(symbol) + " | " + str(side) +
                      " | P&L: " + str(pnl) + " USDT | Reason: " + str(reason))
    positions = _read_positions(state_path)
    for symbol, position in positions.items():
        if symbol not in old_positions:
            side = str(position.get("side", position.get("direction", ""))).upper()
            entry = position.get("entry", position.get("entry_price", ""))
            events.append("PAPER " + ("BUY" if side in ("LONG", "BUY") else "SELL" if side in ("SHORT", "SELL") else "ENTRY") +
                          " | " + str(symbol) + " | Entry: " + str(entry))
    original_messages = _SENT_BY_STRATEGY.get(scanner, [])
    for event in events:
        parts = event.split(" | ")
        symbol = parts[1] if len(parts) > 1 else ""
        is_exit = event.startswith("PAPER EXIT")
        matching = [m.upper() for m in original_messages if symbol and symbol in m]
        already_sent = any(
            any(word in m for word in (("EXIT", "CLOSED", "STOP LOSS", "TAKE PROFIT") if is_exit else ("BUY", "SELL", "ENTRY", "OPENED")))
            for m in matching
        )
        if already_sent:
            print("TELEGRAM DUPLICATE AVOIDED | scanner=" + scanner + " | symbol=" + symbol, flush=True)
        else:
            _telegram_event(scanner, event)
    print("PAPER NOTIFICATION AUDIT | scanner=" + scanner + " | new_events=" + str(len(events)), flush=True)

_original_run_one = combined.run_one

def _run_one_with_notification_audit(name):
    csv_path, state_path = _RECORDS[name]
    before_rows = _read_rows(csv_path)
    before_positions = _read_positions(state_path)
    _SENT_BY_STRATEGY[name] = []
    succeeded = _original_run_one(name)
    if succeeded:
        try:
            _report_new_events(name, before_rows, before_positions)
        except Exception as exc:
            # A Telegram outage must never block persistence of completed paper trades.
            print("PAPER NOTIFICATION ERROR | scanner=" + name + " | " + str(exc), flush=True)
    return succeeded

combined.run_one = _run_one_with_notification_audit

if __name__ == "__main__":
    combined.main()
