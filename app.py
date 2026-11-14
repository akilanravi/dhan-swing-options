import os
from datetime import date, timedelta
import pandas as pd
import streamlit as st
from dhan_api import DhanAPI, read_instrument_master, build_universe
from signals import indicators, annotate, score

st.set_page_config(page_title='Dhan Swing Options V3', page_icon='📊', layout='wide')
st.title('Dhan Swing Options V3 | F&O Scanner + Paper Trading')
st.caption('Dhan API only · Manual execution · No Telegram · No Yahoo · No automatic orders')

try:
    secret_token = st.secrets.get('DHAN_ACCESS_TOKEN', '')
except Exception:
    secret_token = ''
with st.sidebar:
    st.header('Dhan connection')
    token = st.text_input('Access token', value=os.getenv('DHAN_ACCESS_TOKEN', secret_token), type='password')
    capital = st.number_input('Capital (₹)', 1000, 100000000, 100000, 5000)
    risk_pct = st.slider('Risk per trade (%)', 1.0, 2.0, 1.0, .5)
    cpr_width = st.slider('Maximum weekly CPR width (%)', .1, 5.0, 1.5, .1) / 100
    min_score = st.slider('Minimum score', 7, 10, 7)
    st.caption('A score of 7 does not override mandatory trend, volume, ADX and trigger filters.')
    st.header('Scan settings')
    scan_days = st.selectbox('Signal lookback', [1, 5, 10, 20], index=3, help='Trading days. Includes older historical signals; not all are current entries.')
    scan_limit = st.number_input('Maximum F&O stocks per scan', 1, 400, 30, 10, help='Start with 10–30; Dhan API requests are rate-limited.')
    index_vol = st.checkbox('Include indices in scanner', value=True)
    st.caption('Index spot volume may be zero. Such index signals remain unconfirmed and cannot become eligible trades.')
    events = st.text_area('Event blackout dates (YYYY-MM-DD, one per line)', '')

if 'scan' not in st.session_state: st.session_state.scan = pd.DataFrame()
if 'history' not in st.session_state: st.session_state.history = pd.DataFrame()
if 'paper' not in st.session_state:
    st.session_state.paper = pd.DataFrame(columns=['id','opened_at','symbol','side','reference_entry','reference_stop','reference_target1','reference_target2','score','status','closed_at','reference_exit','underlying_points','notes'])
if 'master' not in st.session_state: st.session_state.master = None
if 'universe' not in st.session_state: st.session_state.universe = pd.DataFrame()

config = {'min_score': min_score, 'max_weekly_cpr_width': cpr_width,
          'event_blackout_dates': {}}
blackouts = [x.strip() for x in events.splitlines() if x.strip()]

t1,t2,t3,t4 = st.tabs(['1 · Market scanner','2 · Top CE/PE setups','3 · Trade analysis','4 · Paper trading'])
with t1:
    st.subheader('Load current NSE F&O universe')
    uploaded = st.file_uploader('Optional: upload current Dhan detailed instrument master CSV', type='csv')
    if st.button('Load Dhan instrument master', type='primary'):
        try:
            with st.spinner('Loading instrument master…'):
                master = read_instrument_master(uploaded)
                universe = build_universe(master)
                if universe.empty: raise ValueError('No matched F&O equities. Dhan master schema or symbol matching needs adjustment; inspect source CSV.')
                st.session_state.master = master
                st.session_state.universe = universe
            st.success(f'Found {len(universe)} F&O equity underlyings. Please review symbols before scanning.')
        except Exception as e: st.error(str(e))
    u = st.session_state.universe
    if not u.empty:
        st.write(f'{len(u)} F&O equity underlyings identified from current master')
        st.dataframe(u.head(25), use_container_width=True, hide_index=True)
    st.info('Dhan API limits apply. The scanner processes a bounded batch on demand, not every symbol continuously. Re-run subsequent batches to cover the whole universe.')
    offset = st.number_input('Start at stock number', 1, 1000, 1, 10)
    if st.button('Scan selected batch', disabled=not token, type='primary'):
        api = DhanAPI(token)
        today = date.today()
        stocks = [] if u.empty else u.iloc[int(offset)-1:int(offset)-1+int(scan_limit)].to_dict('records')
        if index_vol:
            stocks = [{'symbol':'NIFTY','sid':'13','segment':'IDX_I','instrument':'INDEX'},
                      {'symbol':'BANKNIFTY','sid':'25','segment':'IDX_I','instrument':'INDEX'}] + stocks
        if not stocks: st.warning('Load instrument master first, or enable indices.')
        else:
            results=[]; history=[]; errors=[]
            progress = st.progress(0, text='Starting scan')
            for i, x in enumerate(stocks):
                try:
                    # ~350 calendar days for EMA200 warm-up, 85-day bounded Dhan requests.
                    daily = api.candles(x['sid'],x['segment'],x['instrument'], today-timedelta(days=480), today+timedelta(days=1))
                    if len(daily) < 220: raise ValueError(f'Only {len(daily)} candles (need 220+)')
                    cfg = dict(config)
                    cfg['event_blackout_dates'] = {x['symbol']:blackouts}
                    d = annotate(indicators(daily), cfg, x['symbol'])
                    d = d[d.index.date < today]  # only completed prior trading days
                    if d.empty: raise ValueError('No completed daily candles')
                    r = d.iloc[-1]
                    volok = bool(r.Volume > 0 and r.vol20 > 0)
                    latest_signal = r.signal if volok else ''
                    side = latest_signal or ('CE' if score(r,'CE') >= score(r,'PE') else 'PE')
                    best = score(r,side)
                    eligible = bool(latest_signal and best >= min_score)
                    direction = 1 if side == 'CE' else -1
                    entry = float(r.Close)
                    risk = float(r.atr) * 1.5
                    row = {'Symbol':x['symbol'],'Date':d.index[-1].strftime('%Y-%m-%d'),
                           'Side':side,'Score':int(best),'Eligible':eligible,'Close':entry,
                           'Underlying SL':entry-direction*risk,'Target 1 (1R)':entry+direction*risk,
                           'Target 2 (2R)':entry+direction*2*risk,
                           'ADX':float(r.adx),'RSI':float(r.rsi),'Volume OK':volok,
                           'Weekly CPR width %':float(r.cpr_width)*100 if pd.notna(r.cpr_width) else None,
                           'Reason': 'Eligible daily signal' if eligible else ('Missing index/stock volume' if not volok else 'Mandatory filter or trigger not met')}
                    results.append(row)
                    for dt, s in d.tail(scan_days).iterrows():
                        if s.signal:
                            history.append({'Symbol':x['symbol'],'Date':dt.strftime('%Y-%m-%d'), 'Side':s.signal,'Score':int(s.score), 'Close':float(s.Close)})
                except Exception as e:
                    errors.append({'Symbol':x['symbol'],'Error':str(e)})
                progress.progress((i+1)/len(stocks),text=f'Processed {i+1}/{len(stocks)}: {x["symbol"]}')
            if results:
                new = pd.DataFrame(results)
                prior = st.session_state.scan
                st.session_state.scan = pd.concat([prior[~prior.Symbol.isin(new.Symbol)],new],ignore_index=True) if not prior.empty else new
                st.session_state.scan = st.session_state.scan.sort_values(['Eligible','Score','ADX'],ascending=[False,False,False]).reset_index(drop=True)
                st.session_state.history = pd.concat([st.session_state.history,pd.DataFrame(history)],ignore_index=True).drop_duplicates(['Symbol','Date','Side']) if history else st.session_state.history
                st.success(f'Scanned {len(results)} instruments; {int(new.Eligible.sum())} eligible. Results from previous batches retained in this session.')
            if errors: st.warning(f'{len(errors)} instruments failed'); st.dataframe(pd.DataFrame(errors),hide_index=True)
    if not st.session_state.scan.empty:
        st.subheader('Latest batch results — eligible first')
        st.dataframe(st.session_state.scan, use_container_width=True, hide_index=True)
        st.download_button('Download scanner CSV',st.session_state.scan.to_csv(index=False).encode(),file_name='dhan_scan.csv')

with t2:
    d = st.session_state.scan
    if d.empty: st.info('Run a market scan first.')
    else:
        current = d[d.Eligible].sort_values(['Score','ADX'],ascending=[False,False])
        st.metric('Eligible current daily setups',len(current))
        if current.empty: st.warning('No eligible trades in scanned batch. Do not force a trade.')
        else: st.dataframe(current,use_container_width=True,hide_index=True)
        with st.expander('Rejected / watchlist candidates'):
            st.dataframe(d[~d.Eligible],use_container_width=True,hide_index=True)
        st.caption('Historical signals from selected lookback are not fresh entries; see below.')
        if not st.session_state.history.empty: st.dataframe(st.session_state.history.sort_values('Date',ascending=False),use_container_width=True,hide_index=True)

