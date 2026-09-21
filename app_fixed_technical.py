
"""
Technical-data repair wrapper for the Shahnawaz Mansuri Dhan backend.

This file intentionally reuses app_working_backup.py for the already-working
Dhan LTP/authentication routes and replaces only the technical scanner logic.
"""

import math
import time
from datetime import datetime, timedelta, timezone

import app_working_backup as base
from flask import jsonify, request

app = base.app

# Reuse the existing backend helpers/configuration.
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


def fixed_historical_request(security_id, from_date, to_date):
    """Use the existing Dhan historical request, preserving auth/rate limiting."""
    return base.dhan_historical_request(security_id, from_date, to_date)


def fixed_get_daily_history(symbol, days=800):
    """
    Fetch enough daily history for 200 EMA/MACD/BB/RSI.

    Dhan v2 returns `timestamp`. A fallback for `start_Time` is included so
    older response shapes do not silently produce an empty history.
    """
    symbol = clean_symbol(symbol)
    security_id = base.instrument_map.get(symbol)
    if security_id is None:
        return None

    now = time.time()
    cache_key = "technical_v2:" + symbol

    with base._historical_cache_lock:
        cached = base._historical_cache.get(cache_key)
        if cached and now - cached["time"] < HIST_CACHE_SECONDS:
            return cached["data"]

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)

    raw = fixed_historical_request(
        security_id,
        start.isoformat(),
        end.isoformat(),
    )

    timestamps = raw.get("timestamp")
    if not timestamps:
        timestamps = raw.get("start_Time") or raw.get("startTime") or []

    opens = raw.get("open") or []
    highs = raw.get("high") or []
    lows = raw.get("low") or []
    closes = raw.get("close") or []
    volumes = raw.get("volume") or []

    # Technical indicators need OHLC + timestamp. Volume is allowed to be
    # missing because MACD/RSI/EMA/Bollinger should still work.
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

        if len(volumes) >= n:
            volumes = list(volumes[-n:])
        else:
            volumes = [None] * n

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


