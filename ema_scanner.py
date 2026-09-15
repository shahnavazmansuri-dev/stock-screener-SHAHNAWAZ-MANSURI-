#!/usr/bin/env python3

import json
import time
from pathlib import Path

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parent
STOCKS_FILE = ROOT / "stocks_data.json"
OUTPUT_FILE = ROOT / "ema_scanner_data.json"

BASE_URL = (
    "https://huggingface.co/datasets/"
    "vishnun0027/indian-market-historical-ohlcv/"
    "resolve/main/stocks/"
)

REQUEST_TIMEOUT = 20


def load_stocks():
    with open(STOCKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    return data if isinstance(data, list) else []


def get_symbol(stock):
    return str(
        stock.get("symbol")
        or stock.get("ticker")
        or stock.get("s")
        or ""
    ).strip().upper()


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

    df["date"] = pd.to_datetime(
        df["date"],
        errors="coerce"
    )

    df["close"] = pd.to_numeric(
        df["close"],
        errors="coerce"
    )

    df = df.dropna(
        subset=["date", "close"]
    )

    return (
        df.sort_values("date")
        .reset_index(drop=True)
    )


def calculate_rsi(close, period=14):
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    rs = avg_gain / avg_loss.replace(0, pd.NA)

    rsi = 100 - (100 / (1 + rs))

    return rsi.fillna(50)


def scan_stock(stock):

    symbol = get_symbol(stock)

    if not symbol:
        return None

    try:

        df = download_history(symbol)

        # Need enough candles for 200 EMA + RSI
        if df is None or len(df) < 210:
            return None

        close = df["close"]

        # =========================
        # EMA CALCULATIONS
        # =========================

        ema9 = close.ewm(
            span=9,
            adjust=False
        ).mean()

        ema20 = close.ewm(
            span=20,
            adjust=False
        ).mean()

        ema200 = close.ewm(
            span=200,
            adjust=False
        ).mean()

        # =========================
        # RSI 14
        # =========================

        rsi = calculate_rsi(
            close,
            period=14
        )

        # =========================
        # LAST / PREVIOUS CANDLE
        # =========================

        last = len(df) - 1
        prev = last - 1

        price = float(close.iloc[last])

        e9 = float(ema9.iloc[last])
        e20 = float(ema20.iloc[last])
        e200 = float(ema200.iloc[last])

        prev_e9 = float(ema9.iloc[prev])
        prev_e20 = float(ema20.iloc[prev])

        current_rsi = float(rsi.iloc[last])
        previous_rsi = float(rsi.iloc[prev])

        # =========================
        # 9 / 20 EMA CROSSOVER
        # =========================

        crossed_up = (
            prev_e9 <= prev_e20
            and e9 > e20
        )

        # =========================
        # MAIN BUY CONDITIONS
        # =========================

        price_above_200 = price > e200
        ema_bullish = e9 > e20
        rsi_bullish = current_rsi > 60

        buy_cross = (
            crossed_up
            and price_above_200
            and rsi_bullish
        )

        bullish = (
            ema_bullish
            and price_above_200
            and rsi_bullish
        )

        # =========================
        # SIGNAL
        # =========================

        if buy_cross:
            signal = "BUY CROSS"
        elif bullish:
            signal = "BULLISH"
        else:
            signal = "WATCH"

        # =========================
        # RESULT
        # =========================

        return {

            "symbol": symbol,

            "stock": (
                stock.get("stock")
                or stock.get("name")
                or symbol
            ),

            "name": (
                stock.get("name")
                or stock.get("stock")
                or symbol
            ),

            "category": (
                stock.get("category")
                or stock.get("cap")
                or "Unknown"
            ),

            "price": round(price, 2),

            "ema9": round(e9, 2),

            "ema20": round(e20, 2),

            "ema200": round(e200, 2),

            "rsi": round(current_rsi, 2),

            "previous_rsi": round(
                previous_rsi,
                2
            ),

            "distance200": round(
                ((price - e200) / e200) * 100,
                2
            ) if e200 else 0,

            "signal": signal,

            "crossed_up": crossed_up,

            "price_above_200": price_above_200,

            "ema_bullish": ema_bullish,

            "rsi_bullish": rsi_bullish,

            "rsi_threshold": 60,

            "date": (
                df["date"]
                .iloc[last]
                .strftime("%Y-%m-%d")
            ),

            "strategy": (
                "9 EMA + 20 EMA + 200 EMA + RSI"
            )
        }

    except Exception as exc:

        print(
            f"[WARN] {symbol}: {exc}"
        )

        return None


def main():

    stocks = load_stocks()

    results = []

    print(
        f"Scanning {len(stocks)} stocks..."
    )

    for i, stock in enumerate(
        stocks,
        1
    ):

        result = scan_stock(stock)

        if result:
            results.append(result)

        if i % 25 == 0:

            print(
                f"Processed "
                f"{i}/{len(stocks)}"
            )

        time.sleep(0.05)

    # BUY CROSS first,
    # then BULLISH,
    # then WATCH
    signal_order = {
        "BUY CROSS": 0,
        "BULLISH": 1,
        "WATCH": 2
    }

    results.sort(
        key=lambda x: (
            signal_order.get(
                x["signal"],
                9
            ),
            -x["rsi"],
            -x["distance200"]
        )
    )

    output = {

        "generated_at":
            pd.Timestamp.utcnow().isoformat(),

        "strategy":
            "9 EMA + 20 EMA + 200 EMA + RSI",

        "rsi_period":
            14,

        "rsi_threshold":
            60,

        "rules": [

            "EMA data is published for every stock with valid history",

            "9 EMA + 20 EMA + 200 EMA + RSI BUY: "
            "9 EMA above 20 EMA, "
            "price above 200 EMA, "
            "and RSI above 60",

            "9/20 EMA Crossover BUY: "
            "previous 9 EMA <= previous 20 EMA, "
            "current 9 EMA > current 20 EMA, "
            "price above 200 EMA, "
            "and RSI above 60"

        ],

        "stocks": results,

        "count": len(results)

    }

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            output,
            f,
            ensure_ascii=False,
            indent=2
        )

    print(
        f"Done. "
        f"{len(results)} stocks saved to "
        f"{OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
