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

def supertrend(f,n,m):
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
    return pd.Series(state,index=f.index)

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

def combine(rules, selected, logic, index):
    if not selected:return pd.Series(False,index=index)
    d=pd.DataFrame({k:rules[k] for k in selected},index=index).fillna(False)
    return d.all(axis=1) if logic=='All selected' else d.any(axis=1)

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

def simulate(f, entries, exits, plan, start):
    """Use completed-bar signals, subsequent opens and conservative OHLC exits."""
    cash=plan.capital
    cost=plan.cost_bps/10000
    slip=plan.slip_bps/10000
    position=None
    pending=0
    pending_exit=None
    ledger=[]
    marks=[]
    bars=f[['open','high','low','close']].itertuples(index=True,name=None)
    for i,(stamp,o,h,l,c) in enumerate(bars):
        if stamp.date()<start:continue
        last=i==len(f)-1
        crossed_day=i>0 and f.index[i-1].date()!=stamp.date()
        minute=stamp.hour*60+stamp.minute
        eod=plan.flat and minute+plan.minutes>=930
        def close(raw, reason):
            nonlocal cash,position
            p=position
            fill=raw*(1-p['side']*slip)
            fees=p['fees']+fill*p['qty']*cost
            net=(fill-p['price'])*p['qty']*p['side']-fees
            cash+=net
            ledger.append({'Signal time (bar start)':p['signal'],'Entry time':p['time'],'Exit bar / fill time':stamp,
                           'Direction':'Long' if p['side']==1 else 'Short proxy','Units':p['qty'],
                           'Entry price':p['price'],'Exit price':fill,'Costs':fees,'Net P&L':net,
                           'Equity after exit':cash,'Exit reason':reason})
            position=None
        if position and plan.flat and crossed_day:close(o,'Next session open — closing bar missing')
        if position and pending_exit:close(o,pending_exit)
        if plan.flat and crossed_day:pending=0
        if not position and pending and not last and not eod:
            fill=o*(1+pending*slip)
            qty=int(max(cash,0)*plan.allocation/100/(fill*(1+cost)))
            if qty>=1:
                position=dict(side=pending,price=fill,qty=qty,fees=fill*qty*cost,time=stamp,
                              signal=f.index[i-1],bars=0)
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
            if plan.stop and (o<=stop if s==1 else o>=stop):close(o,'Stop gap')
            elif plan.target and (o>=goal if s==1 else o<=goal):close(goal,'Target at open (capped)')
            elif stop_hit:close(stop,'Stop first — both touched' if target_hit else 'Stop')
            elif target_hit:close(goal,'Target')
        if position:
            position['bars']+=1
            if eod:close(c,'Session close')
            elif last:close(c,'End of sample')
            else:
                if bool(exits[position['side']].iat[i]):pending_exit='Selected exit conditions'
                elif plan.max_bars>0 and position['bars']>=plan.max_bars:pending_exit='Maximum bars held'
        elif not last and not eod:
            signals=[s for s,v in entries.items() if bool(v.iat[i])]
            if len(signals)==1:pending=signals[0]
        unrealized=(c-position['price'])*position['qty']*position['side']-position['fees'] if position else 0
        marks.append((stamp,cash+unrealized))
    curve=pd.DataFrame(marks,columns=['Bar (IST)','Equity']).set_index('Bar (IST)')
    return pd.DataFrame(ledger),curve

