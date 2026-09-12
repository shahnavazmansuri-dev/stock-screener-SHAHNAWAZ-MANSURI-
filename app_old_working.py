import os
import csv
import io
import time
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

DHAN_TOKEN_URL = (
    "https://auth.dhan.co/app/generateAccessToken"
)

DHAN_MASTER_URL = (
    "https://images.dhan.co/api-data/api-scrip-master.csv"
)


# ============================================================
# RENDER ENVIRONMENT VARIABLES
# ============================================================

DHAN_CLIENT_ID = os.getenv(
    "DHAN_CLIENT_ID",
    ""
).strip()

DHAN_PIN = os.getenv(
    "DHAN_PIN",
    ""
).strip()

DHAN_TOTP_SECRET = os.getenv(
    "DHAN_TOTP_SECRET",
    ""
).strip()

# Optional old manual token
DHAN_ACCESS_TOKEN = os.getenv(
    "DHAN_ACCESS_TOKEN",
    ""
).strip()


# ============================================================
# GLOBAL VARIABLES
# ============================================================

instrument_map = {}

_token = None
_token_expiry = None

_token_lock = threading.Lock()

_quote_lock = threading.Lock()

_last_quote_time = 0.0


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

        reader = csv.DictReader(
            io.StringIO(response.text)
        )

        mapping = {}

        for row in reader:

            exchange = str(
                row.get(
                    "SEM_EXM_EXCH_ID",
                    ""
                )
            ).strip().upper()

            segment = str(
                row.get(
                    "SEM_SEGMENT",
                    ""
                )
            ).strip().upper()

            symbol = clean_symbol(
                row.get(
                    "SEM_TRADING_SYMBOL",
                    ""
                )
            )

            security_id = str(
                row.get(
                    "SEM_SMST_SECURITY_ID",
                    ""
                )
            ).strip()

            # NSE EQUITY ONLY
            if (
                exchange != "NSE"
                or segment != "E"
                or not symbol
                or not security_id
            ):
                continue

            try:

                mapping[symbol] = int(
                    security_id
                )

            except (
                TypeError,
                ValueError
            ):
                continue

        instrument_map = mapping

        print(
            "Loaded NSE instruments:",
            len(instrument_map)
        )

        return instrument_map

    except Exception as e:

        print(
            "Instrument master error:",
            str(e)
        )

        instrument_map = {}

        return instrument_map


# Load instruments at startup
load_instruments()


# ============================================================
# GENERATE AUTOMATIC DHAN ACCESS TOKEN
# ============================================================

def generate_access_token():

    if not DHAN_CLIENT_ID:

        raise RuntimeError(
            "DHAN_CLIENT_ID is missing"
        )

    if not DHAN_PIN:

        raise RuntimeError(
            "DHAN_PIN is missing"
        )

    if not DHAN_TOTP_SECRET:

        raise RuntimeError(
            "DHAN_TOTP_SECRET is missing"
        )

    # Generate current TOTP
    totp_code = pyotp.TOTP(
        DHAN_TOTP_SECRET
    ).now()

    params = {
        "dhanClientId": DHAN_CLIENT_ID,
        "pin": DHAN_PIN,
        "totp": totp_code
    }

    response = requests.post(
        DHAN_TOKEN_URL,
        params=params,
        timeout=20
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

        raise RuntimeError(
            "Dhan token generation failed: "
            + str(message)
        )

    access_token = data.get(
        "accessToken"
    )

    if not access_token:

        raise RuntimeError(
            "Dhan did not return accessToken"
        )

    # Safe local expiry
    expiry = (
        utc_now()
        + timedelta(
            hours=23,
            minutes=30
        )
    )

    expiry_text = data.get(
        "expiryTime"
    )

    if expiry_text:

        try:

            expiry = datetime.fromisoformat(
                str(expiry_text).replace(
                    "Z",
                    "+00:00"
                )
            )

            if expiry.tzinfo is None:

                expiry = expiry.replace(
                    tzinfo=timezone.utc
                )

        except (
            TypeError,
            ValueError
        ):

            pass

    print(
        "Dhan automatic token generated"
    )

    return access_token, expiry


# ============================================================
# GET VALID TOKEN
# ============================================================

def get_access_token(
    force_refresh=False
):

    global _token
    global _token_expiry

    with _token_lock:

        token_valid = (
            _token
            and _token_expiry
            and utc_now()
            <
            _token_expiry
            - timedelta(minutes=5)
        )

        if (
            token_valid
            and not force_refresh
        ):

            return _token

        # Automatic TOTP mode
        if (
            DHAN_CLIENT_ID
            and DHAN_PIN
            and DHAN_TOTP_SECRET
        ):

            (
                _token,
                _token_expiry
            ) = generate_access_token()

            return _token

        # Manual fallback
        if DHAN_ACCESS_TOKEN:

            return DHAN_ACCESS_TOKEN

        raise RuntimeError(
            "Dhan authentication is not configured. "
            "Set DHAN_CLIENT_ID, DHAN_PIN and "
            "DHAN_TOTP_SECRET in Render."
        )


# ============================================================
# DHAN LTP REQUEST
# ============================================================

def dhan_ltp_request(
    security_ids
):

    global _last_quote_time

    if not security_ids:

        return {}

    with _quote_lock:

        # Dhan rate-limit protection
        elapsed = (
            time.time()
            - _last_quote_time
        )

        if elapsed < 1.1:

            time.sleep(
                1.1 - elapsed
            )

        token = get_access_token()

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": token,
            "client-id": DHAN_CLIENT_ID
        }

        body = {
            "NSE_EQ": [
                int(x)
                for x in security_ids
            ]
        }

        response = requests.post(
            DHAN_LTP_URL,
            headers=headers,
            json=body,
            timeout=20
        )

        _last_quote_time = time.time()

        # Token expired
        if response.status_code in (
            401,
            403
        ):

            print(
                "Dhan token rejected. "
                "Refreshing..."
            )

            token = get_access_token(
                force_refresh=True
            )

            headers["access-token"] = token

            response = requests.post(
                DHAN_LTP_URL,
                headers=headers,
                json=body,
                timeout=20
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
# HOME
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "status": "OK",
        "message": (
            "Shahnawaz Mansuri "
            "Dhan Live Price Backend"
        ),
        "version": "dhan-auto-token-v2"
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "healthy",
        "instruments_loaded": len(
            instrument_map
        )
    })


