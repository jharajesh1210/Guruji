#!/usr/bin/env python3
"""Paper-only Telegram attribution adapter. Original strategies are unchanged."""
import functools
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
        return getattr(self._original, method)(url, *args, **kwargs)

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

if __name__ == "__main__":
    combined.main()
