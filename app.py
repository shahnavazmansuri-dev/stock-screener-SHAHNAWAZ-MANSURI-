import os
import csv
import io
import requests

from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# =========================================================
# Dhan Configuration
# =========================================================

DHAN_API_BASE = "https://api.dhan.co/v2"
DHAN_LTP_URL = f"{DHAN_API_BASE}/marketfeed/ltp"
DHAN_PROFILE_URL = f"{DHAN_API_BASE}/profile"
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")

instrument_map = None


# =========================================================
# Load Dhan Instrument Master
# =========================================================

def load_instruments():
    global instrument_map

    if instrument_map is not None:
        return instrument_map

    response = requests.get(
        DHAN_MASTER_URL,
        timeout=30
    )

    response.raise_for_status()

    text = response.content.decode(
        "utf-8-sig",
        errors="replace"
    )

    reader = csv.DictReader(
        io.StringIO(text)
    )

    mapping = {}

    for row in reader:

        exchange = (
            row.get("SEM_EXM_EXCH_ID", "")
            or ""
        ).strip().upper()

        segment = (
            row.get("SEM_SEGMENT", "")
            or ""
        ).strip().upper()

        symbol = (
            row.get("SEM_TRADING_SYMBOL", "")
            or ""
        ).strip().upper()

        security_id = (
            row.get("SEM_SMST_SECURITY_ID", "")
            or ""
        ).strip()

        if (
            exchange == "NSE"
            and segment == "E"
            and symbol
            and security_id
        ):
            clean_symbol = symbol

            if clean_symbol.endswith("-EQ"):
                clean_symbol = clean_symbol[:-3]

            try:
                mapping[clean_symbol] = int(security_id)
            except ValueError:
                pass

    instrument_map = mapping

    return instrument_map


# =========================================================
# Basic Routes
# =========================================================

@app.route("/")
def home():
    return jsonify({
        "status": "OK",
        "message": "Shahnawaz Mansuri Dhan Live Price Backend",
        "version": "diagnostic-v1"
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy"
    })


# =========================================================
# Dhan Profile Test
# =========================================================

@app.route("/api/profile-test")
def profile_test():

    if not DHAN_ACCESS_TOKEN:
        return jsonify({
            "status": "error",
            "message": "DHAN_ACCESS_TOKEN is missing in Render Environment Variables"
        }), 500

    headers = {
        "Accept": "application/json",
        "access-token": DHAN_ACCESS_TOKEN
    }

    try:

        response = requests.get(
            DHAN_PROFILE_URL,
            headers=headers,
            timeout=15
        )

        try:
            data = response.json()
        except Exception:
            data = {
                "raw_response": response.text[:1000]
            }

        # Never expose token
        safe_data = {}

        if isinstance(data, dict):

            allowed_fields = [
                "dhanClientId",
                "tokenValidity",
                "activeSegment",
                "ddpi",
                "mtf",
                "dataPlan",
                "dataValidity"
            ]

            for field in allowed_fields:
                if field in data:
                    safe_data[field] = data[field]

        return jsonify({
            "status": "profile_test",
            "http_status": response.status_code,
            "profile": safe_data
        })

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": "Profile request failed",
            "details": str(e)
        }), 500


# =========================================================
# HARD-CODED TCS TEST
# Dhan official example:
# NSE_EQ -> 11536
# =========================================================

@app.route("/api/tcs-test")
def tcs_test():

    if not DHAN_CLIENT_ID:
        return jsonify({
            "status": "error",
            "message": "DHAN_CLIENT_ID is missing in Render Environment Variables"
        }), 500

    if not DHAN_ACCESS_TOKEN:
        return jsonify({
            "status": "error",
            "message": "DHAN_ACCESS_TOKEN is missing in Render Environment Variables"
        }), 500

    # IMPORTANT:
    # Exactly as Dhan official documentation example
    dhan_body = {
        "NSE_EQ": [11536]
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id": DHAN_CLIENT_ID
    }

    try:

        response = requests.post(
            DHAN_LTP_URL,
            headers=headers,
            json=dhan_body,
            timeout=15
        )

        try:
            data = response.json()
        except Exception:
            data = {
                "raw_response": response.text[:2000]
            }

        return jsonify({
            "status": "tcs_test",
            "request_sent": dhan_body,
            "http_status": response.status_code,
            "dhan_response": data
        })

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": "TCS request failed",
            "details": str(e)
        }), 500


# =========================================================
# FULL DIAGNOSTIC
# Profile + TCS LTP
# =========================================================

