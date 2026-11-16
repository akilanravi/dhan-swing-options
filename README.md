# Independent Dhan Swing Paper Scanner

Separate GitHub Actions worker for the existing Streamlit swing website. No real orders, Yahoo, or Telegram. Browser does not need to be open. **Scheduled GitHub Actions are best-effort, not guaranteed real-time**; missed/late jobs and Dhan outages can cause missed exits. Quotes are snapshots, not fills.

## Install in the existing *swing* GitHub repository only

Copy `swing_worker.py`, `.github/workflows/swing-paper.yml`, and `requirements.txt`. Replace `app.py` with this package's `app.py` (the only change to the existing V4 35k app is guarding its `main()` call so the worker can import its functions safely). Do not place these files in the intraday repository.

Add these in **GitHub repository → Settings → Secrets and variables → Actions → Repository secrets**:
- `DHAN_ACCESS_TOKEN`: Dhan access token
- `DHAN_CLIENT_ID`: Dhan client ID
- `PAPER_GIST_TOKEN`: GitHub personal access token with Gist write access (not Dhan token)
- `PAPER_GIST_ID`: an EXISTING private Gist journal identifier containing `paper_trades.csv`

Keep the Streamlit swing app Secrets' `GITHUB_TOKEN` and `PAPER_GIST_ID` pointing to the **same swing journal**, so the app can display trades recorded by the independent worker. The intraday app MUST use a different Gist ID. Do not commit secrets or tokens. GitHub Actions' built-in `GITHUB_TOKEN` is not the same as `PAPER_GIST_TOKEN`.

Use GitHub → Actions → Independent Swing Paper Scanner → Run workflow for an immediate test. The workflow runs weekdays at 12:30 IST (exit checks only, because it is before the close) and at 16:15 IST (after-close full scan and simulated entries/exits). No scan/entry on weekends. Indian holidays with no fresh daily candle cannot open trades.

**Safety:** Automatic entries require current Dhan option-chain asks, valid expiry, delta, liquidity, one-lot affordability, 1% risk and a maximum Rs 35,000 premium per trade. A missing/failed Gist read aborts. Running the Streamlit `SCAN NOW` *with its own auto paper feature enabled* at the same time as the worker can race: turn off the Streamlit sidebar's `Auto-enter ... on SCAN NOW` and `Update and exit ... on SCAN NOW` toggles; use it to view/scan only. Do not put the intraday app on this Gist.

**Critical:** This is a scheduled paper research system, not a reliable live stop-loss service. It sees only Dhan snapshots when jobs run; stops/targets hit between snapshots can be missed, and prices can gap. A once/twice daily swing paper check is not equivalent to continuous monitoring. Refresh your Dhan token in BOTH Streamlit Secrets and swing GitHub Actions secrets after expiry. If the option chain or historical service is not entitled, worker logs will report errors.

Python syntax checked; no live Dhan or Gist integration tested.

## Month-to-date next-session scan

After 15:45 IST on a regular trading day, the market scanner expects that
session's completed daily candle. Before the cutoff it expects the previous
regular session. The scheduled daily watchlist run remains at 16:15 IST.
The app uses older bars as indicator warm-up (e.g. EMA 200), but only searches
for qualifying EMA 9/21 and weekly CPR/R3 triggers inside the month of the
verified candle. A same-month CE/PE trigger still needs to pass the existing
setup gates and score requirements on the newest candle. No thresholds changed.

The date on each result is the candle checked, not the trigger date. A valid
earlier trigger in the same month can still qualify. A symbol that only returns
an older candle goes into scan diagnostics and cannot become an eligible setup.
Click SCAN NOW again to retry missing candles. An empty eligible list with
verified candles means the conditions did not match. An unverified scan means
the data is not ready or there was an API error.

The app includes regular NSE equities closure dates for 2026. For future years
or additional full-day closures, provide comma-separated ISO dates in the
`NSE_CLOSED_DATES` environment variable.

### Yahoo Finance fallback (Swing only)

The dashboard and the scheduled next-session watchlist use Dhan daily candles
when available. Outside regular market hours (15:45 IST onwards), the scanner
can batch Yahoo Finance daily downloads for missing/mapped instruments. For a
full-market scan, if sample Dhan symbols all return the same stale date, the app
skips a full Dhan pass for mapped symbols, uses verified cached Dhan candles
first, and retries Dhan for Yahoo misses. If either provider has no candle for
the exact session date, its data cannot stand in for that date. Yahoo uses its
own OHLCV history and may produce different indicator readings.

NSE stocks use their NSE ticker with `.NS`. Among indices, Yahoo fallback
is mapped to NIFTY and BANKNIFTY only, with no ETF or alternate-index substitution.
An index with no reliable volume still fails the existing volume condition.
Rows and signal cards show the source, and diagnostics indicate missing bars.
Yahoo-sourced candidates stay out of dashboard automatic paper entries.
Dhan still supplies the instrument list, option contracts, quotes and simulated
fills. This does not change the Intraday project or the separate backtest.
