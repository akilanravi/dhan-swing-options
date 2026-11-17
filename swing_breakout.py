"""Swing breakout signals and an underlying-only direction/ATR backtest."""
import numpy as np
import pandas as pd

D10 = 'Daily close · 10-session range'
D20 = 'Daily close · 20-session range'
WEEK = 'Daily close · prior-week range'
H60 = '60m close · 20-session range'
DAILY_METHODS = (D20, D10, WEEK)
METHODS = (*DAILY_METHODS, H60)
OHLC = ['Open','High','Low','Close']
HOUR_START = (555,615,675,735,795,855)


def local_index(index):
    out = pd.DatetimeIndex(index)
    return out.tz_convert('Asia/Kolkata').tz_localize(None) if out.tz is not None else out


def clean(frame, daily):
    d = frame.rename(columns=str.title).reindex(columns=[*OHLC,'Volume']).copy()
    d.index = local_index(d.index)
    if daily: d.index = d.index.normalize()
    d=d.loc[d.index.dayofweek<5]
    d = d.sort_index().loc[lambda q: ~q.index.duplicated(keep='last')]
    d = d.apply(pd.to_numeric, errors='coerce')
    hi, lo = d[OHLC].max(axis=1), d[OHLC].min(axis=1)
    return d.loc[np.isfinite(d[OHLC]).all(axis=1) & d[OHLC].gt(0).all(axis=1) & d.High.eq(hi) & d.Low.eq(lo)]


def prepare_daily(frame):
    d = clean(frame, True)
    prior = d.Close.shift()
    tr = pd.concat([d.High-d.Low,(d.High-prior).abs(),(d.Low-prior).abs()],axis=1).max(axis=1)
    d['atr'] = tr.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    for n in (10,20):
        d[f'hi{n}'] = d.High.shift().rolling(n).max()
        d[f'lo{n}'] = d.Low.shift().rolling(n).min()
    weeks = d.index.to_period('W-FRI')
    w = d.groupby(weeks).agg({'High':'max','Low':'min'})
    d[['hiW','loW']] = w.reindex(weeks-1).to_numpy()
    return d


def full_hours(hourly, as_of=None):
    h = clean(hourly, False)
    h = h.loc[(h.index.hour*60+h.index.minute).isin(HOUR_START) & (h.index.second==0)]
    if as_of is not None:
        end = pd.Timestamp(as_of)
        if end.tzinfo is not None: end=end.tz_convert('Asia/Kolkata').tz_localize(None)
        h = h.loc[h.index + pd.Timedelta(hours=1) <= end]
    return h


def comparable_days(hourly):
    counts = hourly.groupby(hourly.index.normalize()).size()
    return pd.DatetimeIndex(counts[counts.eq(len(HOUR_START))].index)


def signal_rows(d, method, hourly=None, start_date=None):
    if method == H60:
        if hourly is None or hourly.empty:
            return []
        f = hourly.join(d[['hi20','lo20']].rename(columns={'hi20':'upper','lo20':'lower'}),
                        on=hourly.index.normalize(), how='inner')
    else:
        suffix = {D10:10, D20:20, WEEK:'W'}[method]
        f = d[[*OHLC,'Volume',f'hi{suffix}',f'lo{suffix}']].rename(
            columns={f'hi{suffix}':'upper',f'lo{suffix}':'lower'})
        if method == WEEK and len(d):
            # Do not test against a truncated first week in a historical file.
            f = f.loc[f.index.to_period('W-FRI') > d.index[0].to_period('W-FRI')+1]
    if start_date is not None:
        f=f.loc[pd.Timestamp(start_date):]
    up = f.Open.le(f.upper) & f.Close.gt(f.upper)
    dn = f.Open.ge(f.lower) & f.Close.lt(f.lower)
    out = []
    for time, row in f.loc[up|dn].iterrows():
        side = 'CE' if row.Close > row.upper and row.Open <= row.upper else 'PE'
        level = row.upper if side=='CE' else row.lower
        ending = time + (pd.Timedelta(hours=1) if method==H60 else pd.Timedelta(hours=15,minutes=30))
        day = time.normalize()
        out.append({'Method':method,'Side':side,'Session':day,'Confirmed IST':ending,
                    'Open ₹':row.Open,'High ₹':row.High,'Low ₹':row.Low,'Close ₹':row.Close,
                    'Volume':row.Volume,'Level ₹':level,'Beyond level %':100*(row.Close/level-1),
                    'Range high ₹':row.upper,'Range low ₹':row.lower})
    return out


