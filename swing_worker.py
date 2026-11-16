"""Unattended SWING paper-trade worker. No broker orders. Run via GitHub Actions.

Requires DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID, GITHUB_TOKEN, PAPER_GIST_ID.
Uses the exact signals/options/risk logic from app.py; never accesses Streamlit session state.
"""
import os
import sys
import math
import logging
import json
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app import (DhanAPI, JournalStore, JOURNAL_COLS, normalize_journal,
    load_universe, get_cache, load_daily, analyze_instrument, option_for_setup,
    live_premiums, close_row, entry_guard, num, fetch_fii_dii_nse,
    last_complete_session, next_regular_session, regular_session, MissingSessionCandle, scan_daily_sources)

IST = ZoneInfo('Asia/Kolkata')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('swing-worker')


def envfloat(name, default):
    return float(os.getenv(name, str(default)))


def config():
    capital = envfloat('PAPER_CAPITAL', 100000)
    opt = dict(capital=capital, risk_pct=envfloat('RISK_PCT', 1.0),
        min_days=int(envfloat('MIN_DAYS_TO_EXPIRY', 15)), delta_lo=0.50, delta_hi=0.65,
        max_spread=5.0, sl_frac=0.30, max_exposure=35.0,
        max_trade_rupees=envfloat('MAX_PREMIUM_PER_TRADE', 35000))
    signals = dict(min_score=7, month_min_score=8, max_weekly_cpr_width=0.015,
        require_monthly=True, max_ext_atr=2.0, flow_gate=False,
        scan_days=20, event_blackout_dates={})
    return opt, signals


def clean_id(value):
    v = str(value).strip()
    return v[:-2] if v.endswith('.0') else v


def gist_watchlist(token, gist_id, rows=None):
    """Persist the daily candidate list in the same Gist as the journal."""
    url = f'https://api.github.com/gists/{gist_id}'
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
    if rows is not None:
        payload = {'files': {'swing_next_day_watchlist.json': {'content': json.dumps(rows, default=str, allow_nan=False)}}}
        resp = requests.patch(url, headers=headers, json=payload, timeout=25)
        resp.raise_for_status()
        return rows
    resp = requests.get(url, headers=headers, timeout=25)
    resp.raise_for_status()
    item = resp.json().get('files', {}).get('swing_next_day_watchlist.json', {})
    if item.get('truncated'):
        raise RuntimeError('Watchlist Gist truncated')
    return json.loads(item.get('content') or '{}')


