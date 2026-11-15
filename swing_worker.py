"""Unattended SWING paper-trade worker. No broker orders. Run via GitHub Actions.

Requires DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID, GITHUB_TOKEN, PAPER_GIST_ID.
Uses the exact signals/options/risk logic from app.py; never accesses Streamlit session state.
"""
import os
import sys
import math
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app import (DhanAPI, JournalStore, JOURNAL_COLS, normalize_journal,
    load_universe, get_cache, load_daily, analyze_instrument, option_for_setup,
    live_premiums, close_row, entry_guard, num, fetch_fii_dii_nse)

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
    # Entries are only evaluated after daily market close; no retroactive fills.
    if mode == 'exit' or (now.hour,now.minute) < (15,45):
        log.info('Exit-only cycle complete')
        return
    stocks, indices = load_universe()
    universe = pd.concat([stocks,indices], ignore_index=True)
    if universe.empty:
        raise RuntimeError('Dhan F&O universe empty; no new paper trades')
    # FII/DII may be unavailable. Unknown flow neither grants nor blocks a setup.
    f = fetch_fii_dii_nse()
    fii, dii = (f.get('fii'), f.get('dii')) if 'error' not in f else (None, None)
    cache = get_cache()
    candidates=[]; errors=[]
    cutoff = now.date()
    for i, x in universe.iterrows():
        x = x.to_dict()
        try:
            d = load_daily(api, cache, x, cutoff)
            if d.index[-1].date() != cutoff:
                continue  # No fresh daily close; do not enter yesterday's signal.
            row, _ = analyze_instrument(x, d, cfg, fii, dii)
            if row.get('Eligible'):
                candidates.append(row)
        except Exception as e:
            errors.append((x.get('symbol'), str(e)[:110]))
    log.info('Scan: %d instruments, %d eligible candidates, %d failures',len(universe),len(candidates),len(errors))
    for s,e in errors[:10]: log.warning('%s: %s',s,e)
    candidates.sort(key=lambda x:(-x['Score'],-x['ADX']))
    seg_mem={}
    for row in candidates:
        if len(journal[journal.status == 'OPEN']) >= 3:
            break
        sym, side = row['Symbol'], row['Side']
        if not journal[(journal.symbol==sym)&(journal.side==side)&(journal.signal_date.astype(str)==str(row['Date']))].empty:
            continue
        try:
            pick = option_for_setup(api, row, opt, seg_mem)
            if pick.get('Verdict') != 'TRADE':
                log.info('SKIP %s: %s', sym,pick.get('Note'))
                continue
            prem, lot, lots = num(pick.get('Entry premium (ask)')), int(num(pick.get('Lot size'),0)), int(num(pick.get('Lots'),0))
            if not all([np.isfinite(prem),prem>0,lot>0,lots>0,num(pick.get('Ask'))>0]): continue
            if (pd.Timestamp(pick['Expiry']).date()-now.date()).days < opt['min_days']: continue
            reasons = entry_guard(journal,sym,side,prem*lot*lots,opt['capital'],3,opt['max_exposure'],4.0,opt['max_trade_rupees'])
            if reasons:
                log.info('SKIP %s: %s',sym,'; '.join(reasons)); continue
            rec={c:np.nan for c in JOURNAL_COLS}
            rec.update(dict(id=now.strftime('%Y%m%d%H%M%S%f')+'_'+sym,
                opened_at=now.isoformat(),signal_date=str(row['Date']), symbol=sym,side=side,
                status='OPEN',setup=row['Status'],score=row['Score'],underlying_entry=row['Close'],
                underlying_sl=row['Underlying SL'],underlying_t1=row['Target 1 (1R)'],underlying_t2=row['Target 2 (2R)'],
                contract=pick['Contract'],expiry=pick['Expiry'],strike=pick['Strike'],
                lot_size=lot,lots=lots,entry_premium=prem,entry_delta=pick.get('Delta'),entry_iv=pick.get('IV %'),
                sl_frac=opt['sl_frac'],sid=clean_id(row['sid']),segment=row['segment'],instrument=row['instrument'],
                chain_sid=clean_id(pick['chain_sid']),chain_seg=pick['chain_seg'],
                stop_current=row['Underlying SL'],t1_hit=False,days_held=0,last_premium=prem,
                premium_source='Dhan ask snapshot (simulated)',unrealized_pnl=0.0,
                notes='UNATTENDED PAPER ONLY; quote snapshot not an execution price'))
            # Save each entry immediately for restart safety.
            journal = pd.concat([journal,pd.DataFrame([rec])],ignore_index=True)
            status = store.save(journal)
            if not status.startswith('Saved to GitHub Gist'):
                raise RuntimeError(status)
            log.info('ENTRY %s %s %s x%d @ %.2f',sym,side,pick['Contract'],lots,prem)
        except Exception as e:
            log.exception('Option evaluation failed for %s: %s',sym,e)
    log.info('Completed unattended swing-paper cycle')


if __name__ == '__main__':
    try:
        run()
    except Exception as exc:
        log.exception('Worker failure: %s',exc)
        sys.exit(1)
