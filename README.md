# Dhan Swing Options Research Website

No Yahoo Finance, Telegram, broker orders, or scheduled jobs.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```
Open the local URL shown by Streamlit. Enter your Dhan access token in the password field. For deployment, set `DHAN_ACCESS_TOKEN` as an environment variable or Streamlit secret. Never commit a real token.

## Data
Uses Dhan v2 `/charts/historical`, `/charts/intraday` (60-minute), `/optionchain/expirylist`, and `/optionchain` APIs. Access depends on Dhan API permissions. Verify security IDs with Dhan's current instrument master. This code makes no order requests.

## Important limitations
- No actual historical option P&L or historical option premium backtest.
- Underlying-only daily CE/PE research signals; option chain is raw inspection.
- 60-minute candles are displayed, not automatically integrated into signal trigger.
- Index volume may be zero, blocking volume-confirmed signals.
- Weekly CPR and indicator rules are research approximations; validate on historical data.
- Results and corporate-event blackout dates must be supplied manually.
- Position sizing, portfolio exposure/correlation, expiry checks, option delta/IV percentile and full walk-forward testing are pending.
- Market close is not a guaranteed executable entry price.
