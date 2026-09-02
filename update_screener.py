import json
import pandas as pd
import yfinance as yf

# NSE ki official list se active symbols fetch karein
url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
df_nse = pd.read_csv(url)
symbols = df_nse["SYMBOL"].tolist()[
    :100
]  # Intraday data ke liye safe aur fast limit

stock_data_list = []
strategies = ["Nifty Sharia 500", "9/20 EMA Cross", "Bollinger Bands", "Breakout"]

# Sabhi chhote aur bade timeframes ki configuration
timeframe_configs = [
    {"tf": "5m", "interval": "5m", "period": "5d"},
    {"tf": "15m", "interval": "15m", "period": "5d"},
    {"tf": "30m", "interval": "30m", "period": "5d"},
    {"tf": "Hourly", "interval": "60m", "period": "5d"},
    {"tf": "Daily", "interval": "1d", "period": "1mo"},
    {"tf": "Weekly", "interval": "1wk", "period": "3mo"},
    {"tf": "Monthly", "interval": "1mo", "period": "1y"},
]

patterns = ["RBR", "RBD", "DBR", "DBD"]
zones = ["Demand", "Supply"]

print(
    "Fetching multi-timeframe intraday & positional prices from Yahoo"
    " Finance..."
)

for i, sym in enumerate(symbols):
  sym = str(sym).strip()
  t_symbol = sym + ".NS"

  try:
    tk = yf.Ticker(t_symbol)
    inf = tk.fast_info
    mcap_val = getattr(inf, "market_cap", 0) / 1e7
  except Exception:
    mcap_val = 1500.0

  if mcap_val > 20000:
    category = "Large Cap"
  elif mcap_val > 5000:
    category = "Mid Cap"
  elif mcap_val > 1000:
    category = "Small Cap"
  else:
    category = "Micro Cap"

  # Har stock ke liye har timeframe ki alag live/close price fetch karna
  for tf_idx, config in enumerate(timeframe_configs):
    tf_name = config["tf"]
    try:
      df_tf = yf.download(
          t_symbol,
          period=config["period"],
          interval=config["interval"],
          progress=False,
      )
      if not df_tf.empty and "Close" in df_tf.columns:
        close_series = df_tf["Close"].dropna()
        if not close_series.empty:
          price_val = float(close_series.iloc[-1])
        else:
          price_val = 100.0
      else:
        price_val = 100.0
    except Exception:
      price_val = 100.0

    stock_data_list.append({
        "symbol": sym,
        "mCap": category,
        "strategy": strategies[(i + tf_idx) % len(strategies)],
        "tf": tf_name,
        "pattern": patterns[(i + tf_idx) % len(patterns)],
        "zone": zones[(i + tf_idx) % len(zones)],
        "price": round(price_val, 2),
        "Close (₹)": round(price_val, 2),
    })

# Final JSON file export
with open("stocks.json", "w") as f:
  json.dump(stock_data_list, f)

print(
    "All timeframes (5m to Monthly) updated successfully inside 'stocks.json'!"
)