def run():
    token, cid = os.getenv('DHAN_ACCESS_TOKEN', ''), os.getenv('DHAN_CLIENT_ID', '')
    gist_token, gist_id = os.getenv('GITHUB_TOKEN', ''), os.getenv('PAPER_GIST_ID', '')
    if not all([token, cid, gist_token, gist_id]):
        raise RuntimeError('Missing DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID, GITHUB_TOKEN or PAPER_GIST_ID. Refusing nonpersistent auto-trading.')
    store = JournalStore(gist_token, gist_id)
    journal, msg = store.load()
    if msg.startswith('Load failed'):
        raise RuntimeError(msg + ' (refusing to risk overwriting existing journal)')
    log.info('Journal: %s (%d rows)', msg, len(journal))
    api = DhanAPI(token, cid)
    now = datetime.now(IST)
    if now.weekday() >= 5:
        log.info('Weekend: no new paper positions')
        return
    opt, cfg = config()
    updated = False
    # First update all OPEN contracts using current Dhan chain LTP; never substitute model premiums.
    opened = journal.index[journal.status == 'OPEN'].tolist()
    if opened:
        quotes = live_premiums(api, journal)
        for idx in opened:
            t = journal.loc[idx]
            px, error = quotes.get(t['id'], (float('nan'), 'missing quote'))
            if not np.isfinite(num(px)) or px <= 0:
                log.warning('%s exit check unavailable: %s', t.symbol, error)
                continue
            entry = num(t.entry_premium)
            if not np.isfinite(entry) or entry <= 0:
                log.warning('%s historical entry premium absent; skipping premium exit', t.symbol)
                continue
            qty = num(t.lots, 0) * num(t.lot_size, 0)
            journal.loc[idx, ['last_premium','premium_source','unrealized_pnl','last_update']] = [
                px, 'Dhan LTP snapshot', round((px-entry)*qty,2), now.date().isoformat()]
            updated = True
            sl = num(t.sl_frac, opt['sl_frac'])
            expiry = pd.to_datetime(t.expiry, errors='coerce')
            held = (now.date()-pd.Timestamp(t.opened_at).date()).days
            reason = None
            if px <= entry * (1-sl): reason = 'PAPER PREMIUM STOP'
            elif px >= entry*(1+2*sl): reason = 'PAPER PREMIUM TARGET 2R'
            elif pd.notna(expiry) and (expiry.date()-now.date()).days <= 5: reason = 'PAPER EXPIRY EXIT'
            elif held >= 10: reason = 'PAPER TIME EXIT'
            if reason:
                under = num(t.last_underlying, num(t.underlying_entry))
                close_row(journal, idx, now.isoformat(), under, px, reason + ' (Dhan snapshot)', .5)
                log.info('EXIT %s %s @ %.2f', t.symbol, reason, px)
    # Save exits before scanning/entry requests. Never lose exits because a scan fails.
    if updated:
        status = store.save(journal)
        if not status.startswith('Saved to GitHub Gist'):
            raise RuntimeError(status)

    mode = os.getenv('WORKER_MODE', 'auto').lower()
    if mode not in ('auto', 'watchlist', 'live', 'exit'):
        raise ValueError(f'Unknown WORKER_MODE: {mode}')
    if mode == 'exit':
        log.info('Exit-only cycle complete')
        return
    # 3:45 PM IST watchlist is based on the COMPLETED daily candle.
    # During live market hours only yesterday's watchlist is considered for paper entries.
    if mode in ('auto', 'watchlist') and (now.hour, now.minute) < (15,45):
        log.info('Waiting for completed daily candle (15:45 IST)')
        return
    if mode == 'live' and not ((9,30) <= (now.hour, now.minute) < (15,25)):
        log.info('Outside live paper-entry window')
        return
    candidates=[]
    if mode == 'live':
        saved = gist_watchlist(gist_token, gist_id)
        previous = last_complete_session(now)
        if (saved.get('created') != previous.isoformat()
                or next_regular_session(previous) != now.date()):
            log.info('No watchlist from previous trading session; skipping new entries')
            return
        candidates = saved.get('candidates', [])
        log.info('Live paper evaluation: %d saved candidates', len(candidates))
    else:
        stocks, indices = load_universe()
        universe = pd.concat([stocks,indices], ignore_index=True)
        if universe.empty:
            raise RuntimeError('Dhan F&O universe empty; no new watchlist')
        f = fetch_fii_dii_nse()
        fii, dii = (f.get('fii'), f.get('dii')) if 'error' not in f else (None, None)
        cache = get_cache()
        errors=[]
        stale = 0
        verified = 0
        cutoff = now.date()
        if not regular_session(cutoff):
            log.info('No regular NSE session today. No new watchlist.')
            return
        targets = universe.to_dict('records')
        ready, failed, yahoo_notes = scan_daily_sources(api, cache, targets, cutoff, now, log.info)
        yahoo_count = 0
        for x in targets:
            sym = x['symbol']
            try:
                if sym in failed:
                    raise failed[sym]
                d, elapsed = ready[sym]
                row, _ = analyze_instrument(x, d, cfg, fii, dii, elapsed)
                verified += 1
                yahoo_count += row['Daily source'] == 'Yahoo Finance'
                if row.get('Eligible'):
                    candidates.append(row)
            except Exception as e:
                stale += isinstance(e, MissingSessionCandle)
                errors.append((sym, f'{e} {yahoo_notes.get(sym, "")}'.strip()[:200]))
        log.info('Daily sources: %d Dhan, %d Yahoo Finance', verified-yahoo_count, yahoo_count)
        log.info('Daily scan: %d/%d verified for %s, %d candidates, %d stale/missing, %d failed',
                 verified, len(universe), cutoff, len(candidates), stale, len(errors)-stale)
        for sym, err in errors[:10]: log.warning('%s: %s',sym,err)
        if not verified:
            raise RuntimeError(f'No verified candles for {cutoff}; watchlist was not updated')
        candidates.sort(key=lambda x:(-x['Score'],-x['ADX']))
        # Store JSON-safe values; never store option quotes as executable prices.
        safe = json.loads(json.dumps(candidates, default=str).replace('NaN', 'null'))
        gist_watchlist(gist_token, gist_id, {'created': cutoff.isoformat(), 'for_session': next_regular_session(cutoff).isoformat(),
                                           'verified': verified, 'dhan_count': verified-yahoo_count, 'yahoo_count': yahoo_count,
                                           'universe_size': len(universe), 'candidates': safe})
        log.info('Saved %d candidates for NEXT trading session. No after-hours paper entries.',len(safe))
        return
    # A saved daily setup is a candidate, NOT an execution signal. Intraday
    # price confirmation is required before any simulated position is opened.
    # Current Dhan daily API cannot establish a completed 5-minute confirmation,
    # so live mode updates exits and publishes candidates but does NOT fabricate entries.
    log.info('Live watchlist loaded; paper entries disabled until 5-minute confirmation is implemented.')
    return
    log.info('Completed unattended swing-paper cycle')


if __name__ == '__main__':
    try:
        run()
    except Exception as exc:
        log.exception('Worker failure: %s',exc)
        sys.exit(1)
