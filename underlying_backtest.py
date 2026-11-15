"""Underlying-only research simulator. No option chain, journal or order calls."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

IST = ZoneInfo('Asia/Kolkata')
TIMES = {'5 min':5, '15 min':15, '60 min':60, 'Daily':0}

def load_bars(api, instrument, start, end, minutes, now=None):
    now=now or datetime.now(IST)
    if start>end:
        raise ValueError('Start date must be before or equal to end date.')
    if minutes and start<(pd.Timestamp(now.date())-pd.DateOffset(years=5)).date():
        raise ValueError('Dhan minute history is limited to about five years.')
    warmup=450 if not minutes else 60
    first=start-timedelta(days=warmup)
    if minutes:
        first=max(first,(pd.Timestamp(now.date())-pd.DateOffset(years=5)).date())
    cursor=first
    final=min(end+timedelta(days=1),now.date()+timedelta(days=1))
    frames=[]
    while cursor<final:
        nxt=min(final,cursor+timedelta(days=85 if minutes else 730))
        p=dict(securityId=str(instrument['sid']),exchangeSegment=instrument['segment'],
               instrument=instrument['instrument'],oi=False,fromDate=cursor.isoformat(),toDate=nxt.isoformat())
        if minutes:
            p.update(interval=str(minutes),fromDate=p['fromDate']+' 00:00:00',toDate=p['toDate']+' 00:00:00')
        else:p['expiryCode']=0
        try:
            raw=api.post('/charts/intraday' if minutes else '/charts/historical',p)
        except RuntimeError as exc:
            # DH-907 can indicate no history (e.g. pre-listing), but it is not proof of coverage.
            if 'DH-907' not in str(exc):raise
            raw={}
        d=raw.get('data',raw)
        if d.get('timestamp'):
            index=pd.to_datetime(d['timestamp'],unit='s',utc=True).tz_convert(IST)
            if not minutes:index=index.normalize()
            frames.append(pd.DataFrame({k:pd.to_numeric(d.get(k,[np.nan]*len(index)),errors='coerce')
                                       for k in ('open','high','low','close','volume')},index=index))
        cursor=nxt
    if not frames:raise ValueError('Dhan returned no candles. Check the symbol, date range and Data API access.')
    f=pd.concat(frames).sort_index()
    f=f.loc[~f.index.duplicated(keep='last')]
    f=f.dropna(subset=['open','high','low','close'])
    f=f.loc[f[['open','high','low','close']].gt(0).all(axis=1)&f.high.ge(f[['open','close','low']].max(axis=1))&
            f.low.le(f[['open','close','high']].min(axis=1))]
    f=f.loc[(f.index.date>=first)&(f.index.date<=end)]
    if minutes:
        minute=f.index.hour*60+f.index.minute
        complete=f.index.normalize()+pd.to_timedelta(np.minimum(minute+minutes,930),unit='m')
        f=f.loc[(minute>=555)&(minute<930)&(complete<=now)]
    else:
        cut=now.date() if (now.hour,now.minute)>=(15,45) else now.date()-timedelta(days=1)
        f=f.loc[f.index.date<=cut]
    return f

def crosses(a,b,side):
    return (a.gt(b)&a.shift().le(b.shift())) if side==1 else (a.lt(b)&a.shift().ge(b.shift()))

def atr(f, n):
    t=pd.concat([f.high-f.low,(f.high-f.close.shift()).abs(),(f.low-f.close.shift()).abs()],axis=1).max(axis=1)
    return t.ewm(alpha=1/n,adjust=False,min_periods=n).mean()

def supertrend(f,n,m,return_line=False):
    mid=(f.high+f.low)/2
    a=atr(f,n)
    u=(mid+m*a).to_numpy(copy=True)
    l=(mid-m*a).to_numpy(copy=True)
    close=f.close.to_numpy()
    state=np.zeros(len(f),dtype=int)
    for i in range(1,len(f)):
        if np.isnan(u[i]):continue
        if not np.isnan(u[i-1]):
            if u[i]>=u[i-1] and close[i-1]<=u[i-1]:u[i]=u[i-1]
            if l[i]<=l[i-1] and close[i-1]>=l[i-1]:l[i]=l[i-1]
        state[i]=(1 if close[i]>=mid.iloc[i] else -1) if not state[i-1] else (
            (-1 if close[i]<l[i] else 1) if state[i-1]==1 else (1 if close[i]>u[i] else -1))
    direction=pd.Series(state,index=f.index)
    if return_line:
        return direction,pd.Series(np.where(state==1,l,np.where(state==-1,u,np.nan)),index=f.index)
    return direction

RULES={
    'ema_cross':'EMA 9/21 crossover',
    'ema_align':'EMA 9/21 alignment',
    'ema50_200':'EMA 50/200 alignment',
    'ema200':'Price vs EMA 200',
    'vwap':'Price vs session VWAP',
    'vwap_cross':'VWAP reclaim / loss',
    'rsi':'RSI (14) >55 / <45',
    'adx':'ADX (14) >22 and rising',
    'di':'Directional DI',
    'st10':'Supertrend (10,3) direction',
    'st7_cross':'Supertrend (7,2) flip',
    'volume':'Volume vs prior 20-bar average',
    'cpr':'Price outside CPR',
    'cpr_break':'CPR breakout / breakdown',
    'retest':'CPR breakout then retest',
    'pdh':'Prior-day high / low break',
    'camarilla':'Camarilla R3 / S3 break',
    'orb':'First 30-minute opening-range break',
}
INTRADAY_ONLY={'vwap','vwap_cross','orb'}
VOLUME_RULES={'vwap','vwap_cross','volume'}

def levels(f,weekly):
    grouping=f.index.tz_localize(None).to_period('W-FRI') if weekly else f.index.normalize()
    prior=f.groupby(grouping).agg({'high':'max','low':'min','close':'last'}).shift()
    return {k:pd.Series(prior[k].reindex(grouping).to_numpy(),index=f.index) for k in prior}

def conditions(f, side, minutes, volume_multiplier):
    c=f.close
    bull=side==1
    toward=lambda a,b:a.gt(b) if bull else a.lt(b)
    ema={n:c.ewm(span=n,adjust=False,min_periods=n).mean() for n in (9,21,50,200)}
    dif=c.diff()
    gain=dif.clip(lower=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    loss=(-dif.clip(upper=0)).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    rsi=(100-100/(1+gain/loss)).mask((gain==0)&(loss==0),50)
    a=atr(f,14)
    moveup=f.high.diff()
    movedown=-f.low.diff()
    di1=100*moveup.where((moveup>movedown)&(moveup>0),0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()/a.replace(0,np.nan)
    di2=100*movedown.where((movedown>moveup)&(movedown>0),0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()/a.replace(0,np.nan)
    adx=(100*(di1-di2).abs()/(di1+di2).replace(0,np.nan)).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    st10=supertrend(f,10,3); st7=supertrend(f,7,2)
    prior=levels(f,False)
    cpr=levels(f,not minutes)
    pivot=(cpr['high']+cpr['low']+cpr['close'])/3
    bc=(cpr['high']+cpr['low'])/2
    tc=2*pivot-bc
    boundary=pd.Series(np.maximum(bc,tc) if bull else np.minimum(bc,tc),index=f.index)
    hit=crosses(c,boundary,side)
    group=f.index.normalize() if minutes else f.index.tz_localize(None).to_period('W-FRI')
    broke=hit.groupby(group).cummax().groupby(group).shift(fill_value=False)
    r3=cpr['close']+side*(cpr['high']-cpr['low'])*1.1/4
    rule={
        'ema_cross':crosses(ema[9],ema[21],side),'ema_align':toward(ema[9],ema[21]),
        'ema50_200':toward(ema[50],ema[200]),'ema200':toward(c,ema[200]),
        'rsi':rsi.gt(55) if bull else rsi.lt(45),'adx':adx.gt(22)&adx.gt(adx.shift()),
        'di':toward(di1,di2),'st10':st10.eq(side),'st7_cross':st7.eq(side)&st7.shift().eq(-side),
        'volume':f.volume.gt(0)&f.volume.ge(volume_multiplier*f.volume.shift().rolling(20).mean()),
        'cpr':toward(c,boundary),'cpr_break':hit,
        'retest':broke&toward(c,boundary)&(f.low.le(boundary) if bull else f.high.ge(boundary))&toward(c,f.open),
        'pdh':crosses(c,prior['high' if bull else 'low'],side),'camarilla':crosses(c,r3,side),
    }
    if minutes:
        day=f.index.normalize()
        v=f.volume.where(f.volume>0)
        vw=((f.high+f.low+c)/3*v).groupby(day).cumsum()/v.groupby(day).cumsum()
        same_day=pd.Series(day,index=f.index).eq(pd.Series(day,index=f.index).shift())
        rule.update(vwap=toward(c,vw),vwap_cross=crosses(c,vw,side)&same_day)
        if minutes<=15:
            mm=f.index.hour*60+f.index.minute
            first=(mm>=555)&(mm<585)
            s=f['high' if bull else 'low'].where(first)
            opening=s.groupby(day).transform('max' if bull else 'min')
            enough=s.groupby(day).transform('count').eq(30//minutes)
            # Opening levels are used only after all opening-range bars have closed.
            rule['orb']=crosses(c,opening,side)&same_day&enough&(mm>=585)
    return rule

@dataclass(frozen=True)
class Plan:
    capital:float
    allocation:float
    cost_bps:float
    slip_bps:float
    stop:float
    target:float
    max_bars:int
    minutes:int
    flat:bool
    entry_start:int=555
    entry_end:int=930
    daily_max:int=0
    signal_only:bool=False

def simulate(f, entries, exits, plan, start, entry_audit=None, exit_audit=None):
    """Use completed-bar signals, subsequent opens and conservative OHLC exits."""
    cash=plan.capital
    cost=plan.cost_bps/10000
    slip=plan.slip_bps/10000
    position=None
    pending=0
    pending_exit=None
    ledger=[]
    daily_entries=0
    prior_session=None
    marks=[]
    bars=f[['open','high','low','close']].itertuples(index=True,name=None)
    for i,(stamp,o,h,l,c) in enumerate(bars):
        if stamp.date()<start:continue
        last=i==len(f)-1
        crossed_day=i>0 and f.index[i-1].date()!=stamp.date()
        if stamp.date()!=prior_session:
            daily_entries=0
            prior_session=stamp.date()
        minute=stamp.hour*60+stamp.minute
        eod=plan.flat and minute+plan.minutes>=930
        open_time=stamp if plan.minutes else stamp.normalize()+pd.Timedelta(minutes=555)
        def close(raw, reason, matches="", signal_time=pd.NaT, phase="Intrabar (estimated)"):
            bar_end = stamp.normalize() + pd.Timedelta(minutes=min(minute+plan.minutes,930) if plan.minutes else 930)
            exit_time = open_time if phase=="Open" else bar_end if phase=="Close" else pd.NaT
            nonlocal cash,position
            p=position
            fill=raw*(1-p['side']*slip)
            fees=p['fees']+fill*p['qty']*cost
            net=(fill-p['price'])*p['qty']*p['side']-fees
            cash+=net
            ledger.append({'Entry signal bar start (IST)':p['signal'],'Entry fill time (IST)':p['time'], 'Exit signal bar start (IST)':signal_time,'Exit bar start (IST)':stamp,'Exit fill time (IST)':exit_time,'Exit fill phase':phase,
                           'Direction':'Long' if p['side']==1 else 'Short proxy','Units':p['qty'],
                            'Entry raw price':p['raw'],'Exit raw price':raw,'Entry price':p['price'],'Exit price':fill,
                           'Gross P&L at raw prices':(raw-p['raw'])*p['qty']*p['side'],
                           'Slippage':((raw-p['raw'])*p['side']-(fill-p['price'])*p['side'])*p['qty'],
                           'Costs':fees,'Net P&L':net,'Matched entry rules':p['matches'],
                           'Matched exit rules':matches,
                           'Equity after exit':cash,'Exit reason':reason})
            position=None
        if position and plan.flat and crossed_day:close(o,'Next session open — closing bar missing',phase='Open')
        if position and pending_exit:close(o,*pending_exit,phase="Open")
        if plan.flat and crossed_day:pending=0
        window_ok=not plan.minutes or plan.entry_start<=minute<plan.entry_end
        quota_ok=not plan.daily_max or daily_entries<plan.daily_max
        if not position and pending and not last and not eod and window_ok and quota_ok:
            fill=o*(1+pending*slip)
            qty=1 if plan.signal_only else int(max(cash,0)*plan.allocation/100/(fill*(1+cost)))
            if qty>=1:
                position=dict(side=pending,price=fill,qty=qty,fees=fill*qty*cost,time=open_time,
                              signal=f.index[i-1],bars=0,raw=o,
                              matches=entry_audit[pending].iat[i-1] if entry_audit else '')
                daily_entries+=1
        pending=0
        pending_exit=None
        if position:
            p=position
            s=p['side']; px=p['price']
            stop=px*(1-s*plan.stop/100)
            goal=px*(1+s*plan.target/100)
            stop_hit=plan.stop>0 and (l<=stop if s==1 else h>=stop)
            target_hit=plan.target>0 and (h>=goal if s==1 else l<=goal)
            # If the open has already crossed a level it has priority; otherwise stop first.
            if plan.stop and (o<=stop if s==1 else o>=stop):close(o,'Stop gap',phase='Open')
            elif plan.target and (o>=goal if s==1 else o<=goal):close(goal,'Target at open (capped)',phase='Open')
            elif stop_hit:close(stop,'Stop first — both touched' if target_hit else 'Stop')
            elif target_hit:close(goal,'Target')
        if position:
            position['bars']+=1
            if eod:close(c,'Session close',phase='Close')
            elif last:close(c,'End of sample',phase='Close')
            else:
                if bool(exits[position['side']].iat[i]):pending_exit=('Selected exit conditions',exit_audit[position['side']].iat[i] if exit_audit else '',stamp)
                elif plan.max_bars>0 and position['bars']>=plan.max_bars:pending_exit=('Maximum bars held','')
        elif not last and not eod:
            signals=[s for s,v in entries.items() if bool(v.iat[i])]
            if len(signals)==1:pending=signals[0]
        unrealized=(c-position['price'])*position['qty']*position['side']-position['fees'] if position else 0
        marks.append((stamp,cash+unrealized))
    curve=pd.DataFrame(marks,columns=['Bar (IST)','Equity']).set_index('Bar (IST)')
    return pd.DataFrame(ledger),curve


# Conditions use comparisons of completed candles. The broker provides historical
# OHLCV, while the app computes the indicator series.
SERIES = {
    'Open': (), 'High': (), 'Low': (), 'Close': (), 'Volume': (),
    'EMA': (9,), 'SMA': (20,), 'WMA': (20,), 'RSI': (14,), 'ATR': (14,),
    'ADX': (14,), '+DI': (14,), '-DI': (14,), 'Supertrend': (10, 3.),
    'Supertrend direction': (7, 2.), 'MACD line': (12,26,9),
    'MACD signal': (12,26,9), 'MACD histogram': (12,26,9), 'CCI': (20,),
    'Williams %R': (14,), 'Stochastic %K': (14,), 'ROC %': (12,),
    'Bollinger upper': (20,2.), 'Bollinger middle': (20,), 'Bollinger lower': (20,2.),
    'Highest high (prior bars)': (20,), 'Lowest low (prior bars)': (20,),
    'Prior volume average': (20,), 'Relative volume': (20,), 'Session VWAP': (),
    'Prior day high': (), 'Prior day low': (), 'Prior day close': (),
    'Prior week high': (), 'Prior week low': (), 'Prior week close': (),
    'CPR top': (), 'CPR bottom': (), 'CPR width %': (),
    'Pivot': (), 'Pivot R1': (), 'Pivot S1': (), 'Camarilla R3': (), 'Camarilla S3': (),
    'Opening range high (30 min)': (), 'Opening range low (30 min)': (),
}
VW = {'Session VWAP','Volume','Prior volume average','Relative volume'}
OPEN_RANGE = {'Opening range high (30 min)','Opening range low (30 min)'}
OPS = ('Above','Below','At or above','At or below','Equal to','Crosses above','Crosses below')

@dataclass(frozen=True)
class Operand:
    name:str
    args:tuple=()
    lag:int=0

@dataclass(frozen=True)
class Clause:
    join:str
    left:Operand
    op:str
    right:Operand=Operand('Number')

def available(minutes):
    return [n for n in SERIES if not
            (n in OPEN_RANGE and minutes not in (5,15)
             or n=='Session VWAP' and not minutes)]

def legacy_available(minutes):
    return [k for k in RULES if (minutes or k not in INTRADAY_ONLY)
            and not (minutes==60 and k=='orb')]

class MarketSeries:
    def __init__(self, bars, minutes):
        self.f=bars
        self.minutes=minutes
        self.day=bars.index.normalize()
        self.cache={}

    def get(self, operand):
        key=(operand.name,operand.args)
        if key not in self.cache:
            self.cache[key]=self.build(*key)
        return self.cache[key].shift(operand.lag) if operand.lag else self.cache[key]

    def cached_levels(self, weekly):
        key=('levels',weekly)
        if key not in self.cache:
            self.cache[key]=levels(self.f,weekly)
        return self.cache[key]

    def build(self, name, args):
        f=self.f
        c=f.close
        if name=='Number':
            return pd.Series(args[0],index=f.index,dtype=float)
        if name in ('Open','High','Low','Close','Volume'):
            return f[name.lower()]
        if name.startswith(('Prior day ','Prior week ')):
            return self.cached_levels(name.startswith('Prior week'))[name.split()[-1]]
        if name in OPEN_RANGE:
            mm=f.index.hour*60+f.index.minute
            v=(f.high if 'high' in name else f.low).where((mm>=555)&(mm<585))
            return v.groupby(self.day).transform('max' if 'high' in name else 'min').where(
                (mm+self.minutes>=585)&(v.groupby(self.day).transform('count')==30//self.minutes))
        if name=='Session VWAP':
            v=f.volume.where(f.volume>0)
            return (((f.high+f.low+c)/3*v).groupby(self.day).cumsum()/v.groupby(self.day).cumsum())
        if name in ('Pivot','CPR top','CPR bottom','CPR width %','Pivot R1','Pivot S1','Camarilla R3','Camarilla S3'):
            prior=self.cached_levels(not self.minutes)
            h,l,pc=(prior[k] for k in ('high','low','close'))
            p=(h+l+pc)/3
            b=(h+l)/2
            t=2*p-b
            return {'Pivot':p,'CPR top':np.maximum(b,t),'CPR bottom':np.minimum(b,t),
                    'CPR width %':(t-b).abs()/p.replace(0,np.nan)*100,
                    'Pivot R1':2*p-l,'Pivot S1':2*p-h,
                    'Camarilla R3':pc+1.1*(h-l)/4,'Camarilla S3':pc-1.1*(h-l)/4}[name]
        n=int(args[0])
        roll=c.rolling(n)
        if name=='EMA':return c.ewm(span=n,adjust=False,min_periods=n).mean()
        if name=='SMA':return roll.mean()
        if name=='WMA':
            weights=np.arange(1,n+1)
            return roll.apply(lambda x:np.dot(x,weights)/weights.sum(),raw=True)
        if name=='ATR':return atr(f,n)
        if name=='RSI':
            delta=c.diff()
            up=delta.clip(lower=0).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
            down=(-delta.clip(upper=0)).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
            return (100-100/(1+up/down)).mask((up==0)&(down==0),50)
        if name in ('ADX','+DI','-DI'):
            up=f.high.diff(); down=-f.low.diff()
            den=self.get(Operand('ATR',(n,))).replace(0,np.nan)
            plus=100*up.where((up>down)&(up>0),0).ewm(alpha=1/n,adjust=False,min_periods=n).mean()/den
            minus=100*down.where((down>up)&(down>0),0).ewm(alpha=1/n,adjust=False,min_periods=n).mean()/den
            dx=(100*(plus-minus).abs()/(plus+minus).replace(0,np.nan)).mask((plus==0)&(minus==0),0)
            return {'+DI':plus,'-DI':minus,'ADX':dx.ewm(alpha=1/n,adjust=False,min_periods=n).mean()}[name]
        if name in ('Supertrend','Supertrend direction'):
            key=('st',args)
            if key not in self.cache:self.cache[key]=supertrend(f,n,args[1],return_line=True)
            return self.cache[key][int(name=='Supertrend')].replace(0,np.nan)
        if name.startswith('MACD'):
            a,b,s=args
            line=self.get(Operand('EMA',(a,)))-self.get(Operand('EMA',(b,)))
            signal=line.ewm(span=s,adjust=False,min_periods=s).mean()
            return {'MACD line':line,'MACD signal':signal,'MACD histogram':line-signal}[name]
        if name.startswith('Bollinger'):
            mid=roll.mean()
            if name=='Bollinger middle':return mid
            dev=roll.std(ddof=0)*args[1]
            return mid+dev if name.endswith('upper') else mid-dev
        if name=='CCI':
            typ=(f.high+f.low+c)/3
            tr=typ.rolling(n)
            mad=tr.apply(lambda x:np.abs(x-x.mean()).mean(),raw=True)
            return (typ-tr.mean())/(.015*mad.replace(0,np.nan))
        if name in ('Williams %R','Stochastic %K'):
            hi=f.high.rolling(n).max(); lo=f.low.rolling(n).min()
            t=100*(c-lo)/(hi-lo).replace(0,np.nan)
            return t if name=='Stochastic %K' else t-100
        if name=='ROC %':return (c/c.shift(n)-1)*100
        if name=='Highest high (prior bars)':return f.high.shift().rolling(n).max()
        if name=='Lowest low (prior bars)':return f.low.shift().rolling(n).min()
        if name=='Prior volume average':return f.volume.shift().rolling(n).mean()
        if name=='Relative volume':
            return f.volume/self.get(Operand('Prior volume average',(n,))).replace(0,np.nan)
        raise ValueError(f'Unknown indicator {name}')

def describe_operand(operand):
    args=', '.join(f'{v:g}' for v in operand.args)
    label=args if operand.name=='Number' else operand.name+(f' ({args})' if args else '')
    return label+(f' [{operand.lag} bars ago]' if operand.lag else '')

def describe_rule(row):
    if row.op=='Setup':return describe_operand(row.left)
    return f'{describe_operand(row.left)} {row.op.lower()} {describe_operand(row.right)}'

def evaluate(market, rows):
    """AND binds inside groups separated by OR. Missing data cannot trigger a rule."""
    ix=market.f.index
    result=pd.Series(False,index=ix)
    group=None
    group_labels=None
    audit=pd.Series('',index=ix)
    for i,r in enumerate(rows,1):
        if r.op=='Setup':
            _,key,side=r.left.name.split('/')
            k=('setup',side,r.left.args)
            if k not in market.cache:
                market.cache[k]=conditions(market.f,int(side),market.minutes,r.left.args[0])
            hit=market.cache[k][key].fillna(False)
        else:
            a=market.get(r.left); b=market.get(r.right)
            hit={'Above':a.gt,'Below':a.lt,'At or above':a.ge,'At or below':a.le,
                 'Equal to':a.eq}.get(r.op)
            hit=hit(b) if hit else crosses(a,b,1 if r.op=='Crosses above' else -1)
            hit=hit & a.notna() & b.notna()
            if (r.op.startswith('Crosses') and market.minutes
                and any(o.name=='Session VWAP' for o in (r.left,r.right))):
                ds=pd.Series(market.day,index=ix)
                hit &= ds.eq(ds.shift())
        label=pd.Series(f'{i}, ',index=ix).where(hit,'')
        if group is None or r.join=='OR':
            if group is not None:
                result |= group
                audit += group_labels.where(group,'')
            group=hit.copy()
            group_labels=label
        else:
            group &= hit
            group_labels += label
    if group is not None:
        result |= group
        audit += group_labels.where(group,'')
    return result,audit.str.rstrip(', ')

def operand_widgets(box,key,options,default,allow_number=False):
    options=(['Number'] if allow_number else [])+options
    name=box.selectbox('Compare with' if allow_number else 'Indicator or price',options,
                       index=options.index(default.name),key=key+'_name')
    if name=='Number':
        return Operand(name,(box.number_input('Value',value=float(default.args[0] if default.name==name else 0.),
                                             key=key+'_number'),))
    args=[]
    for i,v in enumerate(SERIES[name]):
        if name.startswith('MACD'):label=('Fast period','Slow period','Signal period')[i]
        elif i==1:label='Multiplier'
        else:label='Period'
        val=default.args[i] if default.name==name and len(default.args)>i else v
        args.append(box.number_input(label,min_value=1 if isinstance(v,int) else .1,
                                     max_value=300 if isinstance(v,int) else 20.,
                                     value=int(val) if isinstance(v,int) else float(val),
                                     key=f'{key}_{name}_{i}'))
    lag=box.number_input('Bars ago',min_value=0,max_value=50,value=0,key=key+'_lag',
                         help='0 is the candle that just completed. 1 is the previous candle.')
    return Operand(name,tuple(args),lag)

SIDES={1:'Call (CE)',-1:'Put (PE)'}
MODES={'Call (CE) only':(1,), 'Put (PE) only':(-1,), 'Compare Call and Put':(1,-1)}

def rule_widgets(st, phase, side, minutes):
    label=SIDES[side]+' '+phase
    key=f'qb_{minutes}_{side}_{phase}'
    options=available(minutes)
    default_op='Crosses above' if (side==1)==(phase=='entry') else 'Crosses below'
    rows=[]
    with st.expander(label,expanded=phase=='entry'):
        count=st.number_input('Number of conditions',min_value=int(phase=='entry'),
                              value=1,step=1,key=key+'_count',
                              help='Add as many rules as needed. Very large rule sets can slow the app. Use 0 exit conditions to rely on other exit settings.')
        for i in range(count):
            rk=f'{key}_{i}'
            st.markdown(f'**Condition {i+1}**')
            join=st.radio('Join to previous condition',('AND','OR'),horizontal=True,key=rk+'_join') if i else 'AND'
            kind=st.radio('Rule type',('Comparison','Built-in setup'),horizontal=True,key=rk+'_type')
            if kind=='Built-in setup':
                a,b=st.columns(2)
                setup=a.selectbox('Setup',legacy_available(minutes),format_func=RULES.__getitem__,key=rk+'_setup')
                direction=b.selectbox('Setup direction',('Bullish','Bearish'),
                                      index=int(default_op=='Crosses below'),key=rk+'_dir')
                mult=b.number_input('Volume multiplier',.1,20.,1.5,step=.1,key=rk+'_mult') if setup=='volume' else 1.5
                rows.append(Clause(join,Operand(f'Setup/{setup}/{1 if direction=="Bullish" else -1}',(mult,)),'Setup'))
            else:
                a,b,c=st.columns(3)
                left=operand_widgets(a,rk+'_left',options,Operand('EMA',(9,)))
                op=b.selectbox('Operator',OPS,index=OPS.index(default_op),key=rk+'_op')
                right=operand_widgets(c,rk+'_right',options,Operand('EMA',(21,)),True)
                rows.append(Clause(join,left,op,right))
        if rows:st.caption(format_rows(rows))
    return rows

def format_rows(rows):
    groups=[]
    for row in rows:
        if not groups or row.join=='OR':groups.append([])
        if row.op=='Setup':
            _,key,side=row.left.name.split('/')
            desc=('Bullish ' if side=='1' else 'Bearish ')+RULES[key]
            if key=='volume':desc+=f' ×{row.left.args[0]:g}'
        else:desc=describe_rule(row)
        groups[-1].append(desc)
    return ' OR '.join('('+ ' AND '.join(group)+')' for group in groups) or 'None'

def validate_rows(rows):
    for row in rows:
        for o in (row.left,row.right):
            if o.name.startswith('MACD') and o.args[0]>=o.args[1]:
                raise ValueError('MACD fast period must be shorter than the slow period.')

def needs_volume(rows):
    return any(o.name in VW or o.name in {f'Setup/{key}/{s}' for key in VOLUME_RULES for s in (1,-1)}
               for row in rows for o in (row.left,row.right))

def score_signals(trades, side, min_move):
    """Win rate uses all completed signals, including flat and sub-threshold moves."""
    moves=pd.Series(dtype=float) if trades.empty else (
        side*(trades['Exit raw price']/trades['Entry raw price']-1)*100)
    tolerance=1e-10
    flat=moves.abs().le(tolerance)
    wins=moves.gt(tolerance)&moves.ge(min_move-tolerance)
    adverse=moves.lt(-tolerance)
    small=~(flat|wins|adverse)
    rates={
        'Completed signals':len(moves),'Wins':int(wins.sum()),
        'Adverse':int(adverse.sum()),'Flat':int(flat.sum()),
        'Favorable below minimum':int(small.sum()),
        'Win %':100*float(wins.mean()) if len(moves) else None,
        'Avg signed move %':float(moves.mean()) if len(moves) else None,
    }
    out=trades.drop(columns=['Direction','Units','Gross P&L at raw prices','Slippage','Costs',
                             'Net P&L','Equity after exit','Entry price','Exit price'],errors='ignore').copy()
    out.insert(0,'Signal side',SIDES[side])
    out['Signed underlying move %']=moves
    out['Outcome']=np.select([wins.to_numpy(),adverse.to_numpy(),flat.to_numpy()],
                             ['Win','Adverse','Flat'],default='Favorable below minimum')
    return rates,out

def render_backtest(universe, token, client, make_api, default_frame):
    import hashlib
    import json
    from dataclasses import asdict
    from datetime import time
    import streamlit as st

    st.subheader('Call / Put Signal Backtest')
    st.caption('Directional replay on Dhan underlying candles. No option premiums or broker orders. '
               'CE wins when the underlying rises by your required move. PE wins when it falls by that amount.')
    if universe.empty:
        st.info('The Dhan instrument list is unavailable. Try reloading the app.')
        return
    symbols=sorted(universe.symbol.drop_duplicates().tolist())
    a,b,c=st.columns(3)
    symbol=a.selectbox('F&O stock or index',symbols,index=symbols.index('NIFTY') if 'NIFTY' in symbols else 0,key='qb_symbol')
    tf=b.selectbox('Timeframe',list(TIMES),index=list(TIMES).index(default_frame),key='qb_frame')
    mode=c.selectbox('Signals to test',list(MODES),index=2,key='qb_ce_pe_mode')
    minutes=TIMES[tf]
    sides=MODES[mode]
    st.caption('Call and Put are replayed independently. In Compare mode one side never blocks the other. '
               'Intraday entry limits apply per side.')
    st.caption('AND joins rules in the same group. OR starts a new group. '
               'For example, Condition 1 AND Condition 2 OR Condition 3 means (1 AND 2) OR 3.')
    st.caption('CPR, pivots and Camarilla use the prior session on minute bars and the prior week on daily bars.')
    entry={s:rule_widgets(st,'entry',s,minutes) for s in sides}
    exit={s:rule_widgets(st,'exit',s,minutes) for s in sides}
    now=datetime.now(IST)
    end_default=now.date()-timedelta(days=1)
    start_default=end_default-timedelta(days=90 if minutes else 1095)

    @st.cache_data(ttl=600,max_entries=12,show_spinner=False)
    def cached_data(account, sid, segment, instrument, start, end, interval, _token, _client, _api):
        return load_bars(_api(_token,_client),dict(sid=sid,segment=segment,instrument=instrument),start,end,interval)

    is_daytrade=default_frame!='Daily' and bool(minutes)
    with st.form('ce_pe_run_'+str(minutes)):
        a,b,c=st.columns(3)
        start=a.date_input('Test start',start_default,min_value=date(1970,1,1),max_value=end_default)
        end=b.date_input('Test end',end_default,min_value=date(1970,1,1),max_value=now.date())
        name=c.text_input('Strategy name',max_chars=80,placeholder='Example: EMA with RSI confirmation')
        a,b=st.columns(2)
        min_move=a.number_input('Minimum favorable underlying move to count as a win (%)',
                                min_value=0.,value=0.,step=.05,format='%.3f',
                                help='0 counts any strictly favorable move as a win. Flat results never count as wins.')
        flat=b.checkbox('Close at end of each session',value=is_daytrade,disabled=not minutes)
        a,b,c=st.columns(3)
        stop=a.number_input('Underlying stop % (0 = off)',min_value=0.,max_value=99.,value=0.,step=.1)
        target=b.number_input('Underlying target % (0 = off)',min_value=0.,max_value=99.,value=0.,step=.1)
        holding=c.number_input('Maximum bars held (0 = off)',min_value=0,value=0,step=1)
        if minutes:
            a,b,c=st.columns(3)
            first_fill=a.time_input('Earliest entry fill (IST)',time(9,30) if is_daytrade else time(9,15),step=300)
            last_fill=b.time_input('No new entries from (IST)',time(15,15) if is_daytrade else time(15,30),step=300)
            daily_max=c.number_input('Maximum entries per side per day (0 = off)',min_value=0,value=2 if is_daytrade else 0)
            en_start=first_fill.hour*60+first_fill.minute
            en_end=last_fill.hour*60+last_fill.minute
        else:
            en_start,en_end,daily_max=555,930,0
        submitted=st.form_submit_button('Run signal backtest',disabled=not bool(token),type='primary')
    if not token:st.info('Enter your Dhan access token to download candles.')
    account=hashlib.sha256(f'{client or ""}|{token or ""}'.encode()).hexdigest()
    identity=(symbol,tf,account,mode)
    state=st.session_state
    if submitted:
        try:
            if not 555<=en_start<en_end<=930:raise ValueError('Use an entry window within 09:15–15:30 IST.')
            rows=[r for rules in (*entry.values(),*exit.values()) for r in rules]
            validate_rows(rows)
            with st.spinner('Loading Dhan candles and replaying signals…'):
                meta=universe[universe.symbol.eq(symbol)].iloc[0]
                f=cached_data(account,str(meta.sid),meta.segment,meta.instrument,start,end,minutes,token,client,make_api)
                test=f[f.index.date>=start]
                if len(test)<2:raise ValueError('Fewer than two usable candles in the requested dates.')
                market=MarketSeries(f,minutes)
                if needs_volume(rows) and not test.volume.gt(0).any():
                    raise ValueError('Dhan returned no positive underlying volume for this period. Remove volume/VWAP conditions or choose an instrument with volume.')
                plan=Plan(0.,100.,0.,0.,stop,target,holding,minutes,bool(flat and minutes),
                          en_start,en_end,daily_max,signal_only=True)
                results=[]
                logs=[]
                for side in sides:
                    en,ea=evaluate(market,entry[side])
                    ex,xa=evaluate(market,exit[side])
                    trades,_=simulate(f,{side:en},{side:ex},plan,start,{side:ea},{side:xa})
                    stats,log=score_signals(trades,side,min_move)
                    results.append({'Side':SIDES[side],**stats})
                    if not log.empty:logs.append(log)
                trades=pd.concat(logs,ignore_index=True).sort_values('Entry fill time (IST)') if logs else pd.DataFrame()
                run_no=state.get('ce_pe_run_no',0)+1
                state.ce_pe_run_no=run_no
                trade_label=name.strip() or f'Strategy {run_no}'
                settings=dict(name=trade_label,symbol=symbol,timeframe=tf,requested_start=start.isoformat(),
                              requested_end=end.isoformat(),first_candle_ist=test.index[0].isoformat(),
                              last_candle_ist=test.index[-1].isoformat(),bars=len(test),sessions=len(set(test.index.date)),
                              warmup_bars=len(f)-len(test),mode=mode,min_favorable_move_pct=min_move,
                              replay=dict(stop_pct=stop,target_pct=target,max_bars=holding,flat=plan.flat,
                                          entry_start_ist_minutes=en_start,entry_end_ist_minutes=en_end,
                                          max_entries_per_side_per_day=daily_max),
                              entry_rules={SIDES[s]:[asdict(r) for r in entry[s]] for s in sides},
                              exit_rules={SIDES[s]:[asdict(r) for r in exit[s]] for s in sides},
                              entry_text={SIDES[s]:format_rows(entry[s]) for s in sides},
                              exit_text={SIDES[s]:format_rows(exit[s]) for s in sides})
                fingerprint=hashlib.sha256(pd.util.hash_pandas_object(f,index=True).to_numpy().tobytes()).hexdigest()
                match=((symbol,tf,account),start,end,fingerprint,min_move,en_start,en_end,daily_max,plan.flat)
                runs=state.get('ce_pe_runs',[])
                state.ce_pe_runs=(runs+[(match,{'Run':run_no,'Strategy':trade_label,**stats})
                                        for stats in results])[-100:]
                state.ce_pe_result=(identity,settings,trades,results,match)
        except Exception as exc:
            st.error(f'Backtest failed — {str(exc)[:320]}')
            return

    saved=state.get('ce_pe_result')
    if not saved or saved[0]!=identity:return
    _,settings,trades,results,match=saved
    st.markdown(f'**Last completed run · {settings["name"]} · {symbol} · {tf}**')
    st.caption(f'Requested {settings["requested_start"]} → {settings["requested_end"]} · Returned '
               f'{settings["first_candle_ist"]} → {settings["last_candle_ist"]} · {settings["bars"]:,} bars · '
               f'{settings["sessions"]:,} sessions · {settings["warmup_bars"]:,} warm-up bars.')
    st.caption(f'Win threshold {settings["min_favorable_move_pct"]:g}% · '
               'Win % = wins ÷ all completed signals. Flat and sub-threshold moves remain in the denominator.')
    for side in settings['entry_text']:
        st.text(f'{side} entry — {settings["entry_text"][side]}')
        st.text(f'{side} exit — {settings["exit_text"][side]}')
    cols={'Win %':st.column_config.NumberColumn(format='%.2f%%'),
          'Avg signed move %':st.column_config.NumberColumn(format='%.3f%%')}
    st.dataframe(pd.DataFrame(results),hide_index=True,column_config=cols)
    if trades.empty:st.info('No completed signals for these rules and candles. A missing win rate does not mean 0%.')
    else:
        st.dataframe(trades,hide_index=True)
        st.download_button('Download signal trades CSV',trades.to_csv(index=False),'ce_pe_signals.csv')
    st.download_button('Download run settings JSON',json.dumps(settings,indent=2),'ce_pe_settings.json')
    matches=[row for scope,row in state.get('ce_pe_runs',[]) if scope==match]
    if len({row['Run'] for row in matches})>1:
        st.markdown('**Compare strategies on matching data**')
        st.caption('Sorted within each side by win %. Read it with the completed-signal count. '
                   'A small sample or a high win % alone does not establish an edge. '
                   'Test your shortlist on new dates before drawing conclusions.')
        comparison=pd.DataFrame(matches).sort_values(
            ['Side','Win %','Completed signals'],ascending=[True,False,False],na_position='last')
        st.dataframe(comparison,hide_index=True,column_config=cols)
        st.download_button('Download strategy comparison CSV',comparison.to_csv(index=False),'ce_pe_comparison.csv')
    st.caption('Each side holds at most one signal at a time. Entry and indicator exit use the next available open. '
               'Stops and targets refer to the underlying, and stop wins if both are touched in the same candle '
               'unless the open already triggered an exit. Remaining signals are closed on the last available bar. '
               'No premium, time decay, volatility, execution costs or option P&L is modeled.')
