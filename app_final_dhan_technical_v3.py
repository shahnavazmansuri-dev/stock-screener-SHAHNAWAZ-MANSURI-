import os
import csv
import io
import time
import math
import threading
from datetime import datetime, timedelta, timezone

import requests
import pyotp

from flask import Flask, jsonify, request
from flask_cors import CORS


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)
CORS(app)


# ============================================================
# DHAN URLS
# ============================================================

DHAN_API_BASE = "https://api.dhan.co/v2"
DHAN_LTP_URL = f"{DHAN_API_BASE}/marketfeed/ltp"
DHAN_PROFILE_URL = f"{DHAN_API_BASE}/profile"
DHAN_HISTORICAL_URL = f"{DHAN_API_BASE}/charts/historical"
DHAN_TOKEN_URL = "https://auth.dhan.co/app/generateAccessToken"
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"


# ============================================================
# RENDER ENVIRONMENT VARIABLES
# ============================================================

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
DHAN_PIN = os.getenv("DHAN_PIN", "").strip()
DHAN_TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET", "").strip()
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "").strip()


# ============================================================
# GLOBALS
# ============================================================

instrument_map = {}

_token = None
_token_expiry = None
_token_lock = threading.Lock()

_quote_lock = threading.Lock()
_last_quote_time = 0.0

# Historical data is cached so the website can refresh every 15 sec
# without repeatedly downloading hundreds of daily candles.
_historical_cache = {}
_historical_cache_lock = threading.Lock()
HIST_CACHE_SECONDS = 15 * 60

# Dhan Data APIs currently allow 5 requests/sec. Keep a small gap
# between historical calls so the backend stays safely below the limit.
_last_historical_time = 0.0
_historical_lock = threading.Lock()


# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def clean_symbol(symbol):
    symbol = str(symbol or "").strip().upper()
    if symbol.endswith("-EQ"):
        symbol = symbol[:-3]
    return symbol


