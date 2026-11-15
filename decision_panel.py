"""Read-only option preflight shared by the two Streamlit dashboards."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None

def money(value):
    return '—' if value is None else f'₹{value:,.2f}'

def zerodha_estimate(buy, sell, qty):
    """Estimate for one NSE options buy and one sell, not exercise/settlement."""
    buy, sell = buy*qty, sell*qty
    exchange = .0003553*(buy+sell)
    sebi = .000001*(buy+sell)
    return (40 + exchange + sebi)*1.18 + .0015*sell + .00003*buy

def evaluate(idea, quote, policy, journal, now, extra=(), lookup_state=None):
    checks=list(extra)
    def add(label, ok, detail):
        checks.append((label,ok,detail))
    add('Technical setup', bool(idea['eligible']), idea['reason'])
    opened = journal[journal.status.eq('OPEN')] if 'status' in journal else pd.DataFrame()
    journal_ok = isinstance(policy['journal_ok'],bool) and policy['journal_ok']
    add('Paper journal readable', True if journal_ok else None, 'Portfolio checks require a loaded journal.')
    add('Open-position limit', len(opened)<policy['max_open'] if journal_ok else None,
        f"{len(opened)} open / {policy['max_open']} allowed" if journal_ok else 'Unavailable')
    if journal_ok and 'symbol' in opened:
        add('Duplicate underlying', not opened.symbol.eq(idea['symbol']).any(), 'No second open trade in the same symbol.')
    if journal_ok and len(journal):
        day=str(now.date())
        if idea['mode']=='intraday' and 'opened_at' in journal:
            count=journal.opened_at.astype(str).str.startswith(day).sum()
            add('Daily entry limit', count<2, f'{count} / 2 entries today')
        if {'symbol','side','signal_candle' if idea['mode']=='intraday' else 'signal_date'} <= set(journal):
            key='signal_candle' if idea['mode']=='intraday' else 'signal_date'
            same=journal.symbol.eq(idea['symbol']) & journal.side.eq(idea['side']) & journal[key].astype(str).eq(idea['candle'])
            add('Signal not already used', not same.any(), 'This candle and side cannot be entered twice.')
        if idea['mode']=='swing':
            indices={'NIFTY','BANKNIFTY','FINNIFTY','MIDCPNIFTY','NIFTYNXT50'}
            if idea['symbol'] in indices and 'symbol' in opened:
                add('Index concentration', not opened.symbol.isin(indices).any(), 'No second open index setup.')
            if {'status','closed_at','net_pnl'}<=set(journal):
                closed=journal[journal.status.eq('CLOSED')].copy()
                dates=pd.to_datetime(closed.closed_at,errors='coerce',format='mixed',utc=True).dt.tz_convert(IST)
                pnl=pd.to_numeric(closed.net_pnl,errors='coerce')
                monday=now.date()-pd.Timedelta(days=now.weekday())
                weekly=float(pnl[dates.dt.date.ge(monday)].sum())
                add('Weekly loss limit', weekly > -policy['capital']*policy['weekly_limit']/100, f'Week to date: {money(weekly)}')
                if len(closed):
                    tail=pd.DataFrame({'date':dates,'pnl':pnl}).sort_values('date').tail(3)
                    add('Consecutive losses', (not tail.pnl.lt(0).all() if len(tail)==3 and tail.pnl.notna().all() else None if len(tail)==3 else True),
                        'Review before proceeding after three consecutive losses.')
    metrics={}
    if quote is None:
        add('Dhan option snapshot', None if lookup_state is None else False,
            lookup_state or 'Select Check with Dhan to inspect an exact contract.')
    else:
        received=quote['received']
        age=(now-received).total_seconds()
        add('Snapshot receipt age', 0<=age<=30, f'{max(age,0):.0f}s since received. Exchange quote time unavailable in option-chain response.')
        ask,bid=number(quote.get('ask')),number(quote.get('bid'))
        lot=number(quote.get('lot'))
        book=ask is not None and bid is not None and 0<bid<=ask
        add('Bid and ask valid',book,'Both must be positive and bid must not exceed ask.')
        spread=(ask-bid)/((ask+bid)/2)*100 if book else None
        add('Bid–ask spread',spread<=policy['spread'] if spread is not None else None,
            f"{spread:.2f}% vs {policy['spread']:g}% limit" if spread is not None else 'Unavailable')
        delta=number(quote.get('delta'))
        add('Delta in range',policy['delta_lo']<=abs(delta)<=policy['delta_hi'] if delta is not None else None,
            f"|Delta| {abs(delta):.3f}" if delta is not None else 'Unavailable')
        oi,vol=number(quote.get('oi')),number(quote.get('volume'))
        add('OI and traded volume',oi>0 and vol>0 if oi is not None and vol is not None else None,
            f'OI {oi:,.0f} · Volume {vol:,.0f}' if oi is not None and vol is not None else 'Unavailable')
        dte=(quote['expiry']-now.date()).days
        add('Days to expiry',dte>=policy['min_days'],f"{dte} calendar days vs {policy['min_days']} minimum")
        valid_lot=lot is not None and lot>=1 and lot.is_integer()
        add('Lot size from instrument mapping', True if valid_lot else None, str(int(lot)) if valid_lot else 'Unavailable')
        if valid_lot and ask is not None and ask>0:
            qty=int(lot)
            bq,aq=number(quote.get('bid_qty')),number(quote.get('ask_qty'))
            add('Top-of-book quantity for one lot',min(bq,aq)>=qty if bq is not None and aq is not None else None,
                f'{bq:,.0f} bid / {aq:,.0f} ask vs {qty} units' if bq is not None and aq is not None else 'Dhan did not return both quantities.')
            debit=ask*qty
            stop=ask*(1-policy['stop'])
            target=ask*(1+policy['stop']*policy['target_r'])
            stop_fill=stop*(1-policy['slip'])
            risk=(ask-stop_fill)*qty+zerodha_estimate(ask,stop_fill,qty)
            goal_fill=target*(1-policy['slip'])
            gain=(goal_fill-ask)*qty-zerodha_estimate(ask,goal_fill,qty)
            budget=policy['capital']*policy['risk']/100
            add('Per-trade premium cap',debit<=policy['cap'],f"{money(debit)} vs {money(policy['cap'])}")
            add('One-lot planned loss incl. costs',risk<=budget,f'{money(risk)} vs {money(budget)}')
            if journal_ok:
                if opened.empty:
                    exposure=0.0
                elif 'cost' in opened:
                    nums=pd.to_numeric(opened.cost,errors='coerce')
                    exposure=float(nums.sum()) if nums.notna().all() else None
                elif {'entry_premium','lot_size','lots'}<=set(opened):
                    nums=opened[['entry_premium','lot_size','lots']].apply(pd.to_numeric,errors='coerce')
                    exposure=float(nums.prod(axis=1).sum()) if nums.notna().all().all() else None
                else:
                    exposure=None
                limit=policy['capital']*policy['exposure']/100
                add('Portfolio premium exposure',exposure+debit<=limit if exposure is not None else None,
                    f'{money(exposure+debit)} after entry vs {money(limit)}' if exposure is not None else 'Open premium exposure unavailable')
            metrics={'One-lot debit':debit,'Planned loss at stop':risk,'Risk budget':budget,
                     'Stop premium':stop,'Target premium':target,
                     'Estimated fees at stop':zerodha_estimate(ask,stop_fill,qty),
                     'Estimated net reward/risk':gain/risk if risk>0 else None}
    checks=[(k,None if v is None else bool(v),d) for k,v,d in checks]
    verdict='No trade' if any(v is False for _,v,_ in checks) else (
        'Technical setup only' if any(v is None for _,v,_ in checks) else 'Ready for paper review')
    return verdict, checks, metrics

def fetch_timeseries(api, idea, minutes, days):
    from datetime import timedelta
    end=datetime.now(IST)
    out=api.post('/charts/intraday',dict(securityId=str(idea['sid']),exchangeSegment=idea['segment'],
                 instrument=idea['instrument'],interval=str(minutes),oi=False,
                 fromDate=(end-timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S'),
                 toDate=end.strftime('%Y-%m-%d %H:%M:%S')))
    d=out.get('data',out)
    times=pd.to_datetime(d['timestamp'],unit='s',utc=True).tz_convert(IST)
    bars=pd.DataFrame({k:pd.to_numeric(d[k],errors='coerce') for k in ('open','high','low','close')},index=times)
    return bars.loc[~bars.index.duplicated(keep='last')].sort_index().dropna().loc[
        lambda f:f.index+pd.Timedelta(minutes=minutes)<=end]

def context(api, idea, policy):
    minutes=15 if idea['mode']=='intraday' else 60
    price=fetch_timeseries(api,idea,minutes,10 if minutes==15 else 28)
    now=datetime.now(IST)
    # Current-session completed bar only, not a previous close masquerading as confirmation.
    last=price.iloc[-1]
    elapsed=(now-price.index[-1]).total_seconds()/60
    if price.index[-1].date()!=now.date() or not minutes<=elapsed<2*minutes+1 or len(price)<21:
        return [(f'{minutes}-minute EMA trend',None,'No recent completed bar or EMA warm-up unavailable')],[]
    ema=price.close.ewm(span=21,min_periods=21,adjust=False).mean().iloc[-1]
    aligned=last.close>ema if idea['side']=='CE' else last.close<ema
    gates=[(f'{minutes}-minute EMA trend',bool(aligned),f"Close {last.close:,.2f} vs EMA21 {ema:,.2f} · {price.index[-1]:%d %b %H:%M} IST")]
    notes=[]
    if idea['mode']=='swing':
        opening=price[price.index.date==now.date()].iloc[0]
        first=price[price.index.date==now.date()].index[0]
        prior=number(idea.get('close'))
        if first.strftime('%H:%M')!='09:15' or not prior or not policy.get('daily_verified'):
            gates.append(('Opening gap',None,'Opening candle or verified prior close unavailable'))
        else:
            gap=(opening.open/prior-1)*100
            gates.append(('Opening gap',bool(abs(gap)<=policy['max_gap']),f"{gap:+.2f}% vs ±{policy['max_gap']:g}% review limit"))
    return gates,notes

def load_quote(api,idea,policy,choose_expiry,parse_chain,select_contract):
    raw=api.expiries(idea['sid'],idea['segment'])
    expiries=raw.get('data',raw)
    if isinstance(expiries,dict):expiries=expiries.get('expiry',expiries.get('data',[]))
    today=datetime.now(IST).date()
    expiry=choose_expiry(expiries,today)
    if expiry is None:raise ValueError('No expiry meets this setup’s minimum days rule.')
    response=api.chain(idea['sid'],idea['segment'],expiry.isoformat())
    received=datetime.now(IST)
    spot,chain=parse_chain(response)
    if chain.empty:raise ValueError('No priced contracts returned by Dhan.')
    # Keep invalid strikes in the grid, but make them fail the selector's spread check.
    chain=chain.copy()
    invalid=~((chain.bid>0)&(chain.ask>=chain.bid))
    chain.loc[invalid,'ask']=float('nan')
    if invalid.all():raise ValueError('No contract has a valid two-sided bid/ask.')
    leg,_,why=select_contract(chain,idea['side'],spot,dict(delta_lo=policy['delta_lo'],
                            delta_hi=policy['delta_hi'],max_spread=policy['spread']))
    if leg is None:raise ValueError(why or 'No matching option.')
    return {**leg.to_dict(),'expiry':expiry,'received':received,'lot':idea.get('lot'),'spot':number(spot)}

def show_panel(idea, policy, journal, fetch, *, key, base_checks=(), exit_rules=(), critical_context=()):
    import streamlit as st
    st.caption('Read-only, one-lot preflight. This does not place orders, open paper trades or change worker settings.')
    policy=dict(policy)
    base_checks=list(base_checks)
    identity=(idea['symbol'],idea['side'],idea['candle'],idea['eligible'],idea.get('account'),
              tuple(sorted(policy.items())),tuple(base_checks))
    hit=st.session_state.get(key)
    if st.button('Check with Dhan',key=key+'_check',disabled=not policy['has_connection']):
        quote,error,gates,notes=fetch(policy)
        hit=(identity,quote,error,gates,notes)
        st.session_state[key]=hit
    quote,error,gates,notes=hit[1:] if hit and hit[0]==identity else (None,None,[(x,None,'Click Check with Dhan') for x in critical_context],[])
    now=datetime.now(IST)
    hours=now.weekday()<5 and policy['start']<=(now.hour,now.minute)<policy['end']
    if idea['mode']=='swing' and policy.get('worker_mode'):
        hours=hours and (now.hour,now.minute)<(10,30)
    checks=[('Paper entry window',hours,policy['window'])]+list(base_checks)+list(gates)
    verdict, checks, metrics=evaluate(idea,quote,policy,journal,now,checks,error)
    {'No trade':st.error,'Technical setup only':st.warning,'Ready for paper review':st.success}[verdict](verdict)
    st.write(f"**{idea['symbol']} · {idea['side']}** | Signal candle {idea['candle']}")
    if quote:
        details={k:(f"{v:%d %b %Y}" if k=='expiry' else v) for k,v in quote.items()
                 if k in ('expiry','strike','where','bid','ask','bid_qty','ask_qty','delta','theta','iv','oi','previous_oi','volume','lot','spot')}
        st.write(f"**Option:** {idea['symbol']} {quote['expiry']:%d %b %Y} {quote['strike']:g} {idea['side']} · Received {quote['received']:%H:%M:%S} IST")
        titles={'bid_qty':'Bid quantity','ask_qty':'Ask quantity','iv':'IV %','previous_oi':'Previous OI','lot':'Lot size','spot':'Underlying'}
        st.dataframe(pd.DataFrame([{titles.get(k,k.title()):v for k,v in details.items()}]),hide_index=True)
        oi,prev=number(quote.get('oi')),number(quote.get('previous_oi'))
        if oi is not None and prev is not None:st.caption(f'Change vs previous OI: {oi-prev:+,.0f} (not a directional signal by itself).')
    if metrics:
        for c,k in zip(st.columns(3),list(metrics)[:3]):c.metric(k,money(metrics[k]))
        st.write(f"**Premium stop:** {money(metrics['Stop premium'])} · **Target:** {money(metrics['Target premium'])} · **Fees at stop:** {money(metrics['Estimated fees at stop'])} · **Net reward/risk:** {metrics['Estimated net reward/risk'] or 0:.2f}R")
    st.dataframe(pd.DataFrame([{'Check':k,'Result':'✅ Pass' if v is True else '❌ Block' if v is False else '🟡 Unverified','Detail':d}
                               for k,v,d in checks]),hide_index=True)
    for note in notes:st.caption(note)
    st.markdown('**What the current paper engine actually checks**')
    for rule in exit_rules:st.write('• '+rule)
    st.caption('Fees estimate uses Zerodha NSE option rates for one buy and sell. Planned stop risk is not a maximum loss. A gap or missing bid can cause a larger loss, up to the premium paid plus costs. IV percentile, exact quote age and real fills are not verified here. Stock options can have delivery obligations at expiry.')
