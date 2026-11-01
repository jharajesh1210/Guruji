#!/usr/bin/env python3
"""Notification-only adapter: suppress periodic HOLD/status Telegram messages.
Original strategy logic, paper orders, state, locks and exits are untouched.
"""
import SIX_STRATEGIES_IN_ONE_PAPER as combined

_original_loader = combined.load_original

def load_original_with_alert_filter(name):
    module = _original_loader(name)
    if name == "BTC_EMA_ADX":
        original_telegram = module.telegram
        def signal_only_telegram(message):
            if isinstance(message, str) and message.startswith("COINDCX BTC FUTURES BOT CHECK"):
                # Periodic status reports are not BUY/SELL/entry/exit alerts.
                return None
            return original_telegram(message)
        module.telegram = signal_only_telegram
    return module

combined.load_original = load_original_with_alert_filter

if __name__ == "__main__":
    combined.main()
