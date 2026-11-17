# -*- coding: utf-8 -*-
"""
Dhan Swing Options V4  |  single-file Streamlit app
---------------------------------------------------
Dhan-first daily scanner with a Yahoo Finance after-close fallback. No broker orders.

Contents
  1. Dhan API client (rate-limited, parallel-safe) + instrument master -> F&O universe
  2. Indicators, 10-point scoring, monthly trend / trigger analysis
  3. Option contract selection (monthly expiry, ATM/1-ITM, delta, spread) + position sizing + costs
  4. Paper-trading engine (premium based, auto SL/target/trail/time/signal exits, risk rules,
     persistent journal in a private GitHub Gist or a local file)
  5. Streamlit UI (4 tabs)

Secrets (Streamlit Cloud -> App settings -> Secrets):
  DHAN_ACCESS_TOKEN = "..."
  DHAN_CLIENT_ID    = "..."
  GITHUB_TOKEN      = "..."   # optional, token with the 'gist' scope -> permanent paper journal
  PAPER_GIST_ID     = "..."   # optional, filled after the first save (the app shows the id)
"""
from __future__ import annotations

import base64  # noqa: F401  (kept for future use)
import hashlib
import io
import html
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import RLock
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st
import altair as alt
from underlying_backtest import render_backtest
from decision_panel import show_panel, load_quote, context
from swing_data import yahoo_batch, yahoo_ticker
from dashboard_auth import require_password, read_secret, is_authenticated, NONCE

IST = ZoneInfo('Asia/Kolkata')
BASE = 'https://api.dhan.co/v2'
MASTER_URL = 'https://images.dhan.co/api-data/api-scrip-master-detailed.csv'
INDEX_IDS = {'NIFTY': '13', 'BANKNIFTY': '25', 'FINNIFTY': '27', 'MIDCPNIFTY': '442', 'NIFTYNXT50': '38'}
INDEX_GROUP = set(INDEX_IDS)
JOURNAL_FILE = 'paper_trades.csv'
GIST_FILE = 'paper_trades.csv'


# =====================================================================================
# 0. small helpers
# =====================================================================================
# NSE equities full-day closures in 2026 (regular sessions). For future
# calendars or exceptional closures, set NSE_CLOSED_DATES to ISO dates.
NSE_CLOSED_2026 = frozenset(
    '2026-01-15 2026-01-26 2026-03-03 2026-03-26 2026-03-31 '
    '2026-04-03 2026-04-14 2026-05-01 2026-05-28 2026-06-26 '
    '2026-09-14 2026-10-02 2026-10-20 2026-11-10 2026-11-24 2026-12-25'.split()
)


def regular_session(day):
    extra = {item.strip() for item in os.getenv('NSE_CLOSED_DATES', '').split(',')}
    return day.weekday() < 5 and day.isoformat() not in (NSE_CLOSED_2026 | extra)


def last_complete_session(now=None) -> date:
    """Expected completed session, not evidence of candle delivery by Dhan."""
    n = datetime.now(IST) if now is None else now.astimezone(IST)
    d = n.date()
    if (n.hour, n.minute) < (15, 45):
        d -= timedelta(days=1)
    while not regular_session(d):
        d -= timedelta(days=1)
    return d


def next_regular_session(day):
    d = day + timedelta(days=1)
    while not regular_session(d):
        d += timedelta(days=1)
    return d


def num(x, default=np.nan) -> float:
    try:
        v = float(x)
        return default if math.isnan(v) else v
    except (TypeError, ValueError):
        return default


CANDLE_COLORS = {'Bullish': '#34f5ae', 'Bearish': '#ff688b', 'Neutral': '#ffe082'}
CANDLE_LEGEND = '🟢 Bullish: Close > Open · 🔴 Bearish: Close < Open · 🟡 Neutral: Close = Open'

def candle_direction(open_price, close_price):
    op = pd.to_numeric(open_price, errors='coerce')
    cl = pd.to_numeric(close_price, errors='coerce')
    if pd.isna(op) or pd.isna(cl):
        return 'Unavailable'
    return 'Bullish' if cl > op else 'Bearish' if cl < op else 'Neutral'


def show(df, **kw):
    """Dark-readable tables with the same candle colors as the chart."""
    kw.setdefault('hide_index', True)
    display = df
    if isinstance(df, pd.DataFrame) and not df.empty:
        if {'Open', 'Close'}.issubset(df.columns):
            def candle_colors(r):
                col = CANDLE_COLORS.get(candle_direction(r['Open'], r['Close']))
                return [f'color: {col}; background-color: #102033' if col else ''] * len(r)
            display = df.style.apply(candle_colors, axis=1).format(precision=2).set_table_styles([
                {'selector': 'th', 'props': [('background-color', '#14243a'), ('color', '#eaf4ff')]},
                {'selector': 'td', 'props': [('border-color', '#283e59')]},
            ])
    try:
        st.dataframe(display, width='stretch', **kw)
    except TypeError:
        st.dataframe(display, use_container_width=True, **kw)


# =====================================================================================
# 1. DHAN API
# =====================================================================================
class RateLimiter:
    def __init__(self, interval: float):
        self.interval, self.lock, self.next = interval, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next)
            self.next = start + self.interval
        if start - now > 0:
            time.sleep(start - now)


DATA_LIMITER = RateLimiter(0.22)    # Dhan data APIs: ~5 requests / second
EXPIRY_LIMITER = RateLimiter(1.0)
CHAIN_LIMITER = RateLimiter(3.2)    # Option chain: 1 unique request / 3 seconds


def frame_from_response(out):
    out = out.get('data', out) if isinstance(out, dict) else out
    if not isinstance(out, dict) or not out.get('timestamp'):
        return None
    n = len(out['timestamp'])
    d = pd.DataFrame({name: out.get(src, [0] * n) for src, name in
                      [('open', 'Open'), ('high', 'High'), ('low', 'Low'), ('close', 'Close'), ('volume', 'Volume')]})
    d.index = pd.to_datetime(out['timestamp'], unit='s', utc=True).tz_convert('Asia/Kolkata').tz_localize(None).normalize()
    for c in ['Open', 'High', 'Low', 'Close', 'Volume']:
        d[c] = pd.to_numeric(d[c], errors='coerce')
    d['Volume'] = d['Volume'].fillna(0)
    return d.dropna(subset=['Open', 'High', 'Low', 'Close'])


class DhanAPI:
    def __init__(self, token: str, client_id: str = ''):
        if not token:
            raise ValueError('Dhan access token is required')
        self.client_id = str(client_id or '').strip()
        self.session = requests.Session()
        self.session.headers.update({'access-token': token.strip(), 'Content-Type': 'application/json',
                                     'Accept': 'application/json'})

    def post(self, path, payload, limiter=DATA_LIMITER, with_client=False):
        # Optional cross-process pacing when both workers share a Dhan account on Linux.
        lock_dir=os.environ.get('DHAN_SHARED_LIMIT_DIR')
        if not lock_dir:return self._post(path,payload,limiter,with_client)
        import fcntl
        from pathlib import Path
        root=Path(lock_dir)
        root.mkdir(parents=True,exist_ok=True)
        is_chain=path in ('/optionchain','/optionchain/expirylist')
        with (root/('chain.lock' if is_chain else 'data.lock')).open('a+') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            handle.seek(0)
            prior=float(handle.read() or '0')
            pause=(3.25 if is_chain else .25)
            time.sleep(max(0, min(pause, prior+pause-time.time())))
            try:
                return self._post(path,payload,limiter,with_client)
            finally:
                handle.seek(0)
                handle.truncate()
                handle.write(str(time.time()))
                handle.flush()

    def _post(self, path, payload, limiter=DATA_LIMITER, with_client=False):
        headers = {'client-id': self.client_id} if (with_client and self.client_id) else {}
        last = ''
        for attempt in range(4):
            limiter.wait()
            try:
                r = self.session.post(BASE + path, json=payload, headers=headers, timeout=25)
            except requests.RequestException as e:
                last = f'network error: {e}'
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                last = f'HTTP {r.status_code}'
                time.sleep(2.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f'Dhan HTTP {r.status_code}: {r.text[:250]}')
            out = r.json()
            if isinstance(out, dict) and out.get('status') == 'failure':
                raise RuntimeError(str(out.get('remarks', out))[:250])
            return out
        raise RuntimeError(f'Dhan request failed after retries ({last})')

    def daily(self, sid, segment, instrument, start, end):
        """ONE request for the whole range (fast). Falls back to 85-day chunks only if too little came back."""
        start, end = pd.Timestamp(start), pd.Timestamp(end)

        def one(a, b):
            payload = {'securityId': str(sid), 'exchangeSegment': segment, 'instrument': instrument,
                       'expiryCode': 0, 'oi': False,
                       'fromDate': a.strftime('%Y-%m-%d'), 'toDate': b.strftime('%Y-%m-%d')}
            return frame_from_response(self.post('/charts/historical', payload))

        d = one(start, end)
        if d is None or len(d) < 150:
            frames, cur = ([d] if d is not None else []), start
            while cur < end:
                nxt = min(cur + timedelta(days=85), end)
                f = one(cur, nxt)
                if f is not None:
                    frames.append(f)
                cur = nxt
            d = pd.concat(frames) if frames else None
        if d is None or d.empty:
            raise ValueError('No candles returned (check instrument mapping / token / date range)')
        d = d.sort_index()
        return d[~d.index.duplicated(keep='last')]

    def expiries(self, sid, segment):
        return self.post('/optionchain/expirylist', {'UnderlyingScrip': int(sid), 'UnderlyingSeg': segment},
                         EXPIRY_LIMITER, with_client=True)

    def chain(self, sid, segment, expiry):
        return self.post('/optionchain', {'UnderlyingScrip': int(sid), 'UnderlyingSeg': segment, 'Expiry': expiry},
                         CHAIN_LIMITER, with_client=True)


class DailyCache:
    """Thread-safe in-memory candle cache (survives reruns, shared via st.cache_resource)."""
    def __init__(self):
        self.lock, self.data = threading.Lock(), {}

    def get(self, key, ttl=21600):
        with self.lock:
            v = self.data.get(key)
        return v[1] if v and time.time() - v[0] < ttl else None

    def put(self, key, value):
        with self.lock:
            self.data[key] = (time.time(), value)
            if len(self.data) > 1500:
                for k in sorted(self.data, key=lambda z: self.data[z][0])[:300]:
                    self.data.pop(k, None)


@st.cache_resource
def get_cache():
    return DailyCache()


@st.cache_resource
def get_scan_store():
    return {}


class MissingSessionCandle(ValueError):
    def __init__(self, expected, actual):
        self.expected, self.actual = expected, actual
        super().__init__(f'Expected {expected}, Dhan candles available through {actual or "none"}')


def dhan_daily_key(x, cutoff):
    return (x['sid'], x['segment'], x['instrument'], cutoff.isoformat())


def load_daily(api, cache, x, cutoff, force=False, *, require_session=False):
    key = dhan_daily_key(x, cutoff)
    if not force:
        hit = cache.get(key)
        if hit is not None and not hit.empty and hit.index[-1].date() == cutoff:
            return hit
    end = pd.Timestamp(cutoff) + timedelta(days=1)
    df = api.daily(x['sid'], x['segment'], x['instrument'], end - timedelta(days=420), end)
    df = df[df.index.date <= cutoff].sort_index()
    actual = df.index[-1].date() if not df.empty else None
    if require_session and actual != cutoff:
        raise MissingSessionCandle(cutoff, actual)
    # Index spot candles carry no volume -> use near-month index-futures volume as proxy (flagged in output).
    if x['instrument'] == 'INDEX' and df.Volume.tail(25).sum() <= 0 and str(x.get('fut_sid', '')).isdigit():
        try:
            fut = api.daily(x['fut_sid'], 'NSE_FNO', 'FUTIDX', end - timedelta(days=150), end)
            df = df.copy()
            df['Volume'] = fut['Volume'].reindex(df.index).fillna(0)
            df.attrs['vol_src'] = 'index futures proxy'
        except Exception:
            pass
    df.attrs['candle_source'] = 'Dhan'
    if actual == cutoff:
        cache.put(key, df)
    return df


def swing_after_close(now):
    n = now.astimezone(IST)
    return not regular_session(n.date()) or not (9, 15) <= (n.hour, n.minute) < (15, 45)


def scan_daily_sources(api, cache, targets, cutoff, now, progress=None):
    """Resolve daily bars for a watchlist, with a fast common-delay path."""
    allow_yahoo = swing_after_close(now)
    ready, failed, deferred = {}, {}, {}
    remaining = {x['symbol']: x for x in targets}

    def fetch(x):
        if api is None:
            raise ValueError('Dhan daily data unavailable: no access token')
        t = time.monotonic()
        return load_daily(api, cache, x, cutoff, require_session=True), time.monotonic() - t

    def keep(x, result):
        sym = x['symbol']
        try:
            ready[sym] = result()
            failed.pop(sym, None)
        except Exception as e:
            failed[sym] = e

    def dhan_many(items):
        with ThreadPoolExecutor(max_workers=4) as ex:
            jobs = {ex.submit(fetch, x): x for x in items}
            for i, f in enumerate(as_completed(jobs), 1):
                x = jobs[f]
                keep(x, f.result)
                if progress:
                    progress(f'Dhan daily check {i}/{len(jobs)} · {x["symbol"]}')

    auth_error = None
    if api is not None and allow_yahoo and len(targets) >= 12:
        equities = [x for x in targets if x['segment'] == 'NSE_EQ']
        probe = [x for x in targets if x['symbol'] == 'NIFTY'][:1]
        probe += list({x['symbol']: x for x in equities[:1] + equities[-1:]}.values())
        dhan_many(probe)
        for x in probe:
            remaining.pop(x['symbol'])
        errors = [failed.get(x['symbol']) for x in probe]
        lag = (len(probe) >= 2 and all(isinstance(e, MissingSessionCandle) for e in errors)
               and len({e.actual for e in errors}) == 1)
        auth = bool(errors) and all(any(code in str(e) for code in ('401', 'DH-901', 'DH-902', 'Invalid_Authentication'))
                                    for e in errors if e is not None) and all(e is not None for e in errors)
        if auth:
            auth_error = errors[0]
        if lag or auth:
            # The sample shows a feed-wide delay or auth issue. Avoid a full
            # serial-rate-limited Dhan pass, but retry Dhan for Yahoo misses.
            for sym, x in list(remaining.items()):
                cached = cache.get(dhan_daily_key(x, cutoff))
                if cached is not None and not cached.empty and cached.index[-1].date() == cutoff:
                    ready[sym] = cached, 0.0
                    del remaining[sym]
                elif yahoo_ticker(x):
                    deferred[sym] = x
                    del remaining[sym]

    if api is not None and auth_error is None:
        dhan_many(list(remaining.values()))
    elif auth_error is not None:
        failed.update({sym: auth_error for sym in remaining})
    else:
        for x in remaining.values():
            keep(x, lambda: fetch(x))

    yahoo_notes = {}
    if allow_yahoo:
        yahoo_targets = [x for x in targets if x['symbol'] not in ready and yahoo_ticker(x)]
        if yahoo_targets:
            if progress:
                progress(f'Yahoo Finance fallback · {len(yahoo_targets)} symbols')
            t = time.monotonic()
            frames, notes = yahoo_batch(yahoo_targets, cutoff, cache)
            elapsed = time.monotonic() - t
            for x in yahoo_targets:
                sym, ticker = x['symbol'], yahoo_ticker(x)
                if ticker in frames:
                    ready[sym] = frames[ticker], elapsed
                    failed.pop(sym, None)
                else:
                    yahoo_notes[sym] = notes.get(ticker, 'Yahoo candle unavailable')
            if api is not None and deferred and auth_error is None:
                dhan_many([x for sym, x in deferred.items() if sym not in ready])
            elif auth_error is not None:
                failed.update({sym: auth_error for sym in deferred if sym not in ready})
    for x in targets:
        sym = x['symbol']
        if sym not in ready and sym not in failed:
            failed[sym] = ValueError('No daily candle from either provider')
    return ready, failed, yahoo_notes


