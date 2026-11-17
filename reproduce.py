from pathlib import Path
import sys
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from swing_breakout import compare,summarize

if len(sys.argv)!=2:
    raise SystemExit('Usage: python reproduce.py /path/to/source-files')
root=Path(sys.argv[1]).resolve()
def aggregate(m):
    m=m.sort_index().loc[lambda d:~d.index.duplicated(keep='last')]
    minutes=m.index.hour*60+m.index.minute
    m=m.loc[(minutes >= 555)&(minutes < 930)&(m.index.second==0)&(m.index.dayofweek<5)]
    group=m.index.normalize()
    rel=m.index.hour*60+m.index.minute-555
    hour=group+pd.to_timedelta(555+60*(rel//60),unit='m')
    cols={'Open':'first','High':'max','Low':'min','Close':'last'}
    if 'Volume' in m: cols['Volume']='sum'
    g=m.groupby(hour)
    h=g.agg(cols).loc[lambda x:g.size().eq(60)]
    g=m.groupby(group)
    d=g.agg(cols).loc[lambda x:g.size().eq(375)]
    return d,h

def nifty():
    m=pd.concat([pd.read_csv(root/f'nifty_{y}.csv') for y in (2023,2024,2025)])
    m.index=pd.to_datetime(m.pop('Timestamp'))
    derived,h=aggregate(m)
    daily=pd.concat([pd.read_csv(root/f'nifty_daily_{y}.csv') for y in (2023,2024,2025)])
    daily.columns=daily.columns.str.strip()
    daily.index=pd.to_datetime(daily.pop('Date'),format='%d-%b-%Y')
    q=daily.join(derived[['Close']].rename(columns={'Close':'1m close'})).dropna(subset=['1m close'])
    print('NIFTY check: median absolute closing difference (official-style daily vs last minute):',abs(q.Close-q['1m close']).median())
    return daily,h

trades=[];coverage=[]
start=pd.Timestamp('2024-01-01'); split=pd.Timestamp('2025-01-01'); end=pd.Timestamp('2025-12-31')
for symbol in ('NIFTY','AXISBANK','BHARTIARTL','HDFCBANK','ICICIBANK','INFY','LT','RELIANCE','SBIN','TCS'):
    if symbol=='NIFTY': d,h=nifty()
    else:
        m=pd.read_parquet(root/f'{symbol}.parquet')
        m.columns=[str(c).title() for c in m.columns]
        if not isinstance(m.index,pd.DatetimeIndex):
            k=next(c for c in m.columns if c in ('Date','Datetime','Timestamp','Date_Time'))
            m.index=pd.to_datetime(m.pop(k))
        if m.index.tz: m.index=m.index.tz_convert('Asia/Kolkata').tz_localize(None)
        m=m.sort_index(kind='stable').loc['2023':'2025'].copy()
        # Adopt the data vendor's price transition date, verified from the
        # open/previous-close ratio around each documented 1:1 bonus.
        transition={'RELIANCE':'2024-10-28','HDFCBANK':'2025-08-25'}.get(symbol)
        if transition:
            mask=m.index<pd.Timestamp(transition)
            ohlc=['Open','High','Low','Close']
            m.loc[mask,ohlc]=m.loc[mask,ohlc]/2
        d,h=aggregate(m)
    print(symbol,len(d),len(h),d.index.min(),d.index.max())
    out,cov=compare(symbol,d,h,start,end,split)
    trades+=out; coverage+=cov
t=pd.DataFrame(trades)
s=summarize(t,5)
for title,frame in [('trades',t),('summary',s),('coverage',pd.DataFrame(coverage))]:
    frame.to_csv(root/f'result_{title}.csv',index=False)
print(s[s.Side.eq('Both')].round(3).to_string(index=False))
print(pd.DataFrame(coverage).to_string(index=False))
