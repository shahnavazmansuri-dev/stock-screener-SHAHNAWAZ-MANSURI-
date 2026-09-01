import json
import yfinance as yf

# Load stocks.json
try:
    with open('stocks.json', 'r') as f:
        stocks = json.load(f)
except Exception as e:
    print("Error loading stocks.json:", e)
    stocks = []

# Update prices for each stock
for stock in stocks:
    symbol = stock.get('Symbol') or stock.get('ticker')
    if symbol:
        try:
            ticker = yf.Ticker(symbol)
            todays_data = ticker.history(period='1d')
            if not todays_data.empty:
                current_price = todays_data['Close'].iloc[-1]
                stock['Price'] = round(float(current_price), 2)
                print(f"Updated {symbol} -> {stock['Price']}")
        except Exception as ex:
            print(f"Failed to update {symbol}: {ex}")

# Save updated data back to stocks.json
with open('stocks.json', 'w') as f:
    json.dump(stocks, f, indent=4)

print("stocks.json updated successfully!")