# ---- instrument master -> F&O universe ------------------------------------------------
def read_instrument_master():
    r = requests.get(MASTER_URL, timeout=120, headers={'User-Agent': 'Mozilla/5.0'})
    r.raise_for_status()
    keep = []
    for ch in pd.read_csv(io.BytesIO(r.content), dtype=str, chunksize=50000):
        ch.columns = [str(c).strip().upper() for c in ch.columns]
        icol = next((c for c in ('INSTRUMENT', 'INSTRUMENT_TYPE', 'SEM_INSTRUMENT_NAME') if c in ch.columns), None)
        if icol is None:
            keep.append(ch)
        else:
            keep.append(ch[ch[icol].fillna('').str.upper().isin(['EQUITY', 'EQ', 'FUTSTK', 'FUTIDX', 'FUT'])])
    return pd.concat(keep, ignore_index=True)


def _col(m, names):
    for n in names:
        if n in m.columns:
            return m[n]
    return None


def build_universe(master):
    """Returns (stocks_df, indices_df). stocks: symbol,sid,segment,instrument,lot."""
    m = master.copy()
    m.columns = [str(c).strip().upper() for c in m.columns]
    aliases = {'EXCH_ID': ['EXCH_ID', 'SEM_EXM_EXCH_ID'], 'SEGMENT': ['SEGMENT', 'SEM_SEGMENT'],
               'SECURITY_ID': ['SECURITY_ID', 'SEM_SMST_SECURITY_ID'],
               'INSTRUMENT': ['INSTRUMENT', 'INSTRUMENT_TYPE', 'SEM_INSTRUMENT_NAME'],
               'SYMBOL_NAME': ['SYMBOL_NAME', 'SEM_TRADING_SYMBOL', 'DISPLAY_NAME']}
    for canon, variants in aliases.items():
        src = _col(m, variants)
        if src is None:
            raise ValueError(f'Dhan master missing {canon}; available columns: {list(m.columns)}')
        m[canon] = src
    lot = _col(m, ['LOT_SIZE', 'SEM_LOT_UNITS'])
    exp = _col(m, ['SM_EXPIRY_DATE', 'SEM_EXPIRY_DATE', 'EXPIRY_DATE'])
    m['LOT'] = pd.to_numeric(lot, errors='coerce') if lot is not None else np.nan
    m['EXP'] = pd.to_datetime(exp, errors='coerce') if exp is not None else pd.NaT
    for c in ('EXCH_ID', 'SEGMENT', 'INSTRUMENT'):
        m['_' + c] = m[c].fillna('').astype(str).str.upper().str.strip()
    nse = m['_EXCH_ID'].eq('NSE') | m['_SEGMENT'].str.contains('NSE')
    inst = m['_INSTRUMENT']
    today = pd.Timestamp.now().normalize() - timedelta(days=1)

    # ---- stock futures define the F&O stock list
    fut = m[nse & (inst.str.contains('FUTSTK') | ((inst == 'FUT') & m['_SEGMENT'].str.contains('FNO|F&O|DERIV|^D$')))].copy()
    if fut.empty:
        raise ValueError('No NSE stock futures found in Dhan master.')
    if 'UNDERLYING_SYMBOL' in fut.columns:
        fut['key'] = fut['UNDERLYING_SYMBOL'].fillna('').str.upper().str.strip()
    else:
        fut['key'] = fut['SYMBOL_NAME'].fillna('').str.upper().str.replace(r'[- ]?\d{2}[A-Z]{3}FUT$', '', regex=True).str.strip()
    fut = fut[~fut['key'].str.contains(r'TEST|DUMMY|MOCK', case=False, na=False)]
    live = fut[fut['EXP'].isna() | (fut['EXP'] >= today)].sort_values('EXP')
    lot_map = live.groupby('key')['LOT'].first().to_dict()
    keys = set(fut['key']) - {''}

    # ---- spot equity IDs (candles must use the NSE_EQ id, not the futures id)
    eq = m[(m['_EXCH_ID'].eq('NSE') | m['_SEGMENT'].eq('NSE_EQ')) &
           (inst.isin(['EQUITY', 'EQ']) | m['_SEGMENT'].eq('NSE_EQ'))].copy()
    eq['key'] = eq['SYMBOL_NAME'].fillna('').str.upper().str.strip()
    if 'UNDERLYING_SYMBOL' in eq.columns:
        eq['key'] = eq['UNDERLYING_SYMBOL'].where(eq['UNDERLYING_SYMBOL'].fillna('').ne(''), eq['key']).astype(str).str.upper().str.strip()
    eq = eq[eq['key'].isin(keys)]
    if 'SERIES' in eq.columns:
        eq = eq[eq['SERIES'].fillna('EQ').astype(str).str.upper().isin(['EQ', ''])]
    eq = eq.drop_duplicates('key')
    stocks = pd.DataFrame({'symbol': eq['key'], 'sid': eq['SECURITY_ID'].astype(str).str.replace(r'\.0$', '', regex=True),
                           'segment': 'NSE_EQ', 'instrument': 'EQUITY'})
    stocks['lot'] = stocks['symbol'].map(lot_map)
    stocks = stocks[stocks['sid'].str.fullmatch(r'\d+') & stocks['symbol'].str.fullmatch(r'[A-Z0-9&._-]{2,40}')]
    stocks = stocks[~stocks['symbol'].str.contains(r'TEST|DUMMY|MOCK', case=False)].sort_values('symbol').reset_index(drop=True)
    if stocks.empty:
        raise ValueError(f'Found {len(keys)} F&O symbols but no matching NSE equity IDs.')

    # ---- indices (+ near-month index future: lot size and volume proxy)
    ifut = m[nse & inst.eq('FUTIDX')].copy()
    ifut['key'] = (ifut['UNDERLYING_SYMBOL'] if 'UNDERLYING_SYMBOL' in ifut.columns else ifut['SYMBOL_NAME']).fillna('').str.upper().str.strip()
    ifut = ifut[ifut['EXP'].isna() | (ifut['EXP'] >= today)].sort_values('EXP').drop_duplicates('key')
    imap = ifut.set_index('key')
    rows = []
    for name, sid in INDEX_IDS.items():
        has = name in imap.index
        rows.append({'symbol': name, 'sid': sid, 'segment': 'IDX_I', 'instrument': 'INDEX',
                     'lot': imap.loc[name, 'LOT'] if has else np.nan,
                     'fut_sid': str(imap.loc[name, 'SECURITY_ID']).replace('.0', '') if has else ''})
    return stocks, pd.DataFrame(rows)


@st.cache_data(ttl=86400, show_spinner=False)
def load_universe():
    return build_universe(read_instrument_master())


# =====================================================================================
# 2. INDICATORS, SCORING, MONTHLY ANALYSIS
# =====================================================================================
def ema(s, n): return s.ewm(span=n, adjust=False, min_periods=n).mean()
def rma(s, n): return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def atr(df, n=14):
    p = df.Close.shift(1)
    tr = pd.concat([(df.High - df.Low), (df.High - p).abs(), (df.Low - p).abs()], axis=1).max(axis=1)
    return rma(tr, n)


def indicators(df):
    d = df.copy().sort_index()
    for n in (9, 21, 50, 200):
        d[f'ema{n}'] = ema(d.Close, n)
    d['atr'] = atr(d)
    up, down = d.High.diff(), -d.Low.diff()
    plus = up.where((up > down) & (up > 0), 0.0)
    minus = down.where((down > up) & (down > 0), 0.0)
    trsum = d['atr'] * 14
    pdi = 100 * rma(plus, 14) * 14 / trsum.replace(0, np.nan)
    mdi = 100 * rma(minus, 14) * 14 / trsum.replace(0, np.nan)
    d['pdi'], d['mdi'] = pdi, mdi
    d['adx'] = rma(100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan), 14)
    diff = d.Close.diff()
    gain, loss = rma(diff.clip(lower=0), 14), rma((-diff).clip(lower=0), 14)
    d['rsi'] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    d.loc[(loss == 0) & (gain > 0), 'rsi'] = 100
    d.loc[(loss == 0) & (gain == 0), 'rsi'] = 50
    mid = (d.High + d.Low) / 2
    upper, lower = mid + 3 * d['atr'], mid - 3 * d['atr']
    st_line, direction = np.full(len(d), np.nan), np.zeros(len(d), dtype=int)
    fu, fl, close = upper.to_numpy().copy(), lower.to_numpy().copy(), d.Close.to_numpy()
    midv = mid.to_numpy()
    for i in range(1, len(d)):
        if np.isnan(fu[i]) or np.isnan(fu[i - 1]):
            continue
        if fu[i] >= fu[i - 1] and close[i - 1] <= fu[i - 1]: fu[i] = fu[i - 1]
        if fl[i] <= fl[i - 1] and close[i - 1] >= fl[i - 1]: fl[i] = fl[i - 1]
        if direction[i - 1] == 0: direction[i] = 1 if close[i] >= midv[i] else -1
        elif direction[i - 1] == 1: direction[i] = -1 if close[i] < fl[i] else 1
        else: direction[i] = 1 if close[i] > fu[i] else -1
        st_line[i] = fl[i] if direction[i] == 1 else fu[i]
    d['st'], d['st_dir'] = st_line, direction
    d['vol20'] = d.Volume.rolling(20).mean().shift(1)
    # prior COMPLETED weekly OHLC only (never the running week)
    weekly = d[['High', 'Low', 'Close']].resample('W-FRI').agg({'High': 'max', 'Low': 'min', 'Close': 'last'}).shift(1)
    w = weekly.reindex(d.index, method='ffill')
    pivot = (w.High + w.Low + w.Close) / 3
    bc = (w.High + w.Low) / 2
    tc = 2 * pivot - bc
    d['cpr_top'] = pd.concat([bc, tc], axis=1).max(axis=1)
    d['cpr_bottom'] = pd.concat([bc, tc], axis=1).min(axis=1)
    d['cpr_width'] = (d.cpr_top - d.cpr_bottom) / pivot
    span = w.High - w.Low
    d['r3'], d['s3'] = w.Close + span * 1.1 / 4, w.Close - span * 1.1 / 4
    up_lvl = d[['cpr_top', 'r3']].max(axis=1)
    dn_lvl = d[['cpr_bottom', 's3']].min(axis=1)
    d['cross_up'] = (d.ema9 > d.ema21) & (d.ema9.shift(1) <= d.ema21.shift(1))
    d['cross_down'] = (d.ema9 < d.ema21) & (d.ema9.shift(1) >= d.ema21.shift(1))
    d['break_up'] = (d.Close > up_lvl) & (d.Close.shift(1) <= up_lvl.shift(1))
    d['break_down'] = (d.Close < dn_lvl) & (d.Close.shift(1) >= dn_lvl.shift(1))
    return d


def _side_tests(d, side, validvol):
    bull = side == 'CE'
    t = {
        'ema200': (d.Close > d.ema200) if bull else (d.Close < d.ema200),
        'ema50': (d.ema50 > d.ema200) if bull else (d.ema50 < d.ema200),
        'st': d.st_dir == (1 if bull else -1),
        'adx': d.adx > 22,
        'adx_rise': d.adx > d.prev_adx,
        'di': (d.pdi > d.mdi) if bull else (d.mdi > d.pdi),
        'trigger': (d.cross_up | d.break_up) if bull else (d.cross_down | d.break_down),
        'rsi': (d.rsi > 55) if bull else (d.rsi < 45),
        'volume': validvol & (d.Volume > d.vol20),
        'ema21': (d.Close > d.ema21) if bull else (d.Close < d.ema21),
    }
    return {k: v.fillna(False).astype(bool) for k, v in t.items()}


def annotate(d, cfg, ticker):
    """Vectorised 10-point score + strict daily signal (all mandatory gates)."""
    d = d.copy()
    d['prev_adx'] = d.adx.shift(1)
    validvol = (d.Volume > 0) & (d.vol20 > 0)
    bl = pd.Series(False, index=d.index)
    for e in cfg.get('event_blackout_dates', {}).get(ticker, []):
        bl |= pd.Series(np.abs((d.index - pd.Timestamp(e).normalize()).days) <= 3, index=d.index)
    common = (d.ema200.notna() & d.adx.notna() & (d.adx >= 20) & d.cpr_width.notna() &
              (d.cpr_width <= cfg['max_weekly_cpr_width']) & validvol & ~bl)
    out = {}
    for side in ('CE', 'PE'):
        t = _side_tests(d, side, validvol)
        d[f'score_{side}'] = sum(v.astype(int) for v in t.values())
        out[side] = t['ema200'] & t['st'] & t['adx'] & t['adx_rise'] & t['di'] & t['trigger'] & t['rsi'] & t['volume']
    d['side'] = np.where(d.score_CE > d.score_PE, 'CE', np.where(d.score_PE > d.score_CE, 'PE',
                         np.where(d.st_dir == 1, 'CE', 'PE')))
    d['score'] = np.where(d.side == 'CE', d.score_CE, d.score_PE)
    ok = np.where(d.side == 'CE', out['CE'], out['PE']) & common & (d.score >= cfg['min_score'])
    d['signal'] = np.where(ok, d.side, '')
    return d


