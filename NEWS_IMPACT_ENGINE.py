
"""
NEWS IMPACT ENGINE
Free US economic calendar + CoinDCX BTC reaction
PAPER TRADING ONLY - no order placement.

Returns:
    (True, reason)  = BLOCK new entry
    (False, reason) = News filter permits strategy entry

This is experimental and not a profitable-trading guarantee.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import requests


CALENDAR_URL = (
    "https://www.financecalendar.com/"
    "wp-json/fc/v1/calendar"
)

BEFORE_MIN = 30
AFTER_MIN = 30
REACTION_MIN = 180
CACHE_SEC = 900

KEYWORDS = (
    "cpi",
    "consumer price",
    "ppi",
    "producer price",
    "fomc",
    "federal funds",
    "interest rate",
    "fed rate",
    "non-farm",
    "nonfarm",
    "non farm",
    "payroll",
    "employment situation",
)

_cache = {
    "at": -CACHE_SEC,
    "events": None,
    "error": None,
}


def _parse_utc(value):
    dt = datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        raise ValueError("Missing timezone")

    return dt.astimezone(timezone.utc)


def _calendar(now):
    """Return verified-looking US event records, or fail closed."""

    if time.monotonic() - _cache["at"] < CACHE_SEC:
        return _cache["events"], _cache["error"]

    _cache.update(
        at=time.monotonic(),
        events=None,
        error="Calendar not verified",
    )

    try:
        start = (
            now.date() - timedelta(days=1)
        ).isoformat()

        end = (
            now.date() + timedelta(days=30)
        ).isoformat()

        response = requests.get(
            CALENDAR_URL,
            params={
                "country": "US",
                "impact": "high",
                "from": start,
                "to": end,
                "limit": 500,
            },
            headers={
                "User-Agent": "Guruji-Paper-News/1.0"
            },
            timeout=20,
        )

        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError("Invalid calendar response")

        raw_events = payload.get("events")

        if not isinstance(raw_events, list):
            raise ValueError("Events list missing")

        if not raw_events:
            raise ValueError("Calendar returned zero events")

        total = payload.get("total")

        if total is not None:
            if int(total) > len(raw_events):
                raise ValueError(
                    "Calendar response incomplete or paginated"
                )

        events = []

        for item in raw_events:

            if not isinstance(item, dict):
                raise ValueError("Malformed event")

            if str(
                item.get("country", "")
            ).upper() != "US":
                continue

            title = str(
                item.get("title")
                or item.get("series_name")
                or item.get("name")
                or ""
            ).strip()

            series = str(
                item.get("series_code") or ""
            ).lower()

            combined = (
                title + " " + series
            ).lower()

            if not any(
                word in combined
                for word in KEYWORDS
            ):
                continue

            if str(
                item.get("impact", "")
            ).lower() != "high":
                continue

            if item.get("data_stale") is True:
                raise ValueError(
                    "High-impact event data is stale"
                )

            if str(
                item.get("status", "")
            ).lower() not in (
                "confirmed",
                "scheduled",
            ):
                raise ValueError(
                    "High-impact event not confirmed"
                )

            raw_time = (
                item.get("release_at_utc")
                or item.get("time_utc")
            )

            if not raw_time:
                raise ValueError(
                    "Important event has no UTC time"
                )

            dt = _parse_utc(raw_time)

            if str(
                item.get("time_precision", "minute")
            ).lower() not in (
                "minute",
                "second",
            ):
                raise ValueError(
                    "Important event time is not precise"
                )

            events.append({
                "time": dt,
                "title": title,
                "actual": item.get("actual"),
                "forecast": item.get("consensus"),
            })

        if not events:
            raise ValueError(
                "No matching high-impact US events"
            )

        # Require at least one upcoming event.
        upcoming = [
            ev for ev in events
            if ev["time"] >= now - timedelta(days=1)
        ]

        if not upcoming:
            raise ValueError(
                "Upcoming news coverage unavailable"
            )

        events.sort(key=lambda x: x["time"])

        _cache.update(
            events=events,
            error=None,
        )

        print(
            "NEWS CALENDAR OK:",
            len(events),
            "target events",
            flush=True,
        )

    except Exception as exc:
        _cache.update(
            events=None,
            error=f"Calendar unverified: {exc}",
        )

    return _cache["events"], _cache["error"]


def _btc_reaction(event_time, now):
    """
    BTC futures:
    closed 5m candles,
    15m momentum,
    volume >= 1.2x,
    24-candle VWAP proxy.
    """

    end = int(now.timestamp())

    start = int(
        (
            event_time - timedelta(hours=4)
        ).timestamp()
    )

    response = requests.get(
        "https://public.coindcx.com/"
        "market_data/candlesticks",
        params={
            "pair": "B-BTC_USDT",
            "pcode": "f",
            "resolution": "5",
            "from": start,
            "to": end,
        },
        timeout=25,
    )

    response.raise_for_status()
    payload = response.json()

    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("data"), list)
    ):
        raise ValueError(
            "BTC futures candle data invalid"
        )

    bars = {}

    for item in payload["data"]:

        timestamp = float(item["time"])

        if timestamp > 1e11:
            timestamp /= 1000

        if timestamp + 300 > now.timestamp():
            continue

        o, h, l, c, v = (
            float(item[key])
            for key in (
                "open",
                "high",
                "low",
                "close",
                "volume",
            )
        )

        if c <= 0 or v < 0 or h < l:
            continue

        bars[int(timestamp)] = (
            o, h, l, c, v
        )

    ordered = sorted(bars.items())

    pre = [
        bar
        for ts, bar in ordered
        if ts + 300 <= event_time.timestamp()
    ]

    post = [
        (ts, bar)
        for ts, bar in ordered
        if ts >= event_time.timestamp()
    ]

    if len(pre) < 12 or len(post) < 6:
        return (
            None,
            "BTC reaction data insufficient",
        )

    recent = ordered[-3:]

    if (
        len(recent) != 3
        or any(
            recent[i + 1][0] - recent[i][0] != 300
            for i in (0, 1)
        )
    ):
        return (
            None,
            "BTC recent 15m candles incomplete",
        )

    if recent[0][0] < event_time.timestamp():
        return (
            None,
            "BTC post-news confirmation pending",
        )

    reference = pre[-1][3]
    latest = recent[-1][1][3]

    lookback = ordered[-24:]

    volume = sum(
        bar[4] for _, bar in lookback
    )

    if len(lookback) < 24 or volume <= 0:
        return None, "BTC VWAP unavailable"

    vwap = sum(
        (
            (bar[1] + bar[2] + bar[3]) / 3
        ) * bar[4]
        for _, bar in lookback
    ) / volume

    baseline = sum(
        bar[4] for bar in pre[-12:]
    ) / 12

    recent_average = sum(
        bar[4] for _, bar in recent
    ) / 3

    if (
        baseline <= 0
        or recent_average < baseline * 1.2
    ):
        return (
            None,
            "BTC volume confirmation missing",
        )

    change = (
        latest / reference - 1
    ) * 100

    volume_ratio = (
        recent_average / baseline
    )

    if (
        change >= 0.20
        and latest > vwap
        and recent[-1][1][3] > recent[0][1][0]
    ):
        return (
            "LONG",
            f"BTC bullish {change:+.2f}%, "
            f"volume {volume_ratio:.2f}x, "
            "above VWAP",
        )

    if (
        change <= -0.20
        and latest < vwap
        and recent[-1][1][3] < recent[0][1][0]
    ):
        return (
            "SHORT",
            f"BTC bearish {change:+.2f}%, "
            f"volume {volume_ratio:.2f}x, "
            "below VWAP",
        )

    return (
        None,
        f"BTC direction unconfirmed "
        f"({change:+.2f}%)",
    )


def entry_decision(symbol, side, now=None):
    """
    Return (blocked, reason).
    This function never places an order.
    """

    now = now or datetime.now(timezone.utc)

    if now.tzinfo is None:
        return (
            True,
            "Naive timestamp - entry blocked",
        )

    now = now.astimezone(timezone.utc)

    events, error = _calendar(now)

    if error or events is None:
        return (
            True,
            error or "Calendar unavailable",
        )

    # Block 30 minutes before and after news.
    for event in events:

        event_time = event["time"]

        if (
            event_time - timedelta(
                minutes=BEFORE_MIN
            )
            <= now
            <= event_time + timedelta(
                minutes=AFTER_MIN
            )
        ):
            return (
                True,
                "USD NEWS NO-TRADE +/-30m: "
                f"{event['title']} "
                f"at {event_time:%Y-%m-%d %H:%M UTC}",
            )

    # Check BTC reaction for 30-180 min after news.
    recent_events = [
        event
        for event in events
        if (
            timedelta(minutes=AFTER_MIN)
            < now - event["time"]
            <= timedelta(minutes=REACTION_MIN)
        )
    ]

    if not recent_events:
        return (
            False,
            "No active high-impact USD news "
            "reaction window; normal strategy entry",
        )

    event = recent_events[-1]

    try:
        direction, detail = _btc_reaction(
            event["time"],
            now,
        )

    except Exception as exc:
        return (
            True,
            f"BTC reaction unverified: {exc}",
        )

    if direction is None:
        return (
            True,
            f"{event['title']}: {detail}",
        )

    requested = str(side).upper()

    if requested in ("BUY", "LONG"):
        requested = "LONG"
    elif requested in ("SELL", "SHORT"):
        requested = "SHORT"
    else:
        return True, "Unknown trade direction"

    if requested != direction:
        return (
            True,
            f"{event['title']}: "
            f"{requested} conflicts with "
            f"BTC {direction}; {detail}",
        )

    return (
        False,
        f"{event['title']}: "
        f"BTC {direction} confirmed; {detail}",
    )