# ============================================================
# DIAGNOSTIC
# ============================================================

@app.route("/api/diagnostic")
def diagnostic():

    automatic_mode = bool(
        DHAN_CLIENT_ID
        and DHAN_PIN
        and DHAN_TOTP_SECRET
    )

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
        "automatic_token_mode": automatic_mode,
        "instruments_loaded": len(
            instrument_map
        )
    })


# ============================================================
# TOKEN STATUS
# ============================================================

@app.route("/api/token-status")
def token_status():

    return jsonify({
        "status": "success",
        "automatic_token_mode": bool(
            DHAN_CLIENT_ID
            and DHAN_PIN
            and DHAN_TOTP_SECRET
        ),
        "token_available": bool(
            _token
            or DHAN_ACCESS_TOKEN
        ),
        "token_expiry": (
            _token_expiry.isoformat()
            if _token_expiry
            else None
        )
    })


# ============================================================
# PROFILE TEST
# ============================================================

@app.route("/api/profile-test")
def profile_test():

    try:

        token = get_access_token()

        headers = {
            "Accept": "application/json",
            "access-token": token
        }

        response = requests.get(
            DHAN_PROFILE_URL,
            headers=headers,
            timeout=20
        )

        if response.status_code in (
            401,
            403
        ):

            token = get_access_token(
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

        except ValueError:

            data = {
                "raw_response":
                    response.text[:1000]
            }

        if response.status_code >= 400:

            return jsonify({
                "status": "error",
                "http_status":
                    response.status_code,
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
# LIVE LTP API
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
            "message": (
                "Please provide symbols. "
                "Example: "
                "/api/ltp?symbols=RELIANCE,TCS,INFY"
            )
        }), 400

    requested_symbols = []

    for raw_symbol in symbols_text.split(","):

        symbol = clean_symbol(
            raw_symbol
        )

        if (
            symbol
            and symbol not in requested_symbols
        ):

            requested_symbols.append(
                symbol
            )

    if not requested_symbols:

        return jsonify({
            "status": "error",
            "message": "No valid symbols supplied"
        }), 400

    # Reload if startup failed
    if not instrument_map:

        load_instruments()

    security_ids = {}

    not_found = []

    for symbol in requested_symbols:

        security_id = instrument_map.get(
            symbol
        )

        if security_id is None:

            not_found.append(
                symbol
            )

        else:

            security_ids[symbol] = int(
                security_id
            )

    if not security_ids:

        return jsonify({
            "status": "error",
            "message": (
                "No valid NSE symbols found"
            ),
            "not_found": not_found
        }), 404

    try:

        prices = {}

        symbols = list(
            security_ids.keys()
        )

        # Dhan maximum:
        # 1000 instruments/request
        batch_size = 1000

        for start in range(
            0,
            len(symbols),
            batch_size
        ):

            batch_symbols = symbols[
                start:start + batch_size
            ]

            batch_ids = [
                security_ids[symbol]
                for symbol in batch_symbols
            ]

            data = dhan_ltp_request(
                batch_ids
            )

            market_data = data.get(
                "data",
                {}
            )

            nse_data = market_data.get(
                "NSE_EQ",
                {}
            )

            reverse_map = {
                str(
                    security_ids[symbol]
                ): symbol
                for symbol in batch_symbols
            }

            for security_id, quote in (
                nse_data.items()
            ):

                symbol = reverse_map.get(
                    str(security_id)
                )

                if not symbol:
                    continue

                if not isinstance(
                    quote,
                    dict
                ):
                    continue

                last_price = quote.get(
                    "last_price"
                )

                try:

                    last_price = float(
                        last_price
                    )

                except (
                    TypeError,
                    ValueError
                ):

                    continue

                if last_price <= 0:
                    continue

                prices[symbol] = {
                    "symbol": symbol,
                    "security_id": int(
                        security_id
                    ),
                    "ltp": last_price
                }

        return jsonify({
            "status": "success",
            "prices": prices,
            "not_found": not_found,
            "requested": len(
                requested_symbols
            ),
            "returned": len(prices),
            "source": "Dhan Market Quote"
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

        data = dhan_ltp_request(
            [11536]
        )

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
# START SERVER
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
