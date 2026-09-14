import os
import csv
import io
import time
import math
import threading
import struct
import json
from urllib.parse import quote as urlquote
from datetime import datetime, timedelta, timezone

import requests
import pyotp

try:
    from dhanhq import DhanContext, MarketFeed
except ImportError:
    DhanContext = None
    MarketFeed = None

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
DHAN_OHLC_URL = f"{DHAN_API_BASE}/marketfeed/ohlc"
DHAN_QUOTE_URL = f"{DHAN_API_BASE}/marketfeed/quote"
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

# Live Dhan WebSocket cache. The website can read this cache without
# making a REST Market Quote request for every refresh.
_ws_lock = threading.RLock()
_ws_desired_ids = set()
_ws_subscribed_ids = set()
_ws_prices = {}
_ws_thread = None
_ws_stop = threading.Event()
_ws_connected = False
_ws_last_error = ""
_ws_last_packet_time = 0.0
_ws_started_at = None


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

def _dhan_market_request(url, security_ids, label):
    """Safe shared request helper for Dhan market-feed endpoints."""
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

        response = requests.post(url, headers=headers, json=body, timeout=20)
        _last_quote_time = time.time()

        if response.status_code in (401, 403):
            print("Dhan token rejected on", label, "- refreshing automatically...")
            token = get_access_token(force_refresh=True)
            headers["access-token"] = token
            response = requests.post(url, headers=headers, json=body, timeout=20)
            _last_quote_time = time.time()

        if response.status_code >= 400:
            try:
                error_data = response.json()
            except ValueError:
                error_data = response.text[:1000]
            raise RuntimeError(
                "Dhan " + label + " error " + str(response.status_code) + ": " + str(error_data)
            )

        try:
            return response.json()
        except ValueError:
            raise RuntimeError("Dhan " + label + " returned invalid JSON")


def dhan_ltp_request(security_ids):
    return _dhan_market_request(DHAN_LTP_URL, security_ids, "LTP")


def dhan_ohlc_request(security_ids):
    return _dhan_market_request(DHAN_OHLC_URL, security_ids, "OHLC")


def dhan_quote_request(security_ids):
    return _dhan_market_request(DHAN_QUOTE_URL, security_ids, "QUOTE")


# ============================================================
# DHAN LIVE MARKET FEED - OFFICIAL DHANHQ PYTHON CLIENT
# ============================================================

_official_feed = None


def _official_on_connect(feed):
    global _ws_connected, _ws_last_error
    with _ws_lock:
        _ws_connected = True
        _ws_last_error = ""
        # MarketFeed subscribes the instruments supplied at construction.
        _ws_subscribed_ids.update(_ws_desired_ids)
    print("[WS] DhanHQ MarketFeed connected")


def _official_on_message(feed, data):
    global _ws_last_packet_time
    if not isinstance(data, dict):
        return

    security_id = data.get("security_id")
    ltp = data.get("LTP")

    try:
        security_id = int(security_id)
        ltp = float(ltp)
    except (TypeError, ValueError):
        return

    if not math.isfinite(ltp) or ltp <= 0:
        return

    with _ws_lock:
        _ws_prices[security_id] = {
            "security_id": security_id,
            "ltp": ltp,
            "source": "DhanHQ Python MarketFeed",
            "updated_at": time.time(),
        }
        _ws_last_packet_time = time.time()


def _official_on_close(feed):
    global _ws_connected
    with _ws_lock:
        _ws_connected = False
        _ws_subscribed_ids.clear()
    print("[WS] DhanHQ MarketFeed closed")


def _official_on_error(feed, error):
    global _ws_connected, _ws_last_error
    with _ws_lock:
        _ws_connected = False
        _ws_last_error = str(error)
    print("[WS] DhanHQ MarketFeed error:", str(error))


def _build_official_feed(initial_ids):
    if DhanContext is None or MarketFeed is None:
        raise RuntimeError("dhanhq package is not installed")

    if not DHAN_CLIENT_ID or not DHAN_ACCESS_TOKEN:
        raise RuntimeError(
            "Dhan authentication is not configured. "
            "Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Render."
        )

    context = DhanContext(DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)
    instruments = [
        (MarketFeed.NSE, str(int(sid)), MarketFeed.Ticker)
        for sid in sorted(set(initial_ids))
    ]

    return MarketFeed(
        context,
        instruments,
        "v2",
        on_connect=_official_on_connect,
        on_message=_official_on_message,
        on_close=_official_on_close,
        on_error=_official_on_error,
    )