def simulate(d, events, start, end, *, hold=5, atr_multiple=1.5, target_r=2, cost_bps=10, allowed=None, jumps=()):
    """First signal per symbol/method/session. Fill next session open, then log one open position per method."""
    loc = {t:i for i,t in enumerate(d.index)}
    out, busy, seen = [], -1, set()
    for e in events:
        time = e['Session']
        if time < start or time > end or time in seen or (allowed is not None and time not in allowed):
            continue
        seen.add(time)
        i = loc[time]; ent = i+1
        if ent <= busy or ent+hold-1 >= len(d) or any(i-20 <= j <= ent+hold-1 for j in jumps):
            continue
        risk = float(d.iloc[i].atr)*atr_multiple
        if not np.isfinite(risk) or risk <= 0:
            continue
        price = float(d.iloc[ent].Open)
        direction = 1 if e['Side']=='CE' else -1
        stop, target = price-direction*risk, price+direction*target_r*risk
        exit_idx = ent+hold-1
        for j in range(ent,exit_idx+1):
            row = d.iloc[j]
            if direction*(row.Open-stop) <= 0:
                reason, fill = 'Gap stop', row.Open
            elif direction*(row.Open-target) >= 0:
                reason, fill = 'Gap target (limit)', target
            elif (row.Low<=stop if direction==1 else row.High>=stop):
                # For a daily OHLC bar with both hits, assume stop first.
                reason, fill = 'Stop', stop
            elif (row.High>=target if direction==1 else row.Low<=target):
                reason, fill = 'Target', target
            else:
                continue
            exit_idx = j
            break
        else:
            reason, fill = 'Time', d.iloc[exit_idx].Close
        busy = exit_idx
        close_h = d.iloc[ent+hold-1].Close
        gross = (fill-price)*direction
        net = gross - price*cost_bps/10000
        out.append({**e,'Entry date':d.index[ent],'Exit date':d.index[exit_idx],'Entry ₹':price,
                    'Stop ₹':stop,'Target ₹':target,'Exit ₹':fill,'Exit reason':reason,'Net R':net/risk,
                    'Net underlying %':100*net/price,
                    'Horizon direction hit':bool((close_h-price)*direction>0)})
    return out


def compare(symbol, daily, hours, start, end, split, hold=5, cost_bps=10):
    d = prepare_daily(daily)
    h = full_hours(hours) if hours is not None else None
    allowed = set(comparable_days(h).intersection(d.index)) if h is not None else None
    # Do not assume an unexplained 18%+ overnight discontinuity is an
    # executable return. Exclude histories/hold windows that straddle it.
    jumps=np.flatnonzero(d.Open.div(d.Close.shift()).sub(1).abs().ge(.18))
    # Older test period must finish before the holdout starts. Trades without a
    # full horizon inside either period are not admitted into that period.
    entire = d.loc[:end]
    ranges = (('Earlier',start,min(end,split-pd.Timedelta(days=1))),('Holdout',max(start,split),end))
    results = []
    for label, a, b in ranges:
        subset = entire.loc[:b]
        for method in (METHODS if h is not None else DAILY_METHODS):
            ev = signal_rows(subset, method, h)
            for row in simulate(subset,ev,a,b,hold=hold,cost_bps=cost_bps,allowed=allowed,jumps=jumps):
                results.append({'Symbol':symbol,'Period':label,**row})
    coverage = []
    for label,a,b in ranges:
        dates = entire.loc[a:b].index
        coverage.append({'Symbol':symbol,'Period':label,'Daily sessions':len(dates),
                         'Comparable sessions':len(set(dates)&allowed) if allowed is not None else len(dates),
                         'Large overnight gaps flagged':sum(a<=d.index[j]<=b for j in jumps)})
    return results, coverage


def summarize(trades, hold):
    if trades.empty: return pd.DataFrame()
    rows = []
    for (period,method), g in trades.groupby(['Period','Method'], sort=False):
        for side in ('Both','CE','PE'):
            s = g if side=='Both' else g.loc[g.Side.eq(side)]
            if s.empty: continue
            ret=s['Net R']; loss=-ret[ret<0].sum()
            rows.append({'Period':period,'Method':method,'Side':side,'Trades':len(s),
                         f'{hold}-session direction hit %':100*s['Horizon direction hit'].mean(),
                         'Positive-trade %':100*ret.gt(0).mean(),'Avg net R':ret.mean(),
                         'Profit factor':ret[ret>0].sum()/loss if loss>0 else np.nan,
                         'Avg net underlying %':s['Net underlying %'].mean()})
    return pd.DataFrame(rows)
