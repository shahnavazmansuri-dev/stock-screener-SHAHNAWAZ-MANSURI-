import requests
import pandas as pd
from io import BytesIO

URL = "https://huggingface.co/datasets/vishnun0027/indian-market-historical-ohlcv/resolve/main/stocks/20MICRONS.parquet"

print("================================")
print("   20MICRONS TIMEFRAME TEST")
print("================================")

# Download daily OHLCV
response = requests.get(URL, timeout=60)

print("STATUS:", response.status_code)

if response.status_code != 200:
    print("ERROR:", response.text[:1000])
    raise SystemExit

df = pd.read_parquet(BytesIO(response.content))

# Date convert
df["date"] = pd.to_datetime(df["date"])

# Required columns
df = df[["date", "open", "high", "low", "close", "volume"]]

# Remove incomplete candles
df = df.dropna(subset=["open", "high", "low", "close"])

# Sort
df = df.sort_values("date")

df = df.set_index("date")


def make_ohlc(data):
    return data.resample(data.index.freq).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })


def show_timeframe(name, data):
    print()
    print("--------------------------------")
    print(name)
    print("--------------------------------")
    print("Candles:", len(data))
    print(data.tail(3).to_string())


# DAILY
daily = df.copy()
show_timeframe("DAILY", daily)


# WEEKLY
weekly = df.resample("W-FRI").agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna()

show_timeframe("WEEKLY", weekly)


# MONTHLY
monthly = df.resample("ME").agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna()

show_timeframe("MONTHLY", monthly)


# QUARTERLY
quarterly = df.resample("QE-DEC").agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna()

show_timeframe("QUARTERLY", quarterly)


# HALF YEAR
temp = df.copy()

temp["year"] = temp.index.year
temp["half"] = ((temp.index.month - 1) // 6) + 1

half_year = temp.groupby(["year", "half"]).agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna()

show_timeframe("HALF YEAR", half_year)


# YEARLY
yearly = df.resample("YE-DEC").agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna()

show_timeframe("YEARLY", yearly)


print()
print("================================")
print("     ALL TIMEFRAMES SUCCESS")
print("================================")
