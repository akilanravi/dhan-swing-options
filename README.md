# Dhan Swing Options V3.5

Deploy to Streamlit Cloud with `app.py` and `requirements.txt` at repository root. Configure `DHAN_ACCESS_TOKEN` and `DHAN_CLIENT_ID` in Streamlit Secrets.

## Changes
- Month-to-date return and monthly bullish/bearish/mixed classification from completed daily candles.
- Detect EMA 9/21 cross or weekly CPR/Camarilla breakout occurring during the current calendar month.
- Fresh entries, monthly-trigger watchlist, strong watchlist, and rejected candidates sorted by category and score.
- Score >=8 for watchlist candidates; NOT an automatic option-buy approval.
- Optional manual FII/DII cash flow with as-of date. Dhan historical candles do not provide these figures.
- Preserves 4 tabs, cached historical data, and session-based paper journal.

## Limitations
- The monthly trend is descriptive, not a forecast.
- The index volume issue remains; index spot volume cannot confirm entry.
- Option-chain liquidity, IV percentile, delta, expiry, OI and earnings events are not automatically validated.
- Paper journal uses underlying points and is not persistent after Streamlit restart.
- A full historical option-premium backtest is not provided.