def start_live_feed():
    global _ws_thread, _official_feed, _ws_started_at

    if DhanContext is None or MarketFeed is None:
        with _ws_lock:
            _ws_last_error = "dhanhq package is not installed"
        return False

    with _ws_lock:
        if _ws_thread is not None and _ws_thread.is_alive():
            return True
        initial_ids = list(_ws_desired_ids)

    if not initial_ids:
        return False

    try:
        _official_feed = _build_official_feed(initial_ids)
        _ws_started_at = time.time()

        # Official DhanHQ client manages the asyncio WebSocket and reconnect loop.
        _ws_thread = _official_feed.start()

        print("[WS] Official DhanHQ MarketFeed started")
        return True

    except Exception as exc:
        with _ws_lock:
            _ws_connected = False
            _ws_last_error = str(exc)
        print("[WS] Failed to start DhanHQ MarketFeed:", str(exc))
        return False


def ensure_live_feed_symbols(security_ids):
    ids = [int(x) for x in security_ids if x is not None]
    if not ids:
        return

    new_ids = []
    with _ws_lock:
        for sid in ids:
            if sid not in _ws_desired_ids:
                _ws_desired_ids.add(sid)
                new_ids.append(sid)

    if _official_feed is None:
        start_live_feed()
        return

    with _ws_lock:
        connected = bool(_ws_connected)

    if connected and new_ids:
        try:
            symbols = [
                (MarketFeed.NSE, str(sid), MarketFeed.Ticker)
                for sid in new_ids
            ]
            _official_feed.subscribe_symbols(symbols)
            with _ws_lock:
                _ws_subscribed_ids.update(new_ids)
        except Exception as exc:
            with _ws_lock:
                _ws_last_error = str(exc)
            print("[WS] Dynamic subscription error:", str(exc))


def get_live_feed_prices(security_ids, wait_seconds=4.0):
    ids = [int(x) for x in security_ids if x is not None]
    if not ids:
        return {}

    ensure_live_feed_symbols(ids)
    deadline = time.time() + max(0.0, float(wait_seconds))

    while time.time() < deadline:
        with _ws_lock:
            found = {
                sid: dict(_ws_prices[sid])
                for sid in ids
                if sid in _ws_prices
            }
        if len(found) == len(set(ids)):
            return found
        time.sleep(0.15)

    with _ws_lock:
        return {
            sid: dict(_ws_prices[sid])
            for sid in ids
            if sid in _ws_prices
        }


def live_feed_status():
    with _ws_lock:
        return {
            "websocket_package": DhanContext is not None and MarketFeed is not None,
            "library": "dhanhq",
            "connected": bool(_ws_connected),
            "desired_instruments": len(_ws_desired_ids),
            "subscribed_instruments": len(_ws_subscribed_ids),
            "cached_prices": len(_ws_prices),
            "last_packet_seconds_ago": (
                None if not _ws_last_packet_time
                else round(max(0.0, time.time() - _ws_last_packet_time), 2)
            ),
            "last_error": _ws_last_error,
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
            "message": "Please provide symbols. Example: /api/ltp?symbols=RELIANCE,TCS,INFY",
        }), 400

    requested_symbols = []
    for raw_symbol in symbols_text.split(","):
        symbol = clean_symbol(raw_symbol)
        if symbol and symbol not in requested_symbols:
            requested_symbols.append(symbol)

    if not requested_symbols:
        return jsonify({"status": "error", "message": "No valid symbols supplied"}), 400

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
        # Primary path: Dhan Live Market Feed WebSocket.
        live_data = get_live_feed_prices(list(security_ids.values()), wait_seconds=4.0)
        prices = {}
        for symbol, security_id in security_ids.items():
            quote = live_data.get(int(security_id))
            if quote:
                last_price = safe_float(quote.get("ltp"))
                if last_price is not None and last_price > 0:
                    prices[symbol] = {
                        "symbol": symbol,
                        "security_id": int(security_id),
                        "ltp": last_price,
                    }

        # REST Market Quote remains a fallback for anything not yet received
        # on the WebSocket (for example immediately after first subscription).
        source_used = "Dhan Live Market Feed WebSocket"
        missing_symbols = [s for s in security_ids if s not in prices]
        if missing_symbols:
            missing_ids = [security_ids[s] for s in missing_symbols]
            data = dhan_ltp_request(missing_ids)
            market_data = data.get("data", {}) if isinstance(data, dict) else {}
            nse_data = market_data.get("NSE_EQ", {}) if isinstance(market_data, dict) else {}
            if not isinstance(nse_data, dict):
                nse_data = {}
            reverse_map = {str(security_ids[s]): s for s in missing_symbols}
            for security_id, quote in nse_data.items():
                symbol = reverse_map.get(str(security_id))
                if not symbol or not isinstance(quote, dict):
                    continue
                last_price = safe_float(quote.get("last_price"))
                if last_price is not None and last_price > 0:
                    prices[symbol] = {
                        "symbol": symbol,
                        "security_id": int(security_id),
                        "ltp": last_price,
                    }
                    source_used = "Dhan Live Feed + Market Quote fallback"

        return jsonify({
            "status": "success",
            "prices": prices,
            "not_found": not_found,
            "requested": len(requested_symbols),
            "returned": len(prices),
            "source": source_used,
            "live_feed": live_feed_status(),
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "not_found": not_found,
            "live_feed": live_feed_status(),
        }), 500


