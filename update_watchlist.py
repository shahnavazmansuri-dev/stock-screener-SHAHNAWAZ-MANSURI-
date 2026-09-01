from datetime import datetime
import json
import yfinance as yf

# Watchlist stocks ki list (Aap isme apne 100 stocks ke symbols add kar sakte hain)
watchlist_stocks = [
    "SAKAR.NS",
    "MPHASIS.NS",
    "AEROFLEX.NS",
    "VARROC.NS",
    "DEEPAKNTR.NS",
    "INOXINDIA.NS",
    "HEROMOTOCO.NS",
    "VISHNU.NS",
    "GMMPFAUDLR.NS",
    "ALIVUS.NS",
    "RSYSTEMS.NS",
    "KSB.NS",
    "ARTEMISMED.NS",
    "IOLCP.NS",
    "JINDALSAW.NS",
]

watchlist_data = []

for ticker in watchlist_stocks:
  try:
    df = yf.download(
        ticker, period="2d", interval="5m", progress=False, auto_adjust=True
    )
    if not df.empty:
      if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
      last_close = float(df["Close"].iloc[-1])
      last_time = str(df.index[-1])
      watchlist_data.append({
          "symbol": ticker.replace(".NS", ""),
          "price": round(last_close, 2),
          "time": last_time,
      })
  except Exception as e:
    continue

# watchlist.json file generate karna
with open("watchlist.json", "w") as f:
  json.dump(
      {
          "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          "stocks": watchlist_data,
      },
      f,
  )

print("Watchlist data updated successfully!")

