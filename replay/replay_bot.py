#!/usr/bin/env python3
"""
REPLAY ENGINE - aaj ka din dobara chalao (paper bots ke liye).

Har step par bot ko lagta hai ki "abhi" wahi samay hai, aur use sirf us samay tak
ki COMPLETED candles milti hain. Telegram message asli nahi jaate, record hote hain.
Exchange par koi POST (order) kabhi nahi jaata - sirf data padha jaata hai.

Usage (repo copy ke andar se):
  python replay/replay_bot.py BOT_NAME START_UTC END_UTC STEP_MIN OUT_JSON
"""
import datetime as _dt_mod
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import threading
import time as _time_mod
import traceback
from contextlib import redirect_stdout, redirect_stderr
from urllib.parse import urlparse, parse_qsl

# Heavy libraries pehle load (inke andar asli datetime rahe), ghadi baad me badlegi
import numpy  # noqa: F401
import pandas as pd
import requests
try:
    import yfinance as yf
except Exception:
    yf = None

# ---------------------------------------------------------------- fake clock
_REAL_TIME = _time_mod.time
_REAL_SLEEP = _time_mod.sleep
_RealDT = _dt_mod.datetime
_UTC = _dt_mod.timezone.utc

_CLOCK = {"t": None, "real": 0}   # t = fake epoch seconds; real>0 => real clock


def now_epoch():
    if _CLOCK["t"] is None or _CLOCK["real"] > 0:
        return _REAL_TIME()
    return _CLOCK["t"]


class real_clock:
    """Network / library calls see the real time (cookies, SSL, yfinance periods)."""
    def __enter__(self):
        _CLOCK["real"] += 1

    def __exit__(self, *a):
        _CLOCK["real"] -= 1


class FakeDatetime(_RealDT):
    @classmethod
    def now(cls, tz=None):
        r = _RealDT.fromtimestamp(now_epoch(), tz or _UTC)
        if tz is None:
            r = r.astimezone().replace(tzinfo=None)
        return cls._wrap(r)

    @classmethod
    def utcnow(cls):
        return cls._wrap(_RealDT.fromtimestamp(now_epoch(), _UTC).replace(tzinfo=None))

    @classmethod
    def today(cls):
        return cls.now()

    @classmethod
    def _wrap(cls, d):
        return cls(d.year, d.month, d.day, d.hour, d.minute, d.second, d.microsecond, d.tzinfo)


class FakeDate(_dt_mod.date):
    @classmethod
    def today(cls):
        d = FakeDatetime.now()
        return cls(d.year, d.month, d.day)


_time_mod.time = now_epoch
_time_mod.sleep = lambda s=0: None          # replay me intezaar nahi
_dt_mod.datetime = FakeDatetime
_dt_mod.date = FakeDate

_REAL_TS_NOW = pd.Timestamp.now


def _ts_now(tz=None):
    t = pd.Timestamp(now_epoch(), unit="s", tz="UTC")
    if tz is None:
        return t.tz_localize(None)
    return t.tz_convert(tz)


pd.Timestamp.now = staticmethod(_ts_now)
pd.Timestamp.utcnow = staticmethod(lambda: pd.Timestamp(now_epoch(), unit="s", tz="UTC"))

_REAL_REQUEST = requests.Session.request
_LOCK = threading.Lock()
_KEY_LOCKS = {}
_CANDLE_CACHE = {}      # key -> {"rows": {time_ms: row}, "from": sec, "unit": "ms"/"s"}
_GET_CACHE = {}
TELEGRAM = []           # recorded messages
BLOCKED = []
STATS = {"real_calls": 0, "served": 0}
REAL_END = int(_REAL_TIME())

RES_SEC = {"1": 60, "1m": 60, "3": 180, "5": 300, "5m": 300, "15": 900, "15m": 900,
           "30": 1800, "30m": 1800, "60": 3600, "1h": 3600, "120": 7200, "240": 14400,
           "4h": 14400, "1D": 86400, "1d": 86400, "D": 86400}


