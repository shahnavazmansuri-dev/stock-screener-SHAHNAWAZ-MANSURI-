import os
import csv
import io
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DHAN_API_URL = "https://api.dhan.co/v2/marketfeed/ltp"
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")

instrument_map = None


def load_instruments():
    global instrument_map

    if instrument_map is not None:
        return instrument_map

    response = requests.get(DHAN_MASTER_URL, timeout=30)
    response.raise_for_status()

    text = response.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    mapping = {}

    for row in reader:
        exchange = row.get("SEM_EXM_EXCH_ID", "")
        segment = row.get("SEM_SEGMENT", "")
        symbol = row.get("SEM_TRADING_SYMBOL", "")
        security_id = row.get("SEM_SMST_SECURITY_ID", "")

        if exchange == "NSE" and segment == "E" and symbol and security_id:
            mapping[symbol.upper()] = security_id

    instrument_map = mapping
    return instrument_map


@app.route("/")
def home():
    return jsonify({
        "status": "OK",
        "message": "Shahnawaz Mansuri Dhan Live Price Backend"
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy"
    })


@app.route("/api/ltp")
def get_ltp():
    if not DHAN_CLIENT_ID or not DHAN_ACCESS_TOKEN:
        return jsonify({
            "error": "Dhan credentials are not configured on the server"
        }), 500

    symbols_text = request.args.get("symbols", "")

    if not symbols_text:
        return jsonify({
            "error": "Please provide symbols",
            "example": "/api/ltp?symbols=RELIANCE,TCS,INFY"
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
            security_id = instruments.get(symbol)

            if security_id:
                security_ids[symbol] = security_id
            else:
                not_found.append(symbol)

        if not security_ids:
            return jsonify({
                "error": "No valid NSE symbols found",
                "not_found": not_found
            }), 404

        # Dhan allows up to 1000 instruments per Market Quote request.
        dhan_body = {
            "NSE_EQ": list(security_ids.values())
        }

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": DHAN_ACCESS_TOKEN,
            "client-id": DHAN_CLIENT_ID
        }

        response = requests.post(
            DHAN_API_URL,
            headers=headers,
            json=dhan_body,
            timeout=15
        )

        if response.status_code != 200:
            return jsonify({
                "error": "Dhan API error",
                "status_code": response.status_code,
                "details": response.text[:1000]
            }), response.status_code

        dhan_data = response.json()

        result = {}

        nse_data = dhan_data.get("data", {}).get("NSE_EQ", {})

        reverse_map = {
            str(security_id): symbol
            for symbol, security_id in security_ids.items()
        }

        for security_id, data in nse_data.items():
            symbol = reverse_map.get(str(security_id))

            if symbol:
                result[symbol] = {
                    "symbol": symbol,
                    "security_id": str(security_id),
                    "ltp": data.get("last_price")
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
