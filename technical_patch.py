"""
Technical patch wrapper for app_live_ws.py.

Keeps the existing Dhan WebSocket/LTP routes untouched and injects the
missing historical/technical functions used by /api/technical-scan.
"""

import time
from datetime import datetime, timedelta, timezone

import requests

import app_live_ws as base

app = base.app

safe_float = base.safe_float
clean_symbol = base.clean_symbol
ema_last = base.ema_last
rsi_last = base.rsi_last
bb_last = base.bb_last
macd_last = base.macd_last
cci_last = base.cci_last
aggregate_closes_by_week = base.aggregate_closes_by_week
aggregate_closes_by_month = base.aggregate_closes_by_month

HIST_CACHE_SECONDS = 15 * 60


def dhan_historical_request(security_id, from_date, to_date):
    with base._historical_lock:
        elapsed = time.time() - base._last_historical_time
        if elapsed < 0.25:
            time.sleep(0.25 - elapsed)

        token = base.get_access_token()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": token,
        }
        body = {
            "securityId": str(int(security_id)),
            "exchangeSegment": "NSE_EQ",
            "instrument": "EQUITY",
            "expiryCode": 0,
            "oi": False,
            "fromDate": str(from_date),
            "toDate": str(to_date),
        }

        response = requests.post(
            base.DHAN_HISTORICAL_URL,
            headers=headers,
            json=body,
            timeout=30,
        )
        base._last_historical_time = time.time()

        if response.status_code in (401, 403):
            token = base.get_access_token(force_refresh=True)
            headers["access-token"] = token
            response = requests.post(
                base.DHAN_HISTORICAL_URL,
                headers=headers,
                json=body,
                timeout=30,
            )
            base._last_historical_time = time.time()

        try:
            data = response.json()
        except ValueError:
            data = {}

        if response.status_code >= 400:
            message = (
                data.get("errorMessage")
                or data.get("message")
                or data.get("error")
                or response.text[:500]
            )
            raise RuntimeError(
                f"Dhan Historical error {response.status_code}: {message}"
            )

        if not isinstance(data, dict):
            raise RuntimeError("Dhan Historical returned invalid JSON")

        return data


def get_daily_history(symbol, days=800):
    symbol = clean_symbol(symbol)
    security_id = base.instrument_map.get(symbol)
    if security_id is None:
        return None

    now = time.time()
    cache_key = "technical_v3:" + symbol

    with base._historical_cache_lock:
        cached = base._historical_cache.get(cache_key)
        if cached and now - cached["time"] < HIST_CACHE_SECONDS:
            return cached["data"]

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)

    raw = dhan_historical_request(
        security_id,
        start.isoformat(),
        end.isoformat(),
    )

    timestamps = (
        raw.get("timestamp")
        or raw.get("start_Time")
        or raw.get("startTime")
        or []
    )
    opens = raw.get("open") or []
    highs = raw.get("high") or []
    lows = raw.get("low") or []
    closes = raw.get("close") or []
    volumes = raw.get("volume") or []

    n = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))

    if n <= 0:
        history = {
            "timestamp": [],
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
        }
    else:
        timestamps = list(timestamps[-n:])
        opens = list(opens[-n:])
        highs = list(highs[-n:])
        lows = list(lows[-n:])
        closes = list(closes[-n:])
        volumes = list(volumes[-n:]) if len(volumes) >= n else [None] * n

        history = {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }

    with base._historical_cache_lock:
        base._historical_cache[cache_key] = {
            "time": now,
            "data": history,
        }

    return history


def _weekly_bars(timestamps, opens, highs, lows, closes, volumes):
    buckets = {}

    for ts, op, hi, lo, cl, vol in zip(
        timestamps, opens, highs, lows, closes, volumes
    ):
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            key = (dt.isocalendar().year, dt.isocalendar().week)

            if key not in buckets:
                buckets[key] = {
                    "open": op,
                    "high": hi,
                    "low": lo,
                    "close": cl,
                    "volume": 0.0,
                }

            b = buckets[key]
            b["high"] = max(b["high"], hi)
            b["low"] = min(b["low"], lo)
            b["close"] = cl

            if vol is not None:
                b["volume"] += vol

        except (TypeError, ValueError, OverflowError):
            continue

    return [buckets[k] for k in sorted(buckets)]