def rejection_reasons(r, side, cfg):
    bull = side == 'CE'
    reasons = []

    def chk(cond, label):
        if not bool(cond): reasons.append(label)
    chk(pd.notna(r.ema200) and ((r.Close > r.ema200) if bull else (r.Close < r.ema200)), 'Price on wrong side of EMA200')
    chk(r.st_dir == (1 if bull else -1), 'Supertrend against')
    chk(pd.notna(r.adx) and r.adx > 22, 'ADX <= 22')
    chk(pd.notna(r.prev_adx) and r.adx > r.prev_adx, 'ADX not rising')
    chk((r.pdi > r.mdi) if bull else (r.mdi > r.pdi), '+DI/-DI against')
    chk(bool(r.cross_up or r.break_up) if bull else bool(r.cross_down or r.break_down), 'No trigger today')
    chk((r.rsi > 55) if bull else (r.rsi < 45), 'RSI not confirming')
    chk(r.Volume > 0 and r.vol20 > 0 and r.Volume > r.vol20, 'Volume not above 20d average')
    chk(pd.notna(r.cpr_width) and r.cpr_width <= cfg['max_weekly_cpr_width'], 'Weekly CPR too wide')
    return reasons


def flow_bias(side, fii, dii):
    if fii is None or dii is None:
        return 'unknown', False
    if fii > 0 and dii > 0: label = 'FII+DII buying'
    elif fii < 0 and dii < 0: label = 'FII+DII selling'
    else: label = f"mixed (FII {'+' if fii > 0 else '-'}, DII {'+' if dii > 0 else '-'})"
    return label, (side == 'CE' and label == 'FII+DII selling') or (side == 'PE' and label == 'FII+DII buying')


def analyze_instrument(x, daily, cfg, fii, dii, secs=0.0):
    sym = x['symbol']
    if len(daily) < 215:
        raise ValueError(f'Only {len(daily)} candles (need 215+)')
    d = annotate(indicators(daily), cfg, sym)
    if d.empty or pd.isna(d.iloc[-1].ema200):
        raise ValueError('Indicators unavailable on latest candle')
    r = d.iloc[-1]
    side = r.signal or r.side
    best = int(r.score_CE if side == 'CE' else r.score_PE)
    bull = side == 'CE'
    volavail = bool(r.Volume > 0 and r.vol20 > 0)
    volok = bool(volavail and r.Volume > r.vol20)
    entry, risk = float(r.Close), float(r.atr) * 1.5
    direction = 1 if bull else -1

    month_start = d.index[-1].normalize().replace(day=1)
    mr = d.loc[month_start:]
    month_open = float(mr.iloc[0].Open)
    mtd = (entry / month_open - 1) * 100 if month_open > 0 else np.nan
    trig_mask = (mr.cross_up | mr.break_up) if bull else (mr.cross_down | mr.break_down)
    ev = mr[trig_mask]
    last_trigger = ''
    if len(ev):
        e = ev.iloc[-1]
        crossed = bool(e.cross_up if bull else e.cross_down)
        broke = bool(e.break_up if bull else e.break_down)
        kind = 'EMA9/21 cross + CPR breakout' if crossed and broke else ('EMA9/21 cross' if crossed else 'Weekly CPR/R3 breakout')
        last_trigger = f"{ev.index[-1].strftime('%Y-%m-%d')} {kind}"

    directional = bool((r.Close > r.ema200 and r.st_dir == 1 and r.rsi > 55 and r.pdi > r.mdi) if bull else
                       (r.Close < r.ema200 and r.st_dir == -1 and r.rsi < 45 and r.mdi > r.pdi))
    monthly_trend = ('BULLISH' if (r.Close > r.ema50 and mtd > 0 and r.st_dir == 1) else
                     ('BEARISH' if (r.Close < r.ema50 and mtd < 0 and r.st_dir == -1) else 'SIDEWAYS/MIXED'))
    consistent = monthly_trend == ('BULLISH' if bull else 'BEARISH')
    fbias, fopp = flow_bias(side, fii, dii)
    ext_atr = abs(r.Close - r.ema21) / r.atr if r.atr > 0 else np.nan
    extended = bool(pd.notna(ext_atr) and ext_atr > cfg['max_ext_atr'])

    blocks = []
    if not volavail: blocks.append('No volume data')
    elif not volok: blocks.append('Volume below 20d avg')
    if pd.isna(r.cpr_width) or r.cpr_width > cfg['max_weekly_cpr_width']: blocks.append('Weekly CPR too wide')
    if pd.isna(r.adx) or r.adx <= 22: blocks.append('ADX <= 22')
    elif pd.isna(r.prev_adx) or r.adx <= r.prev_adx: blocks.append('ADX not rising')
    if not directional: blocks.append('Trend/RSI/DI disagree')
    if cfg['require_monthly'] and not consistent: blocks.append('Monthly trend disagrees')
    if any(abs((d.index[-1] - pd.Timestamp(e).normalize()).days) <= 3 for e in cfg.get('event_blackout_dates', {}).get(sym, [])):
        blocks.append('Event blackout')
    if cfg['flow_gate'] and fopp: blocks.append('FII+DII flow opposes')

    fresh = bool(r.signal) and best >= cfg['min_score'] and not blocks
    month_ok = (not fresh) and best >= cfg['month_min_score'] and bool(last_trigger) and not blocks and not extended
    status = ('FRESH ENTRY' if fresh else 'MONTH SETUP' if month_ok else
              'WATCHLIST' if (best >= cfg['month_min_score'] and directional) else 'REJECTED')
    if status == 'WATCHLIST' and extended:
        blocks.append(f'Extended {ext_atr:.1f} ATR from EMA21')
    reasons = rejection_reasons(r, side, cfg)
    row = {
        'Symbol': sym, 'Date': d.index[-1].strftime('%Y-%m-%d'), 'Side': side, 'Score': best,
        'Status': status, 'Eligible': status in ('FRESH ENTRY', 'MONTH SETUP'),
        'Monthly trend': monthly_trend, 'MTD %': round(mtd, 2), 'Month trigger': last_trigger,
        'Month first candle': mr.index[0].date().isoformat(),
        'Close': round(entry, 2), 'Underlying SL': round(entry - direction * risk, 2),
        'Target 1 (1R)': round(entry + direction * risk, 2), 'Target 2 (2R)': round(entry + direction * 2 * risk, 2),
        'ADX': round(float(r.adx), 1), 'RSI': round(float(r.rsi), 1),
        'CPR width %': round(float(r.cpr_width) * 100, 2) if pd.notna(r.cpr_width) else None,
        'Daily source': daily.attrs.get('candle_source', 'Dhan'),
        'Volume OK': volok, 'Volume source': daily.attrs.get('vol_src', 'spot/equity') if volavail else 'Unavailable',
        'Flow bias (market)': fbias, 'Hard blocks': '; '.join(dict.fromkeys(blocks)),
        'Today gates failed': '; '.join(reasons) if status != 'FRESH ENTRY' else '',
        'Lot size': x.get('lot'), 'Load sec': round(secs, 2),
        'sid': x['sid'], 'segment': x['segment'], 'instrument': x['instrument'],
    }
    hist = [{'Symbol': sym, 'Date': dt.strftime('%Y-%m-%d'), 'Side': s.signal, 'Score': int(s.score), 'Close': float(s.Close)}
            for dt, s in d.tail(cfg['scan_days']).iterrows() if s.signal]
    return row, hist


# =====================================================================================
# 3. OPTIONS: expiry, strike, sizing, costs
# =====================================================================================
def pick_monthly_expiry(expiries, min_days, today=None):
    today = today or datetime.now(IST).date()
    dates = sorted({pd.Timestamp(e).date() for e in expiries})
    last = {}
    for dt in dates:
        last[(dt.year, dt.month)] = dt            # ascending -> keeps month-end expiry
    monthly = sorted(dt for dt in last.values() if dt.day >= 22)   # month-end expiry always falls on day 22+
    ok = [dt for dt in monthly if (dt - today).days >= min_days]
    return (ok[0] if ok else None), monthly


def parse_chain(resp):
    data = resp.get('data', resp) if isinstance(resp, dict) else {}
    spot = num(data.get('last_price'))
    rows = []
    for k, v in (data.get('oc') or {}).items():
        strike = num(k)
        for typ in ('ce', 'pe'):
            leg = v.get(typ)
            if not leg:
                continue
            g = leg.get('greeks') or {}
            rows.append({'strike': strike, 'type': typ.upper(), 'ltp': num(leg.get('last_price')),
                         'bid': num(leg.get('top_bid_price')), 'ask': num(leg.get('top_ask_price')),
                         'bid_qty': num(leg.get('top_bid_quantity')), 'ask_qty': num(leg.get('top_ask_quantity')),
                         'previous_oi': num(leg.get('previous_oi')), 'security_id': leg.get('security_id'),
                         'delta': num(g.get('delta')), 'theta': num(g.get('theta')), 'gamma': num(g.get('gamma')), 'vega': num(g.get('vega')), 'iv': num(leg.get('implied_volatility')),
                         'oi': num(leg.get('oi')), 'volume': num(leg.get('volume'))})
    return spot, pd.DataFrame(rows)


def select_contract(df, side, spot, p):
    """ATM or 1-strike ITM, |delta| in range, acceptable spread, liquid. Never forces a trade."""
    typ = side
    sub = df[(df.type == typ) & (df.ltp > 0)].sort_values('strike')
    strikes = sorted(df.strike.unique())
    if sub.empty or not strikes or not (spot > 0):
        return None, pd.DataFrame(), 'Option chain has no priced strikes'
    atm = min(strikes, key=lambda s: abs(s - spot))
    i = strikes.index(atm)
    itm = strikes[i - 1] if side == 'CE' and i > 0 else (strikes[i + 1] if side == 'PE' and i + 1 < len(strikes) else None)
    cands = sub[sub.strike.isin([s for s in (atm, itm) if s is not None])].copy()
    cands['where'] = np.where(cands.strike == atm, 'ATM', '1 ITM')
    mid = (cands.ask + cands.bid) / 2
    cands['spread %'] = np.where((cands.bid > 0) & (cands.ask > 0), (cands.ask - cands.bid) / mid * 100, np.nan)
    cands['|delta|'] = cands.delta.abs()
    reasons = []

    def good(r):
        if not (p['delta_lo'] <= r['|delta|'] <= p['delta_hi']): return False
        if pd.isna(r['spread %']) or r['spread %'] > p['max_spread']: return False
        return r.oi > 0 and r.volume > 0
    ok = cands[cands.apply(good, axis=1)] if len(cands) else cands
    if ok.empty:
        for _, r in cands.iterrows():
            why = []
            if pd.isna(r['|delta|']) or not (p['delta_lo'] <= r['|delta|'] <= p['delta_hi']): why.append(f"delta {r['|delta|']:.2f}" if pd.notna(r['|delta|']) else 'delta missing')
            if pd.isna(r['spread %']) or r['spread %'] > p['max_spread']: why.append('spread ' + (f"{r['spread %']:.1f}%" if pd.notna(r['spread %']) else 'n/a'))
            if not (r.oi > 0 and r.volume > 0): why.append('no OI/volume')
            reasons.append(f"{r['where']} {r.strike:g}: " + ', '.join(why))
        return None, cands, 'No ATM/1-ITM strike passes delta/spread/liquidity -> ' + ' | '.join(reasons)
    best = ok.iloc[(ok['|delta|'] - 0.575).abs().argsort()[:1]].iloc[0]
    return best, cands, ''


def size_position(capital, risk_pct, premium, lot, sl_frac, max_exposure_pct, max_trade_rupees=35000):
    if not (premium > 0 and lot > 0):
        return 0, 0.0, 0.0, 'Premium or lot size missing'
    risk_budget = capital * risk_pct / 100
    by_risk = math.floor(risk_budget / (premium * sl_frac * lot))
    by_exp = math.floor(min(capital * max_exposure_pct / 100, max_trade_rupees) / (premium * lot))
    lots = max(0, min(by_risk, by_exp))
    note = '' if lots >= 1 else (f'One lot costs Rs {premium * lot:,.0f} (max loss Rs {premium * sl_frac * lot:,.0f}); '
                                  f'risk budget Rs {risk_budget:,.0f}, portfolio exposure cap Rs {capital * max_exposure_pct / 100:,.0f}, per-trade cap Rs {max_trade_rupees:,.0f} -> NO TRADE')
    return lots, lots * premium * lot, lots * premium * sl_frac * lot, note


def option_costs(entry_prem, exit_prem, qty):
    """Approximate Zerodha option costs (rates change - verify on your contract note)."""
    buy, sell = entry_prem * qty, exit_prem * qty
    turnover = buy + sell
    brokerage = min(20.0, 0.0003 * buy) + min(20.0, 0.0003 * sell)
    stt = 0.001 * sell
    txn = 0.0003503 * turnover
    sebi = 1e-6 * turnover
    stamp = 0.00003 * buy
    gst = 0.18 * (brokerage + txn + sebi)
    return round(brokerage + stt + txn + sebi + stamp + gst, 2)