def safe_float(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def ema_series(values, period):
    values = [safe_float(x) for x in values]
    values = [x for x in values if x is not None]
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    ema = values[0]
    out = [ema]
    for value in values[1:]:
        ema = alpha * value + (1.0 - alpha) * ema
        out.append(ema)
    return out


def ema_last(values, period):
    s = ema_series(values, period)
    return s[-1] if s else None


def rsi_last(values, period=14):
    values = [safe_float(x) for x in values]
    values = [x for x in values if x is not None]
    if len(values) <= period:
        return None

    gains = []
    losses = []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def bb_last(values, period=20, multiplier=2.0):
    vals = [safe_float(x) for x in values]
    vals = [x for x in vals if x is not None]
    if len(vals) < period:
        return None, None, None

    window = vals[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(max(variance, 0.0))
    return middle + multiplier * std, middle, middle - multiplier * std


def macd_last(values, fast=12, slow=26, signal=9):
    vals = [safe_float(x) for x in values]
    vals = [x for x in vals if x is not None]
    if len(vals) < slow:
        return None, None, None

    fast_s = ema_series(vals, fast)
    slow_s = ema_series(vals, slow)
    # Align from the end. Both series start at the same first candle.
    macd = [fast_s[i] - slow_s[i] for i in range(len(slow_s))]
    signal_s = ema_series(macd, signal)
    if not macd or not signal_s:
        return None, None, None
    line = macd[-1]
    sig = signal_s[-1]
    return line, sig, line - sig


def cci_last(highs, lows, closes, period=20):
    h = [safe_float(x) for x in highs]
    l = [safe_float(x) for x in lows]
    c = [safe_float(x) for x in closes]
    if len(h) < period or len(l) < period or len(c) < period:
        return None

    typical = [(h[i] + l[i] + c[i]) / 3.0 for i in range(len(c))]
    window = typical[-period:]
    mean = sum(window) / period
    mean_dev = sum(abs(x - mean) for x in window) / period
    if mean_dev == 0:
        return 0.0
    return (typical[-1] - mean) / (0.015 * mean_dev)


def aggregate_closes_by_week(timestamps, closes):
    buckets = {}
    for ts, close in zip(timestamps, closes):
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            key = (dt.isocalendar().year, dt.isocalendar().week)
            buckets[key] = close
        except (TypeError, ValueError, OverflowError):
            continue
    return [buckets[k] for k in sorted(buckets)]


def aggregate_closes_by_month(timestamps, closes):
    buckets = {}
    for ts, close in zip(timestamps, closes):
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            key = (dt.year, dt.month)
            buckets[key] = close
        except (TypeError, ValueError, OverflowError):
            continue
    return [buckets[k] for k in sorted(buckets)]


def replace_current_close(closes, live_price):
    out = list(closes)
    live = safe_float(live_price)
    if live is not None and live > 0 and out:
        out[-1] = live
    return out


# ============================================================
# LOAD DHAN INSTRUMENT MASTER
# ============================================================

def load_instruments():
    global instrument_map

    try:
        response = requests.get(DHAN_MASTER_URL, timeout=30)
        response.raise_for_status()

        reader = csv.DictReader(io.StringIO(response.text))
        mapping = {}

        for row in reader:
            exchange = str(row.get("SEM_EXM_EXCH_ID", "")).strip().upper()
            segment = str(row.get("SEM_SEGMENT", "")).strip().upper()
            symbol = clean_symbol(row.get("SEM_TRADING_SYMBOL", ""))
            security_id = str(row.get("SEM_SMST_SECURITY_ID", "")).strip()

            if exchange != "NSE" or segment != "E" or not symbol or not security_id:
                continue

            try:
                mapping[symbol] = int(security_id)
            except (TypeError, ValueError):
                continue

        instrument_map = mapping
        print("Loaded NSE instruments:", len(instrument_map))
        return instrument_map

    except Exception as e:
        print("Instrument master error:", str(e))
        instrument_map = {}
        return instrument_map


load_instruments()


# ============================================================
# DHAN ACCESS TOKEN
# ============================================================

def generate_access_token():
    if not DHAN_CLIENT_ID:
        raise RuntimeError("DHAN_CLIENT_ID is missing")
    if not DHAN_PIN:
        raise RuntimeError("DHAN_PIN is missing")
    if not DHAN_TOTP_SECRET:
        raise RuntimeError("DHAN_TOTP_SECRET is missing")

    totp_code = pyotp.TOTP(DHAN_TOTP_SECRET).now()

    response = requests.post(
        DHAN_TOKEN_URL,
        params={
            "dhanClientId": DHAN_CLIENT_ID,
            "pin": DHAN_PIN,
            "totp": totp_code,
        },
        timeout=20,
    )

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
        raise RuntimeError("Dhan token generation failed: " + str(message))

    access_token = data.get("accessToken")
    if not access_token:
        raise RuntimeError("Dhan did not return accessToken")

    expiry = utc_now() + timedelta(hours=23, minutes=30)
    expiry_text = data.get("expiryTime")

    if expiry_text:
        try:
            expiry = datetime.fromisoformat(str(expiry_text).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    print("Dhan automatic token generated")
    return access_token, expiry


def get_access_token(force_refresh=False):
    global _token, _token_expiry

    with _token_lock:
        token_valid = (
            _token
            and _token_expiry
            and utc_now() < _token_expiry - timedelta(minutes=5)
        )

        if token_valid and not force_refresh:
            return _token

        if DHAN_CLIENT_ID and DHAN_PIN and DHAN_TOTP_SECRET:
            _token, _token_expiry = generate_access_token()
            return _token

        if DHAN_ACCESS_TOKEN:
            return DHAN_ACCESS_TOKEN

        raise RuntimeError(
            "Dhan authentication is not configured. "
            "Set DHAN_CLIENT_ID, DHAN_PIN and DHAN_TOTP_SECRET in Render."
        )


# ============================================================
# DHAN LTP
# ============================================================

def dhan_ltp_request(security_ids):
    global _last_quote_time

    if not security_ids:
        return {}

    with _quote_lock:
        elapsed = time.time() - _last_quote_time
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)

        token = get_access_token()

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": token,
            "client-id": DHAN_CLIENT_ID,
        }

        body = {"NSE_EQ": [int(x) for x in security_ids]}

        response = requests.post(
            DHAN_LTP_URL,
            headers=headers,
            json=body,
            timeout=20,
        )
        _last_quote_time = time.time()

        if response.status_code in (401, 403):
            print("Dhan token rejected. Refreshing...")
            token = get_access_token(force_refresh=True)
            headers["access-token"] = token

            response = requests.post(
                DHAN_LTP_URL,
                headers=headers,
                json=body,
                timeout=20,
            )
            _last_quote_time = time.time()

        if response.status_code >= 400:
            try:
                error_data = response.json()
            except ValueError:
                error_data = response.text[:1000]
            raise RuntimeError(
                "Dhan LTP error "
                + str(response.status_code)
                + ": "
                + str(error_data)
            )

        return response.json()


# ============================================================
# DHAN DAILY HISTORICAL DATA
# ============================================================

def dhan_historical_request(security_id, from_date, to_date):
    global _last_historical_time

    with _historical_lock:
        elapsed = time.time() - _last_historical_time
        # Stay below Dhan's current 5 requests/sec data limit.
        if elapsed < 0.22:
            time.sleep(0.22 - elapsed)

        token = get_access_token()

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": token,
        }

        body = {
            "securityId": str(security_id),
            "exchangeSegment": "NSE_EQ",
            "instrument": "EQUITY",
            "expiryCode": 0,
            "oi": False,
            "fromDate": from_date,
            "toDate": to_date,
        }

        response = requests.post(
            DHAN_HISTORICAL_URL,
            headers=headers,
            json=body,
            timeout=30,
        )
        _last_historical_time = time.time()

        if response.status_code in (401, 403):
            print("Dhan historical token rejected. Refreshing...")
            token = get_access_token(force_refresh=True)
            headers["access-token"] = token

            response = requests.post(
                DHAN_HISTORICAL_URL,
                headers=headers,
                json=body,
                timeout=30,
            )
            _last_historical_time = time.time()

        if response.status_code >= 400:
            try:
                error_data = response.json()
            except ValueError:
                error_data = response.text[:1000]
            raise RuntimeError(
                "Dhan historical error "
                + str(response.status_code)
                + ": "
                + str(error_data)
            )

        return response.json()