def build_technical_row_fixed(symbol, history, live_quote=None):
    if not history:
        return None

    timestamps = list(history.get("timestamp") or [])
    opens = [safe_float(x) for x in (history.get("open") or [])]
    highs = [safe_float(x) for x in (history.get("high") or [])]
    lows = [safe_float(x) for x in (history.get("low") or [])]
    closes = [safe_float(x) for x in (history.get("close") or [])]
    raw_volumes = list(history.get("volume") or [])

    n = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
    if n < 26:
        return None

    timestamps = timestamps[-n:]
    opens = opens[-n:]
    highs = highs[-n:]
    lows = lows[-n:]
    closes = closes[-n:]

    volumes = []
    if len(raw_volumes) >= n:
        volumes = [safe_float(x) for x in raw_volumes[-n:]]
    else:
        volumes = [None] * n

    # Keep only rows with valid OHLC/close. Do not discard a stock merely
    # because its volume is unavailable.
    valid = [
        i for i in range(n)
        if all(v is not None for v in (opens[i], highs[i], lows[i], closes[i]))
    ]
    if len(valid) < 26:
        return None

    timestamps = [timestamps[i] for i in valid]
    opens = [opens[i] for i in valid]
    highs = [highs[i] for i in valid]
    lows = [lows[i] for i in valid]
    closes = [closes[i] for i in valid]
    volumes = [volumes[i] for i in valid]

    quote = live_quote if isinstance(live_quote, dict) else {}

    live_price = safe_float(
        quote.get("last_price")
        if quote.get("last_price") is not None
        else quote.get("ltp")
    )
    if live_price is None or live_price <= 0:
        live_price = closes[-1]

    live_ohlc = quote.get("ohlc") if isinstance(quote.get("ohlc"), dict) else {}
    current_open = safe_float(live_ohlc.get("open"))
    current_high = safe_float(live_ohlc.get("high"))
    current_low = safe_float(live_ohlc.get("low"))

    # For today's price-action fields use the live OHLC when available.
    pa_open = current_open if current_open is not None else opens[-1]
    pa_high = current_high if current_high is not None else highs[-1]
    pa_low = current_low if current_low is not None else lows[-1]

    live_closes = list(closes)
    live_closes[-1] = live_price

    e9 = ema_last(live_closes, 9)
    e20 = ema_last(live_closes, 20)
    e50 = ema_last(live_closes, 50)
    e100 = ema_last(live_closes, 100)
    e200 = ema_last(live_closes, 200)

    daily_rsi = rsi_last(live_closes, 14)

    weekly_closes = aggregate_closes_by_week(timestamps, live_closes)
    monthly_closes = aggregate_closes_by_month(timestamps, live_closes)
    weekly_rsi = rsi_last(weekly_closes, 14)
    monthly_rsi = rsi_last(monthly_closes, 14)

    bb_upper, bb_middle, bb_lower = bb_last(live_closes, 20, 2.0)
    band_width = (
        ((bb_upper - bb_lower) / bb_middle) * 100.0
        if bb_upper is not None and bb_lower is not None and bb_middle
        else None
    )

    macd_line, macd_signal, macd_hist = macd_last(live_closes, 12, 26, 9)
    cci = cci_last(highs, lows, live_closes, 20)

    historical_volume = volumes[-1] if volumes else None
    live_volume = safe_float(quote.get("volume"))
    volume = live_volume if live_volume is not None and live_volume > 0 else historical_volume

    numeric_volumes = [v for v in volumes if v is not None and v >= 0]
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
        if volume is not None and avg20_volume and avg20_volume > 0
        else None
    )
    volume_ratio_5 = (
        volume / avg5_volume
        if volume is not None and avg5_volume and avg5_volume > 0
        else None
    )

    historical_ath = max(highs) if highs else None
    ath = max(historical_ath or 0, live_price)
    distance_to_ath = (
        ((ath - live_price) / ath) * 100.0
        if ath > 0
        else None
    )

    # Previous daily high excludes the latest historical candle, so a current
    # live price can genuinely break it.
    lookback_highs = highs[-20:-1] if len(highs) > 20 else highs[:-1]
    daily_breakout_level = max(lookback_highs) if lookback_highs else None
    daily_breakout = (
        daily_breakout_level is not None and live_price > daily_breakout_level
    )

    weekly_bars = _weekly_bars(
        timestamps, opens, highs, lows, closes, volumes
    )
    weekly_breakout_level = (
        max(b["high"] for b in weekly_bars[-20:-1])
        if len(weekly_bars) > 20
        else max((b["high"] for b in weekly_bars[:-1]), default=None)
    )
    weekly_breakout = (
        weekly_breakout_level is not None
        and live_price > weekly_breakout_level
    )

    monthly_bars = _monthly_bars(
        timestamps, opens, highs, lows, closes, volumes
    )
    monthly_breakout_level = (
        max(b["high"] for b in monthly_bars[-13:-1])
        if len(monthly_bars) > 13
        else max((b["high"] for b in monthly_bars[:-1]), default=None)
    )
    monthly_breakout = (
        monthly_breakout_level is not None
        and live_price > monthly_breakout_level
    )

    near_ath = distance_to_ath is not None and distance_to_ath <= 3.0
    ath_breakout = (
        historical_ath is not None and live_price > historical_ath
    )

    price_action_bullish = (
        pa_open is not None and live_price > pa_open
    )
    strong_close = (
        pa_high is not None
        and pa_low is not None
        and pa_high > pa_low
        and ((live_price - pa_low) / (pa_high - pa_low)) >= 0.65
    )
    bullish_price_action = price_action_bullish and strong_close

    strong_volume = (
        volume_ratio is not None and volume_ratio >= 1.5
    )

    breakout_any = (
        daily_breakout
        or weekly_breakout
        or monthly_breakout
        or ath_breakout
    )
    breakout_buyer = breakout_any and bullish_price_action and strong_volume

    recent20_high = max(highs[-20:]) if len(highs) >= 20 else None
    recent20_low = min(lows[-20:]) if len(lows) >= 20 else None
    recent60_high = max(highs[-60:]) if len(highs) >= 60 else None
    recent60_low = min(lows[-60:]) if len(lows) >= 60 else None

    consolidation_pct = None
    if recent20_high and recent20_low and recent20_low > 0:
        consolidation_pct = (
            (recent20_high - recent20_low) / recent20_low * 100.0
        )

    range60_pct = None
    if recent60_high and recent60_low and recent60_low > 0:
        range60_pct = (
            (recent60_high - recent60_low) / recent60_low * 100.0
        )

    consolidation = (
        consolidation_pct is not None
        and range60_pct is not None
        and consolidation_pct <= 12.0
        and consolidation_pct <= range60_pct * 0.55
    )
    consolidation_breakout = consolidation and daily_breakout

    prior_widths = []
    for end_idx in range(
        max(20, len(live_closes) - 25),
        len(live_closes) - 1,
    ):
        u, m, l = bb_last(live_closes[:end_idx + 1], 20, 2.0)
        if u is not None and l is not None and m:
            prior_widths.append((u - l) / m * 100.0)

    avg_prior_width = (
        sum(prior_widths) / len(prior_widths)
        if prior_widths else None
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
        bb_upper is not None and live_price > bb_upper
    )

    macd_bullish = (
        macd_line is not None
        and macd_signal is not None
        and macd_line > macd_signal
        and macd_hist is not None
        and macd_hist > 0
    )

    cci_bullish = cci is not None and cci > 100

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


