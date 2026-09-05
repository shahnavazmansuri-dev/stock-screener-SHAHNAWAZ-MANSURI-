import requests
import pandas as pd
from io import BytesIO

URL = "https://huggingface.co/datasets/vishnun0027/indian-market-historical-ohlcv/resolve/main/stocks/20MICRONS.parquet"

print("================================")
print("  20MICRONS DEMAND SUPPLY TEST")
print("================================")

# Download OHLCV
response = requests.get(URL, timeout=60)

print("STATUS:", response.status_code)

if response.status_code != 200:
    print("ERROR:", response.text[:1000])
    raise SystemExit

df = pd.read_parquet(BytesIO(response.content))

# Required columns
df = df[["date", "open", "high", "low", "close", "volume"]]

# Date
df["date"] = pd.to_datetime(df["date"])

# Remove incomplete candles
df = df.dropna(subset=["open", "high", "low", "close"])

# Sort
df = df.sort_values("date").reset_index(drop=True)


def find_zones(data, timeframe):

    zones = []

    data = data.copy()

    data["body"] = abs(data["close"] - data["open"])
    data["range"] = data["high"] - data["low"]

    # Average body used to identify strong departure candles
    data["avg_body"] = data["body"].rolling(20).mean()

    for i in range(20, len(data) - 3):

        base = data.iloc[i]

        # Avoid very large base candle
        if base["range"] <= 0:
            continue

        # -------------------------
        # DEMAND ZONE
        # -------------------------

        for base_count in [1, 2, 3]:

            if i + base_count >= len(data):
                continue

            base_data = data.iloc[i:i + base_count]
            departure = data.iloc[i + base_count]

            base_high = base_data["high"].max()
            base_low = base_data["low"].min()

            # Strong bullish departure
            if (
                departure["close"] > departure["open"]
                and departure["body"] > departure["avg_body"] * 1.3
                and departure["close"] > base_high
            ):

                zones.append({
                    "timeframe": timeframe,
                    "type": "DEMAND",
                    "zone_high": round(base_high, 2),
                    "zone_low": round(base_low, 2),
                    "date": str(base_data.iloc[0]["date"].date()),
                    "departure_date": str(departure["date"].date()),
                    "score": 5
                })

                break

        # -------------------------
        # SUPPLY ZONE
        # -------------------------

        for base_count in [1, 2, 3]:

            if i + base_count >= len(data):
                continue

            base_data = data.iloc[i:i + base_count]
            departure = data.iloc[i + base_count]

            base_high = base_data["high"].max()
            base_low = base_data["low"].min()

            # Strong bearish departure
            if (
                departure["close"] < departure["open"]
                and departure["body"] > departure["avg_body"] * 1.3
                and departure["close"] < base_low
            ):

                zones.append({
                    "timeframe": timeframe,
                    "type": "SUPPLY",
                    "zone_high": round(base_high, 2),
                    "zone_low": round(base_low, 2),
                    "date": str(base_data.iloc[0]["date"].date()),
                    "departure_date": str(departure["date"].date()),
                    "score": 5
                })

                break

    return zones


# =========================
# CREATE TIMEFRAMES
# =========================

df_index = df.set_index("date")


timeframes = {}

# Daily
timeframes["DAILY"] = df_index.copy()

# Weekly
timeframes["WEEKLY"] = df_index.resample("W-FRI").agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna()

# Monthly
timeframes["MONTHLY"] = df_index.resample("ME").agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna()

# Quarterly
timeframes["QUARTERLY"] = df_index.resample("QE-DEC").agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna()

# Half Year
temp = df_index.copy()

temp["year"] = temp.index.year
temp["half"] = ((temp.index.month - 1) // 6) + 1

timeframes["HALF_YEAR"] = temp.groupby(
    ["year", "half"]
).agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna()

# Yearly
timeframes["YEARLY"] = df_index.resample("YE-DEC").agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna()


# =========================
# FIND ZONES
# =========================

all_zones = []

for name, data in timeframes.items():

    print()
    print("--------------------------------")
    print(name)
    print("--------------------------------")

    zones = find_zones(data.reset_index(), name)

    print("Zones found:", len(zones))

    for zone in zones[-5:]:
        print(
            zone["type"],
            "|",
            "High:", zone["zone_high"],
            "|",
            "Low:", zone["zone_low"],
            "|",
            "Date:", zone["date"],
            "|",
            "Score:", zone["score"]
        )

    all_zones.extend(zones)


# =========================
# SUMMARY
# =========================

result = pd.DataFrame(all_zones)

print()
print("================================")
print("       ZONE SUMMARY")
print("================================")

if len(result) == 0:

    print("No zones found.")

else:

    print()
    print("DEMAND ZONES:")
    print(
        result[result["type"] == "DEMAND"]
        .tail(10)
        .to_string(index=False)
    )

    print()
    print("SUPPLY ZONES:")
    print(
        result[result["type"] == "SUPPLY"]
        .tail(10)
        .to_string(index=False)
    )

print()
print("================================")
print("      ZONE TEST COMPLETE")
print("================================")