def get_daily_history(symbol, days=450):
    symbol = clean_symbol(symbol)
    security_id = instrument_map.get(symbol)

    if security_id is None:
        return None

    cache_key = symbol
    now = time.time()

    with _historical_cache_lock:
        cached = _historical_cache.get(cache_key)
        if cached and now - cached["time"] < HIST_CACHE_SECONDS:
            return cached["data"]

    # 450 calendar days gives enough trading sessions for 200 EMA
    # plus RSI/MACD/BB/CCI and weekly/monthly RSI.
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)

    data = dhan_historical_request(
        security_id,
        start.isoformat(),
        end.isoformat(),
    )

    closes = data.get("close") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    opens = data.get("open") or []
    volumes = data.get("volume") or []
    timestamps = data.get("timestamp") or []

    n = min(
        len(closes),
        len(highs),
        len(lows),
        len(opens),
        len(volumes),
        len(timestamps),
    )

    history = {
        "timestamp": timestamps[-n:] if n else [],
        "open": opens[-n:] if n else [],
        "high": highs[-n:] if n else [],
        "low": lows[-n:] if n else [],
        "close": closes[-n:] if n else [],
        "volume": volumes[-n:] if n else [],
    }

    with _historical_cache_lock:
        _historical_cache[cache_key] = {
            "time": now,
            "data": history,
        }

    return history


# ============================================================
# TECHNICAL SCAN
# ============================================================

