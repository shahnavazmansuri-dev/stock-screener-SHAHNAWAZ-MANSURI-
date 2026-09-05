import requests
import os
import json

API_KEY = os.environ.get("INDIAN_API_KEY")

headers = {
    "x-api-key": API_KEY,
    "accept": "application/json"
}

url = "https://stock.indianapi.in/historical_data?stock_name=20MICRONS&period=1yr&filter=price"

response = requests.get(
    url,
    headers=headers,
    timeout=30
)

print("STATUS:", response.status_code)
print("RESPONSE:")
print(json.dumps(response.json(), indent=2))
