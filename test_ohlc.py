import requests
import pandas as pd
from io import BytesIO

URL = "https://huggingface.co/datasets/vishnun0027/indian-market-historical-ohlcv/resolve/main/stocks/20MICRONS.parquet"

print("================================")
print("     20MICRONS OHLC TEST")
print("================================")

r = requests.get(URL, timeout=60)

print("STATUS:", r.status_code)
print("SIZE:", len(r.content), "bytes")

if r.status_code != 200:
    print("ERROR:")
    print(r.text[:1000])
    raise SystemExit

df = pd.read_parquet(BytesIO(r.content))

print("\nCOLUMNS:")
print(df.columns.tolist())

print("\nTOTAL CANDLES:", len(df))

print("\nLAST 10 CANDLES:")
print(df.tail(10).to_string(index=False))

required = ["date", "open", "high", "low", "close", "volume"]

print("\nCHECK:")
for col in required:
    if col in df.columns:
        print("OK  :", col)
    else:
        print("MISS:", col)

print("\n================================")
print("          TEST COMPLETE")
print("================================")
