import requests
import pandas as pd
import json
import time
from io import BytesIO
from datetime import datetime, timezone

STOCK_FILE = "stocks_data.json"
OUTPUT_FILE = "zones_data.json"

BASE_URL = (
    "https://huggingface.co/datasets/"
    "vishnun0027/indian-market-historical-ohlcv/"
    "resolve/main/stocks/"
)

TIMEFRAMES = [
    "DAILY",
    "WEEKLY",
    "MONTHLY",
    "QUARTERLY",
    "HALF_YEAR",
    "YEARLY"
]

# Abhi test ke liye sirf 10 stocks
TEST_LIMIT = 10

# Current price se maximum 15% distance
MAX_ZONE_DISTANCE = 0.15


def download_stock(symbol):

    url = BASE_URL + symbol + ".parquet"

    try:

        response = requests.get(
            url,
            timeout=60
        )

        if response.status_code != 200:

            print(
                "Download failed:",
                symbol,
                response.status_code
            )

            return None

        df = pd.read_parquet(
            BytesIO(response.content)
        )

        required = [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]

        for column in required:

            if column not in df.columns:

                print(
                    "Missing column:",
                    column,
                    symbol
                )

                return None

        df = df[required].copy()

        df["date"] = pd.to_datetime(
            df["date"],
            errors="coerce"
        )

        for column in [
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce"
            )

        df = df.dropna(
            subset=[
                "date",
                "open",
                "high",
                "low",
                "close"
            ]
        )

        df = (
            df
            .sort_values("date")
            .reset_index(drop=True)
        )

        if len(df) == 0:

            print(
                "No OHLC data:",
                symbol
            )

            return None

        return df

    except Exception as e:

        print(
            "Error:",
            symbol,
            e
        )

        return None


def make_timeframes(df):

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

    temp = df.copy()

    temp["year"] = temp["date"].dt.year

    temp["half"] = (
        (temp["date"].dt.month - 1) // 6
    ) + 1

    half_year = (
        temp
        .groupby(["year", "half"])
        .agg({
            "date": "max",
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        })
        .reset_index(drop=True)
    )

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

    return {
        "DAILY": daily,
        "WEEKLY": weekly,
        "MONTHLY": monthly,
        "QUARTERLY": quarterly,
        "HALF_YEAR": half_year,
        "YEARLY": yearly
    }


def find_zones(df):

    zones = []

    if len(df) < 25:

        return zones

    df = df.copy()

    df["body"] = (
        df["close"] - df["open"]
    ).abs()

    df["range"] = (
        df["high"] - df["low"]
    )

    df["avg_body"] = (
        df["body"]
        .rolling(20)
        .mean()
    )

    for i in range(
        20,
        len(df) - 1
    ):

        for base_count in [1, 2, 3]:

            base_start = (
                i - base_count + 1
            )

            base_end = i

            if base_start < 1:

                continue

            base = df.iloc[
                base_start:base_end + 1
            ]

            base_range = (
                base["high"].max()
                - base["low"].min()
            )

            current_range = (
                df.iloc[i]["range"]
            )

            if current_range <= 0:

                continue

            if (
                base_range
                > current_range * 1.20
            ):

                continue

            departure = df.iloc[i + 1]

            if departure["avg_body"] <= 0:

                continue

            departure_strength = (
                departure["body"]
                / departure["avg_body"]
            )

            if departure_strength < 1.30:

                continue

            base_high = (
                base["high"].max()
            )

            base_low = (
                base["low"].min()
            )

            demand = (
                departure["close"]
                > departure["open"]
                and
                departure["close"]
                > base_high
            )

            supply = (
                departure["close"]
                < departure["open"]
                and
                departure["close"]
                < base_low
            )

            if not demand and not supply:

                continue

            # -------------------------
            # Pattern
            # -------------------------

            previous = df.iloc[
                base_start - 1
            ]

            if (
                previous["close"]
                > previous["open"]
                and demand
            ):

                pattern = "RBR"

            elif (
                previous["close"]
                < previous["open"]
                and demand
            ):

                pattern = "DBR"

            elif (
                previous["close"]
                > previous["open"]
                and supply
            ):

                pattern = "RBD"

            else:

                pattern = "DBD"

            # -------------------------
            # Freshness
            # -------------------------

            fresh = True

            zone_high = float(
                base_high
            )

            zone_low = float(
                base_low
            )

            for j in range(
                i + 2,
                len(df)
            ):

                future = df.iloc[j]

                overlap = (
                    future["low"]
                    <= zone_high
                    and
                    future["high"]
                    >= zone_low
                )

                if overlap:

                    fresh = False

                    break

            # -------------------------
            # Score
            # -------------------------

            score = 0

            if departure_strength >= 2.0:

                score += 3

            elif departure_strength >= 1.5:

                score += 2

            else:

                score += 1

            if base_count == 1:

                score += 2

            elif base_count == 2:

                score += 1

            if fresh:

                score += 2

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

            score = min(
                score,
                10
            )

            zone_type = (
                "DEMAND"
                if demand
                else "SUPPLY"
            )

            zones.append({

                "pattern": pattern,

                "type": zone_type,

                "high": round(
                    zone_high,
                    2
                ),

                "low": round(
                    zone_low,
                    2
                ),

                "date": (
                    df.iloc[base_start]["date"]
                    .strftime("%Y-%m-%d")
                ),

                "departure_date": (
                    departure["date"]
                    .strftime("%Y-%m-%d")
                ),

                "fresh": bool(
                    fresh
                ),

                "score": int(
                    score
                )

            })

    return zones


