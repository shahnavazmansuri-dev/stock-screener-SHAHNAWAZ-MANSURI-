import requests
import os

API_KEY = os.environ.get("INDIAN_API_KEY")

headers = {
    "x-api-key": API_KEY,
    "accept": "application/json"
}

url = "https://stock.indianapi.in/historical_data"

params = {
    "stock_name": "20MICRONS",
    "period": "1yr",
    "filter": "price"
}

response = requests.get(
    url,
    headers=headers,
    params=params,
    timeout=30
)

print("STATUS:", response.status_code)
print("CONTENT-TYPE:", response.headers.get("content-type"))
print("RESPONSE:")
print(response.text[:5000])