def _live_quote_map(symbols):
    """Fetch live LTP + current-day volume/OHLC in one Dhan quote request."""
    if not symbols:
        return {}

    ids = [base.instrument_map[s] for s in symbols if s in base.instrument_map]
    if not ids:
        return {}

    raw = base.dhan_quote_request(ids)
    root = raw.get("data", {}) if isinstance(raw, dict) else {}
    nse = root.get("NSE_EQ", {}) if isinstance(root, dict) else {}
    if not isinstance(nse, dict):
        return {}

    reverse = {
        str(base.instrument_map[s]): s
        for s in symbols
        if s in base.instrument_map
    }

    result = {}
    for sid, quote in nse.items():
        symbol = reverse.get(str(sid))
        if symbol and isinstance(quote, dict):
            result[symbol] = quote
    return result


def technical_scan_fixed():
    symbols_text = request.args.get("symbols", "").strip()

    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except (TypeError, ValueError):
        offset = 0

    try:
        limit = max(1, min(50, int(request.args.get("limit", "40"))))
    except (TypeError, ValueError):
        limit = 40

    if symbols_text:
        all_symbols = []
        for raw_symbol in symbols_text.split(","):
            symbol = clean_symbol(raw_symbol)
            if symbol and symbol not in all_symbols:
                all_symbols.append(symbol)
    else:
        all_symbols = list(base.instrument_map.keys())[:400]

    all_symbols = all_symbols[:400]
    page_symbols = all_symbols[offset:offset + limit]

    if not page_symbols:
        return jsonify({
            "status": "success",
            "rows": [],
            "requested": len(all_symbols),
            "returned": 0,
            "offset": offset,
            "limit": limit,
            "next_offset": None,
            "done": True,
            "errors": [],
            "source": "Dhan Daily Historical + Dhan Live LTP/Quote",
        })

    valid_symbols = [
        s for s in page_symbols
        if s in base.instrument_map
    ]

    rows = []
    errors = []

    try:
        live_quotes = _live_quote_map(valid_symbols)

        for symbol in valid_symbols:
            try:
                history = fixed_get_daily_history(symbol, days=800)
                row = build_technical_row_fixed(
                    symbol,
                    history,
                    live_quotes.get(symbol),
                )
                if row is not None:
                    rows.append(row)
                else:
                    errors.append({
                        "symbol": symbol,
                        "error": "Insufficient/invalid historical OHLC data",
                    })
            except Exception as exc:
                errors.append({
                    "symbol": symbol,
                    "error": str(exc),
                })

        next_offset = (
            offset + len(page_symbols)
            if offset + len(page_symbols) < len(all_symbols)
            else None
        )

        return jsonify({
            "status": "success",
            "rows": rows,
            "requested": len(all_symbols),
            "returned": len(rows),
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset,
            "done": next_offset is None,
            "page_requested": len(page_symbols),
            "page_returned": len(rows),
            "errors": errors[:25],
            "source": "Dhan Daily Historical + Dhan Live LTP/Quote",
            "cache_minutes": HIST_CACHE_SECONDS / 60,
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
            "offset": offset,
            "limit": limit,
        }), 500


# Replace only the existing technical scanner route.
app.view_functions["technical_scan"] = technical_scan_fixed


if __name__ == "__main__":
    port = int(__import__("os").getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