def overlap_ratio(a, b):

    overlap_high = min(
        a["high"],
        b["high"]
    )

    overlap_low = max(
        a["low"],
        b["low"]
    )

    if overlap_high <= overlap_low:

        return 0

    overlap = (
        overlap_high
        - overlap_low
    )

    width_a = max(
        a["high"] - a["low"],
        0.01
    )

    width_b = max(
        b["high"] - b["low"],
        0.01
    )

    return (
        overlap
        / min(
            width_a,
            width_b
        )
    )


def clean_zones(
    zones,
    current_price
):

    if not zones:

        return []

    # --------------------------------
    # Score 7+ only
    # --------------------------------

    zones = [
        z
        for z in zones
        if z["score"] >= 7
    ]

    # --------------------------------
    # Fresh zones only
    # --------------------------------

    zones = [
        z
        for z in zones
        if z["fresh"] is True
    ]

    if not zones:

        return []

    relevant = []

    for zone in zones:

        zone_high = zone["high"]

        zone_low = zone["low"]

        zone_type = zone["type"]

        # ============================
        # DEMAND ZONE
        # ============================

        if zone_type == "DEMAND":

            # Price zone ke andar
            if (
                zone_low
                <= current_price
                <= zone_high
            ):

                distance = 0

            # Demand price ke neeche
            elif zone_high < current_price:

                distance = (
                    current_price
                    - zone_high
                ) / current_price

            # Demand price ke upar
            else:

                continue

        # ============================
        # SUPPLY ZONE
        # ============================

        else:

            # Price zone ke andar
            if (
                zone_low
                <= current_price
                <= zone_high
            ):

                distance = 0

            # Supply price ke upar
            elif zone_low > current_price:

                distance = (
                    zone_low
                    - current_price
                ) / current_price

            # Supply price ke neeche
            else:

                continue

        # --------------------------------
        # Maximum 15% distance
        # --------------------------------

        if (
            distance
            <= MAX_ZONE_DISTANCE
        ):

            zone["distance_percent"] = round(
                distance * 100,
                2
            )

            relevant.append(zone)

    if not relevant:

        return []

    # --------------------------------
    # Fresh + score + nearest
    # --------------------------------

    relevant.sort(
        key=lambda z: (
            z["score"],
            -z["distance_percent"],
            z["date"]
        ),
        reverse=True
    )

    selected = []

    # --------------------------------
    # Remove overlapping duplicates
    # --------------------------------

    for zone in relevant:

        duplicate = False

        for existing in selected:

            if (
                zone["type"]
                != existing["type"]
            ):

                continue

            if (
                overlap_ratio(
                    zone,
                    existing
                ) >= 0.60
            ):

                duplicate = True

                break

        if not duplicate:

            selected.append(zone)

    # --------------------------------
    # Final sort:
    # nearest first,
    # then highest score
    # --------------------------------

    selected.sort(
        key=lambda z: (
            z["distance_percent"],
            -z["score"],
            z["date"]
        )
    )

    # Maximum 5 zones
    return selected[:5]


def process_stock(symbol):

    print()

    print("=" * 50)

    print(
        "STOCK:",
        symbol
    )

    print("=" * 50)

    df = download_stock(
        symbol
    )

    if df is None:

        return None

    print(
        "Candles:",
        len(df)
    )

    # Latest historical closing price
    current_price = float(
        df.iloc[-1]["close"]
    )

    latest_date = (
        df.iloc[-1]["date"]
        .strftime("%Y-%m-%d")
    )

    print(
        "Reference price:",
        round(
            current_price,
            2
        )
    )

    print(
        "Latest data:",
        latest_date
    )

    timeframe_data = (
        make_timeframes(df)
    )

    result = {}

    for timeframe in TIMEFRAMES:

        tf_df = (
            timeframe_data[
                timeframe
            ]
        )

        zones = find_zones(
            tf_df
        )

        demand = [
            z
            for z in zones
            if z["type"] == "DEMAND"
        ]

        supply = [
            z
            for z in zones
            if z["type"] == "SUPPLY"
        ]

        demand = clean_zones(
            demand,
            current_price
        )

        supply = clean_zones(
            supply,
            current_price
        )

        result[timeframe] = {

            "demand": demand,

            "supply": supply

        }

        print(
            timeframe,
            "| Demand:",
            len(demand),
            "| Supply:",
            len(supply)
        )

    return {

        "reference_price": round(
            current_price,
            2
        ),

        "reference_date": latest_date,

        "timeframes": result

    }


# ==================================
# MAIN
# ==================================

print()

print("=" * 60)

print(
    "   DEMAND / SUPPLY ZONE BUILDER"
)

print("=" * 60)

print()

with open(
    STOCK_FILE,
    "r",
    encoding="utf-8"
) as f:

    stock_data = json.load(f)


if isinstance(
    stock_data,
    dict
):

    stocks = stock_data.get(
        "stocks",
        []
    )

else:

    stocks = stock_data


symbols = []

for stock in stocks:

    symbol = (
        stock.get("symbol")
        or stock.get("s")
    )

    if symbol:

        symbols.append(
            str(symbol).upper()
        )


# Remove duplicates
symbols = list(
    dict.fromkeys(symbols)
)


print(
    "Total stocks found:",
    len(symbols)
)


if TEST_LIMIT:

    symbols = symbols[
        :TEST_LIMIT
    ]

    print(
        "TEST MODE:",
        len(symbols),
        "stocks"
    )


output = {

    "generated_at": (
        datetime.now(
            timezone.utc
        ).isoformat()
    ),

    "source": (
        "Hugging Face "
        "indian-market-historical-ohlcv"
    ),

    "zone_rule": (
        "Fresh zones with score >= 7"
    ),

    "max_zone_distance": (
        "15% from reference price"
    ),

    "stocks": {}

}


success = 0

failed = 0


for number, symbol in enumerate(
    symbols,
    start=1
):

    print()

    print(
        "Processing",
        number,
        "/",
        len(symbols)
    )

    result = process_stock(
        symbol
    )

    if result is not None:

        output["stocks"][symbol] = (
            result
        )

        success += 1

    else:

        failed += 1

    time.sleep(0.5)


with open(
    OUTPUT_FILE,
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        output,
        f,
        indent=2,
        ensure_ascii=False
    )


print()

print("=" * 60)

print(
    "           BUILD COMPLETE"
)

print("=" * 60)

print(
    "Successful:",
    success
)

print(
    "Failed:",
    failed
)

print(
    "Requested:",
    len(symbols)
)

print(
    "Output:",
    OUTPUT_FILE
)

print("=" * 60)