def option_for_setup(api, row, p, seg_memory):
    """Fetch expiries + chain, return a dict describing the proposed contract (or why not)."""
    sym, side = row['Symbol'], row['Side']
    sid = str(row['sid'])
    segs = ['IDX_I'] if row['segment'] == 'IDX_I' else ['NSE_EQ', 'NSE_FNO']
    err, expiries, seg_used = '', None, None
    for seg in segs:
        try:
            resp = api.expiries(sid, seg)
            expiries = resp.get('data', resp) if isinstance(resp, dict) else resp
            if expiries:
                seg_used = seg
                if row['segment'] != 'IDX_I': seg_memory['stock'] = seg
                break
        except Exception as e:
            err = str(e)
    base = {'Symbol': sym, 'Side': side, 'Verdict': 'SKIP', 'Note': '', 'chain_sid': sid, 'chain_seg': seg_used or ''}
    if not expiries:
        base['Note'] = f'Expiry list unavailable: {err or "empty"}'
        return base
    exp, monthlies = pick_monthly_expiry(expiries, p['min_days'])
    if exp is None:
        base['Note'] = f'No monthly expiry with >= {p["min_days"]} days left (monthlies: {[m.isoformat() for m in monthlies[:3]]})'
        return base
    try:
        spot, chain = parse_chain(api.chain(sid, seg_used, exp.isoformat()))
    except Exception as e:
        base['Note'] = f'Option chain failed: {e}'
        return base
    pick, cands, why = select_contract(chain, side, spot or float(row['Close']), p)
    base.update({'Expiry': exp.isoformat(), 'Days left': (exp - datetime.now(IST).date()).days, '_cand': cands})
    if pick is None:
        base['Note'] = why
        return base
    premium = float(pick.ask) if pick.ask > 0 else float(pick.ltp)
    lot = num(row.get('Lot size'))
    lots, outlay, maxloss, note = size_position(p['capital'], p['risk_pct'], premium, lot if lot == lot else 0, p['sl_frac'], p['max_exposure'], p['max_trade_rupees'])
    strike = float(pick.strike)
    delta = abs(float(pick.delta)) * (1 if side == 'CE' else -1)
    base.update({
        'Contract': f"{sym} {exp.strftime('%d%b%Y').upper()} {strike:g} {side}", 'Strike': strike, 'Where': pick['where'],
        'Entry premium (ask)': round(premium, 2), 'LTP': round(float(pick.ltp), 2), 'Bid': pick.bid, 'Ask': pick.ask,
        'Spread %': round(float(pick['spread %']), 2), 'Delta': round(delta, 3), 'IV %': round(float(pick.iv), 1) if pick.iv == pick.iv else None,
        'OI': pick.oi, 'Volume': pick.volume, 'Theta': pick.theta, 'Gamma': pick.gamma, 'Vega': pick.vega, 'Lot size': lot if lot == lot else None, 'Lots': lots,
        'Outlay Rs': round(outlay), 'Max loss Rs (SL)': round(maxloss),
        'Premium SL': round(premium * (1 - p['sl_frac']), 2), 'Premium T1 (1R)': round(premium * (1 + p['sl_frac']), 2),
        'Premium T2 (2R)': round(premium * (1 + 2 * p['sl_frac']), 2),
    })
    if lot != lot:
        base['Note'] = 'Lot size not in Dhan master - enter it manually in the Paper tab.'
        base['Verdict'] = 'CHECK LOT'
    elif lots < 1:
        base['Note'] = note
    else:
        base['Verdict'] = 'TRADE'
        base['Note'] = 'IV percentile is not available from Dhan - check IV manually before real trading.'
    return base


# =====================================================================================
# 4. PAPER TRADING
# =====================================================================================
JOURNAL_COLS = ['id', 'opened_at', 'signal_date', 'symbol', 'side', 'status', 'setup', 'score',
                'underlying_entry', 'underlying_sl', 'underlying_t1', 'underlying_t2',
                'contract', 'expiry', 'strike', 'lot_size', 'lots', 'entry_premium', 'entry_delta', 'entry_iv',
                'sl_frac', 'sid', 'segment', 'instrument', 'chain_sid', 'chain_seg',
                'stop_current', 't1_hit', 'days_held', 'last_update', 'last_underlying', 'last_premium',
                'premium_source', 'unrealized_pnl', 'closed_at', 'exit_reason', 'exit_underlying', 'exit_premium',
                'underlying_points', 'r_underlying', 'gross_pnl', 'costs', 'net_pnl', 'notes']


TEXT_COLS = ['id', 'opened_at', 'signal_date', 'symbol', 'side', 'status', 'setup', 'contract', 'expiry', 'sid', 'segment',
             'instrument', 'chain_sid', 'chain_seg', 'last_update', 'premium_source', 'closed_at', 'exit_reason', 'notes']


def normalize_journal(df):
    df = df.copy() if df is not None else pd.DataFrame()
    for c in JOURNAL_COLS:
        if c not in df.columns:
            df[c] = np.nan
    df = df[JOURNAL_COLS].copy()
    for c in TEXT_COLS:                       # text columns must be object dtype (empty CSV columns load as float)
        df[c] = df[c].astype(object)
    for c in ('id', 'sid', 'chain_sid'):
        df[c] = df[c].map(lambda v: v if pd.isna(v) else str(v).replace('.0', '') if str(v).endswith('.0') else str(v)).astype(object)
    df['t1_hit'] = df['t1_hit'].map(lambda v: str(v).strip().lower() in ('true', '1', '1.0'))
    return df.reset_index(drop=True)


class JournalStore:
    """Private GitHub Gist when GITHUB_TOKEN is set (permanent), else a local file (lost on app reboot)."""
    def __init__(self, token='', gist_id=''):
        self.token, self.gist_id = (token or '').strip(), (gist_id or '').strip()
        self.h = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json'}

    @property
    def mode(self):
        return 'gist' if self.token else 'local'

    def load(self):
        try:
            if self.token and self.gist_id:
                r = requests.get(f'https://api.github.com/gists/{self.gist_id}', headers=self.h, timeout=20)
                r.raise_for_status()
                f = r.json()['files'].get(GIST_FILE)
                if f is None:
                    return normalize_journal(None), 'Gist found but empty'
                text = requests.get(f['raw_url'], headers=self.h, timeout=20).text if f.get('truncated') else f['content']
                return normalize_journal(pd.read_csv(io.StringIO(text))), 'Loaded from GitHub Gist'
            if os.path.exists(JOURNAL_FILE):
                return normalize_journal(pd.read_csv(JOURNAL_FILE)), 'Loaded from local file (not permanent on Streamlit Cloud)'
        except Exception as e:
            return normalize_journal(None), f'Load failed: {str(e)[:150]}'
        return normalize_journal(None), ('GITHUB_TOKEN set but PAPER_GIST_ID empty: first save creates the Gist'
                                         if self.token else 'No saved journal yet')

    def save(self, df):
        text = normalize_journal(df).to_csv(index=False)
        try:
            with open(JOURNAL_FILE, 'w', encoding='utf-8') as fh:
                fh.write(text)
        except Exception:
            pass
        if not self.token:
            return 'Saved locally only (add GITHUB_TOKEN for permanent storage)'
        try:
            if self.gist_id:
                r = requests.patch(f'https://api.github.com/gists/{self.gist_id}', headers=self.h, timeout=20,
                                   json={'files': {GIST_FILE: {'content': text}}})
            else:
                r = requests.post('https://api.github.com/gists', headers=self.h, timeout=20,
                                  json={'description': 'Dhan swing options paper journal', 'public': False,
                                        'files': {GIST_FILE: {'content': text}}})
                if r.ok:
                    self.gist_id = r.json()['id']
            r.raise_for_status()
            return f'Saved to GitHub Gist {self.gist_id}'
        except Exception as e:
            return f'Gist save failed (saved locally): {str(e)[:150]}'


def model_premium(prem0, delta, u0, u):
    """First-order estimate: premium + delta x underlying move. Ignores theta/gamma/IV change."""
    return max(0.05, prem0 + delta * (u - u0))


def evaluate_trade(t, d, prem_sl_frac, max_hold=10):
    """Replay all completed daily bars after the signal date. Stateless -> safe to run repeatedly."""
    sign = 1 if t['side'] == 'CE' else -1
    entry, sl0, t1, t2 = (float(t[k]) for k in ('underlying_entry', 'underlying_sl', 'underlying_t1', 'underlying_t2'))
    prem0 = num(t['entry_premium'])
    delta = num(t['entry_delta'], 0.55 * sign) or 0.55 * sign
    exp = pd.Timestamp(t['expiry']).date() if isinstance(t['expiry'], str) and t['expiry'] else None
    risk_pts = abs(entry - sl0)
    u_prem_sl = entry - prem_sl_frac * prem0 / delta if prem0 == prem0 else None   # underlying level where premium is -30%
    stop, t1_hit, days, ex = sl0, False, 0, None
    bars = d[d.index > pd.Timestamp(t['signal_date']).normalize()]
    for dt, r in bars.iterrows():
        days += 1
        o, h, l, c = float(r.Open), float(r.High), float(r.Low), float(r.Close)
        eff, why = stop, 'STOP-LOSS (1.5 ATR / trailed)'
        if u_prem_sl is not None and ((sign == 1 and u_prem_sl > eff) or (sign == -1 and u_prem_sl < eff)):
            eff, why = u_prem_sl, 'PREMIUM STOP (-30%)'
        if (sign == 1 and l <= eff) or (sign == -1 and h >= eff):
            px = min(eff, o) if sign == 1 else max(eff, o)
            ex = (dt, px, why + (' gap' if px != eff else ''), 'prem_sl' if why.startswith('PREMIUM') and px == eff else 'model')
            break
        if (sign == 1 and h >= t2) or (sign == -1 and l <= t2):
            ex = (dt, max(t2, o) if sign == 1 else min(t2, o), 'TARGET 2 (2R)', 'model')
            break
        if not t1_hit and ((sign == 1 and h >= t1) or (sign == -1 and l <= t1)):
            t1_hit = True
            stop = max(stop, entry) if sign == 1 else min(stop, entry)       # breakeven at 1R
        if t1_hit and pd.notna(r.ema21):                                      # trail with EMA21
            if sign == 1 and r.ema21 < c: stop = max(stop, float(r.ema21))
            if sign == -1 and r.ema21 > c: stop = min(stop, float(r.ema21))
        if bool(r.cross_down if sign == 1 else r.cross_up) or int(r.st_dir) == -sign:
            ex = (dt, c, 'SIGNAL EXIT (EMA cross / Supertrend flip)', 'model'); break
        if exp is not None and (exp - dt.date()).days <= 5:
            ex = (dt, c, 'EXPIRY WINDOW (<=5 days)', 'model'); break
        if days >= max_hold:
            ex = (dt, c, f'MAX HOLD {max_hold} DAYS', 'model'); break
        if days >= 5 and not t1_hit and (c - entry) * sign < 0.5 * risk_pts:
            ex = (dt, c, 'TIME EXIT (no progress in 5 days)', 'model'); break
    last_u = float(bars.iloc[-1].Close) if len(bars) else entry
    res = {'stop_current': round(stop, 2), 't1_hit': t1_hit, 'days_held': days, 'last_underlying': last_u,
           'last_update': bars.index[-1].strftime('%Y-%m-%d') if len(bars) else '', 'exit': None}
    if prem0 == prem0:
        res['model_premium'] = model_premium(prem0, delta, entry, last_u)
    if ex:
        dt, px, reason, kind = ex
        res['exit'] = {'date': dt.strftime('%Y-%m-%d'), 'px': px, 'reason': reason,
                       'prem': prem0 * (1 - prem_sl_frac) if kind == 'prem_sl' else (model_premium(prem0, delta, entry, px) if prem0 == prem0 else np.nan)}
    return res


def close_row(df, idx, exit_date, exit_u, exit_prem, reason, slip_pct):
    t = df.loc[idx]
    sign = 1 if t['side'] == 'CE' else -1
    entry = float(t['underlying_entry'])
    pts = (exit_u - entry) * sign
    risk_pts = abs(entry - float(t['underlying_sl'])) or np.nan
    df.loc[idx, ['status', 'closed_at', 'exit_reason', 'exit_underlying', 'underlying_points', 'r_underlying']] = \
        ['CLOSED', exit_date, reason, round(exit_u, 2), round(pts, 2), round(pts / risk_pts, 2) if risk_pts == risk_pts else np.nan]
    prem0, lots, lot = num(t['entry_premium']), num(t['lots'], 0), num(t['lot_size'], 0)
    qty = lots * lot
    if prem0 == prem0 and qty > 0 and exit_prem == exit_prem:
        eff_exit = exit_prem * (1 - slip_pct / 100)
        gross = (eff_exit - prem0) * qty
        costs = option_costs(prem0, eff_exit, qty)
        df.loc[idx, ['exit_premium', 'gross_pnl', 'costs', 'net_pnl', 'unrealized_pnl']] = \
            [round(exit_prem, 2), round(gross, 2), costs, round(gross - costs, 2), 0.0]
    return df


def journal_stats(df):
    c = df[df.status == 'CLOSED'].copy()
    if c.empty:
        return None
    pnl = pd.to_numeric(c.net_pnl, errors='coerce')
    win = np.where(pnl.notna(), pnl > 0, pd.to_numeric(c.underlying_points, errors='coerce') > 0)
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    eq = pnl.fillna(0).cumsum()
    return {'Closed trades': len(c), 'Win rate %': round(100 * float(np.mean(win)), 1),
            'Net P&L Rs (option trades)': round(float(pnl.sum()), 2),
            'Profit factor': round(gp / gl, 2) if gl > 0 else ('inf' if gp > 0 else 0),
            'Avg R (underlying)': round(float(pd.to_numeric(c.r_underlying, errors='coerce').mean()), 2),
            'Expectancy Rs / trade': round(float(pnl.mean()), 2) if pnl.notna().any() else None,
            'Max drawdown Rs': round(float((eq - eq.cummax()).min()), 2)}, eq


def entry_guard(df, sym, side, new_exposure, capital, max_open, max_exposure_pct, weekly_limit_pct, max_trade_rupees=35000):
    """Spec risk rules. Returns list of blocking messages."""
    msgs = []
    openr = df[df.status == 'OPEN']
    if new_exposure > max_trade_rupees: msgs.append(f'Per-trade premium cap Rs {max_trade_rupees:,.0f} exceeded')
    if len(openr) >= max_open: msgs.append(f'Max {max_open} open trades reached')
    if sym in set(openr.symbol): msgs.append('Already open in this instrument')
    corr = [s for s in openr.symbol if s in INDEX_GROUP]
    if sym in INDEX_GROUP and corr: msgs.append(f'Highly correlated with open index trade ({corr[0]})')
    exposure = float((pd.to_numeric(openr.entry_premium, errors='coerce') * pd.to_numeric(openr.lot_size, errors='coerce') *
                      pd.to_numeric(openr.lots, errors='coerce')).sum())
    if exposure + new_exposure > capital * max_exposure_pct / 100:
        msgs.append(f'Premium exposure would exceed {max_exposure_pct:g}% of capital')
    closed = df[df.status == 'CLOSED'].copy()
    if not closed.empty:
        closed['ts'] = pd.to_datetime(closed.closed_at, errors='coerce')
        wk_start = pd.Timestamp(datetime.now(IST).date() - timedelta(days=datetime.now(IST).weekday()))
        wk = pd.to_numeric(closed[closed.ts >= wk_start].net_pnl, errors='coerce').sum()
        if wk <= -capital * weekly_limit_pct / 100: msgs.append(f'Weekly loss limit {weekly_limit_pct:g}% hit (Rs {wk:,.0f}) - stop trading this week')
        last3 = closed.sort_values('ts').tail(3)
        res = pd.to_numeric(last3.net_pnl, errors='coerce').fillna(pd.to_numeric(last3.underlying_points, errors='coerce'))
        if len(last3) == 3 and (res < 0).all(): msgs.append('3 consecutive losses - pause and review')
    return msgs


