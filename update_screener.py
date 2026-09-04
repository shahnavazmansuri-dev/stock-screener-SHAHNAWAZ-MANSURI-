import requests
import json
import os
import time

API_KEY = os.environ.get("INDIAN_API_KEY")

JSON_FILE = "stocks_data.json"

if not API_KEY:
    print("ERROR: INDIAN_API_KEY secret nahi mila.")
    raise SystemExit

headers = {
    "x-api-key": API_KEY,
    "accept": "application/json"
}

# stocks_data.json load
with open(JSON_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

if isinstance(data, dict):
    stocks = data.get("stocks", [])
else:
    stocks = data

print("==============================")
print("   INDIAN API LIVE PRICE")
print("==============================")
print("Total stocks:", len(stocks))
print()

# Special symbols
search_names = {
    "ZOMATO": "ETERNAL",
    "TATAMOTORS": "Tata Motors"
}

success = 0

for stock in stocks:

    symbol = stock.get("s")

    if not symbol:
        continue

    search_name = search_names.get(symbol, symbol)

    print("------------------------------")
    print("Stock:", symbol)

    url = f"https://stock.indianapi.in/stock?name={search_name}"

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=15
        )

        if response.status_code == 200:

            result = response.json()

            current_price = result.get("currentPrice", {})
            nse_price = current_price.get("NSE")
            change = result.get("percentChange")

            if nse_price is not None:

                # Live price save
                stock["p"] = nse_price
                stock["live_price"] = nse_price
                stock["percent_change"] = change

                print("NSE Price: ₹", nse_price)
                print("Change:", change, "%")

                success += 1

            else:
                print("NSE price nahi mila")

        else:
            print("API Error:", response.status_code)

    except Exception as e:
        print("Error:", e)

    # Free API rate limit ke liye 1 second gap
    time.sleep(1)

# Updated data save
with open(JSON_FILE, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print()
print("==============================")
print("       UPDATE COMPLETE")
print("==============================")
print("Successful stocks:", success)
print("Total stocks:", len(stocks))
print("==============================")
