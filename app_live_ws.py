import os
import csv
import io
import time
import math
import threading
import asyncio
import struct
import json
from datetime import datetime, timedelta, timezone

import requests
import pyotp

try:
    import websockets
except ImportError:
    websockets = None

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
# DHAN LIVE MARKET FEED WEBSOCKET
# ============================================================

def _ws_subscription_message(security_ids):
    ids = list(security_ids)
    return {
        "RequestCode": 15,
        "InstrumentCount": len(ids),
        "InstrumentList": [
            {"ExchangeSegment": "NSE_EQ", "SecurityId": str(x)}
            for x in ids
        ],
    }


def _ws_send_subscriptions(ws, security_ids):
    ids = list(dict.fromkeys(int(x) for x in security_ids))
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        if not batch:
            continue
        ws.send(json.dumps(_ws_subscription_message(batch)))
        with _ws_lock:
            _ws_subscribed_ids.update(batch)


def _ws_parse_packet(packet):
    global _ws_last_packet_time, _ws_last_error

    if not isinstance(packet, (bytes, bytearray)) or len(packet) < 8:
        return

    try:
        response_code = packet[0]
        security_id = int.from_bytes(packet[4:8], byteorder="little", signed=False)

        # Ticker packet: header (8 bytes) + LTP float32 + LTT int32.
        if response_code == 2 and len(packet) >= 17:
            ltp = struct.unpack_from("<f", packet, 8)[0]
            if math.isfinite(ltp) and ltp > 0:
                with _ws_lock:
                    _ws_prices[security_id] = {
                        "security_id": security_id,
                        "ltp": float(ltp),
                        "source": "Dhan Live Market Feed WebSocket",
                        "updated_at": time.time(),
                    }
                    _ws_last_packet_time = time.time()

        # Quote packet also has LTP at bytes 9-12 (offset 8).
        elif response_code == 4 and len(packet) >= 51:
            ltp = struct.unpack_from("<f", packet, 8)[0]
            if math.isfinite(ltp) and ltp > 0:
                with _ws_lock:
                    _ws_prices[security_id] = {
                        "security_id": security_id,
                        "ltp": float(ltp),
                        "source": "Dhan Live Market Feed WebSocket",
                        "updated_at": time.time(),
                    }
                    _ws_last_packet_time = time.time()
    except Exception as exc:
        with _ws_lock:
            _ws_last_error = "packet parse: " + str(exc)


async def _ws_async_worker():
    """Official-Dhan-style asyncio WebSocket worker with reconnects."""
    global _ws_connected, _ws_last_error

    while not _ws_stop.is_set():
        ws = None
        try:
            print("[WS] getting Dhan access token", flush=True)
            token = get_access_token()
            print("[WS] token ready; connecting to Dhan feed", flush=True)

            ws_url = (
                "wss://api-feed.dhan.co?version=2&token="
                + str(token)
                + "&clientId="
                + str(DHAN_CLIENT_ID)
                + "&authType=2"
            )

            # This follows Dhan's current official Python client approach:
            # asyncio + websockets + v2 URL + JSON subscription packets.
            ws = await asyncio.wait_for(
                websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=None,
                ),
                timeout=15,
            )

            print("[WS] Dhan WebSocket connected", flush=True)
            with _ws_lock:
                _ws_connected = True
                _ws_last_error = ""
                _ws_subscribed_ids.clear()
                desired = list(_ws_desired_ids)

            if desired:
                for start in range(0, len(desired), 100):
                    batch = [int(x) for x in desired[start:start + 100]]
                    if not batch:
                        continue
                    await ws.send(json.dumps(_ws_subscription_message(batch)))
                    with _ws_lock:
                        _ws_subscribed_ids.update(batch)
                print("[WS] subscribed instruments:", len(desired), flush=True)

            while not _ws_stop.is_set():
                with _ws_lock:
                    pending = list(_ws_desired_ids - _ws_subscribed_ids)

                if pending:
                    for start in range(0, len(pending), 100):
                        batch = [int(x) for x in pending[start:start + 100]]
                        if not batch:
                            continue
                        await ws.send(json.dumps(_ws_subscription_message(batch)))
                        with _ws_lock:
                            _ws_subscribed_ids.update(batch)

                try:
                    packet = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    # Keep the socket alive and check for newly requested symbols.
                    continue

                if packet is None:
                    raise RuntimeError("Dhan WebSocket closed the connection")

                if isinstance(packet, (bytes, bytearray)):
                    _ws_parse_packet(packet)

        except Exception as exc:
            print("[WS] connection error:", repr(exc), flush=True)
            with _ws_lock:
                _ws_connected = False
                _ws_last_error = str(exc)
                _ws_subscribed_ids.clear()

            if not _ws_stop.is_set():
                # Refresh only after a failed connection; normal operation
                # keeps the existing token and does not hit token endpoint.
                try:
                    get_access_token(force_refresh=True)
                except Exception as token_exc:
                    with _ws_lock:
                        _ws_last_error = str(token_exc)
                await asyncio.sleep(3)
        finally:
            with _ws_lock:
                _ws_connected = False
            try:
                if ws is not None:
                    await ws.close()
            except Exception:
                pass


def _ws_worker():
    global _ws_last_error, _ws_started_at

    if websockets is None:
        with _ws_lock:
            _ws_last_error = "websockets package is not installed"
        return

    _ws_started_at = time.time()
    print("[WS] asyncio worker started", flush=True)
    try:
        asyncio.run(_ws_async_worker())
    except Exception as exc:
        print("[WS] worker stopped:", repr(exc), flush=True)
        with _ws_lock:
            _ws_last_error = str(exc)
            _ws_connected = False


def start_live_feed():
    global _ws_thread
    if websockets is None:
        return False
    with _ws_lock:
        if _ws_thread is not None and _ws_thread.is_alive():
            return True
        _ws_stop.clear()
        _ws_thread = threading.Thread(
            target=_ws_worker,
            name="dhan-live-feed",
            daemon=True,
        )
        _ws_thread.start()
    return True


def ensure_live_feed_symbols(security_ids):
    ids = [int(x) for x in security_ids if x is not None]
    if not ids:
        return
    with _ws_lock:
        _ws_desired_ids.update(ids)
    start_live_feed()


def get_live_feed_prices(security_ids, wait_seconds=4.0):
    ids = [int(x) for x in security_ids if x is not None]
    if not ids:
        return {}

    ensure_live_feed_symbols(ids)
    deadline = time.time() + max(0.0, float(wait_seconds))

    while time.time() < deadline:
