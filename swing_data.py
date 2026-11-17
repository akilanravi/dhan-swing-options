"""Swing daily watchlist provider. Never fetches option prices."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from threading import Lock

import pandas as pd
import requests

OHLCV = ['Open', 'High', 'Low', 'Close', 'Volume']
# Do not substitute ETFs or a different index. Unmapped indices stay on Dhan.
YAHOO_INDICES = {'NIFTY': '^NSEI', 'BANKNIFTY': '^NSEBANK'}
YAHOO_BATCH_LOCK = Lock()


def yahoo_ticker(x):
    symbol = x['symbol'].upper()
    if x['segment'] == 'IDX_I' or x['instrument'] == 'INDEX':
        return YAHOO_INDICES.get(symbol)
    if x['segment'] == 'NSE_EQ' and x['instrument'] == 'EQUITY':
        return symbol + '.NS'
    return None


def yahoo_cache_key(ticker, cutoff):
    return ('swing-yahoo-daily', ticker, cutoff.isoformat())


def normalize_yahoo(frame, cutoff):
    if frame.empty or not set(OHLCV).issubset(frame.columns):
        return pd.DataFrame(columns=OHLCV)
    out = frame[OHLCV].apply(pd.to_numeric, errors='coerce').copy()
    index = pd.DatetimeIndex(out.index)
    if index.tz is not None:
        index = index.tz_convert('Asia/Kolkata').tz_localize(None)
    out.index = index.normalize()
    out = out.loc[out.index.notna() & (out.index <= pd.Timestamp(cutoff))]
    out = out.loc[(out[OHLCV[:4]] > 0).all(axis=1)]
    out = out.loc[~out.index.duplicated(keep='last')].sort_index()
    out['Volume'] = out['Volume'].fillna(0).clip(lower=0)
    return out


def yahoo_chart_daily(ticker, cutoff, get=requests.get):
    """Recover Yahoo stock history when the batch feed did not deliver it."""
    t=pd.Timestamp(cutoff,tz='Asia/Kolkata')
    request={'interval':'1d','period1':int((t-pd.Timedelta(days=420)).timestamp()),
             'period2':int((t+pd.Timedelta(days=1)).timestamp())}
    url=f'https://query2.finance.yahoo.com/v8/finance/chart/{quote(ticker,safe="")}'
    try:
        r=get(url,params=request,headers={'User-Agent':'Mozilla/5.0'},timeout=8)
        r.raise_for_status()
        j=(r.json().get('chart',{}).get('result') or [])[0]
        meta=j.get('meta',{})
        if meta.get('symbol','').upper()!=ticker.upper() or meta.get('currency')!='INR' or meta.get('dataGranularity')!='1d':
            return None
        q=j['indicators']['quote'][0]
        index=pd.to_datetime(j['timestamp'],unit='s',utc=True,errors='coerce')
        f=pd.DataFrame({c:q.get(c.lower(),[float('nan')]*len(index)) for c in OHLCV},index=index)
        return normalize_yahoo(f,cutoff)
    except (requests.RequestException,ValueError,IndexError,KeyError,TypeError,AttributeError):
        return None


def yahoo_batch(targets, cutoff, cache, downloader=None, *, min_history=215, chart_retry=False):
    """Fetch only requested fallback symbols, one batch, with bounded threads."""
    tickers = sorted({t for x in targets if (t := yahoo_ticker(x))})
    found, missing, notes = {}, [], {}
    for t in tickers:
        hit = cache.get(yahoo_cache_key(t, cutoff), ttl=300)
        if hit is not None and len(hit)>=min_history:
            found[t] = hit
        else:
            missing.append(t)
    if not missing:
        return found, notes

    def download():
        if downloader is None:
            import yfinance as yf
            fetch = yf.download
        else:
            fetch = downloader
        return fetch(missing, start=(cutoff - timedelta(days=420)).isoformat(),
                     end=(cutoff + timedelta(days=1)).isoformat(), interval='1d',
                     group_by='ticker', multi_level_index=True, auto_adjust=False,
                     progress=False, ignore_tz=True, timeout=8, threads=min(8, len(missing)))

    try:
        with YAHOO_BATCH_LOCK:
            data = download()
    except Exception as exc:
        data=pd.DataFrame()
        notes.update({t:'Yahoo download failed: '+type(exc).__name__ for t in missing})
    for t in missing:
        f = pd.DataFrame()
        if isinstance(data, pd.DataFrame) and not data.empty:
            if isinstance(data.columns, pd.MultiIndex):
                if t in data.columns.get_level_values(0):
                    f = data[t]
            elif len(missing) == 1:
                f = data
        d = normalize_yahoo(f, cutoff)
        actual = d.index[-1].date() if not d.empty else None
        # Require a full warm-up and a valid dated bar. Never cache stale data.
        if actual != cutoff:
            notes.setdefault(t,f'Yahoo candle date: {actual or "unavailable"}')
        elif len(d) < min_history:
            notes[t] = f'Yahoo returned only {len(d)} usable daily candles (need {min_history}+)'
        else:
            d.attrs.update(candle_source='Yahoo Finance', yahoo_ticker=t)
            cache.put(yahoo_cache_key(t, cutoff), d)
            found[t] = d
    if chart_retry:
        remaining=[t for t in missing if t not in found]
        with ThreadPoolExecutor(max_workers=8) as pool:
            for t,d in zip(remaining,pool.map(lambda t:yahoo_chart_daily(t,cutoff),remaining)):
                if d is not None and len(d)>=min_history and d.index[-1].date()==cutoff:
                    d.attrs.update(candle_source='Yahoo Finance',yahoo_ticker=t)
                    cache.put(yahoo_cache_key(t,cutoff),d)
                    found[t]=d
                    notes.pop(t,None)
    return found, notes