def live_premiums(api, df):
    """One chain request per (symbol, expiry). Returns {trade_id: (ltp, error)}."""
    out = {}
    o = df[(df.status == 'OPEN') & df.expiry.notna() & df.chain_seg.notna() & (pd.to_numeric(df.entry_premium, errors='coerce') > 0)]
    for (sym, exp, sid, seg), g in o.groupby(['symbol', 'expiry', 'chain_sid', 'chain_seg']):
        try:
            _, ch = parse_chain(api.chain(str(sid).replace('.0', ''), seg, exp))
            for _, t in g.iterrows():
                m = ch[(ch.strike == float(t.strike)) & (ch.type == t.side)]
                out[t['id']] = (float(m.iloc[0].ltp), '') if len(m) and m.iloc[0].ltp > 0 else (np.nan, 'strike not in chain')
        except Exception as e:
            for tid in g['id']:
                out[tid] = (np.nan, str(e)[:100])
    return out


# =====================================================================================
# FII / DII (market-wide, NOT from Dhan)
# =====================================================================================
@st.cache_data(ttl=900, show_spinner=False)
def fetch_fii_dii_nse():
    try:
        hdr = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Accept': 'application/json',
               'Referer': 'https://www.nseindia.com/reports/fii-dii'}
        s = requests.Session()
        s.get('https://www.nseindia.com/', headers=hdr, timeout=12)
        r = s.get('https://www.nseindia.com/api/fiidiiTradeReact', headers=hdr, timeout=12)
        r.raise_for_status()
        out = {}
        for it in r.json():
            cat = str(it.get('category', '')).upper()
            net = float(str(it.get('netValue', '0')).replace(',', ''))
            if 'FII' in cat or 'FPI' in cat: out['fii'], out['date'] = net, it.get('date', '')
            elif 'DII' in cat: out['dii'] = net
        return out if 'fii' in out and 'dii' in out else {'error': 'unexpected NSE response'}
    except Exception as e:
        return {'error': str(e)[:150]}


# =====================================================================================
# 5. UI
# =====================================================================================
STATUS_RANK = {'FRESH ENTRY': 0, 'MONTH SETUP': 1, 'WATCHLIST': 2, 'REJECTED': 3}
HIDE = ['sid', 'segment', 'instrument']



def apply_dark_theme():
    st.markdown("""<style>
    .stApp {background: radial-gradient(ellipse at 85% 0%,#14233a 0%,#080c17 45%,#05070e 100%); color:#e8f2ff;}
    [data-testid="stSidebar"] {background:linear-gradient(180deg,#101b31,#070c18); border-right:1px solid #26354c;}
    [data-testid="stHeader"] {background:transparent;}
    h1,h2,h3 {color:#eaf5ff!important;letter-spacing:-.03em;}
    h1 {background:linear-gradient(90deg,#6ee7ff,#b69bff,#ff76c7);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
    div[data-testid="stMetric"] {background:#101d32;border:1px solid #28435b;border-radius:16px;padding:14px;}
    div[data-testid="stMetricValue"] {color:#68e8ff;}
    div[data-testid="stTabs"] button[aria-selected="true"] {color:#69f4d3!important;border-bottom-color:#69f4d3!important;}
    div[data-testid="stDataFrame"] {border:1px solid #263a55;border-radius:12px;overflow:hidden;}
    div.stButton>button[kind="primary"] {background:linear-gradient(110deg,#1763ed,#8546d8);color:white;border:0;border-radius:11px;}
    .signal-card {background:linear-gradient(140deg,#15223a,#0c1426);border:1px solid #324862;border-radius:18px;padding:18px;min-height:220px;margin-bottom:12px;box-shadow:0 9px 25px #0004;}
    .signal-card.ce {border-top:3px solid #28e2ad;} .signal-card.pe {border-top:3px solid #ff5f94;}
    .signal-name {font-size:1.25rem;font-weight:800;color:#f2f8ff;}
    .signal-sub {color:#a9bdd5;font-size:.8rem;margin:6px 0 12px;}
    .signal-score {font-size:2rem;font-weight:800;color:#80e8ff;}
    .signal-pill {display:inline-block;background:#263958;color:#dcf6ff;padding:5px 10px;border-radius:20px;font-size:.75rem;}
    .signal-levels {color:#d2e4f7;font-size:.82rem;line-height:1.9;}
    .signal-good {color:#37e6b1;} .signal-bad {color:#ff9dba;}
    /* Explicit text colors: Streamlit's default light-theme text was black on dark. */
    .stApp, .stApp p, .stApp span, .stApp label, .stApp li,
    [data-testid="stSidebar"] p, [data-testid="stSidebar"] span,
    [data-testid="stSidebar"] label, [data-testid="stWidgetLabel"] *,
    [data-testid="stMarkdownContainer"] {color:#eaf2ff;}
    [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {color:#a8bed9!important;}
    [data-testid="stSidebar"] [data-testid="stWidgetLabel"] *,
    [data-testid="stSidebar"] label {color:#d9e8ff!important;}
    [data-testid="stTabs"] button, [data-testid="stTabs"] button p,
    [data-testid="stTabs"] button span {color:#c6d7f0!important;}
    [data-testid="stTabs"] button[aria-selected="true"] *,
    [data-testid="stTabs"] button[aria-selected="true"] {color:#70f3da!important;}
    [data-testid="stMetricLabel"] *, [data-testid="stMetricLabel"] {color:#b9cfe9!important;}
    [data-testid="stMetricValue"] *, [data-testid="stMetricValue"] {color:#69eaff!important;}
    [data-testid="stMetricDelta"] * {color:#a9d8ff!important;}
    [data-testid="stCheckbox"] label *, [data-testid="stRadio"] label * {color:#eaf2ff!important;}
    [data-testid="stSlider"] *, [data-testid="stNumberInput"] label *,
    [data-testid="stSelectbox"] label * {color:#dcecff!important;}
    [data-testid="stNumberInput"] input, [data-testid="stTextInput"] input,
    [data-testid="stTextArea"] textarea, [data-testid="stSelectbox"] div[data-baseweb="select"] > div {
        background:#17263d!important; color:#f3f8ff!important; border-color:#375170!important;
    }
    [data-testid="stNumberInput"] input, [data-testid="stTextInput"] input {color:#f3f8ff!important;}
    [data-testid="stDataFrame"] {background:#101c2e!important;}
    [data-testid="stAlert"] {border-radius:12px;}
    div.stButton > button {background:#1c3150;color:#f4faff!important;border:1px solid #47658b;border-radius:10px;}
    div.stButton > button * {color:#f4faff!important;}
    div.stButton > button:hover {border-color:#69eaff;color:#69eaff!important;}
    /* Charts rendered by Vega/Altair inherit theme from Streamlit config. */
    </style>""", unsafe_allow_html=True)


def eligible_card(row):
    def safe(x): return html.escape(str(x))
    side = str(row.get('Side', ''))
    cls = 'ce' if side == 'CE' else 'pe'
    name = safe(row.get('Symbol', 'Unknown'))
    score = safe(row.get('Score', '—'))
    status = safe(row.get('Status', '—'))
    trend = safe(row.get('Monthly trend', '—'))
    entry = num(row.get('Close'))
    stop = num(row.get('Underlying SL'))
    t1 = num(row.get('Target 1 (1R)'))
    t2 = num(row.get('Target 2 (2R)'))
    def price(v): return f'₹{v:,.2f}' if np.isfinite(v) else '—'
    return f"""<div class="signal-card {cls}">
      <div class="signal-name">{name} <span class="signal-pill">{safe(side)}</span></div>
      <div class="signal-sub">{status} · Monthly trend: {trend}</div>
      <div class="signal-sub">Candle checked: {safe(row.get('Date', '—'))} · {safe(row.get('Daily source', 'Dhan'))} · Trigger: {safe(row.get('Month trigger') or '—')}</div>
      <div class="signal-score">{score}<span style="font-size:.85rem;color:#9bb0ca"> / 10</span></div>
      <div class="signal-levels">Underlying entry: <b>{price(entry)}</b><br>
      Underlying SL: <b class="signal-bad">{price(stop)}</b><br>
      Target 1: <b class="signal-good">{price(t1)}</b> · Target 2: <b class="signal-good">{price(t2)}</b></div>
      <div class="signal-sub">Reference underlying levels; option premiums must be validated separately.</div>
    </div>"""

def candle_chart(df):
    """Native Streamlit/Altair OHLC chart; no Plotly or external JavaScript CDN."""
    if df is None or df.empty:
        st.info('No candle data available.')
        return
    source = df.reset_index().rename(columns={df.index.name or 'index': 'Date'}).copy()
    source['Date'] = pd.to_datetime(source['Date'])
    source['Direction'] = [candle_direction(op, cl) for op, cl in zip(source['Open'], source['Close'])]
    x = alt.X('Date:T', title='Trading date', axis=alt.Axis(format='%d %b', labelColor='#c6d7f0', titleColor='#c6d7f0'))
    y = alt.Y('Low:Q', title='Price (₹)', scale=alt.Scale(zero=False), axis=alt.Axis(labelColor='#c6d7f0', titleColor='#c6d7f0'))
    color = alt.Color('Direction:N', scale=alt.Scale(domain=list(CANDLE_COLORS), range=list(CANDLE_COLORS.values())), legend=None)
    tooltip = [alt.Tooltip('Date:T', format='%d %b %Y'), alt.Tooltip('Open:Q', format=',.2f'),
               alt.Tooltip('High:Q', format=',.2f'), alt.Tooltip('Low:Q', format=',.2f'),
               alt.Tooltip('Close:Q', format=',.2f'), alt.Tooltip('Volume:Q', format=',.0f'),
               alt.Tooltip('Direction:N', title='Candle direction')]
    base = alt.Chart(source).encode(x=x, color=color, tooltip=tooltip)
    wicks = base.mark_rule(strokeWidth=1.5).encode(y=y, y2='High:Q')
    bodies = base.mark_bar(size=6).encode(y=alt.Y('Open:Q', scale=alt.Scale(zero=False)), y2='Close:Q')
    flat = base.transform_filter(alt.datum.Direction == 'Neutral').mark_tick(
        orient='horizontal', size=9, thickness=2, opacity=1,
    ).encode(y=alt.Y('Close:Q', scale=alt.Scale(zero=False)))
    layers = [wicks, bodies, flat]
    for period, line_color in [(9, '#facc15'), (21, '#69b9ff'), (50, '#ff9f70'), (200, '#c5a0ff')]:
        field = f'ema{period}'
        if field in source:
            layers.append(alt.Chart(source.dropna(subset=[field])).mark_line(color=line_color, strokeWidth=1.6).encode(x=x, y=alt.Y(f'{field}:Q', scale=alt.Scale(zero=False)), tooltip=[alt.Tooltip('Date:T'), alt.Tooltip(f'{field}:Q', format=',.2f')]))
    chart = alt.layer(*layers).resolve_scale(y='shared').properties(height=430, background='#0b1424').configure_view(stroke=None)
    st.altair_chart(chart, use_container_width=True)
    st.caption(CANDLE_LEGEND + ' · EMA9 yellow · EMA21 blue · EMA50 orange · EMA200 purple')




@st.cache_resource
def swing_jobs():
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix='swing-scan'), RLock(), {}

def background_swing_scan(token, client_id, cache, targets, cutoff, scan_now_ist, cfg, blackouts, fii, dii):
    api = DhanAPI(token, client_id) if token else None
    results, hist, errors = [], [], []
    t0 = time.monotonic()
    ready, failed, yahoo_notes = scan_daily_sources(
        api, cache, targets, cutoff, scan_now_ist)
    for i, x in enumerate(targets, 1):
        sym = x['symbol']
        try:
            if sym in failed:
                raise failed[sym]
            daily, secs = ready[sym]
            c = dict(cfg); c['event_blackout_dates'] = {sym: blackouts}
            row, h = analyze_instrument(x, daily, c, fii, dii, secs)
            results.append(row); hist += h
        except Exception as e:
            stale = isinstance(e, MissingSessionCandle)
            errors.append({'Symbol': sym, 'Status': 'STALE / MISSING CANDLE' if stale else 'SCAN FAILED',
                           'Expected date': cutoff.isoformat(),
                           'Dhan candle date': e.actual.isoformat() if stale and e.actual else '',
                           'Yahoo status': yahoo_notes.get(sym, ''),
                           'Error': str(e)})
    df = pd.DataFrame(results)
    if not df.empty:
        df['_r'] = df.Status.map(STATUS_RANK)
        df = df.sort_values(['_r', 'Score', 'ADX'], ascending=[True, False, False]).drop(columns='_r').reset_index(drop=True)
    return df, (pd.DataFrame(hist).drop_duplicates(['Symbol','Date','Side']) if hist else pd.DataFrame()), pd.DataFrame(errors), len(targets), time.monotonic()-t0


@st.fragment(run_every=3)
def watch_swing_job(future):
    if not is_authenticated() or (future is not None and future.done()):
        st.rerun()


