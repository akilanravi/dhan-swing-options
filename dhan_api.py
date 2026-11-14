import requests
import pandas as pd

BASE='https://api.dhan.co/v2'
class DhanAPI:
    def __init__(self,token):
        if not token: raise ValueError('Dhan access token required')
        self.session=requests.Session(); self.session.headers.update({'access-token':token,'Content-Type':'application/json','Accept':'application/json'})
    def post(self,path,payload):
        try:
            r=self.session.post(BASE+path,json=payload,timeout=35)
            r.raise_for_status(); out=r.json()
            if isinstance(out,dict) and out.get('status')=='failure': raise RuntimeError(str(out.get('remarks',out)))
            return out
        except requests.HTTPError as e:
            raise RuntimeError(f'Dhan HTTP {e.response.status_code}: {e.response.text[:300]}') from e
    def candles(self,sid,segment,instrument,start,end,interval=None):
        body={'securityId':str(sid),'exchangeSegment':segment,'instrument':instrument,'fromDate':start,'toDate':end,'oi':False}
        if interval:
            body['interval']=str(interval); path='/charts/intraday'
        else:
            body['expiryCode']=0; path='/charts/historical'
        out=self.post(path,body); out=out.get('data',out)
        if not isinstance(out,dict) or not out.get('timestamp'): raise ValueError('No candles returned; verify instrument mapping and subscription')
        cols={'open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'}
        d=pd.DataFrame({v:out.get(k,[0]*len(out['timestamp'])) for k,v in cols.items()})
        d.index=pd.to_datetime(out['timestamp'],unit='s',utc=True).tz_convert('Asia/Kolkata').tz_localize(None)
        return d.sort_index().dropna(subset=['Open','High','Low','Close'])
    def expiries(self,sid,segment):
        out=self.post('/optionchain/expirylist',{'UnderlyingScrip':int(sid),'UnderlyingSeg':segment})
        return out.get('data',out) if isinstance(out,dict) else out
    def chain(self,sid,segment,expiry):
        out=self.post('/optionchain',{'UnderlyingScrip':int(sid),'UnderlyingSeg':segment,'Expiry':expiry})
        return out.get('data',out) if isinstance(out,dict) else out
