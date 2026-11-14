# Dhan Swing Options V3.7 — Streamlit paper-trading fix

## Deployment
Replace repository files with this folder's files, commit to `main`, and let Streamlit Community Cloud redeploy. Main file: `app.py`.

## Streamlit Secrets
```toml
DHAN_CLIENT_ID = "YOUR_DHAN_CLIENT_ID"
DHAN_ACCESS_TOKEN = "YOUR_DHAN_ACCESS_TOKEN"
GITHUB_TOKEN = "YOUR_GITHUB_FINE_GRAINED_OR_CLASSIC_GIST_WRITE_TOKEN"
# After the first journal save, copy the displayed ID here:
PAPER_GIST_ID = "YOUR_EXISTING_PRIVATE_GIST_ID"
```
Never commit secrets to GitHub. Gist write access is required for permanent paper-trade storage. Treat the Gist as sensitive, even when unlisted/private.

## Changes
- Fixes `StreamlitValueAboveMaxError` for Lots input when the auto-calculated lots exceed 1000.
- Optional automatic paper entries for eligible CE/PE signals when pressing **SCAN NOW**. Only contracts with Dhan option-chain ask, delta/spread, monthly expiry, valid lot size and positive affordable lots are considered. Never sends orders.
- Optional automatic paper exits on **SCAN NOW** based on current Dhan chain LTP snapshot: 30% stop, 2R target, within 5 days of expiry, or 10 calendar days holding. These are simulated snapshot executions, NOT actual fills and not intraday continuous monitoring.
- Existing manual paper update and trailing/underlying exit logic remain available in Paper Trading tab.
- Re-entry for the same signal date/symbol/side is blocked, as are max open trades, premium exposure and weekly loss limits.
- Entries require Gist configuration for persistence; the journal may still fail to save if GitHub credentials/permissions are wrong. Check the status text and verify the Gist exists before relying on persistence.

## Limitations
- Streamlit Cloud runs on interactions, not as a 24/7 market monitor. There is no unattended exit between scans; price can gap through a stop.
- Option-chain snapshots are not trade executions. Premium-based simulated P&L includes estimated charges/slippage, but does not reconstruct historical intraday prices.
- No IV percentile history, earnings calendar automation, or robust stock-to-stock correlation checks.
- Monthly setup eligibility is inherited from the existing strategy; it can be stale and should be paper-tested first.
- Institutional flows are market-wide, not stock-specific, and may be delayed.
- Dhan API limits apply to option chain requests.


V3.9 VISIBILITY FIX: Upload the `.streamlit/config.toml` folder to the repository root as well as app.py. All UI text is high-contrast. In Trade Analysis, candle table rows are green when Open > Close and red when Open < Close, per requested convention (the reverse of standard candlestick conventions). No trading logic changed.
