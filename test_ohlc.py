name: Test OHLC Data

on:
  workflow_dispatch:

jobs:
  test-ohlc:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: |
          pip install requests pandas pyarrow

      - name: Test OHLC Data
        run: python test_ohlc.py
