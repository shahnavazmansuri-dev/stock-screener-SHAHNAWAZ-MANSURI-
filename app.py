import os
import csv
import io
from datetime import date, timedelta

import requests

from flask import Flask, jsonify, request
from flask_cors import CORS


# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__)
CORS(app)


# =========================================================
# DHAN CONFIGURATION
# =========================================================

DHAN_API_BASE = "https://api.dhan.co/v2"

DHAN_LTP_URL = f"{DHAN_API_BASE}/marketfeed/ltp"
DHAN_PROFILE_URL = f"{DHAN_API_BASE}/profile"

DHAN_HISTORICAL_URL = f"{DHAN_API_BASE}/charts/historical"

DHAN_MASTER_URL = (
    "https://images.dhan.co/api-data/api-scrip-master.csv"
)

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")


# =========================================================
# GLOBAL INSTRUMENT CACHE
# =========================================================

instrument_map = None


# =========================================================
# LOAD DHAN INSTRUMENT MASTER
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

        # Only NSE Equity
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
                mapping[clean_symbol] = int(
                    security_id
                )

            except ValueError:
                pass

    instrument_map = mapping

    return instrument_map


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():

    return jsonify({
        "status": "OK",
        "message":
            "Shahnawaz Mansuri Dhan Live Price + NSE Data Backend",
        "version": "all-nse-v1"
    })


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "healthy"
    })


# =========================================================
# PROFILE TEST
# =========================================================

@app.route("/api/profile-test")
def profile_test():

    if not DHAN_ACCESS_TOKEN:

        return jsonify({
            "status": "error",
            "message":
                "DHAN_ACCESS_TOKEN is missing in Render Environment Variables"
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
                "raw_response":
                    response.text[:1000]
            }

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
            "http_status":
                response.status_code,
            "profile":
                safe_data
        })

    except Exception as e:

        return jsonify({
            "status": "error",
            "message":
                "Profile request failed",
            "details":
                str(e)
        }), 500


# =========================================================
# TCS TEST
# =========================================================

@app.route("/api/tcs-test")
def tcs_test():

    if not DHAN_CLIENT_ID:

        return jsonify({
            "status": "error",
            "message":
                "DHAN_CLIENT_ID is missing"
        }), 500

    if not DHAN_ACCESS_TOKEN:

        return jsonify({
            "status": "error",
            "message":
                "DHAN_ACCESS_TOKEN is missing"
        }), 500

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
                "raw_response":
                    response.text[:2000]
            }

        return jsonify({
            "status": "tcs_test",
            "request_sent":
                dhan_body,
            "http_status":
                response.status_code,
            "dhan_response":
                data
        })

    except Exception as e:

        return jsonify({
            "status": "error",
            "message":
                "TCS request failed",
            "details":
                str(e)
        }), 500


# =========================================================
# DIAGNOSTIC
# =========================================================

@app.route("/api/diagnostic")
def diagnostic():

    result = {
        "backend": "OK",
        "profile": {},
        "tcs_ltp": {}
    }

    # -----------------------------------------------------
    # PROFILE
    # -----------------------------------------------------

    if not DHAN_ACCESS_TOKEN:

        result["profile"] = {
            "error":
                "DHAN_ACCESS_TOKEN missing"
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
                profile_data = (
                    profile_response.json()
                )

            except Exception:

                profile_data = {
                    "raw_response":
                        profile_response.text[:1000]
                }

            safe_profile = {}

            if isinstance(
                profile_data,
                dict
            ):

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
                        safe_profile[field] = (
                            profile_data[field]
                        )

            result["profile"] = {
                "http_status":
                    profile_response.status_code,
                "data":
                    safe_profile
            }

        except Exception as e:

            result["profile"] = {
                "error":
                    str(e)
            }

    # -----------------------------------------------------
    # TCS LTP
    # -----------------------------------------------------

    if not DHAN_CLIENT_ID:

        result["tcs_ltp"] = {
            "error":
                "DHAN_CLIENT_ID missing"
        }

        return jsonify(result)

    if not DHAN_ACCESS_TOKEN:

        result["tcs_ltp"] = {
            "error":
                "DHAN_ACCESS_TOKEN missing"
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
            tcs_data = (
                tcs_response.json()
            )

        except Exception:

            tcs_data = {
                "raw_response":
                    tcs_response.text[:2000]
            }

        result["tcs_ltp"] = {
            "request":
                tcs_body,
            "http_status":
                tcs_response.status_code,
            "response":
                tcs_data
        }

    except Exception as e:

        result["tcs_ltp"] = {
            "error":
                str(e)
        }

    return jsonify(result)


# =========================================================
# GENERAL LTP API
# =========================================================

@app.route("/api/ltp")
def get_ltp():

    if not DHAN_CLIENT_ID:

        return jsonify({
            "error":
                "DHAN_CLIENT_ID is not configured"
        }), 500

    if not DHAN_ACCESS_TOKEN:

        return jsonify({
            "error":
                "DHAN_ACCESS_TOKEN is not configured"
        }), 500

    symbols_text = request.args.get(
        "symbols",
        ""
    )

    if not symbols_text:

        return jsonify({
            "error":
                "Please provide symbols",
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
                clean_symbol = (
                    clean_symbol[:-3]
                )

            security_id = instruments.get(
                clean_symbol
            )

            if security_id is not None:

                security_ids[
                    clean_symbol
                ] = int(security_id)

            else:

                not_found.append(
                    clean_symbol
                )

        if not security_ids:

            return jsonify({
                "error":
                    "No valid NSE symbols found",
                "not_found":
                    not_found
            }), 404

        dhan_body = {
            "NSE_EQ":
                list(
                    security_ids.values()
                )
        }

        headers = {
            "Accept":
                "application/json",
            "Content-Type":
                "application/json",
           
