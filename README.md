# Dhan Swing Options V3

Streamlit research dashboard with Dhan-only F&O universe scanning, score-ranked CE/PE signals and a session-based paper journal.

## Deploy
Upload `app.py`, `dhan_api.py`, `signals.py`, `requirements.txt` to the root of your existing GitHub repository (replace old files). Streamlit Cloud entrypoint: `app.py`. Add both `DHAN_ACCESS_TOKEN = "..."` and `DHAN_CLIENT_ID = "..."` in Streamlit Cloud Secrets (never commit secrets).

## Usage
1. Load the Dhan detailed instrument master. If the download fails, upload a current CSV from Dhan.
2. Check the matched F&O stock universe. Run a small batch of 10–30 first, then use the Start at stock number offset for subsequent batches.
3. Top CE/PE tab shows valid current daily signals, sorted by score and ADX. Scan history shows older triggers separately.
4. Use Paper Trading to record simulated underlying reference entries and exits; export CSV frequently and re-import after restarts.

## Critical limitations
- No verified live API test has been performed; Dhan's instrument-master schema may require adaptation.
- Index spot volume can be absent: index signals remain ineligible under the strict volume rule.
- Data fetch is on demand and bounded by Dhan rate limits. It does not continuously scan every F&O stock.
- Events require manual blackout dates. No automatic earnings calendar.
- No option strike/premium, lot-size, expiry selection, IV percentile, Greeks, bid-ask, or historical option premium P&L is simulated or invented.
- The paper journal uses underlying price points only, not actual option returns. Session state is ephemeral on Streamlit Cloud.
- Index IDs 13 and 25 are starting mappings and must be verified against Dhan master.
- No automated orders or brokerage charges; not a production-ready live execution system.

## V3.2 fixes
- Adds Dhan client-id header for option-chain requests and a missing-client-ID diagnostic.
- Excludes TEST/DUMMY/MOCK entries from discovered F&O stock underlyings.
- Option-chain calls still require a valid token, correct client ID and Dhan permissions.