def render_backtest(universe, token, client, make_api, default_frame):
    import hashlib
    import streamlit as st

    st.subheader('Underlying Backtest')
    st.caption('The rule builder tests separate, explicit indicator conditions. It does not recreate the live scanner score or the paper worker’s combined strategy.')
    st.caption('Asset OHLC only. No option premiums, Greeks, or option P&L. Stock split/dividend adjustments are not applied by this simulator. An index position is a point-value proxy, not a tradable cash instrument.')
    if universe.empty:
        st.info('The Dhan instrument list is unavailable. Try reloading the app.')
        return
    symbols=sorted(universe.symbol.drop_duplicates().tolist())
    pick_col,frame_col=st.columns(2)
    symbol=pick_col.selectbox('F&O stock or index',symbols,index=symbols.index('NIFTY') if 'NIFTY' in symbols else 0,key='bt_symbol')
    tf=frame_col.selectbox('Timeframe',list(TIMES),index=list(TIMES).index(default_frame),key='bt_frame')
    minutes=TIMES[tf]
    keys=[k for k in RULES if (minutes or k not in INTRADAY_ONLY) and not (minutes==60 and k=='orb')]
    rules=[RULES[k] for k in keys]
    reverse={v:k for k,v in RULES.items()}
    now=datetime.now(IST)
    end_default=now.date()-timedelta(days=1)
    start_default=end_default-timedelta(days=90 if minutes else 1095)
    @st.cache_data(ttl=600,max_entries=12,show_spinner=False)
    def cached_data(account, sid, segment, instrument, start, end, interval, _token, _client, _api):
        return load_bars(_api(_token,_client),dict(sid=sid,segment=segment,instrument=instrument),start,end,interval)
    with st.form('bt_controls_'+str(minutes)):
        a,b,c=st.columns(3)
        start=a.date_input('Test start',start_default,min_value=date(1970,1,1),max_value=end_default)
        end=b.date_input('Test end',end_default,min_value=date(1970,1,1),max_value=now.date())
        direction=c.selectbox('Direction',('Long only','Short proxy only','Long + short proxy'),
                              help='Short proxy is directional research, not cash-market borrowing or an options trade.')
        left,right=st.columns(2)
        with left:
            st.markdown('**Entry conditions**')
            selected_entry=st.multiselect('Use these entry indicators',rules,default=[RULES['ema_cross']])
            entry_logic=st.radio('Entry logic',('All selected','Any selected'),horizontal=True)
        with right:
            st.markdown('**Exit conditions**')
            selected_exit=st.multiselect('Use these exit indicators',rules,default=[RULES['ema_cross']],
                                         help='Directional exits are evaluated on the opposite side of the position.')
            exit_logic=st.radio('Exit logic',('Any selected','All selected'),horizontal=True)
        a,b,c=st.columns(3)
        capital=a.number_input('Starting capital ₹',min_value=100.,value=100000.,step=5000.)
        allocation=b.number_input('Capital allocated per trade (%)',min_value=1.,max_value=100.,value=100.)
        flat=c.checkbox('Close at end of each session',value=bool(minutes),disabled=not minutes)
        a,b,c=st.columns(3)
        stop=a.number_input('Underlying stop % (0 = off)',min_value=0.,max_value=99.,value=0.,step=.1)
        target=b.number_input('Underlying target % (0 = off)',min_value=0.,max_value=99.,value=0.,step=.1)
        holding=c.number_input('Maximum bars held (0 = off)',min_value=0,value=0,step=1)
        a,b,c=st.columns(3)
        fees=a.number_input('Costs per side (basis points)',min_value=0.,max_value=500.,value=5.,step=1.)
        slip=b.number_input('Slippage per side (basis points)',min_value=0.,max_value=500.,value=5.,step=1.)
        volume=c.number_input('Volume multiplier',min_value=.1,max_value=20.,value=1.5,step=.1)
        submitted=st.form_submit_button('Run underlying backtest',disabled=not bool(token),type='primary')
    if not token:st.info('Enter your Dhan access token to download candles.')
    identity=(symbol,tf,hashlib.sha256((token or '').encode()).hexdigest())
    if submitted:
        if not selected_entry:st.warning('Select at least one entry indicator.');return
        st.session_state.pop('bt_output',None)
        try:
            with st.spinner('Downloading underlying candles and replaying trades…'):
                meta=universe[universe.symbol.eq(symbol)].iloc[0]
                f=cached_data(identity[2],str(meta.sid),meta.segment,meta.instrument,
                              start,end,minutes,token,client,make_api)
                test=f[f.index.date>=start]
                if len(test)<2:raise ValueError('Fewer than two usable candles in the requested period.')
                entry,exit=[reverse[x] for x in selected_entry],[reverse[x] for x in selected_exit]
                if (set(entry)|set(exit)) & VOLUME_RULES and not f.volume.gt(0).any():
                    raise ValueError('Dhan returned no positive underlying volume. Remove VWAP/volume rules for this index, or select a stock with volume.')
                pairs={s:conditions(f,s,minutes,volume) for s in (1,-1)}
                allowed=(1,) if direction=='Long only' else (-1,) if direction=='Short proxy only' else (1,-1)
                en={s:combine(pairs[s],entry,entry_logic,f.index) for s in allowed}
                ex={s:combine(pairs[-s],exit,exit_logic,f.index) for s in allowed}
                plan=Plan(capital,allocation,fees,slip,stop,target,int(holding),minutes,bool(flat and minutes))
                trades,curve=simulate(f,en,ex,plan,start)
                sessions=len(set(test.index.date))
                coverage=(test.index[0],test.index[-1],sessions,len(test),len(f)-len(test),start,end)
                summary=f'{direction} · Entry ({entry_logic}): {", ".join(selected_entry)} · Exit ({exit_logic}): {", ".join(selected_exit) or "none"}'
                st.session_state.bt_output=(identity,trades,curve,coverage,plan,summary)
        except Exception as exc:
            st.error(f'Backtest failed: {str(exc)[:320]}')
            return
    saved=st.session_state.get('bt_output')
    if not saved or saved[0]!=identity:return
    _,trades,curve,(first,last,sessions,count,warm,start,end),plan,summary=saved
    st.markdown(f'**{symbol} · {tf} · Last completed backtest run**')
    st.caption(f'Requested {start} to {end}. Usable candles from {first:%d %b %Y %H:%M} to {last:%d %b %Y %H:%M} IST · {count:,} bars · {sessions:,} sessions · {(last.date()-first.date()).days+1:,} calendar days covered · {warm:,} warm-up bars.')
    if first.date()>start or last.date()<end:
        st.caption('Actual returned dates differ from the requested dates. Non-trading days, incomplete sessions or missing Dhan history can affect coverage.')
    if warm<200:st.caption('Fewer than 200 warm-up bars. Slow indicators still need sufficient data before they can signal.')
    st.caption(summary)
    st.caption(f'Allocation {plan.allocation:g}% · Per-side cost/slippage {plan.cost_bps:g}/{plan.slip_bps:g} bps · Stop/target {plan.stop:g}%/{plan.target:g}% · Max hold {plan.max_bars} bars · Session close {"on" if plan.flat else "off"}.')
    pnl=float(curve.Equity.iloc[-1])-plan.capital
    series=curve.Equity
    dd=(series/series.cummax().clip(lower=plan.capital)-1).min()*100
    a,b,c,d=st.columns(4)
    a.metric('Starting capital',f'₹{plan.capital:,.2f}')
    b.metric('Ending equity',f'₹{series.iloc[-1]:,.2f}')
    c.metric('Net P&L',f'₹{pnl:+,.2f}',f'{pnl/plan.capital*100:+.2f}%')
    d.metric('Maximum drawdown',f'{min(0,dd):.2f}%')
    if len(trades):
        n=trades['Net P&L']
        wins=n[n>0].sum(); losses=-n[n<0].sum()
        pf=f'{wins/losses:.2f}' if losses else ('∞ (no losing trades)' if wins else '—')
        st.write(f'**Trades** {len(n):,} · **Win rate** {(n>0).mean():.1%} · **Average net P&L** ₹{n.mean():,.2f}/trade · **Profit factor** {pf}')
        st.dataframe(trades,hide_index=True)
        st.download_button('Download underlying trades CSV',trades.to_csv(index=False),'underlying_trades.csv')
    else:st.info('No trades for these rules and supplied candles.')
    st.line_chart(curve)
    st.download_button('Download equity curve CSV',curve.to_csv(),'underlying_equity.csv')
    st.caption('Completed-bar signals fill at the next bar open. Session/end-of-test closes use available closing prices. If both stop and target are touched inside one candle and the open hit neither, the stop wins. Fixed-cost and slippage settings are assumptions, not a broker fee model. Results are not forecasts.')
