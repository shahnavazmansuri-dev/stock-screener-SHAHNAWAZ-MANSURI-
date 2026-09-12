import os
import time
import threading
from datetime import datetime, timedelta, timezone

import requests
import pyotp

from flask import Flask, jsonify, request
from flask_cors import CORS


# ============================================================
# APP
# ============================================================

app = Flask(__name__)
CORS(app)


# ============================================================
# DHAN CONFIG
# ============================================================

DHAN_API_BASE = "https://api.dhan.co/v2"

DHAN_LTP_URL = f"{DHAN_API_BASE}/marketfeed/ltp"
DHAN_PROFILE_URL = f"{DHAN_API_BASE}/profile"

DHAN_AUTH_URL = (
    "https://auth.dhan.co/app/generateAccessToken"
)

DHAN_MASTER_URL = (
    "https://images.dhan.co/api-data/api-scrip-master.csv"
)


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
DHAN_PIN = os.getenv("DHAN_PIN", "").strip()
DHAN_TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET", "").strip()

# Optional manual fallback
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "").strip()


# ============================================================
# GLOBAL VARIABLES
# ============================================================

instrument_map = {}

token_cache = None
token_expiry = None

token_lock = threading.Lock()

quote_lock = threading.Lock()
last_quote_time = 0.0


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def clean_symbol(symbol):
    symbol = str(symbol or "").strip().upper()

    if symbol.endswith("-EQ"):
        symbol = symbol[:-3]

    return symbol


# ============================================================
# LOAD DHAN INSTRUMENT MASTER
# ============================================================

def load_instruments():
    global instrument_map

    try:
        response = requests.get(
            DHAN_MASTER_URL,
            timeout=30
        )

        response.raise_for_status()

        lines = response.text.splitlines()

        if not lines:
            raise Exception("Dhan instrument master is empty")

        header = lines[0].split(",")

        required = [
            "SEM_EXM_EXCH_ID",
            "SEM_SEGMENT",
            "SEM_TRADING_SYMBOL",
            "SEM_SMST_SECURITY_ID",
        ]

        for column in required:
            if column not in header:
                raise Exception(
                    f"Missing Dhan master column: {column}"
                )

        idx_exchange = header.index("SEM_EXM_EXCH_ID")
        idx_segment = header.index("SEM_SEGMENT")
        idx_symbol = header.index("SEM_TRADING_SYMBOL")
        idx_security = header.index("SEM_SMST_SECURITY_ID")

        mapping = {}

        for line in lines[1:]:
            try:
                row = line.split(",")

                if len(row) <= max(
                    idx_exchange,
                    idx_segment,
                    idx_symbol,
                    idx_security
                ):
                    continue

                exchange = row[idx_exchange].strip().upper()
                segment = row[idx_segment].strip().upper()
                symbol = row[idx_symbol].strip().upper()
                security_id = row[idx_security].strip()

                # NSE Equity only
                if (
                    exchange == "NSE"
                    and segment == "E"
                    and symbol
                    and security_id
                ):
                    clean = clean_symbol(symbol)

                    try:
                        mapping[clean] = int(security_id)
                    except ValueError:
                        pass

            except Exception:
                continue

        instrument_map = mapping

        print(
            f"Dhan instrument master loaded: "
            f"{len(instrument_map)} NSE equity symbols"
        )

        return instrument_map

    except Exception as e:
        print(f"Instrument master error: {e}")

        instrument_map = {}

        return instrument_map


# Load instruments when server starts
load_instruments()


# ============================================================
# AUTOMATIC DHAN TOKEN
# ============================================================