@app.route("/api/live-feed-status")
def live_feed_status_route():
    return jsonify({"status": "success", "live_feed": live_feed_status()})


@app.route("/api/tcs-ws-test")
def tcs_ws_test():
    """Safe diagnostic for TCS through Dhan Live Market Feed WebSocket."""
    security_id = 11536
    live_data = get_live_feed_prices([security_id], wait_seconds=6.0)
    quote = live_data.get(security_id)
    return jsonify({
        "status": "success",
        "symbol": "TCS",
        "security_id": security_id,
        "live_quote": quote,
        "live_feed": live_feed_status(),
    })


@app.route("/api/technical-scan")
def technical_scan():
    """
    Final paginated technical scanner.

    Small batches are used so Render does not timeout while
    Dhan historical data is being requested.

    Examples:
      /api/technical-scan?symbols=TCS,INFY,RELIANCE
      /api/technical-scan?symbols=TCS,INFY,RELIANCE&offset=0&limit=40
    """
    symbols_text = request.args.get("symbols", "").strip()

    try:
        offset = int(request.args.get("offset", "0"))
    except (TypeError, ValueError):
        offset = 0

    try:
        limit = int(request.args.get("limit", "40"))
    except (TypeError, ValueError):
        limit = 40

    offset = max(0, offset)
    limit = max(1, min(limit, 50))

    if symbols_text:
        all_symbols = []
        for raw_symbol in symbols_text.split(","):
            symbol = clean_symbol(raw_symbol)
            if symbol and symbol not in all_symbols:
                all_symbols.append(symbol)
    else:
        all_symbols = list(instrument_map.keys())[:400]

    if not all_symbols:
        return jsonify({
            "status": "error",
            "message": "No NSE symbols available",
        }), 400

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
            "source": "Dhan Daily Historical + Dhan Live LTP",
            "cache_minutes": HIST_CACHE_SECONDS / 60,
        })

    valid_symbols = [s for s in page_symbols if s in instrument_map]
    live_prices = {}
    errors = []
    rows = []

    try:
        # --------------------------------------------------------
        # LIVE PRICE FOR THIS SMALL PAGE
        # --------------------------------------------------------
        if valid_symbols:
            ids = [instrument_map[s] for s in valid_symbols]
            data = dhan_ltp_request(ids)
            nse_data = data.get("data", {}).get("NSE_EQ", {})
            reverse_map = {str(instrument_map[s]): s for s in valid_symbols}

            for security_id, quote in nse_data.items():
                symbol = reverse_map.get(str(security_id))
                if not symbol or not isinstance(quote, dict):
                    continue
                px = safe_float(quote.get("last_price"))
                if px is not None and px > 0:
                    live_prices[symbol] = px

        # --------------------------------------------------------
        # HISTORICAL + TECHNICAL CALCULATION
        # --------------------------------------------------------
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
            "source": "Dhan Daily Historical + Dhan Live LTP",
            "cache_minutes": HIST_CACHE_SECONDS / 60,
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "offset": offset,
            "limit": limit,
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


@app.route("/api/tcs-hard-test")
def tcs_hard_test():
    """Safe diagnostic: test TCS directly with known NSE security ID 11536.
    No credentials or tokens are returned.
    """
    security_id = 11536
    result = {
        "status": "success",
        "symbol": "TCS",
        "security_id": security_id,
        "mapped_security_id": instrument_map.get("TCS"),
        "tests": {}
    }

    for label, fn in (("LTP", dhan_ltp_request), ("OHLC", dhan_ohlc_request), ("QUOTE", dhan_quote_request)):
        try:
            raw = fn([security_id])
            result["tests"][label] = raw
        except Exception as e:
            result["tests"][label] = {"error": str(e)}

    return jsonify(result)


# ============================================================
# START LIVE FEED THREAD
# ============================================================

start_live_feed()


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
