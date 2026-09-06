#!/usr/bin/env python3
import json
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
STOCKS_FILE = ROOT / "stocks_data.json"
ZONES_FILE = ROOT / "zones_data.json"
OUTPUT_FILE = ROOT / "confluence_scanner_data.json"

BASE_URL = "https://huggingface.co/datasets/vishnun0027/indian-market-historical-ohlcv/resolve/main/stocks/"
REQUEST_TIMEOUT = 25
DEMAND_MAX_DISTANCE = 15.0
DEMAND_MIN_SCORE = 7


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[WARN] Could not load {path.name}: {exc}")
        return default


def get_symbol(stock):
    return str(stock.get("symbol") or stock.get("ticker") or stock.get("s") or "").strip().upper()


def download_history(symbol):
    url = BASE_URL + f"{symbol}.parquet"
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        return None

    tmp = ROOT / f".confluence_{symbol}.parquet"
    tmp.write_bytes(response.content)
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
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        return None

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    df = df.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    return df.sort_values("date").reset_index(drop=True)


def calculate_indicators(df):
    close = df["close"]
    volume = df["volume"]

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(100).astype(float)

    macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - macd_signal

    volume_avg20 = volume.rolling(20).mean()

    return ema9, ema20, ema200, rsi, macd_line, macd_signal, macd_hist, volume_avg20


def find_nearby_demand(zones_data, symbol, reference_price):
    stock_zone = zones_data.get(symbol) or zones_data.get(symbol.upper())
    if not stock_zone:
        return None

    best = None
    timeframes = stock_zone.get("timeframes", {})

    for timeframe, tf_data in timeframes.items():
        for zone in tf_data.get("demand", []) or []:
            if not zone.get("fresh", False):
                continue

            score = float(zone.get("score", 0) or 0)
            if score < DEMAND_MIN_SCORE:
                continue

            distance = zone.get("distance_percent")
            if distance is None:
                high = float(zone.get("high", 0) or 0)
                low = float(zone.get("low", 0) or 0)
                if high <= 0 or low <= 0 or reference_price <= 0:
                    continue
                if reference_price < low:
                    distance = (low - reference_price) / reference_price * 100
                elif reference_price > high:
                    distance = (reference_price - high) / reference_price * 100
                else:
                    distance = 0.0
            distance = float(distance)

            if distance > DEMAND_MAX_DISTANCE:
                continue

            candidate = {
                "timeframe": timeframe,
                "pattern": zone.get("pattern", "-"),
                "high": float(zone.get("high", 0) or 0),
                "low": float(zone.get("low", 0) or 0),
                "score": int(round(score)),
                "distance_percent": round(distance, 2),
            }

            if best is None or (candidate["distance_percent"], -candidate["score"]) < (best["distance_percent"], -best["score"]):
                best = candidate

    return best


def scan_stock(stock, zones_data):
    symbol = get_symbol(stock)
    if not symbol:
        return None

    try:
        df = download_history(symbol)
        if df is None or len(df) < 210:
            return None

        ema9, ema20, ema200, rsi, macd_line, macd_signal, macd_hist, volume_avg20 = calculate_indicators(df)

        last = len(df) - 1
        prev = last - 1

        price = float(df["close"].iloc[last])
        e9 = float(ema9.iloc[last])
        e20 = float(ema20.iloc[last])
        e200 = float(ema200.iloc[last])
        rsi14 = float(rsi.iloc[last])
        macd = float(macd_line.iloc[last])
        macd_sig = float(macd_signal.iloc[last])
        hist = float(macd_hist.iloc[last])
        vol = float(df["volume"].iloc[last])
        vol_avg = float(volume_avg20.iloc[last]) if pd.notna(volume_avg20.iloc[last]) else 0.0

        ema_cross = float(ema9.iloc[prev]) <= float(ema20.iloc[prev]) and e9 > e20
        ema_condition = e9 > e20
        trend_condition = price > e200
        rsi_condition = rsi14 > 55
        macd_condition = macd > macd_sig and hist > 0
        volume_condition = vol > vol_avg if vol_avg > 0 else False
        demand = find_nearby_demand(zones_data, symbol, price)
        demand_condition = demand is not None

        all_conditions = all([
            ema_condition,
            trend_condition,
            rsi_condition,
            macd_condition,
            volume_condition,
            demand_condition,
        ])

        if not all_conditions:
            return None

        return {
            "symbol": symbol,
            "stock": stock.get("stock") or stock.get("name") or symbol,
            "name": stock.get("name") or stock.get("stock") or symbol,
            "category": stock.get("category") or stock.get("cap") or "Unknown",
            "price": round(price, 2),
            "ema9": round(e9, 2),
            "ema20": round(e20, 2),
            "ema200": round(e200, 2),
            "rsi14": round(rsi14, 2),
            "macd": round(macd, 4),
            "macd_signal": round(macd_sig, 4),
            "macd_hist": round(hist, 4),
            "volume": vol,
            "volume_avg20": vol_avg,
            "volume_ratio": round(vol / vol_avg, 2) if vol_avg > 0 else 0,
            "ema_cross": ema_cross,
            "demand": demand,
            "signal": "BUY CONFLUENCE" if ema_cross else "CONFLUENCE",
            "date": df["date"].iloc[last].strftime("%Y-%m-%d"),
            "strategy": "Confluence: EMA + RSI + MACD + Volume + Demand",
        }
    except Exception as exc:
        print(f"[WARN] {symbol}: {exc}")
        return None


def main():
    stocks_data = load_json(STOCKS_FILE, [])
    stocks = stocks_data if isinstance(stocks_data, list) else stocks_data.get("stocks", [])

    zones_root = load_json(ZONES_FILE, {})
    zones_data = zones_root.get("stocks", {}) if isinstance(zones_root, dict) else {}

    print("==============================")
    print("   CONFLUENCE SCANNER UPDATE")
    print("==============================")
    print("Screener stocks:", len(stocks))
    print("Rules: 9 EMA > 20 EMA + Price > 200 EMA + RSI > 55 + MACD bullish + Volume > 20D average + Fresh Demand Zone")
    print(f"Demand zone: score >= {DEMAND_MIN_SCORE}, distance <= {DEMAND_MAX_DISTANCE}%, fresh only")

    results = []

    for i, stock in enumerate(stocks, 1):
        result = scan_stock(stock, zones_data)
        if result:
            results.append(result)
        if i % 25 == 0:
            print(f"Processed {i}/{len(stocks)}")
        time.sleep(0.05)

    results.sort(key=lambda x: (
        0 if x["signal"] == "BUY CONFLUENCE" else 1,
        x["demand"]["distance_percent"],
        -x["rsi14"],
    ))

    output = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "strategy": "Confluence: EMA + RSI + MACD + Volume + Demand",
        "rules": [
            "9 EMA above 20 EMA",
            "Price above 200 EMA",
            "RSI(14) above 55",
            "MACD line above signal and histogram positive",
            "Volume above 20-day average",
            "Fresh Demand Zone score >= 7 within 15% of reference price",
            "BUY CONFLUENCE when 9 EMA crosses above 20 EMA while all conditions pass",
        ],
        "stocks": results,
        "count": len(results),
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("==============================")
    print("       UPDATE COMPLETE")
    print("==============================")
    print("Confluence stocks:", len(results))
    print("Saved:", OUTPUT_FILE.name)
    print("==============================")


if __name__ == "__main__":
    main()