def generate_dhan_token():
    """
    Generates a fresh Dhan access token using:
    Client ID + PIN + TOTP Secret
    """

    if not DHAN_CLIENT_ID:
        raise Exception("DHAN_CLIENT_ID is missing")

    if not DHAN_PIN:
        raise Exception("DHAN_PIN is missing")

    if not DHAN_TOTP_SECRET:
        raise Exception("DHAN_TOTP_SECRET is missing")

    try:
        totp = pyotp.TOTP(DHAN_TOTP_SECRET).now()

        params = {
            "dhanClientId": DHAN_CLIENT_ID,
            "pin": DHAN_PIN,
            "totp": totp,
        }

        response = requests.post(
            DHAN_AUTH_URL,
            params=params,
            timeout=20
        )

        try:
            data = response.json()
        except Exception:
            data = {}

        if response.status_code != 200:
            message = (
                data.get("errorMessage")
                or data.get("message")
                or response.text
            )

            raise Exception(
                f"Dhan token generation failed: {message}"
            )

        access_token = data.get("accessToken")

        if not access_token:
            raise Exception(
                "Dhan did not return an accessToken"
            )

        # Dhan access tokens are 24-hour tokens.
        # Keep a conservative local expiry.
        expiry = now_utc() + timedelta(hours=23, minutes=30)

        expiry_text = data.get("expiryTime")

        if expiry_text:
            try:
                parsed = datetime.fromisoformat(
                    expiry_text.replace("Z", "+00:00")
                )

                if parsed.tzinfo is None:
                    parsed = parsed.replace(
                        tzinfo=timezone.utc
                    )

                expiry = parsed

            except Exception:
                pass

        print(
            "Dhan access token generated successfully"
        )

        return access_token, expiry

    except Exception as e:
        print(f"Token generation error: {e}")
        raise


# ============================================================
# GET VALID TOKEN
# ============================================================

def get_dhan_token(force_refresh=False):

    global token_cache
    global token_expiry

    with token_lock:

        # Existing automatic token still valid
        if (
            not force_refresh
            and token_cache
            and token_expiry
            and now_utc() < token_expiry - timedelta(minutes=5)
        ):
            return token_cache

        # Automatic TOTP token
        if (
            DHAN_CLIENT_ID
            and DHAN_PIN
            and DHAN_TOTP_SECRET
        ):
            token, expiry = generate_dhan_token()

            token_cache = token
            token_expiry = expiry

            return token

        # Manual fallback
        if DHAN_ACCESS_TOKEN:
            return DHAN_ACCESS_TOKEN

        raise Exception(
            "Dhan authentication is not configured. "
            "Set DHAN_CLIENT_ID, DHAN_PIN and "
            "DHAN_TOTP_SECRET in Render Environment Variables."
        )


# ============================================================
# DHAN REQUEST
# ============================================================

def dhan_request(body):

    global last_quote_time

    # Dhan Quote API rate limit is 1 request/sec.
    with quote_lock:

        elapsed = time.time() - last_quote_time

        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)

        token = get_dhan_token()

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": token,
            "client-id": DHAN_CLIENT_ID,
        }

        response = requests.post(
            DHAN_LTP_URL,
            headers=headers,
            json=body,
            timeout=20
        )

        last_quote_time = time.time()

        # Token expired / unauthorized
        if response.status_code in (401, 403):

            print(
                "Dhan token rejected. "
                "Generating fresh token..."
            )

            token = get_dhan_token(
                force_refresh=True
            )

            headers["access-token"] = token

            response = requests.post(
                DHAN_LTP_URL,
                headers=headers,
                json=body,
                timeout=20
            )

            last_quote_time = time.time()

        response.raise_for_status()

        return response.json()


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "status": "OK",
        "message": "Shahnawaz Mansuri Dhan Live Price Backend",
        "version": "dhan-auto-token-v1"
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "healthy",
        "instruments_loaded": len(instrument_map)
    })


# ============================================================
# TOKEN STATUS
# ============================================================

@app.route("/api/token-status")
def token_status():

    automatic = bool(
        DHAN_CLIENT_ID
        and DHAN_PIN
        and DHAN_TOTP_SECRET
    )

    if token_expiry:
        expiry = token_expiry.isoformat()
    else:
        expiry = None

    return jsonify({
        "status": "success",
        "automatic_token": automatic,
        "token_available": bool(
            token_cache or DHAN_ACCESS_TOKEN
        ),
        "token_expiry": expiry
    })


# ============================================================
# PROFILE TEST
# ============================================================

