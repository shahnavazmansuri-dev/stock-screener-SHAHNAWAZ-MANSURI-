import csv
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

JSON_FILE = "stocks_data.json"
MAX_LOOKBACK_DAYS = 7

# NSE UDiFF CM Bhavcopy archive URL pattern.
BASE_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (GitHub Actions; stock screener)",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}


def download_bhavcopy():
    today = datetime.now(timezone.utc).date()

    for offset in range(MAX_LOOKBACK_DAYS + 1):
        d = today - timedelta(days=offset)
        date_str = d.strftime("%Y%m%d")
        url = BASE_URL.format(date=date_str)

        print(f"Trying NSE Bhavcopy: {date_str}")

        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=30) as response:
                content = response.read()

            if len(content) < 1000:
                print("File too small; trying previous date.")
                continue

            print(f"Downloaded: {len(content):,} bytes")
            return content, date_str

        except Exception as e:
            print(f"Not available: {e}")

    raise RuntimeError("NSE Bhavcopy could not be downloaded for the last 7 days.")


def read_bhavcopy(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        csv_names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError("No CSV file found inside NSE Bhavcopy ZIP.")

        with z.open(csv_names[0]) as f:
            text = io.TextIOWrapper(f, encoding="utf-8-sig")
            rows = list(csv.DictReader(text))

    # Do NOT filter only SctySrs == EQ.
    # Some NSE-listed stocks in the screener can be in BE/other series.
    return [
        row for row in rows
        if (row.get("TckrSymb") or "").strip()
    ]


def number(value):
    try:
        value = str(value).strip().replace(",", "")
        if value in ("", "-", "NA", "N/A"):
            return None
        return float(value)
    except Exception:
        return None


def main():
    with open(JSON_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    stocks = data.get("stocks", []) if isinstance(data, dict) else data

    print("==============================")
    print("      NSE BHAVCOPY UPDATE")
    print("==============================")
    print("Screener stocks:", len(stocks))

    zip_bytes, bhav_date = download_bhavcopy()
    rows = read_bhavcopy(zip_bytes)

    bhav = {}
    for row in rows:
        symbol = row["TckrSymb"].strip().upper()

        last_price = number(row.get("LastPric"))
        close_price = number(row.get("ClsPric"))
        previous_close = number(row.get("PrvsClsgPric"))

        # Prefer LastPric; use ClsPric when LastPric is unavailable.
        market_price = last_price if last_price is not None else close_price
        if market_price is None:
            continue

        pct = None
        if previous_close not in (None, 0):
            pct = round(
                (market_price - previous_close) / previous_close * 100,
                2
            )

        bhav[symbol] = {
            "price": market_price,
            "close": close_price if close_price is not None else market_price,
            "percent_change": pct,
        }

    success = 0
    missing = []

    for stock in stocks:
        symbol = (stock.get("symbol") or stock.get("s") or "").strip().upper()
        if not symbol:
            continue

        item = bhav.get(symbol)
        if not item:
            missing.append(symbol)
            continue

        price = item["price"]

        # Keep all common price fields synchronized so the existing
        # frontend continues to work without changes.
        stock["price"] = price
        stock["ltp"] = price
        stock["currentPrice"] = price
        stock["p"] = price
        stock["live_price"] = price
        stock["close"] = item["close"]

        if item["percent_change"] is not None:
            stock["percent_change"] = item["percent_change"]

        success += 1

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print()
    print("==============================")
    print("        UPDATE COMPLETE")
    print("==============================")
    print("Bhavcopy date:", bhav_date)
    print("NSE rows:", len(rows))
    print("Updated stocks:", success)
    print("Missing stocks:", len(missing))

    if missing:
        print("First missing symbols:", ", ".join(missing[:30]))

    print("==============================")


if __name__ == "__main__":
    main()
