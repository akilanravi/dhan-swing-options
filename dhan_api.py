import io
import time
import requests
import pandas as pd
from datetime import timedelta

BASE = 'https://api.dhan.co/v2'
MASTER_URL = 'https://images.dhan.co/api-data/api-scrip-master-detailed.csv'

class DhanAPI:
    def __init__(self, token):
        if not token: raise ValueError('Dhan access token is required')
        self.session = requests.Session()
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

    def expiries(self, sid, segment):
        return self.post('/optionchain/expirylist', {'UnderlyingScrip': int(sid), 'UnderlyingSeg': segment})

    def chain(self, sid, segment, expiry):
        return self.post('/optionchain', {'UnderlyingScrip': int(sid), 'UnderlyingSeg': segment, 'Expiry': expiry})


def read_instrument_master(uploaded=None):
    if uploaded is None:
        r = requests.get(MASTER_URL, timeout=45)
        r.raise_for_status()
        return pd.read_csv(io.BytesIO(r.content), low_memory=False)
    return pd.read_csv(uploaded, low_memory=False)


def build_universe(master):
    # Detailed Dhan instrument master schema; fail visibly if Dhan changes it.
    names = {str(c).strip().upper(): c for c in master.columns}
    needed = ['SEM_EXM_EXCH_ID','SEM_SEGMENT','SEM_INSTRUMENT_NAME','SEM_SMST_SECURITY_ID','SEM_TRADING_SYMBOL']
    missing = [x for x in needed if x not in names]
    if missing: raise ValueError('Dhan master columns changed. Missing: '+', '.join(missing)+'; available: '+', '.join(map(str, master.columns[:20])))
    m = master.rename(columns={names[k]:k for k in needed}).copy()
    seg = m.SEM_SEGMENT.astype(str).str.upper()
    inst = m.SEM_INSTRUMENT_NAME.astype(str).str.upper()
    exch = m.SEM_EXM_EXCH_ID.astype(str).str.upper()
    # NSE F&O stock universe from current futures stock contracts, deduplicated.
    fut = m[(exch == 'NSE') & (seg.str.contains('FNO|F&O|DERIV', regex=True)) & (inst == 'FUTSTK')].copy()
    if fut.empty:
        # Different Dhan master releases encode segment differently.
        fut = m[(exch == 'NSE') & (inst == 'FUTSTK')].copy()
    # Equity spot IDs must be looked up separately; futures IDs are not equity IDs.
    eq = m[(exch == 'NSE') & (inst.isin(['EQUITY','EQ']))].copy()
    eq['key'] = eq.SEM_TRADING_SYMBOL.astype(str).str.upper().str.strip()
    eq = eq.drop_duplicates('key')
    fut['key'] = fut.SEM_TRADING_SYMBOL.astype(str).str.upper().str.replace(r'[- ]?\d{2}[A-Z]{3}FUT$', '', regex=True).str.strip()
    keys = set(fut.key)
    universe = eq[eq.key.isin(keys)].copy()
    universe['symbol'] = universe['key']
    universe['sid'] = universe.SEM_SMST_SECURITY_ID.astype(str).str.replace(r'\.0$', '', regex=True)
    universe['segment'] = 'NSE_EQ'
    universe['instrument'] = 'EQUITY'
    return universe[['symbol','sid','segment','instrument']].drop_duplicates('symbol').sort_values('symbol').reset_index(drop=True)