def build_technical_row(symbol, history, live_price=None):
    if not history:
        return None

    timestamps = history["timestamp"]
    opens = [safe_float(x) for x in history["open"]]
    highs = [safe_float(x) for x in history["high"]]
    lows = [safe_float(x) for x in history["low"]]
    closes = [safe_float(x) for x in history["close"]]
    volumes = [safe_float(x) for x in history["volume"]]

    valid = [
        i for i in range(
            min(len(timestamps), len(opens), len(highs), len(lows), len(closes), len(volumes))
        )
        if all(v is not None for v in (opens[i], highs[i], lows[i], closes[i], volumes[i]))
    ]

    if len(valid) < 210:
        return None

    timestamps = [timestamps[i] for i in valid]
    opens = [opens[i] for i in valid]
    highs = [highs[i] for i in valid]
    lows = [lows[i] for i in valid]
    closes = [closes[i] for i in valid]
    volumes = [volumes[i] for i in valid]

    # Historical values as-of latest completed daily candle.
    # Then replace the latest close with Dhan live LTP so the
    # current technical values react to the live market price.
    base_close = closes[-1]
    live = safe_float(live_price)
    live_close = live if live and live > 0 else base_close
    live_closes = list(closes)
    live_closes[-1] = live_close

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

    volume = volumes[-1]
    avg20_volume = (
        sum(volumes[-20:]) / 20.0
        if len(volumes) >= 20
        else None
    )
    volume_ratio = (
        volume / avg20_volume
        if avg20_volume and avg20_volume > 0
        else None
    )

    # ATH uses historical highs. Include the live price in the
    # current ATH check so a live new high is immediately visible.
    historical_ath = max(highs) if highs else None
    ath = max(historical_ath or 0, live_close)

    distance_to_ath = (
        ((ath - live_close) / ath) * 100.0
        if ath
        else None
    )

    # Recent resistance / breakout level.
    lookback_highs = highs[-20:-1] if len(highs) > 20 else highs[:-1]
    breakout_level = max(lookback_highs) if lookback_highs else None

    breakout = (
        live_close > breakout_level
        if breakout_level is not None
        else False
    )

    near_ath = (
        distance_to_ath is not None
        and distance_to_ath <= 3.0
    )

    ath_breakout = (
        historical_ath is not None
        and live_close > historical_ath
    )

    # Consolidation proxy:
    # recent 20-day range is relatively tight versus the 60-day range.
    recent20_high = max(highs[-20:]) if len(highs) >= 20 else None
    recent20_low = min(lows[-20:]) if len(lows) >= 20 else None
    recent60_high = max(highs[-60:]) if len(highs) >= 60 else None
    recent60_low = min(lows[-60:]) if len(lows) >= 60 else None

    consolidation_pct = None
    if recent20_high and recent20_low and recent20_low > 0:
        consolidation_pct = (recent20_high - recent20_low) / recent20_low * 100.0

    range60_pct = None
    if recent60_high and recent60_low and recent60_low > 0:
        range60_pct = (recent60_high - recent60_low) / recent60_low * 100.0

    consolidation = (
        consolidation_pct is not None
        and range60_pct is not None
        and consolidation_pct <= 12.0
        and consolidation_pct <= range60_pct * 0.55
    )

    consolidation_breakout = consolidation and breakout

    # Bollinger squeeze / expansion
    prior_widths = []
    for end_idx in range(max(20, len(live_closes) - 25), len(live_closes) - 1):
        w_u, w_m, w_l = bb_last(live_closes[:end_idx + 1], 20, 2.0)
        if w_u is not None and w_l is not None and w_m:
            prior_widths.append((w_u - w_l) / w_m * 100.0)

    avg_prior_width = (
        sum(prior_widths) / len(prior_widths)
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
        and live_close > bb_upper
    )

    macd_bullish = (
        macd_line is not None
        and macd_signal is not None
        and macd_line > macd_signal
        and (macd_hist is None or macd_hist > 0)
    )

    cci_bullish = cci is not None and cci > 100
    trend_bullish = (
        e9 is not None
        and e20 is not None
        and e200 is not None
        and live_close > e9
        and live_close > e200
        and e9 > e20
    )

    strong_breakout = (
        breakout
        and (volume_ratio is not None and volume_ratio >= 1.5)
        and trend_bullish
    )

    strong_confluence = (
        consolidation_breakout
        and (volume_ratio is not None and volume_ratio >= 1.5)
        and bb_breakout
        and macd_bullish
        and cci_bullish
        and (near_ath or ath_breakout)
    )

    # Previous EMA values for crossover detection.
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
        "live_price": live_close,
        "last_close": base_close,
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
        "volume_ratio": volume_ratio,
        "ath": ath,
        "distance_to_ath": distance_to_ath,
        "near_ath": near_ath,
        "ath_breakout": ath_breakout,
        "breakout_level": breakout_level,
        "breakout": breakout,
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
            else "STRONG BREAKOUT"
            if strong_breakout
            else "ATH BREAKOUT"
            if ath_breakout
            else "BREAKOUT"
            if breakout
            else "BULLISH"
            if trend_bullish
            else "WATCH"
        ),
        "source": "Dhan Daily Historical + Dhan Live LTP",
    }


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def home():
    return jsonify({
        "status": "OK",
        "message": "Shahnawaz Mansuri Dhan Live + Technical Backend",
        "version": "dhan-auto-token-v3",
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "instruments_loaded": len(instrument_map),
        "technical_cache": len(_historical_cache),
    })