class FakeResponse:
    def __init__(self, payload, status=200, url=""):
        self._payload = payload
        self.status_code = status
        self.ok = status < 400
        self.url = url
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload
        self.content = self.text.encode()
        self.headers = {"Content-Type": "application/json"}
        self.encoding = "utf-8"
        self.reason = "OK" if self.ok else "BLOCKED"

    def json(self, **kw):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} replay block", response=self)

    def iter_content(self, *a, **k):
        yield self.content

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _row_time_ms(row):
    t = int(float(row.get("time")))
    return t if t > 1e11 else t * 1000


def _coindcx_candles(sess, method, url, params, kw):
    params = dict(params or {})
    q = dict(parse_qsl(urlparse(url).query))
    params = {**q, **params}
    base = url.split("?")[0]
    res = RES_SEC.get(str(params.get("resolution", params.get("interval", "5"))), 300)
    key = (base,) + tuple(sorted((k, str(v)) for k, v in params.items() if k not in ("from", "to")))
    req_from = int(float(params.get("from", now_epoch() - 4 * 86400)))
    req_to = int(float(params.get("to", now_epoch())))
    with _LOCK:
        klock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with klock:
        c = _CANDLE_CACHE.get(key)
        if c is None or req_from < c["from"]:
            rows = {} if c is None else c["rows"]
            unit = None if c is None else c["unit"]
            fetch_to = REAL_END if c is None else c["from"]
            chunk = res * 1400
            left = req_from
            while left < fetch_to:
                right = min(left + chunk, fetch_to)
                p = {k: v for k, v in params.items()}
                p["from"], p["to"] = int(left), int(right)
                with real_clock():
                    STATS["real_calls"] += 1
                    r = _REAL_REQUEST(sess, "GET", base, params=p, timeout=30)
                data = r.json() if r.status_code == 200 else {}
                for row in (data.get("data") or []) if isinstance(data, dict) else []:
                    if unit is None:
                        unit = "ms" if int(float(row["time"])) > 1e11 else "s"
                    rows[_row_time_ms(row)] = row
                left = right
            c = {"rows": rows, "from": req_from, "unit": unit or "ms"}
            _CANDLE_CACHE[key] = c
    now_ms = int(now_epoch() * 1000)
    lo, hi = req_from * 1000, min(req_to * 1000, now_ms)
    out = [row for t, row in sorted(c["rows"].items())
           if lo <= t <= hi and t + res * 1000 <= now_ms]
    STATS["served"] += 1
    return FakeResponse({"s": "ok", "data": out}, 200, url)


def fake_request(self, method, url, params=None, data=None, json=None, **kw):
    u = str(url)
    if "api.telegram.org" in u:
        payload = {}
        for src in (params, data, json):
            if isinstance(src, dict):
                payload.update(src)
        text = payload.get("text") or payload.get("caption") or ""
        ist = FakeDatetime.fromtimestamp(now_epoch(), _UTC) + _dt_mod.timedelta(hours=5, minutes=30)
        TELEGRAM.append({"time_ist": ist.strftime("%H:%M"), "text": str(text)})
        return FakeResponse({"ok": True, "result": {"message_id": len(TELEGRAM)}}, 200, u)
    if method.upper() != "GET":
        BLOCKED.append(u)
        return FakeResponse({"error": "replay: non-GET blocked"}, 403, u)
    if "market_data/candlesticks" in u:
        return _coindcx_candles(self, method, url, params, kw)
    ck = (u, tuple(sorted((params or {}).items())) if isinstance(params, dict) else str(params))
    if ck in _GET_CACHE:
        return _GET_CACHE[ck]
    with real_clock():
        STATS["real_calls"] += 1
        r = _REAL_REQUEST(self, method, url, params=params, data=data, json=json, **kw)
    _GET_CACHE[ck] = r
    return r


requests.Session.request = fake_request

# ---------------------------------------------------------------- yfinance
_YF_CACHE = {}
if yf is not None:
    _REAL_YF = yf.download
    _IV = {"1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600, "1h": 3600,
           "90m": 5400, "1d": 86400}

    def fake_download(tickers, *a, **kw):
        interval = kw.get("interval", "1d")
        key = (str(tickers), interval, str(kw.get("period")), str(kw.get("start")), str(kw.get("end")))
        if key not in _YF_CACHE:
            with real_clock():
                _time_mod.sleep = _REAL_SLEEP
                try:
                    STATS["real_calls"] += 1
                    _YF_CACHE[key] = _REAL_YF(tickers, *a, **kw)
                finally:
                    _time_mod.sleep = lambda s=0: None
        d = _YF_CACHE[key]
        if d is None or len(d) == 0:
            return d
        sec = _IV.get(interval, 300)
        idx = d.index
        idx_utc = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
        cut = pd.Timestamp(now_epoch() - sec, unit="s", tz="UTC")
        return d[idx_utc <= cut].copy()

    yf.download = fake_download


