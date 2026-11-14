import numpy as np
import pandas as pd

def ema(s, n): return s.ewm(span=n, adjust=False, min_periods=n).mean()
def rma(s,n): return s.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
def atr(df,n=14):
    p=df.Close.shift(1)
    tr=pd.concat([(df.High-df.Low), (df.High-p).abs(),(df.Low-p).abs()],axis=1).max(axis=1)
    return rma(tr,n)
def indicators(df):
    d=df.copy().sort_index()
    for n in (9,21,50,200): d[f'ema{n}']=ema(d.Close,n)
    d['atr']=atr(d)
    up=d.High.diff(); down=-d.Low.diff()
    plus=up.where((up>down)&(up>0),0.0)
    minus=down.where((down>up)&(down>0),0.0)
    trsum=atr(d)*14
    pdi=100*rma(plus,14)*14/trsum.replace(0,np.nan)
    mdi=100*rma(minus,14)*14/trsum.replace(0,np.nan)
    d['pdi']=pdi; d['mdi']=mdi
    d['adx']=rma(100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan),14)
    diff=d.Close.diff(); gain=rma(diff.clip(lower=0),14); loss=rma((-diff).clip(lower=0),14)
    rs=gain/loss.replace(0,np.nan)
    d['rsi']=100-100/(1+rs)
    d.loc[(loss==0)&(gain>0),'rsi']=100
    d.loc[(loss==0)&(gain==0),'rsi']=50
    mid=(d.High+d.Low)/2; upper=mid+3*d['atr']; lower=mid-3*d['atr']
    st=np.full(len(d),np.nan); direction=np.zeros(len(d),dtype=int)
    fu=upper.to_numpy().copy(); fl=lower.to_numpy().copy(); close=d.Close.to_numpy()
    for i in range(1,len(d)):
        if np.isnan(fu[i]) or np.isnan(fu[i-1]): continue
        if fu[i]>=fu[i-1] and close[i-1]<=fu[i-1]: fu[i]=fu[i-1]
        if fl[i]<=fl[i-1] and close[i-1]>=fl[i-1]: fl[i]=fl[i-1]
        if direction[i-1]==0: direction[i]=1 if close[i]>=mid.iloc[i] else -1
        elif direction[i-1]==1: direction[i]=-1 if close[i]<fl[i] else 1
        else: direction[i]=1 if close[i]>fu[i] else -1
        st[i]=fl[i] if direction[i]==1 else fu[i]
    d['st']=st; d['st_dir']=direction
    d['vol20']=d.Volume.rolling(20).mean().shift(1)
    # Prior completed weekly OHLC only: never use current incomplete week.
    weekly=d[['High','Low','Close']].resample('W-FRI').agg({'High':'max','Low':'min','Close':'last'})
    weekly=weekly.shift(1)
    w=weekly.reindex(d.index,method='ffill')
    pivot=(w.High+w.Low+w.Close)/3
    bc=(w.High+w.Low)/2; tc=2*pivot-bc
    d['cpr_top']=pd.concat([bc,tc],axis=1).max(axis=1)
    d['cpr_bottom']=pd.concat([bc,tc],axis=1).min(axis=1)
    d['cpr_width']=(d.cpr_top-d.cpr_bottom)/pivot
    span=w.High-w.Low
    d['r3']=w.Close+span*1.1/4
    d['s3']=w.Close-span*1.1/4
    d['r4']=w.Close+span*1.1/2
    d['s4']=w.Close-span*1.1/2
    d['cross_up']=(d.ema9>d.ema21)&(d.ema9.shift(1)<=d.ema21.shift(1))
    d['cross_down']=(d.ema9<d.ema21)&(d.ema9.shift(1)>=d.ema21.shift(1))
    d['break_up']=(d.Close>d[['cpr_top','r3']].max(axis=1))&(d.Close.shift(1)<=d[['cpr_top','r3']].max(axis=1).shift(1))
    d['break_down']=(d.Close<d[['cpr_bottom','s3']].min(axis=1))&(d.Close.shift(1)>=d[['cpr_bottom','s3']].min(axis=1).shift(1))
    return d

def score(row,side,volume_available=True):
    bullish=side=='CE'; sign=1 if bullish else -1
    tests=[(row.Close>row.ema200) if bullish else (row.Close<row.ema200),
           (row.ema50>row.ema200) if bullish else (row.ema50<row.ema200),
           row.st_dir==sign, row.adx>22, row.adx>row.prev_adx,
           (row.pdi>row.mdi) if bullish else (row.mdi>row.pdi),
           bool(row.cross_up or row.break_up) if bullish else bool(row.cross_down or row.break_down),
           row.rsi>55 if bullish else row.rsi<45,
           row.Volume>row.vol20 if volume_available else False,
           row.Close>row.ema21 if bullish else row.Close<row.ema21]
    return sum(bool(x) for x in tests)

def annotate(d,cfg,ticker):
    d=d.copy(); d['prev_adx']=d.adx.shift(1)
    # Yahoo index volume is often 0: index signals cannot pass volume confirmation.
    validvol=(d.Volume>0)&(d.vol20>0)
    events=set(cfg.get('event_blackout_dates',{}).get(ticker,[]))
    signals=[]; scores=[]
    for dt,r in d.iterrows():
        blackout=any(abs((pd.Timestamp(dt).date()-pd.Timestamp(e).date()).days)<=3 for e in events)
        common=(pd.notna(r.ema200) and pd.notna(r.adx) and r.adx>=20
                and pd.notna(r.cpr_width) and r.cpr_width<=cfg['max_weekly_cpr_width']
                and bool(validvol.loc[dt]) and not blackout)
        choices=[(side,score(r,side)) for side in ('CE','PE')]
        chosen=max(choices,key=lambda x:x[1]); scores.append(chosen[1])
        trigger=bool(r.cross_up or r.break_up) if chosen[0]=='CE' else bool(r.cross_down or r.break_down)
        trend=(r.Close>r.ema200 and r.st_dir==1) if chosen[0]=='CE' else (r.Close<r.ema200 and r.st_dir==-1)
        strength=(r.adx>22 and r.adx>r.prev_adx and (r.pdi>r.mdi if chosen[0]=='CE' else r.mdi>r.pdi))
        momentum=(r.rsi>55 if chosen[0]=='CE' else r.rsi<45)
        signals.append(chosen[0] if common and chosen[1]>=cfg['min_score'] and trigger and trend and strength and momentum and r.Volume>r.vol20 else '')
    d['score']=scores; d['signal']=signals
    return d