@app.route("/api/diagnostic")
def diagnostic():
    automatic_mode = bool(
        DHAN_CLIENT_ID and DHAN_PIN and DHAN_TOTP_SECRET
    )
    return jsonify({
        "status": "success",
        "client_id_configured": bool(DHAN_CLIENT_ID),
        "pin_configured": bool(DHAN_PIN),
        "totp_configured": bool(DHAN_TOTP_SECRET),
        "manual_token_configured": bool(DHAN_ACCESS_TOKEN),
        "automatic_token_mode": automatic_mode,
        "instruments_loaded": len(instrument_map),
        "technical_cache": len(_historical_cache),
    })


@app.route("/api/token-status")
def token_status():
    return jsonify({
        "status": "success",
        "automatic_token_mode": bool(
            DHAN_CLIENT_ID and DHAN_PIN and DHAN_TOTP_SECRET
        ),
        "token_available": bool(_token or DHAN_ACCESS_TOKEN),
        "token_expiry": (
            _token_expiry.isoformat()
            if _token_expiry else None
        ),
    })


@app.route("/api/profile-test")
def profile_test():
    try:
        token = get_access_token()
        headers = {
            "Accept": "application/json",
            "access-token": token,
        }
        response = requests.get(
            DHAN_PROFILE_URL,
            headers=headers,
            timeout=20,
        )

        if response.status_code in (401, 403):
            token = get_access_token(force_refresh=True)
            headers["access-token"] = token
            response = requests.get(
                DHAN_PROFILE_URL,
                headers=headers,
                timeout=20,
            )

        try:
            data = response.json()
        except ValueError:
            data = {"raw_response": response.text[:1000]}

        if response.status_code >= 400:
            return jsonify({
                "status": "error",
                "http_status": response.status_code,
                "dhan_response": data,
            }), response.status_code

        return jsonify({
            "status": "success",
            "profile": data,
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
        }), 500