with t3:
    d=st.session_state.scan
    if d.empty: st.info('Run the scanner to select an instrument.')
    else:
        chosen = st.selectbox('Instrument',d.Symbol.tolist())
        r=d[d.Symbol==chosen].iloc[0]
        st.dataframe(pd.DataFrame([r]),hide_index=True,use_container_width=True)
        st.warning('Entry, SL and targets are underlying reference levels, NOT option premium prices. No strike, expiry, premium, lot size or rupee risk can be confirmed without a validated option chain.')
        if token:
            st.subheader('Option chain — manual inspection')
            api = DhanAPI(token)
            mapping = {'NIFTY':('13','IDX_I'),'BANKNIFTY':('25','IDX_I')}
            if chosen in mapping: sid,seg=mapping[chosen]
            else:
                u=st.session_state.universe
                match=u[u.symbol==chosen]
                sid,seg=(str(match.iloc[0].sid),'NSE_EQ') if not match.empty else ('','NSE_EQ')
            if st.button('Get available expiries'):
                try: st.json(api.expiries(sid,seg))
                except Exception as e: st.error(str(e))
            expiry=st.text_input('Expiry YYYY-MM-DD (from expiry list)')
            if st.button('Get option chain') and expiry:
                try: st.json(api.chain(sid,seg,expiry),expanded=False)
                except Exception as e: st.error(str(e))

with t4:
    st.subheader('Paper trading journal')
    st.caption('This records UNDERLYING reference-point movements, not actual CE/PE option P&L. No orders are sent.')
    st.warning('Streamlit Cloud session data can disappear on app restart. Export CSV regularly and re-import to restore your journal.')
    import_csv=st.file_uploader('Restore paper trades CSV',type='csv',key='paper_import')
    if import_csv and st.button('Import paper trades'):
        try:
            restored=pd.read_csv(import_csv)
            expected=list(st.session_state.paper.columns)
            if not set(expected).issubset(restored.columns): raise ValueError('CSV missing required paper-trade columns')
            st.session_state.paper=restored[expected]
            st.success(f'Restored {len(restored)} rows')
        except Exception as e: st.error(str(e))
    d=st.session_state.scan
    if not d.empty:
        eligible=d[d.Eligible]
        if not eligible.empty:
            sym=st.selectbox('Eligible setup to paper trade',eligible.Symbol.tolist())
            r=eligible[eligible.Symbol==sym].iloc[0]
            if st.button('Record paper entry'):
                if len(st.session_state.paper[st.session_state.paper.status=='OPEN']) >= 3:
                    st.error('Maximum 3 open paper trades.')
                elif sym in set(st.session_state.paper.loc[st.session_state.paper.status=='OPEN','symbol']):
                    st.error('Already have an open paper trade for this instrument.')
                else:
                    record={'id':pd.Timestamp.now().strftime('%Y%m%d%H%M%S%f'),'opened_at':pd.Timestamp.now().isoformat(),
                            'symbol':sym,'side':r.Side,'reference_entry':r.Close,'reference_stop':r['Underlying SL'],
                            'reference_target1':r['Target 1 (1R)'],'reference_target2':r['Target 2 (2R)'],
                            'score':r.Score,'status':'OPEN','closed_at':'','reference_exit':None,'underlying_points':None,'notes':'Paper reference only; not option P&L'}
                    st.session_state.paper=pd.concat([st.session_state.paper,pd.DataFrame([record])],ignore_index=True)
                    st.success('Paper entry recorded')
    journal=st.session_state.paper
    if not journal.empty:
        st.dataframe(journal,use_container_width=True,hide_index=True)
        opened=journal[journal.status=='OPEN']
        if not opened.empty:
            trade_id=st.selectbox('Open trade ID',opened.id.astype(str).tolist())
            exit_price=st.number_input('Underlying exit reference price',min_value=.01,value=1.0,step=1.0)
            if st.button('Close paper trade'):
                idx=st.session_state.paper.index[st.session_state.paper.id.astype(str)==trade_id][0]
                side=st.session_state.paper.loc[idx,'side']; entry=float(st.session_state.paper.loc[idx,'reference_entry'])
                points=(exit_price-entry)*(1 if side=='CE' else -1)
                st.session_state.paper.loc[idx,['status','closed_at','reference_exit','underlying_points']]=['CLOSED',pd.Timestamp.now().isoformat(),exit_price,points]
                st.success('Paper trade closed')
        st.download_button('Export paper journal CSV',st.session_state.paper.to_csv(index=False).encode(),file_name='paper_trades.csv',mime='text/csv')
    else: st.info('No paper trades yet. Scan first and record a qualifying setup.')
