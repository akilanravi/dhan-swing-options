"""Read-only checks for the next regular session after a daily breakout."""
from datetime import datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

IST=ZoneInfo('Asia/Kolkata')


def classify_open(side, previous_close, level, first_open, high, low, close):
    prices=(previous_close,level,first_open,high,low,close)
    if not all(isfinite(x) and x>0 for x in prices) or low>min(first_open,close) or high<max(first_open,close):
        raise ValueError('Invalid first 15-minute candle or daily reference')
    if side not in ('CE','PE'):
        raise ValueError('Unknown signal direction')
    sign=1 if side=='CE' else -1
    agrees=sign*(close-first_open)>0
    past_prior=sign*(close-previous_close)>0
    held=sign*(close-level)>0
    touch=low<=level if side=='CE' else high>=level
    return {'open':first_open,'high':high,'low':low,'close':close,
            'gap_pct':100*(first_open/previous_close-1),'first_15m_pct':100*(close/first_open-1),
            'vs_prior_pct':100*(close/previous_close-1),
            'first_candle_agrees':agrees,'beyond_prior_close':past_prior,'broken_level_holds':held,
            'confirm':agrees and past_prior and held,
            'retest':touch and held and agrees}


def fetch_first_15m(api, instrument, session, now=None):
    now=now or datetime.now(IST)
    start=datetime.combine(session,time(9,15),tzinfo=IST)
    end=start+timedelta(minutes=15)
    if now<end:
        return None
    params={'securityId':str(instrument['sid']),'exchangeSegment':instrument['segment'],
            'instrument':instrument['instrument'],'interval':'15','oi':False,
            'fromDate':start.strftime('%Y-%m-%d %H:%M:%S'),
            'toDate':end.strftime('%Y-%m-%d %H:%M:%S')}
    r=api.post('/charts/intraday',params)
    if not isinstance(r,dict):
        return None
    d=r.get('data',r)
    if not isinstance(d,dict):
        return None
    stamp=int(start.timestamp())
    for i,t in enumerate(d.get('timestamp') or []):
        try:
            if int(t)!=stamp:
                continue
        except (ValueError,TypeError,OverflowError):
            continue
        try:
            o,h,l,c=[float(d[k][i]) for k in ('open','high','low','close')]
        except (IndexError,KeyError,ValueError,TypeError):
            return None
        if all(isfinite(x) and x>0 for x in (o,h,l,c)) and l<=min(o,c)<=max(o,c)<=h:
            return o,h,l,c
    return None
