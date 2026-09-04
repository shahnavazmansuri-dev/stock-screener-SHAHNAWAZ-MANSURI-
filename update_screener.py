  stock_data_list.append({
      # Stock name ke liye saare naam
      "symbol": sym,
      "stock": sym,
      "name": sym,
      "ticker": sym,
      # Category / Market Cap ke liye saare naam
      "mCap": category,
      "category": category,
      "marketCap": category,
      "cap": category,
      # Price ke liye saare naam
      "price": tf_prices.get("Daily", 100.0),
      "ltp": tf_prices.get("Daily", 100.0),
      "close": tf_prices.get("Daily", 100.0),
      "currentPrice": tf_prices.get("Daily", 100.0),
      # Strategy, Pattern, Zones
      "strategy": strategies[i % len(strategies)],
      "pattern": patterns[i % len(patterns)],
      "zone": zones[i % len(zones)],
      "prices": tf_prices,
  })