def _monthly_bars(timestamps, opens, highs, lows, closes, volumes):
    buckets = {}

    for ts, op, hi, lo, cl, vol in zip(
        timestamps, opens, highs, lows, closes, volumes
    ):
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            key = (dt.year, dt.month)

            if key not in buckets:
                buckets[key] = {
                    "open": op,
                    "high": hi,
                    "low": lo,
                    "close": cl,
                    "volume": 0.0,
                }

            b = buckets[key]
            b["high"] = max(b["high"], hi)
            b["low"] = min(b["low"], lo)
            b["close"] = cl

            if vol is not None:
                b["volume"] += vol

        except (TypeError, ValueError, OverflowError):
            continue

    return [buckets[k] for k in sorted(buckets)]


def build_technical_row(symbol, history, live_quote=None):
    if not history:
        return None

    timestamps = list(history.get("timestamp") or [])
    opens = [safe_float(x) for x in (history.get("open") or [])]
    highs = [safe_float(x) for x in (history.get("high") or [])]
    lows = [safe_float(x) for x in (history.get("low") or [])]
    closes = [safe_float(x) for x in (history.get("close") or [])]
    raw_volumes = list(history.get("volume") or [])

    n = min(
        len(timestamps),
        len(opens),
        len(highs),
        len(lows),
        len(closes),
    )

    if n < 26:
        return None

    timestamps = timestamps[-n:]
    opens = opens[-n:]
    highs = highs[-n:]
    lows = lows[-n:]
    closes = closes[-n:]

    if len(raw_volumes) >= n:
        volumes = [safe_float(x) for x in raw_volumes[-n:]]
    else:
        volumes = [None] * n

    valid = [
        i for i in range(n)
        if all(
            v is not None
            for v in (opens[i], highs[i], lows[i], closes[i])
        )
    ]

    if len(valid) < 26:
        return None

    timestamps = [timestamps[i] for i in valid]
    opens = [opens[i] for i in valid]
    highs = [highs[i] for i in valid]
    lows = [lows[i] for i in valid]
    closes = [closes[i] for i in valid]
    volumes = [volumes[i] for i in valid]

    if isinstance(live_quote, dict):
        quote = live_quote
        live_price = safe_float(
            quote.get("last_price")
            if quote.get("last_price") is not None
            else quote.get("ltp")
        )
    else:
        quote = {}
        live_price = safe_float(live_quote)

    if live_price is None or live_price <= 0:
        live_price = closes[-1]

    live_ohlc = (
        quote.get("ohlc")
        if isinstance(quote.get("ohlc"), dict)
        else {}
    )

    current_open = safe_float(live_ohlc.get("open"))
    current_high = safe_float(live_ohlc.get("high"))
    current_low = safe_float(live_ohlc.get("low"))

    pa_open = (
        current_open
        if current_open is not None
        else opens[-1]
    )
    pa_high = (
        current_high
        if current_high is not None
        else highs[-1]
    )
    pa_low = (
        current_low
        if current_low is not None
        else lows[-1]
    )

    live_closes = list(closes)
    live_closes[-1] = live_price

    e9 = ema_last(live_closes, 9)
    e20 = ema_last(live_closes, 20)
    e50 = ema_last(live_closes, 50)
    e100 = ema_last(live_closes, 100)
    e200 = ema_last(live_closes, 200)

    daily_rsi = rsi_last(live_closes, 14)

    weekly_closes = aggregate_closes_by_week(
        timestamps,
        live_closes,
    )
    monthly_closes = aggregate_closes_by_month(
        timestamps,
        live_closes,
    )

    weekly_rsi = rsi_last(weekly_closes, 14)
    monthly_rsi = rsi_last(monthly_closes, 14)

    bb_upper, bb_middle, bb_lower = bb_last(
        live_closes,
        20,
        2.0,
    )

    band_width = (
        ((bb_upper - bb_lower) / bb_middle) * 100.0
        if (
            bb_upper is not None
            and bb_lower is not None
            and bb_middle
        )
        else None
    )

    macd_line, macd_signal, macd_hist = macd_last(
        live_closes,
        12,
        26,
        9,
    )

    cci = cci_last(
        highs,
        lows,
        live_closes,
        20,
    )

    historical_volume = (
        volumes[-1]
        if volumes
        else None
    )

    live_volume = safe_float(
        quote.get("volume")
    )

    volume = (
        live_volume
        if live_volume is not None and live_volume > 0
        else historical_volume
    )

    numeric_volumes = [
        v for v in volumes
        if v is not None and v >= 0
    ]

    avg20_volume = (
        sum(numeric_volumes[-20:]) / 20.0
        if len(numeric_volumes) >= 20
        else None
    )

    avg5_volume = (
        sum(numeric_volumes[-5:]) / 5.0
        if len(numeric_volumes) >= 5
        else None
    )

    volume_ratio = (
        volume / avg20_volume
        if (
            volume is not None
            and avg20_volume
            and avg20_volume > 0
        )
        else None
    )

    volume_ratio_5 = (
        volume / avg5_volume
        if (
            volume is not None
            and avg5_volume
            and avg5_volume > 0
        )
        else None
    )

    historical_ath = (
        max(highs)
        if highs
        else None
    )

    ath = max(
        historical_ath or 0,
        live_price,
    )

    distance_to_ath = (
        ((ath - live_price) / ath) * 100.0
        if ath > 0
        else None
    )

    lookback_highs = (
        highs[-20:-1]
        if len(highs) > 20
        else highs[:-1]
    )

    daily_breakout_level = (
        max(lookback_highs)
        if lookback_highs
        else None
    )

    daily_breakout = (
        daily_breakout_level is not None
        and live_price > daily_breakout_level
    )

    weekly_bars = _weekly_bars(
        timestamps,
        opens,
        highs,
        lows,
        closes,
        volumes,
    )

    weekly_breakout_level = (
        max(
            b["high"]
            for b in weekly_bars[-20:-1]
        )
        if len(weekly_bars) > 20
        else max(
            (
                b["high"]
                for b in weekly_bars[:-1]
            ),
            default=None,
        )
    )

    weekly_breakout = (
        weekly_breakout_level is not None
        and live_price > weekly_breakout_level
    )

    monthly_bars = _monthly_bars(
        timestamps,
        opens,
        highs,
        lows,
        closes,
        volumes,
    )

    monthly_breakout_level = (
        max(
            b["high"]
            for b in monthly_bars[-13:-1]
        )
        if len(monthly_bars) > 13
        else max(
            (
                b["high"]
                for b in monthly_bars[:-1]
            ),
            default=None,
        )
    )

    monthly_breakout = (
        monthly_breakout_level is not None
        and live_price > monthly_breakout_level
    )

    near_ath = (
        distance_to_ath is not None
        and distance_to_ath <= 3.0
    )

    ath_breakout = (
        historical_ath is not None
        and live_price > historical_ath
    )

    price_action_bullish = (
        pa_open is not None
        and live_price > pa_open
    )

    strong_close = (
        pa_high is not None
        and pa_low is not None
        and pa_high > pa_low
        and (
            (live_price - pa_low)
            / (pa_high - pa_low)
        ) >= 0.65
    )

    bullish_price_action = (
        price_action_bullish
        and strong_close
    )

    strong_volume = (
        volume_ratio is not None
        and volume_ratio >= 1.5
    )

    volume_spike = (
        volume_ratio is not None
        and volume_ratio >= 3.0
    )

    volume_spike_5 = (
        volume_ratio_5 is not None
        and volume_ratio_5 >= 1.5
    )

    breakout_any = (
        daily_breakout
        or weekly_breakout
        or monthly_breakout
        or ath_breakout
    )

    breakout_buyer = (
        breakout_any
        and bullish_price_action
        and strong_volume
    )

    recent20_high = (
        max(highs[-20:])
        if len(highs) >= 20
        else None
    )

    recent20_low = (
        min(lows[-20:])
        if len(lows) >= 20
        else None
    )

    recent60_high = (
        max(highs[-60:])
        if len(highs) >= 60
        else None
    )

    recent60_low = (
        min(lows[-60:])
        if len(lows) >= 60
        else None
    )

    consolidation_pct = (
        (recent20_high - recent20_low)
        / recent20_low
        * 100.0
        if (
            recent20_high
            and recent20_low
            and recent20_low > 0
        )
        else None
    )

    range60_pct = (
        (recent60_high - recent60_low)
        / recent60_low
        * 100.0
        if (
            recent60_high
            and recent60_low
            and recent60_low > 0
        )
        else None
    )

    consolidation = (
        consolidation_pct is not None
        and range60_pct is not None
        and consolidation_pct <= 12.0
        and consolidation_pct <= range60_pct * 0.55
    )

    consolidation_breakout = (
        consolidation
        and daily_breakout
    )

    prior_widths = []

    for end_idx in range(
        max(20, len(live_closes) - 25),
        len(live_closes) - 1,
    ):
        u, m, l = bb_last(
            live_closes[:end_idx + 1],
            20,
            2.0,
        )

        if (
            u is not None
            and l is not None
            and m
        ):
            prior_widths.append(
                (u - l) / m * 100.0
            )

    avg_prior_width = (
        sum(prior_widths)
        / len(prior_widths)
        if prior_widths
        else None
    )

    squeeze = (
        band_width is not None
        and avg_prior_width is not None
        and band_width <= avg_prior_width * 0.75
    )

    expansion = (
        band_width is not None
        and avg_prior_width is not None
        and band_width >= avg_prior_width * 1.20
    )

    bb_breakout = (
        bb_upper is not None
        and live_price > bb_upper
    )

    macd_bullish = (
        macd_line is not None
        and macd_signal is not None
        and macd_line > macd_signal
        and macd_hist is not None
        and macd_hist > 0
    )

    cci_bullish = (
        cci is not None
        and cci > 100
    )

    trend_bullish = (
        e9 is not None
        and e20 is not None
        and e200 is not None
        and live_price > e9
        and live_price > e200
        and e9 > e20
    )

    strong_breakout = (
        daily_breakout
        and strong_volume
        and trend_bullish
    )

    strong_confluence = (
        consolidation_breakout
        and strong_volume
        and bb_breakout
        and macd_bullish
        and cci_bullish
        and (near_ath or ath_breakout)
    )

    prev_closes = closes[:-1]
    prev_e9 = ema_last(prev_closes, 9)
    prev_e20 = ema_last(prev_closes, 20)

    ema_cross = (
        prev_e9 is not None
        and prev_e20 is not None
        and e9 is not None
        and e20 is not None
        and prev_e9 <= prev_e20
        and e9 > e20
    )

    return {
        "symbol": symbol,
        "live_price": live_price,
        "last_close": closes[-1],
        "ema9": e9,
        "ema20": e20,
        "ema50": e50,
        "ema100": e100,
        "ema200": e200,
        "daily_rsi": daily_rsi,
        "weekly_rsi": weekly_rsi,
        "monthly_rsi": monthly_rsi,
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "bb_band_width": band_width,
        "bb_squeeze": squeeze,
        "bb_expansion": expansion,
        "macd_line": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "cci": cci,
        "volume": volume,
        "volume_avg20": avg20_volume,
        "volume_avg5": avg5_volume,
        "volume_ratio": volume_ratio,
        "volume_ratio_5": volume_ratio_5,
        "volume_spike": volume_spike,
        "volume_spike_5": volume_spike_5,
        "ath": ath,
        "distance_to_ath": distance_to_ath,
        "near_ath": near_ath,
        "ath_breakout": ath_breakout,
        "daily_breakout_level": daily_breakout_level,
        "daily_breakout": daily_breakout,
        "weekly_breakout_level": weekly_breakout_level,
        "weekly_breakout": weekly_breakout,
        "monthly_breakout_level": monthly_breakout_level,
        "monthly_breakout": monthly_breakout,
        "breakout_level": daily_breakout_level,
        "breakout": daily_breakout,
        "price_action_bullish": price_action_bullish,
        "strong_close": strong_close,
        "bullish_price_action": bullish_price_action,
        "strong_volume": strong_volume,
        "breakout_buyer": breakout_buyer,
        "breakout_any": breakout_any,
        "consolidation": consolidation,
        "consolidation_breakout": consolidation_breakout,
        "ema_cross": ema_cross,
        "macd_bullish": macd_bullish,
        "cci_bullish": cci_bullish,
        "trend_bullish": trend_bullish,
        "strong_breakout": strong_breakout,
        "strong_confluence": strong_confluence,
        "signal": (
            "STRONG CONFLUENCE"
            if strong_confluence
            else "BREAKOUT BUYER"
            if breakout_buyer
            else "STRONG BREAKOUT"
            if strong_breakout
            else "ATH BREAKOUT"
            if ath_breakout
            else "MONTHLY BREAKOUT"
            if monthly_breakout
            else "WEEKLY BREAKOUT"
            if weekly_breakout
            else "DAILY BREAKOUT"
            if daily_breakout
            else "BULLISH"
            if trend_bullish
            else "WATCH"
        ),
        "source": "Dhan Daily Historical + Dhan Live LTP/Quote",
    }


# Inject the missing names into app_live_ws's module namespace.
# The existing /api/technical-scan route can then use them without changing
# the existing Dhan LTP/WebSocket routes.
base.get_daily_history = get_daily_history
base.build_technical_row = build_technical_row


if __name__ == "__main__":
    port = int(__import__("os").getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