# ---------------------------------------------------------------- bot runners
def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


BOTS = {
    # name: (file, how to call one cycle)
    "ASIAN_LONDON_NY_ORIGINAL": ("ASIAN_LONDON_NY_PAPER.py", "main", None),
    "ASIAN_LONDON_NY_NEWS_IMPACT": ("ASIAN_LONDON_NY_PAPER_NEWS_IMPACT.py", "main", None),
    "BTC_SMC_ORIGINAL": ("BTC_SMC_Paper.py", "main", None),
    "BTC_SMC_NEWS_IMPACT": ("BTC_SMC_Paper_NEWS_IMPACT.py", "main", None),
    "GOLD_COMBO_PAPER": ("GOLD_COMBO_PAPER.py", "main", None),
    "TOP100_ORIGINAL": ("TOP100_CRIPTO_AMC_POC.py", "main", None),
    "TOP100_NEWS_IMPACT": ("TOP100_CRIPTO_AMC_POC_NEWS_IMPACT.py", "main", None),
    "TOP100_AMC_POC_V4": ("TOP100_CRIPTO_AMC_POC_V4.py", "main", None),
    "NIFTY50_EQUITY": ("NIFTY50_EQUITY_INTRADAY.py", "run", ("paper",)),
    "RIL_CANDLESTICK_BIBLE": ("RIL_CANDLESTICK_BIBLE.py", "main", None),
    "RIL_TRADING_SYSTEM": ("RIL_TRADING_SYSTEM.PY", "main", "argv:--mode paper"),
}


def main():
    bot, start, end, step, out = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
    fname, func, args = BOTS[bot]
    t0 = _RealDT.fromisoformat(start).replace(tzinfo=_UTC).timestamp()
    t1 = _RealDT.fromisoformat(end).replace(tzinfo=_UTC).timestamp()
    t1 = min(t1, REAL_END - 60)
    os.environ["BOT_TOKEN"] = os.environ["TELEGRAM_BOT_TOKEN"] = "REPLAY"
    os.environ["CHAT_ID"] = os.environ["TELEGRAM_CHAT_ID"] = "0"
    _CLOCK["t"] = t0
    sys.path.insert(0, os.getcwd())   # bots import helper files (NEWS_IMPACT_ENGINE, gold_combo_strategy)
    mod = load(os.path.abspath(fname), "bot_" + bot.lower())
    call = getattr(mod, func)
    steps, errors, last_err = 0, 0, ""
    t = t0
    log = io.StringIO()
    while t <= t1:
        _CLOCK["t"] = t
        if isinstance(args, str) and args.startswith("argv:"):
            sys.argv = [fname] + args[5:].split()
        try:
            with redirect_stdout(log), redirect_stderr(log):
                call(*args) if isinstance(args, tuple) else call()
        except SystemExit:
            pass
        except Exception:
            errors += 1
            last_err = traceback.format_exc()[-600:]
        steps += 1
        t += step * 60
    result = {"bot": bot, "start": start, "end": _RealDT.fromtimestamp(t1, _UTC).isoformat(),
              "steps": steps, "errors": errors, "last_error": last_err,
              "telegram": TELEGRAM, "blocked_posts": len(BLOCKED), "stats": STATS,
              "log_tail": log.getvalue()[-3000:]}
    _CLOCK["real"] = 1
    with open(out, "w") as f:
        json.dump(result, f, indent=1, default=str)
    sys.__stdout__.write(f"{bot}: steps={steps} errors={errors} telegram={len(TELEGRAM)} "
                         f"real_calls={STATS['real_calls']}\n")
    if last_err:
        sys.__stdout__.write("LAST ERROR:\n" + last_err + "\n")


if __name__ == "__main__":
    main()