@app.route("/api/profile-test")
def profile_test():

    try:

        token = get_dhan_token()

        headers = {
            "Accept": "application/json",
            "access-token": token,
        }

        response = requests.get(
            DHAN_PROFILE_URL,
            headers=headers,
            timeout=20
        )

        if response.status_code in (401, 403):

            token = get_dhan_token(
                force_refresh=True
            )

            headers["access-token"] = token

            response = requests.get(
                DHAN_PROFILE_URL,
                headers=headers,
                timeout=20
            )

        try:
            data = response.json()
        except Exception:
            data = {
                "raw": response.text
            }

        if response.status_code >= 400:

            return jsonify({
                "status": "error",
                "http_status": response.status_code,
                "dhan_response": data
            }), response.status_code

        return jsonify({
            "status": "success",
            "profile": data
        })

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# ============================================================
# LTP API
# ============================================================

@app.route("/api/ltp")
def get_ltp():

    symbols_text = request.args.get(
        "symbols",
        ""
    ).strip()

    if not symbols_text:

        return jsonify({
            "status": "error",
            "message": "symbols parameter is required"
        }), 400

    raw_symbols = symbols_text.split(",")

    symbols = []

    for symbol in raw_symbols:

        clean = clean_symbol(symbol)

        if clean and clean not in symbols:
            symbols.append(clean)

    if not symbols:

        return jsonify({
            "status": "error",
            "message": "No valid symbols supplied"
        }), 400

    # Make sure instrument master exists
    if not instrument_map:

        load_instruments()

    security_ids = {}
    not_found = []

    for symbol in symbols:

        security_id = instrument_map.get(symbol)

        if security_id is not None:

            security_ids[symbol] = int(
                security_id
            )

        else:

            not_found.append(symbol)

    if not security_ids:

        return jsonify({
            "status": "error",
            "message": "No valid NSE symbols found",
            "not_found": not_found
        }), 404

    try:

        dhan_body = {
            "NSE_EQ": list(
                security_ids.values()
            )
        }

        data = dhan_request(
            dhan_body
        )

        prices = {}

        dhan_data = data.get(
            "data",
            {}
        )

        nse_data = dhan_data.get(
            "NSE_EQ",
            {}
        )

        # Reverse security ID -> symbol
        reverse_map = {
            str(sec_id): symbol
            for symbol, sec_id
            in security_ids.items()
        }

        for sec_id, item in nse_data.items():

            symbol = reverse_map.get(
                str(sec_id)
            )

            if not symbol:
                continue

            if not isinstance(item, dict):
                continue

            ltp = item.get(
                "last_price"
            )

            if ltp is None:
                continue

            try:

                ltp = float(ltp)

                if ltp > 0:

                    prices[symbol] = {
                        "ltp": ltp,
                        "security_id": int(sec_id)
                    }

            except Exception:
                continue

        return jsonify({
            "status": "success",
            "prices": prices,
            "not_found": not_found,
            "requested": len(symbols),
            "returned": len(prices)
        })

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e),
            "not_found": not_found
        }), 500


# ============================================================
# TCS TEST
# ============================================================

@app.route("/api/tcs-test")
def tcs_test():

    try:

        # TCS NSE security ID
        body = {
            "NSE_EQ": [11536]
        }

        data = dhan_request(body)

        return jsonify({
            "status": "success",
            "data": data
        })

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# ============================================================
# DIAGNOSTIC
# ============================================================

@app.route("/api/diagnostic")
def diagnostic():

    return jsonify({
        "status": "success",
        "client_id_configured": bool(
            DHAN_CLIENT_ID
        ),
        "pin_configured": bool(
            DHAN_PIN
        ),
        "totp_configured": bool(
            DHAN_TOTP_SECRET
        ),
        "manual_token_configured": bool(
            DHAN_ACCESS_TOKEN
        ),
        "instruments_loaded": len(
            instrument_map
        ),
        "automatic_token_mode": bool(
            DHAN_CLIENT_ID
            and DHAN_PIN
            and DHAN_TOTP_SECRET
        )
    })


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv("PORT", "10000")
    )

    app.run(
        host="0.0.0.0",
        port=port
            )