@app.route("/api/diagnostic")
def diagnostic():

    result = {
        "backend": "OK",
        "profile": {},
        "tcs_ltp": {}
    }

    # -----------------------------------------------------
    # 1. PROFILE
    # -----------------------------------------------------

    if not DHAN_ACCESS_TOKEN:

        result["profile"] = {
            "error": "DHAN_ACCESS_TOKEN missing"
        }

    else:

        profile_headers = {
            "Accept": "application/json",
            "access-token": DHAN_ACCESS_TOKEN
        }

        try:

            profile_response = requests.get(
                DHAN_PROFILE_URL,
                headers=profile_headers,
                timeout=15
            )

            try:
                profile_data = profile_response.json()
            except Exception:
                profile_data = {
                    "raw_response":
                        profile_response.text[:1000]
                }

            safe_profile = {}

            if isinstance(profile_data, dict):

                for field in [
                    "dhanClientId",
                    "tokenValidity",
                    "activeSegment",
                    "ddpi",
                    "mtf",
                    "dataPlan",
                    "dataValidity"
                ]:

                    if field in profile_data:
                        safe_profile[field] = profile_data[field]

            result["profile"] = {
                "http_status":
                    profile_response.status_code,
                "data":
                    safe_profile
            }

        except Exception as e:

            result["profile"] = {
                "error": str(e)
            }

    # -----------------------------------------------------
    # 2. TCS LTP
    # -----------------------------------------------------

    if not DHAN_CLIENT_ID:

        result["tcs_ltp"] = {
            "error": "DHAN_CLIENT_ID missing"
        }

        return jsonify(result)

    if not DHAN_ACCESS_TOKEN:

        result["tcs_ltp"] = {
            "error": "DHAN_ACCESS_TOKEN missing"
        }

        return jsonify(result)

    tcs_body = {
        "NSE_EQ": [11536]
    }

    tcs_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id": DHAN_CLIENT_ID
    }

    try:

        tcs_response = requests.post(
            DHAN_LTP_URL,
            headers=tcs_headers,
            json=tcs_body,
            timeout=15
        )

        try:
            tcs_data = tcs_response.json()
        except Exception:
            tcs_data = {
                "raw_response":
                    tcs_response.text[:2000]
            }

        result["tcs_ltp"] = {
            "request": tcs_body,
            "http_status":
                tcs_response.status_code,
            "response":
                tcs_data
        }

    except Exception as e:

        result["tcs_ltp"] = {
            "error": str(e)
        }

    return jsonify(result)


# =========================================================
# General LTP API
# =========================================================

@app.route("/api/ltp")
def get_ltp():

    if not DHAN_CLIENT_ID:
        return jsonify({
            "error": "DHAN_CLIENT_ID is not configured"
        }), 500

    if not DHAN_ACCESS_TOKEN:
        return jsonify({
            "error": "DHAN_ACCESS_TOKEN is not configured"
        }), 500

    symbols_text = request.args.get(
        "symbols",
        ""
    )

    if not symbols_text:

        return jsonify({
            "error": "Please provide symbols",
            "example":
                "/api/ltp?symbols=RELIANCE,TCS,INFY"
        }), 400

    symbols = [
        s.strip().upper()
        for s in symbols_text.split(",")
        if s.strip()
    ]

    try:

        instruments = load_instruments()

        security_ids = {}
        not_found = []

        for symbol in symbols:

            clean_symbol = symbol

            if clean_symbol.endswith("-EQ"):
                clean_symbol = clean_symbol[:-3]

            security_id = instruments.get(
                clean_symbol
            )

            if security_id is not None:

                security_ids[clean_symbol] = int(
                    security_id
                )

            else:

                not_found.append(
                    clean_symbol
                )

        if not security_ids:

            return jsonify({
                "error": "No valid NSE symbols found",
                "not_found": not_found
            }), 404

        dhan_body = {
            "NSE_EQ":
                list(security_ids.values())
        }

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": DHAN_ACCESS_TOKEN,
            "client-id": DHAN_CLIENT_ID
        }

        response = requests.post(
            DHAN_LTP_URL,
            headers=headers,
            json=dhan_body,
            timeout=15
        )

        try:
            dhan_data = response.json()
        except Exception:
            dhan_data = {
                "raw_response":
                    response.text[:2000]
            }

        if response.status_code != 200:

            return jsonify({
                "error": "Dhan API error",
                "status_code":
                    response.status_code,
                "request_sent":
                    dhan_body,
                "details":
                    dhan_data
            }), response.status_code

        result = {}

        nse_data = (
            dhan_data
            .get("data", {})
            .get("NSE_EQ", {})
        )

        reverse_map = {
            str(security_id): symbol
            for symbol, security_id
            in security_ids.items()
        }

        for security_id, data in nse_data.items():

            symbol = reverse_map.get(
                str(security_id)
            )

            if symbol:

                result[symbol] = {
                    "symbol": symbol,
                    "security_id":
                        str(security_id),
                    "ltp":
                        data.get("last_price")
                }

        return jsonify({
            "status": "success",
            "count": len(result),
            "prices": result,
            "not_found": not_found
        })

    except Exception as e:

        return jsonify({
            "error": "Backend error",
            "details": str(e)
        }), 500


# =========================================================
# Start Server
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
