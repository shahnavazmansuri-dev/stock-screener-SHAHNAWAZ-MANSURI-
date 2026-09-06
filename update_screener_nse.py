import csv
import json
from pathlib import Path

JSON_FILE = "stocks_data.json"


def find_bhavcopy_csv():
    files = sorted(
        Path(".").glob("BhavCopy_NSE_CM_0_0_0_*_F_0000.csv"),
        reverse=True,
    )

    if not files:
        raise RuntimeError(
            "Repo root me NSE Bhavcopy CSV nahi mila. "
            "Expected: BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv"
        )

    print("Using Bhavcopy file:", files[0].name)
    return files[0]


def read_bhavcopy(csv_path):
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

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

    if isinstance(data, dict):
        stocks = data.get("stocks", [])
    else:
        stocks = data

    print("==============================")
    print(" NSE BHAVCOPY CSV UPDATE")
    print("==============================")
    print("Screener stocks:", len(stocks))

    csv_path = find_bhavcopy_csv()
    rows = read_bhavcopy(csv_path)

    bhav = {}

    for row in rows:

        symbol = (row.get("TckrSymb") or "").strip().upper()

        if not symbol:
            continue

        last_price = number(row.get("LastPric"))
        close_price = number(row.get("ClsPric"))
        previous_close = number(row.get("PrvsClsgPric"))

        # Prefer LastPric; if unavailable use ClsPric.
        market_price = (
            last_price
            if last_price is not None
            else close_price
        )

        if market_price is None:
            continue

        pct = None

        if previous_close not in (None, 0):
            pct = round(
                (market_price - previous_close)
                / previous_close * 100,
                2,
            )

        bhav[symbol] = {
            "price": market_price,
            "close": (
                close_price
                if close_price is not None
                else market_price
            ),
            "percent_change": pct,
        }

    success = 0
    missing = []

    for stock in stocks:

        symbol = (
            stock.get("symbol")
            or stock.get("s")
            or ""
        ).strip().upper()

        if not symbol:
            continue

        item = bhav.get(symbol)

        if not item:
            missing.append(symbol)
            continue

        price = item["price"]

        # Update all commonly used price fields.
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
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )

    print()
    print("==============================")
    print(" UPDATE COMPLETE")
    print("==============================")
    print("Bhavcopy file:", csv_path.name)
    print("NSE rows:", len(rows))
    print("Updated stocks:", success)
    print("Missing stocks:", len(missing))

    if missing:
        print(
            "First missing symbols:",
            ", ".join(missing[:30])
        )

    print("==============================")


if __name__ == "__main__":
    main()
