"""Read-only, multi-month Swing Breakout snapshot utilities."""
import pandas as pd

from swing_breakout import D10, D20, WEEK, DAILY_METHODS, prepare_daily, signal_rows

MONTHS = 6
BANDS = {D20: ('hi20','lo20'), D10: ('hi10','lo10'), WEEK: ('hiW','loW')}


def month_start(cutoff):
    return (pd.Timestamp(cutoff).to_period('M') - (MONTHS-1)).start_time


def month_labels(cutoff):
    latest = pd.Timestamp(cutoff).to_period('M')
    return [str(latest-i) for i in range(MONTHS)]


def row_status(checks):
    count = sum(v is True for v in checks.values())
    return f'{count}/10', sum(v is None for v in checks.values())


def make_snapshot(symbol, candles, cutoff, indicators, side_tests, source, feed_status):
    """Find historical events independently of EMA200 warm-up and strategy gates."""
    daily=prepare_daily(candles).loc[:pd.Timestamp(cutoff)]
    coverage={'Symbol':symbol,'Daily source':source,'First candle':'','Candles through':'',
              'Dhan outcome':feed_status,'days':{}}
    if daily.empty:
        return [],coverage
    coverage.update({'First candle':daily.index[0].date().isoformat(),
                     'Candles through':daily.index[-1].date().isoformat()})
    start=month_start(cutoff)
    d=indicators(daily)
    d['prev_adx']=d.adx.shift()
    usable=d.Volume.gt(0)&d.vol20.gt(0)
    tests={s:side_tests(d,s,usable) for s in ('CE','PE')}
    previous=d.shift()
    ema_trigger=d[['ema9','ema21']].notna().all(axis=1)&previous[['ema9','ema21']].notna().all(axis=1)
    cpr_trigger=d[['cpr_top','cpr_bottom','r3','s3']].notna().all(axis=1)&previous[['cpr_top','cpr_bottom','r3','s3']].notna().all(axis=1)
    available={
        'ema200':d.ema200.notna(),'ema50':d.ema50.notna()&d.ema200.notna(),
        'st':d.st_dir.ne(0),'adx':d.adx.notna(),'adx_rise':d.adx.notna()&d.prev_adx.notna(),
        'di':d.pdi.notna()&d.mdi.notna(),'trigger':ema_trigger&cpr_trigger,
        'rsi':d.rsi.notna(),'volume':usable,'ema21':d.ema21.notna(),
    }
    events=[]
    for method in DAILY_METHODS:
        ready=daily.loc[start:].loc[lambda f: f[list(BANDS[method])].notna().all(axis=1)]
        if method==WEEK:
            ready=ready[ready.index.to_period('W-FRI')>daily.index[0].to_period('W-FRI')+1]
        coverage['days'][method]=set(ready.index.strftime('%Y-%m-%d'))
        for event in signal_rows(daily,method,start_date=start):
            day=event['Session']
            side=event['Side']
            check={key:bool(series.at[day]) if available[key].at[day] or (key=='trigger' and series.at[day]) else None
                   for key,series in tests[side].items()}
            score,nmissing=row_status(check)
            close=event['Close ₹']
            risk=1.5*d.at[day,'atr']
            direction=1 if side=='CE' else -1
            events.append({
                'Session date':day.date().isoformat(),
                'Symbol':symbol,
                'Breakout':'↑ BREAKOUT UP' if side=='CE' else '↓ BREAKOUT DOWN',
                **{k:v for k,v in event.items() if k not in ('Session','Confirmed IST')},
                'Daily source':source,
                'Reference stop ₹':close-direction*risk if pd.notna(risk) and risk>0 else None,
                'Reference 2R ₹':close+direction*2*risk if pd.notna(risk) and risk>0 else None,
                'Existing score':score if not nmissing else f'{10-nmissing} checks available',
                'Passed checks':sum(v is True for v in check.values()),
                'Unavailable checks':nmissing,'Checks':check,
            })
    return events,coverage


def provider_filtered(events, coverage, method, requested_month, include_fallback=False):
    """Dhan wins per symbol/session with enough history, including when it has no cross."""
    prefer={x['Symbol']:x['days'].get(method,set()) for x in coverage if x['Daily source']=='Dhan'}
    out=[]
    for e in events:
        date=e['Session date']
        if e['Method']!=method or not date.startswith(requested_month):
            continue
        if e['Daily source']=='Dhan':
            out.append(e)
        elif include_fallback and date not in prefer.get(e['Symbol'],set()):
            out.append(e)
    return out


def session_sources(coverage, method, session):
    """Return per-symbol source sets verified for exactly this date and method."""
    d,y=set(),set()
    for c in coverage:
        if session in c['days'].get(method,set()):
            if c['Daily source']=='Dhan':
                d.add(c['Symbol'])
            elif c['Daily source']=='Yahoo Finance':
                y.add(c['Symbol'])
    return d,y-d
