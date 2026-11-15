"""Swing worker. Dhan option quotes only, no real orders."""
import argparse
import logging
import os
import time
from datetime import datetime, timedelta
import uuid
import pandas as pd
from worker_store import cloud_journal, market_open, load_env, enabled
from app import (IST,DhanAPI,DailyCache,read_instrument_master,build_universe,load_daily,
                 analyze_instrument,option_for_setup,normalize_journal,num,entry_guard,close_row)

class SwingWorker:
    def __init__(self):
        self.cache=DailyCache()
        self.universe=None
        self.universe_time=0
        self.scan_key=None
        self.rows=[]

    def cycle(self,env,journal,now=None):
        now=now or datetime.now(IST)
        frame,_=journal.read()
        frame=normalize_journal(frame)
        before=frame.copy(deep=True)
        notes=[]
        def finish(state):
            return (frame if not frame.equals(before) else None), {
                'checked_at':datetime.now(IST).isoformat(),'state':state,'notes':notes[-15:]}
        if not market_open(now,env):return finish('market closed')
        api=DhanAPI(env.get('DHAN_ACCESS_TOKEN',''),env.get('DHAN_CLIENT_ID',''))
        if not api.client_id:raise ValueError('Missing DHAN_CLIENT_ID')
        # Check exact saved contract and use current bid for simulated exits.
        from app import parse_chain
        for ix,t in frame[frame.status=='OPEN'].iterrows():
            try:
                entry=num(t.entry_premium)
                if not entry>0 or pd.isna(t.expiry):raise ValueError('Legacy entry lacks contract or entry premium')
                expiry=pd.Timestamp(t.expiry).date()
                if expiry<now.date():raise ValueError('Contract expired without a verified exit; review journal')
                spot,chain=parse_chain(api.chain(str(t.chain_sid),t.chain_seg,t.expiry))
                leg=chain[(chain.strike==float(t.strike))&(chain.type==t.side)]
                bid=num(leg.iloc[0].bid) if len(leg) else float('nan')
                if not 0<bid<float('inf'):raise ValueError('No valid Dhan bid')
                if not market_open(datetime.now(IST),env):break
                stop=num(t.sl_frac,.30)
                reason=('PREMIUM STOP' if bid<=entry*(1-stop) else
                        'PREMIUM TARGET 2R' if bid>=entry*(1+2*stop) else
                        'EXPIRY WINDOW' if (expiry-now.date()).days<=5 else
                        'TIME EXIT' if (now.date()-pd.Timestamp(t.opened_at).date()).days>=10 else '')
                if reason:
                    close_row(frame,ix,now.date().isoformat(),num(spot),bid,reason+' · Dhan bid snapshot',.5)
                    notes.append(f'{t.symbol} closed on {reason}')
                else:
                    qty=num(t.lots,0)*num(t.lot_size,0)
                    frame.loc[ix,['last_premium','premium_source','unrealized_pnl','last_update']]=[
                        bid,'Dhan bid snapshot',round((bid-entry)*qty,2),now.isoformat()]
            except Exception as e:
                notes.append(f'{t.symbol} left open: {str(e)[:150]}')
        # Signals use completed daily candles. Take entries only in the morning window.
        if not enabled(env.get('ALLOW_NEW_ENTRIES','true')) or not (9,30)<=(now.hour,now.minute)<(10,30):
            return finish('exit checks complete')
        watch=env.get('SWING_SYMBOLS','NIFTY,BANKNIFTY,RELIANCE,HDFCBANK').strip().upper()
        cutoff=now.date()-timedelta(days=1)
        key=(now.date(),watch,env['DHAN_ACCESS_TOKEN'])
        if key!=self.scan_key:
            if self.universe is None or time.monotonic()-self.universe_time>86400:
                self.universe=pd.concat(build_universe(read_instrument_master()))
                self.universe_time=time.monotonic()
            want={x.strip() for x in watch.split(',')}
            rows=[]
            attempts=0
            # Use the NIFTY session as the freshness reference, including holidays.
            ref=self.universe[self.universe.symbol=='NIFTY'].iloc[0].to_dict()
            anchor=load_daily(api,self.cache,ref,cutoff)
            latest=anchor.index[-1].date()
            if (now.date()-latest).days>4:
                notes.append('No sufficiently recent reference candle')
                return finish('no recent completed session')
            if watch!='ALL' and not want.intersection(self.universe.symbol):
                notes.append('No SWING_SYMBOLS matched the Dhan universe')
            for x in self.universe.to_dict('records'):
                if watch!='ALL' and x['symbol'] not in want:continue
                if (datetime.now(IST).hour,datetime.now(IST).minute)>=(10,30):break
                attempts+=1
                try:
                    d=load_daily(api,self.cache,x,cutoff)
                    if d.index[-1].date()!=latest:continue
                    events=[v.strip() for v in env.get('SWING_EVENT_DATES','').split(',') if v.strip()]
                    if any(abs((now.date()-pd.Timestamp(e).date()).days)<=3 for e in events):continue
                    cfg={'min_score':7,'month_min_score':8,'max_weekly_cpr_width':.015,'require_monthly':True,
                         'max_ext_atr':2,'flow_gate':False,'scan_days':1,'event_blackout_dates':{x['symbol']:events}}
                    row,_=analyze_instrument(x,d,cfg,None,None)
                    if row['Eligible']:rows.append(row)
                except Exception as e:
                    notes.append(f"{x['symbol']}: {str(e)[:140]}")
                    if any(code in str(e) for code in ('DH-901','DH-902','HTTP 401','HTTP 403')):raise
            self.scan_key=key
            self.rows=sorted(rows,key=lambda r:(r['Status']!='FRESH ENTRY',-r['Score'],-r['ADX']))
            notes.append(f'Daily scan {latest}: {attempts} checked, {len(rows)} technically eligible.')
        p={'capital':float(env.get('SWING_CAPITAL','100000')),'risk_pct':float(env.get('SWING_RISK_PCT','1')),
           'max_trade_rupees':35000,'max_exposure':35,'sl_frac':.30,'min_days':15,
           'delta_lo':.50,'delta_hi':.65,'max_spread':5}
        for row in self.rows:
            clock=datetime.now(IST)
            if not (9,30)<=(clock.hour,clock.minute)<(10,30) or sum(frame.status=='OPEN')>=3:break
            sym,side=row['Symbol'],row['Side']
            if ((frame.symbol==sym)&((frame.status=='OPEN')|((frame.signal_date==row['Date'])&(frame.side==side)))).any():continue
            opt=option_for_setup(api,row,p,{})
            if opt.get('Verdict')!='TRADE':
                notes.append(f"{sym} skipped: {opt.get('Note','missing contract')}")
                continue
            premium=num(opt.get('Ask'))
            lot=num(opt.get('Lot size'))
            lots=num(opt.get('Lots'))
            if not all(0<v<float('inf') for v in (premium,lot,lots)):continue
            clock=datetime.now(IST)
            if not (9,30)<=(clock.hour,clock.minute)<(10,30):break
            exposure=premium*lot*lots
            bad=entry_guard(frame,sym,side,exposure,p['capital'],3,35,4,35000)
            if exposure*.30>p['capital']*p['risk_pct']/100:bad.append('Premium stop risk above budget')
            if bad:
                notes.append(f'{sym} skipped: {", ".join(bad)}')
                continue
            rec=dict(id=uuid.uuid4().hex,opened_at=clock.isoformat(),signal_date=row['Date'],symbol=sym,
                     side=side,status='OPEN',setup=row['Status'],score=row['Score'],underlying_entry=row['Close'],
                     underlying_sl=row['Underlying SL'],underlying_t1=row['Target 1 (1R)'],
                     underlying_t2=row['Target 2 (2R)'],contract=opt['Contract'],expiry=opt['Expiry'],
                     strike=opt['Strike'],lot_size=lot,lots=lots,entry_premium=premium,sl_frac=p['sl_frac'],
                     sid=row['sid'],segment=row['segment'],instrument=row['instrument'],
                     chain_sid=opt['chain_sid'],chain_seg=opt['chain_seg'],last_premium=premium,
                     premium_source='Dhan ask snapshot',unrealized_pnl=0)
            frame=normalize_journal(pd.concat([frame,pd.DataFrame([rec])],ignore_index=True))
            notes.append(f'{sym} {side} paper entry at {premium:.2f}')
        return finish('updated')

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--env',required=True)
    p.add_argument('--once',action='store_true')
    args=p.parse_args()
    w=SwingWorker()
    logging.basicConfig(level=logging.INFO)
    last_closed=0
    errors_last=0
    j=None
    while True:
        start=time.monotonic()
        try:
            j=None
            env=load_env(args.env)
            os.environ["DHAN_SHARED_LIMIT_DIR"]=env.get("DHAN_SHARED_LIMIT_DIR","")
            now=datetime.now(IST)
            if args.once or market_open(now,env) or start-last_closed>=900:
                j=cloud_journal(env.get('GITHUB_TOKEN'),env.get('PAPER_GIST_ID'),'swing')
                frame,status=w.cycle(env,j,now)
                j.write(frame,status)
                logging.info('%s: %s',status['state'],status['notes'])
                if not market_open(now,env):last_closed=start
        except Exception as e:
            logging.error('%s',str(e)[:230])
            if j is not None and time.monotonic()-errors_last>=60:
                try:
                    j.write(None,{'checked_at':datetime.now(IST).isoformat(),'state':'worker error','notes':[str(e)[:200]]})
                    errors_last=time.monotonic()
                except Exception:
                    pass
            if args.once:raise
        if args.once:return
        time.sleep(max(3,60-(time.monotonic()-start)))

if __name__=='__main__':
    main()
