#!/usr/bin/env python3
import json
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
STOCKS_FILE = ROOT / "stocks_data.json"
OUTPUT_FILE = ROOT / "ema_scanner_data.json"

BASE_URL = "https://huggingface.co/datasets/vishnun0027/indian-market-historical-ohlcv/resolve/main/stocks/"
REQUEST_TIMEOUT = 20

def load_stocks():
    with open(STOCKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []

def get_symbol(stock):
    return str(stock.get("symbol") or stock.get("ticker") or stock.get("s") or "").strip().upper()

def download_history(symbol):
    url = BASE_URL + f"{symbol}.parquet"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        return None
    tmp = ROOT / f".ema_{symbol}.parquet"
    tmp.write_bytes(r.content)
    try:
        df = pd.read_parquet(tmp)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if df.empty:
        return None
    df.columns = [str(c).lower() for c in df.columns]
    if "date" not in df.columns or "close" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)

def scan_stock(stock):
    symbol = get_symbol(stock)
    if not symbol:
        return None
    try:
        df = download_history(symbol)
        if df is None or len(df) < 210:
            return None

        close = df["close"]
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean()

        last = len(df) - 1
        prev = last - 1
        price = float(close.iloc[last])
        e9 = float(ema9.iloc[last])
        e20 = float(ema20.iloc[last])
        e200 = float(ema200.iloc[last])

        crossed_up = float(ema9.iloc[prev]) <= float(ema20.iloc[prev]) and e9 > e20
        bullish = e9 > e20 and price > e200
        if not bullish:
            return None

        return {
            "symbol": symbol,
            "stock": stock.get("stock") or stock.get("name") or symbol,
            "name": stock.get("name") or stock.get("stock") or symbol,
            "category": stock.get("category") or stock.get("cap") or "Unknown",
            "price": price,
            "ema9": round(e9, 2),
            "ema20": round(e20, 2),
            "ema200": round(e200, 2),
            "distance200": round((price - e200) / e200 * 100, 2) if e200 else 0,
            "signal": "BUY CROSS" if crossed_up else "BULLISH",
            "crossed_up": crossed_up,
            "date": df["date"].iloc[last].strftime("%Y-%m-%d"),
            "strategy": "9 EMA + 20 EMA + 200 EMA"
        }
    except Exception as exc:
        print(f"[WARN] {symbol}: {exc}")
        return None

def main():
    stocks = load_stocks()
    results = []
    print(f"Scanning {len(stocks)} stocks...")

    for i, stock in enumerate(stocks, 1):
        result = scan_stock(stock)
        if result:
            results.append(result)
        if i % 25 == 0:
            print(f"Processed {i}/{len(stocks)}")
        time.sleep(0.05)

    results.sort(key=lambda x: (0 if x["signal"] == "BUY CROSS" else 1, -x["distance200"]))

    output = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "strategy": "9 EMA + 20 EMA + 200 EMA",
        "rules": [
            "9 EMA above 20 EMA",
            "Price above 200 EMA",
            "BUY CROSS when 9 EMA crosses above 20 EMA"
        ],
        "stocks": results,
        "count": len(results)
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Done. {len(results)} bullish stocks saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
