import requests
import pandas as pd
from io import BytesIO

URL = "https://huggingface.co/datasets/vishnun0027/indian-market-historical-ohlcv/resolve/main/stocks/20MICRONS.parquet"

print("================================")
print("  20MICRONS ADVANCED ZONE TEST")
print("================================")

# ==================================
# DOWNLOAD DATA
# ==================================

response = requests.get(URL, timeout=60)

print("STATUS:", response.status_code)

if response.status_code != 200:
    print("ERROR:", response.text[:1000])
    raise SystemExit

df = pd.read_parquet(BytesIO(response.content))

df["date"] = pd.to_datetime(df["date"])

df = df[
    [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]
]

df = df.dropna(
    subset=["open", "high", "low", "close"]
)

df = df.sort_values("date").reset_index(drop=True)


# ==================================
# ZONE DETECTION
# ==================================

def find_zones(data, timeframe):

    data = data.copy().reset_index(drop=True)

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

    # --------------------------------
    # BASE = 1 to 3 candles
    # --------------------------------

    for i in range(20, len(data) - 3):

        for base_count in [1, 2, 3]:

            if i + base_count >= len(data):
                continue

            base = data.iloc[
                i:i + base_count
            ]

            before = data.iloc[i - 1]

            departure = data.iloc[
                i + base_count
            ]

            # Average base size
            avg_range = base["range"].mean()

            if avg_range <= 0:
                continue

            # Base should be relatively small
            if (
                avg_range >
                data["range"].rolling(20).mean().iloc[i]
                * 1.20
            ):
                continue

            base_high = base["high"].max()
            base_low = base["low"].min()

            # ==================================
            # DEPARTURE STRENGTH
            # ==================================

            if departure["avg_body"] <= 0:
                continue

            departure_strength = (
                departure["body"]
                / departure["avg_body"]
            )

            if departure_strength < 1.30:
                continue

            # ==================================
            # DEMAND
            # ==================================

            demand = False

            if (
                departure["close"] >
                departure["open"]
                and
                departure["close"] >
                base_high
            ):
                demand = True

            # ==================================
            # SUPPLY
            # ==================================

            supply = False

            if (
                departure["close"] <
                departure["open"]
                and
                departure["close"] <
                base_low
            ):
                supply = True

            if not demand and not supply:
                continue

            # ==================================
            # PATTERN
            # ==================================

            if before["close"] > before["open"]:

                if demand:
                    pattern = "RBR"

                else:
                    pattern = "RBD"

            else:

                if demand:
                    pattern = "DBR"

                else:
                    pattern = "DBD"

            # ==================================
            # FRESHNESS
            # ==================================

            fresh = True

            future = data.iloc[
                i + base_count + 1:
            ]

            for _, candle in future.iterrows():

                # Zone touched again
                if (
                    candle["low"] <= base_high
                    and
                    candle["high"] >= base_low
                ):
                    fresh = False
                    break

            # ==================================
            # SCORE 0-10
            # ==================================

            score = 0

            # Strong departure
            if departure_strength >= 2.0:
                score += 3

            elif departure_strength >= 1.5:
                score += 2

            else:
                score += 1

            # Base quality
            if base_count == 1:
                score += 2

            elif base_count == 2:
                score += 1

            # Fresh zone
            if fresh:
                score += 2

            # Clean departure
            departure_range = (
                departure["high"]
                - departure["low"]
            )

            if departure_range > 0:

                body_ratio = (
                    departure["body"]
                    / departure_range
                )

                if body_ratio >= 0.70:
                    score += 2

                elif body_ratio >= 0.55:
                    score += 1

            # Maximum 10
            score = min(score, 10)

            zones.append(
                {
                    "timeframe": timeframe,
                    "pattern": pattern,
                    "type":
                        "DEMAND"
                        if demand
                        else "SUPPLY",
                    "zone_high":
                        round(base_high, 2),
                    "zone_low":
                        round(base_low, 2),
                    "date":
                        str(
                            base.iloc[0]["date"].date()
                        ),
                    "departure_date":
                        str(
                            departure["date"].date()
                        ),
                    "fresh":
                        "YES"
                        if fresh
                        else "NO",
                    "score": score
                }
            )

            # Only one zone for this base
            break

    return zones


# ==================================
# TIMEFRAMES
# ==================================

daily = df.copy()

weekly = (
    df.set_index("date")
    .resample("W-FRI")
    .agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })
    .dropna()
    .reset_index()
)

monthly = (
    df.set_index("date")
    .resample("ME")
    .agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })
    .dropna()
    .reset_index()
)

quarterly = (
    df.set_index("date")
    .resample("QE-DEC")
    .agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })
    .dropna()
    .reset_index()
)


# ==================================
# HALF YEAR
# ==================================

temp = df.copy()

temp["year"] = temp["date"].dt.year

temp["half"] = (
    (temp["date"].dt.month - 1) // 6
) + 1

half_year = (
    temp.groupby(["year", "half"])
    .agg({
        "date": "max",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })
    .reset_index()
)

half_year = half_year[
    [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]
].dropna()


# ==================================
# YEARLY
# ==================================

yearly = (
    df.set_index("date")
    .resample("YE-DEC")
    .agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })
    .dropna()
    .reset_index()
)


timeframes = {
    "DAILY": daily,
    "WEEKLY": weekly,
    "MONTHLY": monthly,
    "QUARTERLY": quarterly,
    "HALF_YEAR": half_year,
    "YEARLY": yearly
}


# ==================================
# RUN ALL TIMEFRAMES
# ==================================

all_zones = []

for name, data in timeframes.items():

    print()
    print("--------------------------------")
    print(name)
    print("--------------------------------")

    print("Candles:", len(data))

    zones = find_zones(
        data,
        name
    )

    print(
        "Zones found:",
        len(zones)
    )

    # Show latest 10
    for zone in zones[-10:]:

        print(
            zone["pattern"],
            "|",
            zone["type"],
            "| High:",
            zone["zone_high"],
            "| Low:",
            zone["zone_low"],
            "| Fresh:",
            zone["fresh"],
            "| Score:",
            zone["score"]
        )

    all_zones.extend(zones)


# ==================================
# FINAL SUMMARY
# ==================================

print()
print("================================")
print("       FINAL ZONE SUMMARY")
print("================================")

if not all_zones:

    print("No zones found.")

else:

    result = pd.DataFrame(
        all_zones
    )

    print()
    print("DEMAND ZONES")
    print("--------------------------------")

    demand = result[
        result["type"] == "DEMAND"
    ]

    print(
        demand[
            demand["score"] >= 7
        ]
        .tail(20)
        .to_string(index=False)
    )

    print()
    print("SUPPLY ZONES")
    print("--------------------------------")

    supply = result[
        result["type"] == "SUPPLY"
    ]

    print(
        supply[
            supply["score"] >= 7
        ]
        .tail(20)
        .to_string(index=False)
    )

    print()
    print("ZONE COUNTS")
    print("--------------------------------")

    print(
        result.groupby(
            ["timeframe", "type"]
        ).size()
    )

    print()
    print("SCORE COUNTS")
    print("--------------------------------")

    print(
        result["score"]
        .value_counts()
        .sort_index()
    )


print()
print("================================")
print("     ADVANCED ZONE TEST DONE")
print("================================")
