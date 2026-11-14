# Dhan Swing Options V4 (single file)

Upload only **app.py** and **requirements.txt** to the root of your GitHub repo (replace the old ones).
`dhan_api.py` and `signals.py` are no longer used - you can delete them.

## Streamlit Secrets
```toml
DHAN_ACCESS_TOKEN = "..."
DHAN_CLIENT_ID    = "..."
# optional but recommended - permanent paper journal (private GitHub Gist)
GITHUB_TOKEN      = "..."   # classic token with only the 'gist' scope
PAPER_GIST_ID     = ""      # leave empty first; after the first paper trade the app shows the id -> paste it here
```
Dhan access tokens expire (about 24h). If every instrument fails with 401 / DH-901, generate a new token.

## What it does
1. **Scanner** - one click scans all F&O stocks + NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50 in parallel
   (one candle request per stock, cached 6h). Index volume = near-month index-futures volume (proxy, flagged).
2. **Status ranking** - FRESH ENTRY (strict signal on last candle) > MONTH SETUP (score >= 8, EMA-cross/CPR breakout
   this month, all hard gates pass, not extended > 2 ATR from EMA21) > WATCHLIST > REJECTED. Each row shows monthly
   bull/bear, MTD %, trigger date/type, exact blocking reasons, FII/DII bias.
3. **Option picker** - monthly expiry >= 15 days, ATM/1-ITM, |delta| 0.50-0.65, spread <= 5%, OI/volume > 0,
   lot size from Dhan master, lots from risk budget and 20% exposure cap. It says NO TRADE when one lot is too big.
4. **Paper trading** - premium-based journal; "Update open trades" replays daily candles and applies SL (1.5 ATR),
   -30% premium stop, T1 breakeven + EMA21 trail, T2, EMA-cross/Supertrend exit, 5-day no-progress exit, <=5 days to
   expiry exit, 10-day max hold. Rules enforced on entry: max 3 open, no two index trades, 20% exposure,
   4% weekly loss limit, pause after 3 straight losses. Stats + equity curve. Export/import CSV.

## Limits (read before trusting numbers)
- Not live-tested against your Dhan account by the author: first run may reveal mapping issues (errors are listed per symbol).
- Stock option-chain segment (NSE_FNO vs NSE_EQ) is auto-tried; if both fail the Note column shows Dhan's message.
- Paper P&L for model exits is a delta-only estimate (no theta/IV change) - optimistic. "chain LTP" is used for live marks.
- IV percentile, earnings dates and historical option premiums are not available from Dhan: add event dates manually.
- FII/DII is market-wide, fetched best-effort from NSE (may be blocked on cloud) or entered manually; it is context only
  unless you tick the block option.
- Research tool only. No orders are sent. No profit is guaranteed.