@app.route("/api/ltp")
def get_ltp():
    symbols_text = request.args.get("symbols", "").strip()

    if not symbols_text:
        return jsonify({
            "status": "error",
            "message": (
                "Please provide symbols. Example: "
                "/api/ltp?symbols=RELIANCE,TCS,INFY"
            ),
        }), 400

    requested_symbols = []
    for raw_symbol in symbols_text.split(","):
        symbol = clean_symbol(raw_symbol)
        if symbol and symbol not in requested_symbols:
            requested_symbols.append(symbol)

    if not requested_symbols:
        return jsonify({
            "status": "error",
            "message": "No valid symbols supplied",
        }), 400

    if not instrument_map:
        load_instruments()

    security_ids = {}
    not_found = []

    for symbol in requested_symbols:
        security_id = instrument_map.get(symbol)
        if security_id is None:
            not_found.append(symbol)
        else:
            security_ids[symbol] = int(security_id)

    if not security_ids:
        return jsonify({
            "status": "error",
            "message": "No valid NSE symbols found",
            "not_found": not_found,
        }), 404

    try:
        prices = {}
        symbols = list(security_ids.keys())
        batch_size = 1000

        for start in range(0, len(symbols), batch_size):
            batch_symbols = symbols[start:start + batch_size]
            batch_ids = [security_ids[s] for s in batch_symbols]
            data = dhan_ltp_request(batch_ids)

            market_data = data.get("data", {})
            nse_data = market_data.get("NSE_EQ", {})

            reverse_map = {
                str(security_ids[s]): s for s in batch_symbols
            }

            for security_id, quote in nse_data.items():
                symbol = reverse_map.get(str(security_id))
                if not symbol or not isinstance(quote, dict):
                    continue

                last_price = safe_float(quote.get("last_price"))
                if last_price is None or last_price <= 0:
                    continue

                prices[symbol] = {
                    "symbol": symbol,
                    "security_id": int(security_id),
                    "ltp": last_price,
                }

        return jsonify({
            "status": "success",
            "prices": prices,
            "not_found": not_found,
            "requested": len(requested_symbols),
            "returned": len(prices),
            "source": "Dhan Market Quote",
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "not_found": not_found,
        }), 500


@app.route("/api/technical-scan")
def technical_scan():
    """
    Technical scanner endpoint.

    Example:
      /api/technical-scan?symbols=TCS,INFY,RELIANCE

    If symbols are omitted, the endpoint scans up to 400 NSE symbols.
    Historical candles are cached for 15 minutes. The frontend can
    overlay the separately refreshed Dhan LTP every ~15 seconds.
    """
    symbols_text = request.args.get("symbols", "").strip()

    if symbols_text:
        symbols = []
        for raw in symbols_text.split(","):
            symbol = clean_symbol(raw)
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    else:
        symbols = list(instrument_map.keys())[:400]

    if not symbols:
        return jsonify({
            "status": "error",
            "message": "No NSE symbols available",
        }), 400

    # Prevent an accidental giant request from tying up the free Render service.
    symbols = symbols[:400]

    # Get live LTP in one Dhan request for the selected symbols.
    live_prices = {}
    valid_symbols = [s for s in symbols if s in instrument_map]

    try:
        for start in range(0, len(valid_symbols), 1000):
            batch = valid_symbols[start:start + 1000]
            ids = [instrument_map[s] for s in batch]
            data = dhan_ltp_request(ids)
            nse_data = data.get("data", {}).get("NSE_EQ", {})
            reverse_map = {str(instrument_map[s]): s for s in batch}

            for security_id, quote in nse_data.items():
                symbol = reverse_map.get(str(security_id))
                if symbol and isinstance(quote, dict):
                    px = safe_float(quote.get("last_price"))
                    if px is not None and px > 0:
                        live_prices[symbol] = px

        rows = []
        errors = []

        for symbol in valid_symbols:
            try:
                history = get_daily_history(symbol)
                row = build_technical_row(
                    symbol,
                    history,
                    live_prices.get(symbol),
                )
                if row:
                    rows.append(row)
            except Exception as e:
                errors.append({
                    "symbol": symbol,
                    "error": str(e),
                })

        return jsonify({
            "status": "success",
            "rows": rows,
            "requested": len(symbols),
            "returned": len(rows),
            "errors": errors[:25],
            "source": "Dhan Daily Historical + Dhan Live LTP",
            "cache_minutes": HIST_CACHE_SECONDS / 60,
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
        }), 500


@app.route("/api/tcs-test")
def tcs_test():
    try:
        security_id = instrument_map.get("TCS", 11536)
        data = dhan_ltp_request([security_id])
        return jsonify({
            "status": "success",
            "data": data,
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
        }), 500


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
