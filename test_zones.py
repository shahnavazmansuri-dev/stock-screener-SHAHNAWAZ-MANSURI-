import requests
import pandas as pd
from io import BytesIO

URL = "https://huggingface.co/datasets/vishnun0027/indian-market-historical-ohlcv/resolve/main/stocks/20MICRONS.parquet"

print("================================")
print("  20MICRONS DEMAND SUPPLY TEST")
print("================================")

response = requests.get(URL, timeout=60)

print("STATUS:", response.status_code)

if response.status_code != 200:
    print("ERROR:", response.text[:1000])
    raise SystemExit

df = pd.read_parquet(BytesIO(response.content))

df["date"] = pd.to_datetime(df["date"])

df = df[[
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume"
]]

df = df.dropna(
    subset=["open", "high", "low", "close"]
)

df = df.sort_values("date")


def make_zones(data, timeframe):

    data = data.copy()

    data["body"] = (
        data["close"] - data["open"]
    ).abs()

    data["range"] = (
        data["high"] - data["low"]
    )

    data["avg_body"] = (
        data["body"]
        .rolling(20)
        .mean()
    )

    zones = []

    for i in range(20, len(data) - 1):

        base = data.iloc[i]
        departure = data.iloc[i + 1]

        if base["range"] <= 0:
            continue

        if pd.isna(departure["avg_body"]):
            continue

        # =========================
        # DEMAND
        # =========================

        if (
            departure["close"] > departure["open"]
            and departure["body"] >
                departure["avg_body"] * 1.3
            and departure["close"] > base["high"]
        ):

            zones.append({
                "timeframe": timeframe,
                "type": "DEMAND",
                "zone_high": round(
                    base["high"], 2
                ),
                "zone_low": round(
                    base["low"], 2
                ),
                "date": str(
                    base["date"].date()
                ),
                "departure_date": str(
                    departure["date"].date()
                ),
                "score": 5
            })

        # =========================
        # SUPPLY
        # =========================

        if (
            departure["close"] < departure["open"]
            and departure["body"] >
                departure["avg_body"] * 1.3
            and departure["close"] < base["low"]
        ):

            zones.append({
                "timeframe": timeframe,
                "type": "SUPPLY",
                "zone_high": round(
                    base["high"], 2
                ),
                "zone_low": round(
                    base["low"], 2
                ),
                "date": str(
                    base["date"].date()
                ),
                "departure_date": str(
                    departure["date"].date()
                ),
                "score": 5
            })

    return zones


# ==================================
# CREATE TIMEFRAMES
# ==================================

daily = df.copy()


weekly = df.set_index("date").resample(
    "W-FRI"
).agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna().reset_index()


monthly = df.set_index("date").resample(
    "ME"
).agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna().reset_index()


quarterly = df.set_index("date").resample(
    "QE-DEC"
).agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna().reset_index()


# ==================================
# HALF YEAR
# ==================================

temp = df.copy()

temp["year"] = temp["date"].dt.year

temp["half"] = (
    (temp["date"].dt.month - 1) // 6
) + 1


half_year = temp.groupby(
    ["year", "half"]
).agg({
    "date": "max",
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).reset_index()

half_year = half_year[[
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume"
]]

half_year = half_year.dropna()


# ==================================
# YEARLY
# ==================================

yearly = df.set_index("date").resample(
    "YE-DEC"
).agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum"
}).dropna().reset_index()


timeframes = {
    "DAILY": daily,
    "WEEKLY": weekly,
    "MONTHLY": monthly,
    "QUARTERLY": quarterly,
    "HALF_YEAR": half_year,
    "YEARLY": yearly
}


# ==================================
# FIND ZONES
# ==================================

all_zones = []

for name, data in timeframes.items():

    print()
    print("--------------------------------")
    print(name)
    print("--------------------------------")

    print("Candles:", len(data))

    zones = make_zones(
        data,
        name
    )

    print(
        "Zones found:",
        len(zones)
    )

    for zone in zones[-5:]:

        print(
            zone["type"],
            "| High:",
            zone["zone_high"],
            "| Low:",
            zone["zone_low"],
            "| Date:",
            zone["date"],
            "| Score:",
            zone["score"]
        )

    all_zones.extend(zones)


# ==================================
# SUMMARY
# ==================================

print()
print("================================")
print("       ZONE SUMMARY")
print("================================")

if len(all_zones) == 0:

    print("No zones found.")

else:

    result = pd.DataFrame(
        all_zones
    )

    print()
    print("DEMAND ZONES")

    demand = result[
        result["type"] == "DEMAND"
    ]

    print(
        demand.tail(10)
        .to_string(index=False)
    )

    print()
    print("SUPPLY ZONES")

    supply = result[
        result["type"] == "SUPPLY"
    ]

    print(
        supply.tail(10)
        .to_string(index=False)
    )


print()
print("================================")
print("      ZONE TEST COMPLETE")
print("================================")