def main():
    st.set_page_config(page_title='Dhan Swing Options V3.8 Neon', page_icon='📊', layout='wide')
    require_password('Dhan Swing')
    apply_dark_theme()
    st.title('DHAN QUANT · SWING OPTIONS TERMINAL')
    st.caption('Dhan daily prices + Yahoo after-close fallback · Dhan option quotes · manual execution · no orders are ever sent · research tool, not advice, no guaranteed profit')

    secret = read_secret
    from worker_store import cloud_journal
    worker_mode=False
    token = secret('DHAN_ACCESS_TOKEN')
    client_id = secret('DHAN_CLIENT_ID')
    with st.sidebar:
        st.header('Dhan connection')
        st.caption('Dhan credentials configured on server.' if token and client_id else
                   'Dhan credentials missing in Streamlit Secrets. Yahoo may serve the after-close watchlist.')
        st.header('Risk')
        capital = st.number_input('Capital (Rs)', 10000, 100000000, 100000, 5000)
        risk_pct = st.slider('Risk per trade (%)', 1.0, 2.0, 1.0, 0.5)
        max_open = st.number_input('Max open trades', 1, 10, 3)
        max_exposure = st.slider('Max premium exposure (% capital)', 5, 50, 35)
        max_trade_rupees = st.number_input('Maximum premium per paper trade (Rs)', min_value=1000, max_value=1000000, value=35000, step=1000, help='Separate from stop-loss risk. A trade must satisfy both limits.')
        weekly_limit = st.slider('Weekly loss limit (% capital)', 1.0, 10.0, 4.0, 0.5)
        st.header('Signal rules')
        min_score = st.slider('Min score - fresh entry', 7, 10, 7)
        month_min = st.slider('Min score - month setup', 7, 10, 8)
        cpr_width = st.slider('Max weekly CPR width (%)', 0.1, 5.0, 1.5, 0.1) / 100
        require_monthly = st.checkbox('Require monthly trend to agree with side', True)
        max_ext = st.slider('Month setups: max distance from EMA21 (ATR)', 1.0, 5.0, 2.0, 0.5,
                            help='Stops chasing a stale trigger that already ran far away.')
        scan_days = st.selectbox('Historical-signal lookback (days)', [1, 5, 10, 20], index=3)
        events = st.text_area('Event / results blackout dates (YYYY-MM-DD, one per line, applies to all)', '')
        st.header('FII / DII (market-wide context)')
        use_flow = st.checkbox('Auto-fetch from NSE (best effort)', True)
        flow_gate = st.checkbox('Block CE when FII+DII both selling (PE: both buying)', False)
        fii_m = st.number_input('FII net Rs cr - manual override (0 = none)', value=0.0, step=100.0)
        dii_m = st.number_input('DII net Rs cr - manual override (0 = none)', value=0.0, step=100.0)
        st.header('Automatic paper simulation')
        auto_paper = st.checkbox('Auto-enter eligible option paper trades on SCAN NOW', False,
                                 help='Uses Dhan option-chain ask prices; never places broker orders. Requires persistent Gist for durable records.')
        auto_exit = st.checkbox('Update and exit paper positions on SCAN NOW', True,
                                help='Only use Dhan option-chain LTP; no model premium fills. Runs when you scan, not continuously.')
        st.header('Option rules')
        min_days = st.number_input('Min days to monthly expiry', 5, 45, 15)
        d_lo, d_hi = st.slider('|Delta| range', 0.30, 0.90, (0.50, 0.65), 0.01)
        max_spread = st.slider('Max bid-ask spread (%)', 1.0, 15.0, 5.0, 0.5)
        sl_frac = st.slider('Premium stop-loss (%)', 10, 50, 30) / 100
        slip = st.slider('Exit slippage (%)', 0.0, 3.0, 0.5, 0.1)

    opt_params = {'capital': capital, 'risk_pct': risk_pct, 'min_days': int(min_days), 'delta_lo': d_lo, 'delta_hi': d_hi,
                  'max_spread': max_spread, 'sl_frac': sl_frac, 'max_exposure': max_exposure, 'max_trade_rupees': max_trade_rupees}
    blackouts = [x.strip() for x in events.splitlines() if x.strip()]

    # ---- FII/DII resolution
    fii = dii = None
    flow_src = 'unknown'
    if fii_m or dii_m:
        fii, dii, flow_src = fii_m, dii_m, 'manual'
    elif use_flow:
        f = fetch_fii_dii_nse()
        if 'error' not in f:
            fii, dii, flow_src = f['fii'], f['dii'], f"NSE {f.get('date', '')}"
        else:
            flow_src = 'NSE fetch failed: ' + f['error']
    with st.sidebar:
        st.caption(f'Flow source: {flow_src}' + (f' | FII {fii:,.0f} cr, DII {dii:,.0f} cr' if fii is not None else ''))

    cfg = {'min_score': min_score, 'month_min_score': month_min, 'max_weekly_cpr_width': cpr_width,
           'require_monthly': require_monthly, 'max_ext_atr': max_ext, 'flow_gate': flow_gate,
           'scan_days': scan_days, 'event_blackout_dates': {}}

    # ---- session state
    ss = st.session_state
    scan_now_ist = datetime.now(IST)
    expected = last_complete_session(scan_now_ist)
    store = get_scan_store()
    skey = hashlib.sha256((token or 'none').encode()).hexdigest()[:12]
    scan_key = ('mtd-yahoo-v1', skey, expected.isoformat())
    if ss.get('scan_key') != scan_key:
        saved = store.get(scan_key, {})
        ss.scan, ss.history = saved.get('scan', pd.DataFrame()), saved.get('history', pd.DataFrame())
        ss.scan_errors = saved.get('errors', pd.DataFrame())
        ss.options = {}
        ss.scan_key = scan_key
    pool_exec,jobs_lock,jobs=swing_jobs()
    finished_for_owner=False
    with jobs_lock:
        # Only one scan per data account/session date; all viewers share its result.
        for key in list(jobs):
            if key!=scan_key and jobs[key][0].done():
                jobs.pop(key)
        pending=jobs.get(scan_key)
        if pending is not None and pending[0].done():
            future, owner_nonce=jobs.pop(scan_key)
            try:
                frame, history, errors, requested, seconds=future.result()
                store[scan_key]={'scan':frame,'history':history,'errors':errors,
                                 'summary':f'{len(frame)} of {requested} current candles · {seconds:.1f} seconds',
                                 'revision':id(future)}
                finished_for_owner=(owner_nonce==ss[NONCE])
            except Exception as exc:
                store[scan_key]={'scan':pd.DataFrame(),'history':pd.DataFrame(),
                                 'errors':pd.DataFrame([{'Error':type(exc).__name__}]),
                                 'summary':'Background scan failed. Please retry.','revision':id(future)}
        active=jobs.get(scan_key)
        saved=store.get(scan_key,{})
        if ss.get('scan_revision')!=saved.get('revision'):
            ss.scan=saved.get('scan',pd.DataFrame())
            ss.history=saved.get('history',pd.DataFrame())
            ss.scan_errors=saved.get('errors',pd.DataFrame())
            ss.options={}
            ss.scan_revision=saved.get('revision')
    watch_swing_job(active[0] if active else None)
    ss.setdefault('options', {})
    ss.setdefault('scan_errors', pd.DataFrame())
    ss.setdefault('seg_memory', {})
    ss.setdefault('actions', [])
    journal = JournalStore(secret('GITHUB_TOKEN'), secret('PAPER_GIST_ID'))
    if 'paper' not in ss:
        ss.paper, ss.paper_msg = (normalize_journal(None), '') if worker_mode else journal.load()
        ss.gist_id = journal.gist_id
    journal.gist_id = ss.get('gist_id', journal.gist_id)

    def persist():
        ss.paper_msg = journal.save(ss.paper)
        ss.gist_id = journal.gist_id

    cache = get_cache()
    t1, t2, t3, t4, t5, t6, t7 = st.tabs(['1 · Market scanner', '2 · Top CE/PE setups + options', '3 · Trade analysis', '4 · Paper trading', '5 · Eligible signal cards','6 · Trade Decision','7 · Call / Put Backtest'])

    def auto_paper_cycle(api, candidates=None):
        """Run only on explicit SCAN NOW; no broker orders and no modeled option fills."""
        notes = []
        today = datetime.now(IST).date()
        if auto_exit and not ss.paper.empty:
            open_indices = list(ss.paper.index[ss.paper.status == 'OPEN'])
            if open_indices:
                quotes = live_premiums(api, ss.paper)
                for idx in open_indices:
                    t = ss.paper.loc[idx]
                    ltp, err = quotes.get(t['id'], (np.nan, 'no quote'))
                    if not np.isfinite(num(ltp)) or num(ltp) <= 0:
                        notes.append(f"{t.symbol}: exit check skipped: {err or 'no valid Dhan premium'}")
                        continue
                    entry = num(t.entry_premium)
                    if not np.isfinite(entry) or entry <= 0:
                        continue
                    qty = num(t.lots, 0) * num(t.lot_size, 0)
                    ss.paper.loc[idx, ['last_premium', 'premium_source', 'unrealized_pnl', 'last_update']] = [
                        ltp, 'Dhan chain LTP (snapshot)', round((ltp-entry)*qty, 2), today.isoformat()]
                    sl = num(t.sl_frac, sl_frac)
                    exp = pd.to_datetime(t.expiry, errors='coerce')
                    expiry_days = (exp.date()-today).days if pd.notna(exp) else 999
                    held = (today-pd.to_datetime(t.opened_at).date()).days
                    reason = None
                    if ltp <= entry*(1-sl): reason = 'PAPER PREMIUM STOP'
                    elif ltp >= entry*(1+2*sl): reason = 'PAPER PREMIUM TARGET 2R'
                    elif expiry_days <= 5: reason = 'PAPER EXPIRY EXIT'
                    elif held >= 10: reason = 'PAPER TIME EXIT'
                    if reason:
                        # Snapshot prices are indicative, not executable historical fills.
                        u = num(t.last_underlying, num(t.underlying_entry))
                        close_row(ss.paper, idx, today.isoformat(), u, ltp, reason+' (Dhan LTP snapshot)', slip)
                        notes.append(f'{t.symbol}: simulated exit at Dhan LTP {ltp:.2f} ({reason})')
        if candidates is not None and auto_paper:
            if journal.mode != 'gist':
                notes.append('Automatic entries disabled: configure GITHUB_TOKEN + PAPER_GIST_ID for durable storage.')
            elif not client_id:
                notes.append('Automatic entries disabled: DHAN_CLIENT_ID missing.')
            else:
                eligible = candidates[candidates.Eligible & candidates['Daily source'].eq('Dhan')].sort_values(['Score','ADX'], ascending=[False,False])
                for _, row in eligible.iterrows():
                    sym = row['Symbol']
                    # Do not re-enter the same symbol/side/signal after closing it.
                    previous = ss.paper[(ss.paper.symbol == sym) & (ss.paper.side == row['Side']) &
                                        (ss.paper.signal_date.astype(str) == str(row['Date']))]
                    if not previous.empty:
                        continue
                    opt = option_for_setup(api, row.to_dict(), opt_params, ss.seg_memory)
                    ss.options[sym] = opt
                    if opt.get('Verdict') != 'TRADE':
                        notes.append(f"{sym}: skipped option: {opt.get('Note','not tradeable')}")
                        continue
                    prem, lot, lots = (num(opt.get('Entry premium (ask)')), int(num(opt.get('Lot size'),0)), int(num(opt.get('Lots'),0)))
                    if not (np.isfinite(prem) and prem>0 and lot>0 and lots>0 and num(opt.get('Ask'))>0):
                        notes.append(f'{sym}: missing verified ask, lot size or affordable position')
                        continue
                    exp = pd.to_datetime(opt.get('Expiry'), errors='coerce')
                    if pd.isna(exp) or (exp.date()-today).days < int(min_days):
                        continue
                    bad = entry_guard(ss.paper, sym, row['Side'], prem*lot*lots, capital, int(max_open), max_exposure, weekly_limit, max_trade_rupees)
                    if bad:
                        notes.append(f'{sym}: '+ '; '.join(bad))
                        continue
                    rec = {c: np.nan for c in JOURNAL_COLS}
                    rec.update({'id':datetime.now(IST).strftime('%Y%m%d%H%M%S%f'),
                                'opened_at':datetime.now(IST).isoformat(timespec='seconds'),
                                'signal_date':str(row['Date']), 'symbol':sym,'side':row['Side'],
                                'status':'OPEN','setup':row['Status'],'score':row['Score'],
                                'underlying_entry':row['Close'],'underlying_sl':row['Underlying SL'],
                                'underlying_t1':row['Target 1 (1R)'],'underlying_t2':row['Target 2 (2R)'],
                                'contract':opt['Contract'],'expiry':opt['Expiry'],'strike':opt['Strike'],
                                'lot_size':lot,'lots':lots,'entry_premium':prem,'entry_delta':opt.get('Delta'),
                                'entry_iv':opt.get('IV %'),'sl_frac':sl_frac,
                                'sid':row.sid,'segment':row.segment,'instrument':row.instrument,
                                'chain_sid':opt['chain_sid'],'chain_seg':opt['chain_seg'],
                                'stop_current':row['Underlying SL'],'t1_hit':False,'days_held':0,
                                'last_premium':prem,'premium_source':'Dhan ask snapshot',
                                'unrealized_pnl':0.0,'notes':'AUTOMATED PAPER ONLY; quote snapshot, not actual fill'})
                    ss.paper = pd.concat([ss.paper,pd.DataFrame([rec])],ignore_index=True)
                    notes.append(f'{sym}: paper entry {opt["Contract"]}, {lots} lot(s), ask {prem:.2f}')
        if notes:
            persist()
        return notes

    # ============================ TAB 1 ============================
    with t1:
        try:
            with st.spinner('Loading F&O universe from Dhan instrument master (cached 24h)…'):
                stocks, indices = load_universe()
        except Exception as e:
            stocks, indices = pd.DataFrame(), pd.DataFrame()
            st.error(f'Universe load failed: {e}')
        c1, c2, c3, c4 = st.columns(4)
        c1.metric('F&O stocks found', len(stocks)); c2.metric('Indices', len(indices))
        c3.metric('Expected candle (IST)', expected.isoformat())
        st.caption(f'Month-to-date trigger window: {expected:%B %Y} through {expected:%d %b}. '
                   f'Watchlist for the next regular session: {next_regular_session(expected):%d %b %Y}. '
                   'Dhan first. Yahoo fallback outside market hours. Both must supply the expected candle date.')
        if c4.button('Refresh instrument master'):
            load_universe.clear(); st.rerun()
        with st.expander('Universe preview'):
            show(pd.concat([indices, stocks]).head(60))
        cc1, cc2, cc3 = st.columns([2, 2, 3])
        scan_all = cc1.checkbox('Scan ALL F&O stocks', True)
        n_limit = cc2.number_input('…or first N stocks', 1, 400, 30, disabled=scan_all)
        only = cc3.text_input('Only these symbols (optional, comma separated)', '')
        inc_idx = st.checkbox('Include indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50)', True)
        if saved.get('summary'):
            st.caption(saved['summary'])
        if finished_for_owner and token and (auto_exit or (not ss.scan.empty and auto_paper)):
            for message in auto_paper_cycle(DhanAPI(token,client_id), ss.scan if not ss.scan.empty else None):
                st.info(message)
        if st.button('SCAN NOW', type='primary', disabled=active is not None or not (len(stocks) and (token or swing_after_close(scan_now_ist)))):
            cutoff = expected
            pool = stocks if (scan_all or only) else stocks.head(int(n_limit))
            targets = pool.to_dict('records')
            if only:
                want = {s.strip().upper() for s in only.split(',') if s.strip()}
                targets = [x for x in targets if x['symbol'] in want]
                if inc_idx: targets = [x for x in indices.to_dict('records') if x['symbol'] in want] + targets
            elif inc_idx:
                targets = indices.to_dict('records') + targets
            with jobs_lock:
                existing = jobs.get(scan_key)
                if existing is None:
                    jobs[scan_key] = (pool_exec.submit(background_swing_scan,token,client_id,cache,
                                                        targets,cutoff,scan_now_ist,cfg,blackouts,fii,dii),
                                      ss[NONCE])
            st.rerun()
        if active is not None:
            st.info('Swing scan is running on the server. Minimizing or locking the window does not restart it.')
        if not ss.scan_errors.empty:
            with st.expander(f'{len(ss.scan_errors)} instruments excluded · candle date / error details', expanded=ss.scan.empty):
                show(ss.scan_errors)
                st.download_button('Download scan diagnostics CSV', ss.scan_errors.to_csv(index=False), 'dhan_scan_diagnostics.csv')
        if not ss.scan.empty:
            s = ss.scan
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric('Scanned', len(s)); m2.metric('Fresh entries', int((s.Status == 'FRESH ENTRY').sum()))
            m3.metric('Month setups', int((s.Status == 'MONTH SETUP').sum()))
            m4.metric('Watchlist', int((s.Status == 'WATCHLIST').sum()))
            m5.metric('Bullish / Bearish month', f"{int((s['Monthly trend'] == 'BULLISH').sum())} / {int((s['Monthly trend'] == 'BEARISH').sum())}")
            show(s.drop(columns=HIDE, errors='ignore'))
            st.download_button('Download scanner CSV', s.drop(columns=HIDE, errors='ignore').to_csv(index=False).encode(), 'dhan_scan.csv')
            # Visual market overview (only scanned symbols; not whole-market breadth)
            st.subheader('Scan overview')
            c1, c2 = st.columns(2)
            with c1:
                st.caption('Signal classification')
                if 'Status' in s.columns:
                    status_counts = s['Status'].fillna('Unknown').value_counts().rename('Setups')
                    st.bar_chart(status_counts)
                else:
                    st.info('Status data unavailable.')
            with c2:
                st.caption('CE vs PE signals')
                if 'Side' in s.columns:
                    st.bar_chart(s['Side'].fillna('Unknown').value_counts().rename('Setups'))
                else:
                    st.info('Side data unavailable.')
            if 'Score' in s.columns:
                st.caption('Technical scores · count of scanned setups')
                st.bar_chart(s['Score'].value_counts().sort_index().rename('Setups'))
        else:
            st.info('Press SCAN NOW to check for the expected completed session.')

    # ============================ TAB 2 ============================
    with t2:
        s = ss.scan
        if s.empty:
            st.info('Run a scan first.')
        else:
            elig = s[s.Eligible].sort_values(['Status', 'Score', 'ADX'], ascending=[True, False, False])
            st.markdown('**FRESH ENTRY** = full strict signal on the last completed candle. '
                        '**MONTH SETUP** = score ≥ month minimum, EMA-cross / CPR-breakout happened this month, all hard gates pass, price not extended. '
                        'Both are eligible for CE/PE option selection. Flow context is market-wide, not stock-specific.')
            st.metric('Eligible setups', len(elig))
            if elig.empty:
                st.warning('No eligible setup in this scan. Do not force a trade.')
            else:
                show(elig.drop(columns=HIDE, errors='ignore'))
                top_n = st.slider('Fetch option contracts for the top N eligible setups', 1, 15, min(5, len(elig)))
                if st.button('Select monthly CE/PE contracts (Dhan option chain)', type='primary', disabled=not (token and client_id)):
                    api = DhanAPI(token, client_id)
                    prog = st.progress(0.0, text='Option chains…')
                    rows = elig.head(top_n).to_dict('records')
                    for i, row in enumerate(rows):
                        ss.options[row['Symbol']] = option_for_setup(api, row, opt_params, ss.seg_memory)
                        prog.progress((i + 1) / len(rows), text=f"{i + 1}/{len(rows)} · {row['Symbol']} (chain limit: 1 request / 3 s)")
                    prog.empty()
                if not client_id: st.warning('DHAN_CLIENT_ID is required for option-chain calls.')
            if ss.options:
                st.subheader('Proposed option contracts')
                cols = ['Symbol', 'Side', 'Verdict', 'Contract', 'Expiry', 'Days left', 'Where', 'Entry premium (ask)', 'LTP', 'Spread %',
                        'Delta', 'IV %', 'Theta', 'Gamma', 'Vega', 'OI', 'Volume', 'Lot size', 'Lots', 'Outlay Rs', 'Max loss Rs (SL)', 'Premium SL',
                        'Premium T1 (1R)', 'Premium T2 (2R)', 'Note']
                od = pd.DataFrame([{k: v for k, v in o.items() if not k.startswith('_')} for o in ss.options.values()])
                show(od[[c for c in cols if c in od.columns]])
                st.caption('Premium levels use the premium stop-loss % from the sidebar (T1 = +1R, T2 = +2R). Lots = risk budget / (premium × SL% × lot size), capped by the exposure limit. IV percentile is not provided by Dhan.')
            for lbl, status in (('Watchlist (score ≥ month minimum but a hard gate blocks)', 'WATCHLIST'),):
                w = s[s.Status == status]
                with st.expander(f'{lbl}: {len(w)}'):
                    show(w.drop(columns=HIDE, errors='ignore'))
            with st.expander(f'Rejected: {int((s.Status == "REJECTED").sum())}'):
                show(s[s.Status == 'REJECTED'].drop(columns=HIDE, errors='ignore'))
            if not ss.history.empty:
                with st.expander('Historical daily signals in lookback window (NOT fresh entries)'):
                    show(ss.history.sort_values('Date', ascending=False))

    # ============================ TAB 3 ============================
    with t3:
        s = ss.scan
        if s.empty:
            st.info('Run a scan first.')
        else:
            sym = st.selectbox('Instrument', s.Symbol.tolist(), key='an_sym')
            row = s[s.Symbol == sym].iloc[0]
            show(pd.DataFrame([row.drop(labels=HIDE, errors='ignore')]))
            if row['Hard blocks']: st.warning('Hard blocks: ' + row['Hard blocks'])
            if token or row.get('Daily source') == 'Yahoo Finance':
                try:
                    x = {'symbol': sym, 'sid': row.sid, 'segment': row.segment, 'instrument': row.instrument}
                    if row.instrument == 'INDEX':
                        _, idf = load_universe()
                        x['fut_sid'] = str(idf.set_index('symbol').loc[sym, 'fut_sid'])
                    if row.get('Daily source') == 'Yahoo Finance':
                        chart_date = date.fromisoformat(row['Date'])
                        frames, _ = yahoo_batch([x], chart_date, cache) if swing_after_close(scan_now_ist) else ({}, {})
                        daily = frames.get(yahoo_ticker(x))
                        if daily is None:
                            raise ValueError('Yahoo daily chart unavailable for the selected scan')
                    else:
                        daily = load_daily(DhanAPI(token, client_id), cache, x, expected, require_session=True)
                    d = indicators(daily).tail(120)
                    candle_chart(d)
                    st.caption('Candle table · ' + CANDLE_LEGEND)
                    show(d.tail(10)[['Open', 'High', 'Low', 'Close', 'Volume', 'ema9', 'ema21', 'adx', 'rsi', 'st_dir', 'cpr_width']].reset_index(), hide_index=True)
                except Exception as e:
                    st.error(f'Chart unavailable: {e}')
            st.warning('Underlying SL/targets are reference levels, not option prices. Use tab 2 for the option contract.')
            if st.button('Inspect option contracts for this instrument', disabled=not (token and client_id)):
                ss.options[sym] = option_for_setup(DhanAPI(token, client_id), row.to_dict(), opt_params, ss.seg_memory)
            o = ss.options.get(sym)
            if o:
                st.write(f"**{o.get('Contract', sym)}** · verdict **{o['Verdict']}** · {o.get('Note', '')}")
                if isinstance(o.get('_cand'), pd.DataFrame) and not o['_cand'].empty:
                    show(o['_cand'].round(2))

            st.subheader('Technical condition dashboard')
            fields = ['Score', 'ADX', 'RSI', 'Weekly CPR width %']
            cols = st.columns(4)
            for col, field in zip(cols, fields):
                if field in row.index:
                    col.metric(field, f'{num(row[field]):.2f}' if np.isfinite(num(row[field])) else '—')
            st.caption('Indicator values are from the most recent completed daily candle.')

    # ============================ TAB 4 ============================
    worker_journal_ok=False
    with t4:
        if worker_mode:
            st.button('Reload worker journal', key='worker_reload')
            try:
                frame,heartbeat=cloud_journal(secret('GITHUB_TOKEN'),secret('PAPER_GIST_ID'),'swing').read()
                ss.paper=normalize_journal(frame)
                worker_journal_ok=True
                st.caption(f"Worker owns paper trading · Last worker check: {heartbeat.get('checked_at','not reported')} · {heartbeat.get('state','waiting')}")
                if heartbeat.get('notes'):st.caption(heartbeat['notes'][-1])
            except Exception as e:
                st.error(f'Cannot read worker journal: {e}')
        st.subheader('Paper trading journal (option premium based)')
        mode = journal.mode
        st.caption(f"Storage: {'GitHub Gist (persistent)' if mode == 'gist' else 'local file (lost when Streamlit Cloud reboots - add GITHUB_TOKEN)'} · {ss.get('paper_msg', '')}")
        if mode == 'gist' and ss.get('gist_id') and not secret('PAPER_GIST_ID'):
            st.warning(f"Add this to Streamlit Secrets so the journal reloads after a reboot:  PAPER_GIST_ID = \"{ss.gist_id}\"")
        jr = ss.paper
        openr = jr[jr.status == 'OPEN']
        expo = float((pd.to_numeric(openr.entry_premium, errors='coerce') * pd.to_numeric(openr.lot_size, errors='coerce') * pd.to_numeric(openr.lots, errors='coerce')).sum())
        k1, k2, k3 = st.columns(3)
        k1.metric('Open trades', f'{len(openr)} / {max_open}')
        k2.metric('Premium exposure', f'Rs {expo:,.0f}', f'cap Rs {capital * max_exposure / 100:,.0f}', delta_color='off')
        k3.metric('Unrealized P&L (Rs)', f"{pd.to_numeric(openr.unrealized_pnl, errors='coerce').sum():,.0f}")

        # ---- update open trades
        if st.button('Update open trades (apply SL / targets / trail / time / signal exits)', type='primary',
                     disabled=worker_mode or openr.empty or not token):
            api = DhanAPI(token, client_id)
            cutoff = last_complete_session()
            live = live_premiums(api, ss.paper) if (client_id and st.session_state.get('use_live', True)) else {}
            msgs = []
            for idx in ss.paper.index[ss.paper.status == 'OPEN']:
                t = ss.paper.loc[idx]
                try:
                    x = {'symbol': t.symbol, 'sid': str(t.sid).replace('.0', ''), 'segment': t.segment, 'instrument': t.instrument}
                    if t.instrument == 'INDEX':
                        _, idf = load_universe()
                        x['fut_sid'] = str(idf.set_index('symbol').loc[t.symbol, 'fut_sid'])
                    d = indicators(load_daily(api, cache, x, cutoff, force=True))
                    r = evaluate_trade(t, d, num(t.sl_frac, sl_frac))
                    ltp = live.get(t['id'], (np.nan, ''))[0]
                    ss.paper.loc[idx, ['stop_current', 't1_hit', 'days_held', 'last_update', 'last_underlying']] = \
                        [r['stop_current'], r['t1_hit'], r['days_held'], r['last_update'], round(r['last_underlying'], 2)]
                    prem0, qty = num(t.entry_premium), num(t.lots, 0) * num(t.lot_size, 0)
                    cur = ltp if ltp == ltp else np.nan
                    if prem0 == prem0:
                        ss.paper.loc[idx, ['last_premium', 'premium_source', 'unrealized_pnl']] = \
                            [round(cur, 2), 'Dhan chain LTP' if ltp == ltp else 'unavailable', round((cur - prem0) * qty, 2)]
                    # Historical underlying OHLC must never be used to invent an option fill.
                    # Snapshot-based exits are handled by auto_paper_cycle on SCAN NOW.
                    if r['exit']:
                        msgs.append(f'{t.symbol}: underlying exit condition observed; no historical option fill assumed')
                except Exception as e:
                    msgs.append(f'{t.symbol}: update failed - {str(e)[:120]}')
            persist()
            for m in msgs: st.info(m)
            st.success('Open trades updated.')
        st.checkbox('Also fetch live option premium (chain LTP) when updating', True, key='use_live')

        # ---- new entry
        st.markdown('#### New paper entry')
        s = ss.scan
        eligible = s[s.Eligible] if not s.empty else s
        if eligible.empty:
            st.info('No eligible setup in the latest scan.')
        else:
            sym = st.selectbox('Eligible setup', eligible.Symbol.tolist(), key='pe_sym')
            row = eligible[eligible.Symbol == sym].iloc[0]
            o = ss.options.get(sym) or {}
            st.write(f"{row.Status} · {row.Side} · score {row.Score} · underlying entry {row.Close} · SL {row['Underlying SL']} · T1 {row['Target 1 (1R)']} · T2 {row['Target 2 (2R)']}")
            st.caption('Only Dhan-validated option contracts can enter the premium paper journal. No manual or estimated premium fills.')
            if st.button('Fetch / refresh Dhan option contract', key=f'fetch_paper_{sym}', disabled=not (token and client_id)):
                with st.spinner(f'Fetching expiry, option chain and premium for {sym}…'):
                    ss.options[sym] = option_for_setup(DhanAPI(token, client_id), row.to_dict(), opt_params, ss.seg_memory)
                st.rerun()
            o = ss.options.get(sym) or {}
            if o.get('Contract'):
                st.write(f"**{o['Contract']}** · {o.get('Where', '')} · verdict **{o['Verdict']}**")
                info = st.columns(5)
                for col, (label, key) in zip(info, [('Expiry','Expiry'),('Ask premium','Entry premium (ask)'),('Delta','Delta'),('IV %','IV %'),('Lot size','Lot size')]):
                    col.metric(label, str(o.get(key, '—')))
                st.caption(f"Spread {o.get('Spread %','—')}% · OI {o.get('OI','—')} · volume {o.get('Volume','—')} · theta {o.get('Theta','—')} · {o.get('Note','')}")
            else:
                st.warning('Option contract not yet fetched from Dhan. Click Fetch / refresh above.')
            if o.get('Verdict') != 'TRADE':
                st.warning('No paper option entry until the contract, expiry, bid/ask, Greeks, liquidity and affordable lots are validated. '+str(o.get('Note','')))
            if st.button('Record Dhan-priced paper entry', disabled=worker_mode or o.get('Verdict') != 'TRADE', key=f'record_paper_{sym}'):
                # Fetch again on entry: never use a stale quote from an earlier scan.
                fresh = option_for_setup(DhanAPI(token, client_id), row.to_dict(), opt_params, ss.seg_memory)
                ss.options[sym] = fresh
                prem = num(fresh.get('Entry premium (ask)'))
                lot = int(num(fresh.get('Lot size'), 0))
                lots = int(num(fresh.get('Lots'), 0))
                exp = pd.to_datetime(fresh.get('Expiry'), errors='coerce')
                bad = entry_guard(ss.paper, sym, row.Side, prem*lot*lots, capital, int(max_open), max_exposure, weekly_limit, max_trade_rupees)
                if fresh.get('Verdict') != 'TRADE' or not np.isfinite(prem) or prem <= 0 or lot < 1 or lots < 1 or pd.isna(exp):
                    bad.append('Dhan option contract / expiry / ask / lot validation failed')
                if pd.notna(exp) and (exp.date() - datetime.now(IST).date()).days < min_days:
                    bad.append('Too close to expiry')
                if bad:
                    for message in bad: st.error(message)
                else:
                    rec = {c: np.nan for c in JOURNAL_COLS}
                    rec.update({'id':datetime.now(IST).strftime('%Y%m%d%H%M%S%f'),
                                'opened_at':datetime.now(IST).isoformat(timespec='seconds'),
                                'signal_date':row.Date, 'symbol':sym, 'side':row.Side, 'status':'OPEN',
                                'setup':row.Status, 'score':row.Score, 'underlying_entry':row.Close,
                                'underlying_sl':row['Underlying SL'], 'underlying_t1':row['Target 1 (1R)'],
                                'underlying_t2':row['Target 2 (2R)'], 'contract':fresh['Contract'],
                                'expiry':fresh['Expiry'], 'strike':fresh['Strike'], 'lot_size':lot,
                                'lots':lots, 'entry_premium':prem, 'entry_delta':fresh.get('Delta'),
                                'entry_iv':fresh.get('IV %'), 'sl_frac':sl_frac, 'sid':row.sid,
                                'segment':row.segment, 'instrument':row.instrument,
                                'chain_sid':fresh['chain_sid'], 'chain_seg':fresh['chain_seg'],
                                'stop_current':row['Underlying SL'], 't1_hit':False, 'days_held':0,
                                'last_premium':prem, 'premium_source':'Dhan ASK snapshot (paper assumption)',
                                'unrealized_pnl':0.0,
                                'notes':'Dhan option quote at record time; simulated entry, not broker fill'})
                    ss.paper = pd.concat([ss.paper, pd.DataFrame([rec])], ignore_index=True)
                    persist()
                    st.success(f"Recorded simulated entry: {fresh['Contract']} @ Rs {prem:.2f} x {lot*lots} units")
                    st.rerun()

        # ---- tables
        jr = ss.paper
        st.markdown('#### Open trades')
        legacy = jr[(jr.status == 'OPEN') & (pd.to_numeric(jr.entry_premium, errors='coerce').isna() | jr.contract.isna() | jr.expiry.isna())]
        if not legacy.empty:
            st.warning(f'{len(legacy)} existing open record(s) have no verified option contract/premium. They are UNDERLYING-ONLY records. Current Dhan quotes cannot reconstruct their historical entry price; close or delete them manually and create new Dhan-priced paper trades.')
        st.caption('Premiums are snapshot quotes, not actual broker fills. Quotes update only when you scan or click Update; this is not continuous monitoring.')
        show(jr[jr.status == 'OPEN'].drop(columns=['sid', 'segment', 'instrument', 'chain_sid', 'chain_seg', 'sl_frac'], errors='ignore'))
        res = journal_stats(jr)
        st.markdown('#### Closed trades & statistics')
        if res:
            stats, eq = res
            show(pd.DataFrame([stats]))
            st.line_chart(eq.reset_index(drop=True), height=200)
        show(jr[jr.status == 'CLOSED'].drop(columns=['sid', 'segment', 'instrument', 'chain_sid', 'chain_seg', 'sl_frac'], errors='ignore'))
        st.caption('Premium journal uses Dhan quote snapshots. Simulated fills, not actual executions; costs and slippage are estimates. Never interpret modeled or missing prices as realized premium P&L.')

        # ---- manual close / delete
        if not jr.empty:
            with st.expander('Manual close / delete'):
                tid = st.selectbox('Trade', jr.id.astype(str).tolist(), key='mc_id')
                m1, m2 = st.columns(2)
                xu = m1.number_input('Exit underlying price', 0.0, 10000000.0, 0.0, key='mc_u')
                xp = m2.number_input('Exit premium (0 = unknown)', 0.0, 100000.0, 0.0, 0.05, key='mc_p')
                b1, b2 = st.columns(2)
                if b1.button('Close this trade manually', disabled=worker_mode):
                    idx = ss.paper.index[ss.paper.id.astype(str) == tid][0]
                    if ss.paper.loc[idx, 'status'] == 'OPEN' and xu > 0:
                        close_row(ss.paper, idx, datetime.now(IST).date().isoformat(), xu, xp if xp > 0 else np.nan, 'MANUAL EXIT', slip)
                        persist(); st.success('Closed.')
                    else: st.error('Trade already closed, or exit price missing.')
                if b2.button('Delete this trade', disabled=worker_mode):
                    ss.paper = ss.paper[ss.paper.id.astype(str) != tid].reset_index(drop=True)
                    persist(); st.success('Deleted.')
        up = st.file_uploader('Restore journal CSV', type='csv', key='paper_up')
        if up and st.button('Import journal CSV', disabled=worker_mode):
            try:
                ss.paper = normalize_journal(pd.read_csv(up)); persist(); st.success('Imported.')
            except Exception as e: st.error(str(e))
        st.download_button('Export journal CSV', normalize_journal(ss.paper).to_csv(index=False).encode(), 'paper_trades.csv')


    # ============================ TAB 5 ============================
    with t5:
        st.subheader('ELIGIBLE · CE / PE SIGNAL BOARD')
        st.caption('Only setups that pass your rules appear here. Yahoo setups are for watchlist review, '
                   'not automatic paper entries. The candle date and the original trigger date are shown separately.')
        s = ss.scan
        if s.empty:
            st.info('Run SCAN NOW on the Market scanner tab to populate these cards.')
        else:
            eligible = s[s['Eligible'].fillna(False).astype(bool)].copy()
            a, b, c = st.columns(3)
            a.metric('Eligible setups', len(eligible))
            b.metric('CE opportunities', int((eligible['Side'] == 'CE').sum()))
            c.metric('PE opportunities', int((eligible['Side'] == 'PE').sum()))
            side_filter = st.radio('Show signals', ['All', 'CE', 'PE'], horizontal=True, key='board_side')
            if side_filter != 'All': eligible = eligible[eligible['Side'] == side_filter]
            if eligible.empty:
                st.warning('No fully eligible setups in this scan. High-scoring rejected stocks remain visible in the Top CE/PE tab.')
            else:
                eligible = eligible.sort_values(['Score','ADX'], ascending=[False,False])
                rows = eligible.to_dict('records')
                for i in range(0, len(rows), 3):
                    for col, row in zip(st.columns(3), rows[i:i+3]):
                        with col: st.markdown(eligible_card(row), unsafe_allow_html=True)
                st.subheader('Eligible signal comparison')
                st.bar_chart(eligible.set_index('Symbol')['Score'])
                st.download_button('Export eligible setups', eligible.drop(columns=HIDE, errors='ignore').to_csv(index=False).encode(), 'eligible_setups.csv')
            st.divider()
            st.subheader('High-score watchlist · not confirmed entries')
            watch = s[(~s['Eligible'].fillna(False).astype(bool)) & (pd.to_numeric(s['Score'], errors='coerce') >= month_min)]
            if not watch.empty:
                show(watch.sort_values('Score', ascending=False).drop(columns=HIDE, errors='ignore'))
            else:
                st.caption('No high-score watchlist setups in the current scan.')

    with t6:
        st.subheader('Swing trade decision')
        if ss.scan.empty:
            st.info('Run a Market Scanner scan first.')
        else:
            sym=st.selectbox('Decision symbol', sorted(ss.scan.Symbol.unique()),key='idea_s_symbol')
            row=ss.scan[ss.scan.Symbol.eq(sym)].iloc[0]
            idea=dict(symbol=sym,side=row.Side,mode='swing',eligible=bool(row.Eligible),
                      reason=f"{row.Status} · {row.Score}/10 · {row.get('Hard blocks','') or 'No scanner hard blocks'}",
                      candle=str(row.Date),close=row.Close,sid=row.sid,segment=row.segment,
                      instrument=row.instrument,lot=row.get('Lot size'),account=skey)
            gap=st.number_input('Opening-gap review limit (%)',min_value=0.1,max_value=10.,value=1.,step=.1,
                                help='A panel-only threshold. This does not change the strategy.')
            now=datetime.now(IST)
            reviewed=st.checkbox('I checked upcoming results and major events for this symbol',key=f'decision_results_review_{sym}_{now.date()}')
            known_events=[pd.to_datetime(x,errors='coerce') for x in blackouts]
            has_event=any(pd.notna(x) and abs((x.date()-now.date()).days)<=3 for x in known_events)
            invalid_event=any(pd.isna(x) for x in known_events)
            checks=[('Results / scheduled events',None if invalid_event else False if has_event else True if reviewed else None,
                     'Event within ±3 days' if has_event else 'Invalid blackout date' if invalid_event else
                     'Manual review noted' if reviewed else 'No automatic earnings calendar. Confirm manually.')]
            policy=dict(capital=capital,cap=max_trade_rupees,risk=risk_pct,stop=sl_frac,target_r=2,slip=slip/100,
                delta_lo=d_lo,delta_hi=d_hi,spread=max_spread,min_days=int(min_days),exposure=max_exposure,
                max_open=int(max_open),weekly_limit=weekly_limit,max_gap=gap,has_connection=bool(token and client_id),
                journal_ok=bool(worker_journal_ok if worker_mode else not str(ss.get('paper_msg','')).startswith('Load failed')),
                start=(9,30) if worker_mode else (9,15),end=(10,30) if worker_mode else (15,30),
                window='09:30–10:30 IST (Linux worker)' if worker_mode else '09:15–15:30 IST (panel review, not a scheduled fill)',
                worker_mode=worker_mode)
            def inspect_swing(p):
                api=DhanAPI(token,client_id)
                notes=['A 60-minute EMA trend is context, not a crossover trigger. At the open, wait for a completed current-session bar.']
                if str(row.segment)!='IDX_I':
                    notes.append('Stock option: check physical-settlement obligations before expiry.')
                work=dict(p)
                try:
                    cutoff=datetime.now(IST).date()-timedelta(days=1)
                    ref=load_daily(api,cache,dict(sid='13',segment='IDX_I',instrument='INDEX'),cutoff)
                    latest=ref.index[-1].date()
                    work['daily_verified']=str(latest)==idea['candle'] and 0<(datetime.now(IST).date()-latest).days<=4
                    refgate=('Latest completed daily session',work['daily_verified'],f'NIFTY reference: {latest}; setup: {idea["candle"]}.')
                except Exception as exc:
                    work['daily_verified']=False
                    refgate=('Latest completed daily session',None,'Daily session check unavailable · '+str(exc)[:130])
                try:gates,_=context(api,idea,work)
                except Exception as exc:gates=[('60-minute EMA trend',None,'No verified timeframe data · '+str(exc)[:130]),
                                                ('Opening gap',None,'Opening candle check unavailable')]
                gates.insert(0,refgate)
                def expiry(dates,today):return pick_monthly_expiry(dates,p['min_days'],today)[0]
                try:return load_quote(api,idea,p,expiry,parse_chain,select_contract),None,gates,notes
                except Exception as exc:return None,str(exc)[:230],gates,notes
            st.caption('One-lot review using the dashboard risk settings. It does not authorize a worker entry or certify an existing multi-lot plan.')
            st.write(f"**Underlying references:** Close ₹{row.Close:,.2f} · ATR stop ₹{row['Underlying SL']:,.2f} · 2R target ₹{row['Target 2 (2R)']:,.2f}")
            st.caption('Calendar events and the 60-minute review here do not change the existing automated rules.')
            show_panel(idea,policy,ss.paper,inspect_swing,key='preflight_swing',base_checks=checks,
                       critical_context=['Latest completed daily session','60-minute EMA trend','Opening gap'],
                       exit_rules=['Automatic: premium stop, 2R premium target, expiry window or 10 calendar days.',
                                   'The Linux worker checks Dhan bid snapshots; Scan Now automation checks LTP snapshots.',
                                   'Underlying ATR, trailing and EMA reversal are not automatic exits in this worker.'])

    with t7:
        render_backtest(pd.concat([indices,stocks],ignore_index=True),token,client_id,DhanAPI,'Daily')

if __name__ == "__main__":
    main()
