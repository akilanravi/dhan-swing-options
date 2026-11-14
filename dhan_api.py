import io
import time
import requests
import pandas as pd
from datetime import timedelta

BASE = 'https://api.dhan.co/v2'
MASTER_URL = 'https://images.dhan.co/api-data/api-scrip-master-detailed.csv'

class DhanAPI:
    def __init__(self, token, client_id=None):
        if not token: raise ValueError('Dhan access token is required')
        self.session = requests.Session()
        self.client_id = str(client_id or '').strip()
        self.session.headers.update({'access-token': token, 'Content-Type': 'application/json', 'Accept': 'application/json'})

    def post(self, path, payload):
        for attempt in range(3):
            try:
                r = self.session.post(BASE + path, json=payload, timeout=35)
                if r.status_code == 429 and attempt < 2:
                    time.sleep(2 ** attempt * 3)
                    continue
                r.raise_for_status()
                out = r.json()
                if isinstance(out, dict) and out.get('status') == 'failure':
                    raise RuntimeError(str(out.get('remarks', out)))
                return out
            except requests.HTTPError as e:
                raise RuntimeError(f'Dhan HTTP {e.response.status_code}: {e.response.text[:250]}') from e
        raise RuntimeError('Dhan rate limit exceeded')

    def candles(self, sid, segment, instrument, start, end, interval=None):
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        frames = []
        # Request bounded intervals rather than multi-year requests.
        step = timedelta(days=30 if interval else 85)
        cur = start
        while cur < end:
            nxt = min(cur + step, end)
            payload = {'securityId': str(sid), 'exchangeSegment': segment,
                       'instrument': instrument, 'fromDate': cur.strftime('%Y-%m-%d %H:%M:%S') if interval else cur.strftime('%Y-%m-%d'),
                       'toDate': nxt.strftime('%Y-%m-%d %H:%M:%S') if interval else nxt.strftime('%Y-%m-%d'), 'oi': False}
            if interval:
                payload['interval'] = str(interval)
                path = '/charts/intraday'
            else:
                payload['expiryCode'] = 0
                path = '/charts/historical'
            out = self.post(path, payload)
            out = out.get('data', out) if isinstance(out, dict) else out
            if isinstance(out, dict) and out.get('timestamp'):
                n = len(out['timestamp'])
                d = pd.DataFrame({name: out.get(source, [0]*n) for source, name in [('open','Open'),('high','High'),('low','Low'),('close','Close'),('volume','Volume')]})
                d.index = pd.to_datetime(out['timestamp'], unit='s', utc=True).tz_convert('Asia/Kolkata').tz_localize(None)
                frames.append(d)
            cur = nxt
            time.sleep(.12)
        if not frames: raise ValueError('No candles returned; check instrument mapping, access and date range')
        d = pd.concat(frames).sort_index()
        d = d[~d.index.duplicated(keep='last')]
        for c in ['Open','High','Low','Close','Volume']: d[c] = pd.to_numeric(d[c], errors='coerce')
        return d.dropna(subset=['Open','High','Low','Close'])

    def _option_auth(self):
        if not self.client_id:
            raise ValueError('DHAN_CLIENT_ID is missing. Set it in Streamlit Cloud Secrets; do not use the underlying security ID as client ID.')
        self.session.headers['client-id'] = self.client_id

    def expiries(self, sid, segment):
        self._option_auth()
        return self.post('/optionchain/expirylist', {'UnderlyingScrip': int(sid), 'UnderlyingSeg': segment})

    def chain(self, sid, segment, expiry):
        self._option_auth()
        return self.post('/optionchain', {'UnderlyingScrip': int(sid), 'UnderlyingSeg': segment, 'Expiry': expiry})


def read_instrument_master(uploaded=None):
    """Fetch current public Dhan scrip master automatically; no user CSV needed."""
    if uploaded is None:
        r = requests.get(MASTER_URL, timeout=90, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        return pd.read_csv(io.BytesIO(r.content), low_memory=False, dtype=str)
    return pd.read_csv(uploaded, low_memory=False, dtype=str)


def build_universe(master):
    """Derive active F&O stock underlyings, mapping to NSE_EQ spot IDs."""
    m = master.copy()
    m.columns = [str(c).strip().upper() for c in m.columns]
    aliases = {
        'EXCH_ID': ['EXCH_ID','SEM_EXM_EXCH_ID'],
        'SEGMENT': ['SEGMENT','SEM_SEGMENT'],
        'SECURITY_ID': ['SECURITY_ID','SEM_SMST_SECURITY_ID'],
        'INSTRUMENT': ['INSTRUMENT','INSTRUMENT_TYPE','SEM_INSTRUMENT_NAME'],
        'SYMBOL_NAME': ['SYMBOL_NAME','SEM_TRADING_SYMBOL','DISPLAY_NAME'],
    }
    for canonical, variants in aliases.items():
        for v in variants:
            if v in m.columns:
                m[canonical] = m[v]
                break
        else:
            raise ValueError(f'Dhan master missing {canonical}; available columns: {list(m.columns)}')
    def norm(col):
        return m[col].fillna('').astype(str).str.upper().str.strip()
    exch, seg, inst = norm('EXCH_ID'), norm('SEGMENT'), norm('INSTRUMENT')
    # NSE equity futures are FUTSTK (some releases use FUT or FUTURE).
    futures = m[(exch.eq('NSE') | seg.str.contains('NSE')) &
                (inst.str.contains('FUTSTK') | ((inst == 'FUT') & norm('SEGMENT').str.contains('FNO|F&O|DERIV')))].copy()
    if futures.empty:
        raise ValueError('No NSE stock futures found in current Dhan master. Check master instrument values.')
    if 'UNDERLYING_SYMBOL' in futures.columns:
        futures['key'] = futures['UNDERLYING_SYMBOL'].fillna('').str.upper().str.strip()
    else:
        futures['key'] = futures['SYMBOL_NAME'].fillna('').str.upper().str.replace(r'[- ]?\d{2}[A-Z]{3}FUT$', '', regex=True).str.strip()
    futures = futures[~futures['key'].str.contains(r'TEST|DUMMY|MOCK', case=False, na=False)]
    keys = set(futures['key']) - {''}
    # Spot candles must use the equity spot security ID, NOT the futures contract ID.
    equity = m[(exch.eq('NSE') | seg.eq('NSE_EQ')) &
               (inst.isin(['EQUITY','EQ']) | seg.eq('NSE_EQ'))].copy()
    equity['key'] = equity['SYMBOL_NAME'].fillna('').str.upper().str.strip()
    if 'UNDERLYING_SYMBOL' in equity.columns:
        equity['key'] = equity['UNDERLYING_SYMBOL'].where(equity['UNDERLYING_SYMBOL'].fillna('').ne(''), equity['key']).astype(str).str.upper().str.strip()
    equity = equity[equity['key'].isin(keys)].copy()
    if 'SERIES' in equity.columns:
        equity = equity[equity['SERIES'].fillna('EQ').astype(str).str.upper().isin(['EQ',''])]
    equity = equity.drop_duplicates('key')
    equity['symbol'] = equity['key']
    equity['sid'] = equity['SECURITY_ID'].astype(str).str.replace(r'\.0$', '', regex=True)
    equity['segment'] = 'NSE_EQ'
    equity['instrument'] = 'EQUITY'
    result = equity[['symbol','sid','segment','instrument']].dropna()
    result = result[result['sid'].str.fullmatch(r'\d+')].drop_duplicates('symbol')
    result = result[~result['symbol'].str.contains(r'TEST|DUMMY|MOCK', case=False, na=False)].sort_values('symbol').reset_index(drop=True)
    if result.empty:
        raise ValueError(f'Found {len(keys)} F&O symbols but no matching NSE equity IDs. Sample F&O symbols: {sorted(keys)[:8]}')
    return result
