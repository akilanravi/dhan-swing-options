import os
from datetime import date, timedelta
import time
import pandas as pd
import streamlit as st
from dhan_api import DhanAPI, read_instrument_master, build_universe
from signals import indicators, annotate, score, rejection_reasons
from institutional import fetch_moneycontrol_flows, summarize

st.set_page_config(page_title='Dhan Swing Options V3', page_icon='📊', layout='wide')
st.title('Dhan Swing Options V3.6 | Monthly Scanner + Institutional Flows')
st.caption('Dhan API for trading data · Moneycontrol for published institutional cash flows · No Telegram · No Yahoo · Manual orders only')

try:
    secret_token = st.secrets.get('DHAN_ACCESS_TOKEN', '')
    secret_client_id = st.secrets.get('DHAN_CLIENT_ID', '')
except Exception:
    secret_token = ''
    secret_client_id = ''
with st.sidebar:
    st.header('Dhan connection')
    token = st.text_input('Access token', value=os.getenv('DHAN_ACCESS_TOKEN', secret_token), type='password')
    client_id = os.getenv('DHAN_CLIENT_ID', secret_client_id)
    st.caption('Option-chain client ID: ' + ('configured' if client_id else 'MISSING — add DHAN_CLIENT_ID in Streamlit Secrets'))
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
    st.caption('Moneycontrol institutional flows are published cash-market totals, not live intraday ticks. Each scan attempts a fresh fetch.')

@st.cache_data(ttl=300, show_spinner=False)
def cached_flows():
    return summarize(fetch_moneycontrol_flows())

flows = None
flow_error = None
try:
    flows = cached_flows()
except Exception as exc:
    flow_error = str(exc)
with st.sidebar:
    st.header('Institutional data · Moneycontrol')
    if flows:
        st.write(f"Latest published: {flows['date']}")
        st.write(f"FII ₹Cr: {flows['fii_net']:+,.2f} | DII ₹Cr: {flows['dii_net']:+,.2f}")
        st.caption(f"5-session sentiment: {flows['sentiment']}")
    else:
        st.warning('Institutional data unavailable — no figures assumed')
        st.caption(flow_error or '')

if 'scan' not in st.session_state: st.session_state.scan = pd.DataFrame()
if 'history' not in st.session_state: st.session_state.history = pd.DataFrame()
if 'paper' not in st.session_state:
    st.session_state.paper = pd.DataFrame(columns=['id','opened_at','symbol','side','reference_entry','reference_stop','reference_target1','reference_target2','score','status','closed_at','reference_exit','underlying_points','notes'])
if 'master' not in st.session_state: st.session_state.master = None
if 'universe' not in st.session_state: st.session_state.universe = pd.DataFrame()
if 'scan_errors' not in st.session_state: st.session_state.scan_errors = pd.DataFrame()

config = {'min_score': min_score, 'max_weekly_cpr_width': cpr_width,
          'event_blackout_dates': {}}
blackouts = [x.strip() for x in events.splitlines() if x.strip()]

# Cached public master and completed daily candles; cache is shared across reruns.
# Access token is part of the cache key to avoid cross-account reuse.
@st.cache_data(ttl=86400, show_spinner=False)
def cached_master():
    return read_instrument_master()

@st.cache_data(ttl=21600, max_entries=1200, show_spinner=False)
def cached_daily(token_value, client_value, sid, segment, instrument, through_date):
    api = DhanAPI(token_value, client_id=client_value)
    end = pd.Timestamp(through_date).date() + timedelta(days=1)
    return api.candles(sid, segment, instrument, end-timedelta(days=400), end)

t1,t2,t3,t4 = st.tabs(['1 · Market scanner','2 · Top CE/PE setups','3 · Trade analysis','4 · Paper trading'])
with t1:
    st.subheader('Automatically load current NSE F&O universe')
    st.caption('Automatically downloads the current Dhan instrument master. No file upload needed.')
    if st.button('Refresh Dhan instrument master', type='secondary'):
        cached_master.clear()
        st.session_state.universe = pd.DataFrame()
    if st.session_state.universe.empty:
        try:
            with st.spinner('Loading instrument master…'):
                master = cached_master()
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
    st.info('First scan downloads historical candles (approximately 5 requests per stock); repeat scans reuse cached candles for 6 hours. Dhan API limits still apply. Scan in batches.')
    offset = st.number_input('Start at stock number', 1, 1000, 1, 10)
    st.caption('Stock numbering starts at 1. The universe is loaded automatically.')
    if st.button('Scan selected batch', disabled=not token, type='primary'):
        cached_flows.clear()  # fresh public-data attempt on each explicit scan
        try:
            flows = cached_flows()
            flow_error = None
        except Exception as exc:
            flows = None
            flow_error = str(exc)
        api = DhanAPI(token, client_id=client_id)
        today = date.today()
        stocks = [] if u.empty else u.iloc[int(offset)-1:int(offset)-1+int(scan_limit)].to_dict('records')
        if index_vol:
            stocks = [{'symbol':'NIFTY','sid':'13','segment':'IDX_I','instrument':'INDEX'},
                      {'symbol':'BANKNIFTY','sid':'25','segment':'IDX_I','instrument':'INDEX'}] + stocks
        if not stocks: st.warning('Load instrument master first, or enable indices.')
        else:
            results=[]; history=[]; errors=[]
            started = time.monotonic()
            progress = st.progress(0, text='Starting scan')
            for i, x in enumerate(stocks):
                try:
                    stock_started = time.monotonic()
                    # ~350 calendar days for EMA200 warm-up, 85-day bounded Dhan requests.
                    daily = cached_daily(token, client_id, x['sid'], x['segment'], x['instrument'], today.isoformat())
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
                    reasons = rejection_reasons(r, side, cfg, x['symbol'], volok)
                    month_rows = d[d.index.to_period('M') == d.index[-1].to_period('M')]
                    month_open = float(month_rows.iloc[0].Open) if len(month_rows) else float(r.Close)
                    mtd = (float(r.Close) / month_open - 1) * 100 if month_open > 0 else float('nan')
                    crosses_up = month_rows.index[month_rows.cross_up.fillna(False)]
                    crosses_down = month_rows.index[month_rows.cross_down.fillna(False)]
                    breakouts_up = month_rows.index[month_rows.break_up.fillna(False)]
                    breakouts_down = month_rows.index[month_rows.break_down.fillna(False)]
                    monthly_events = (crosses_up.union(breakouts_up) if side == 'CE' else crosses_down.union(breakouts_down))
                    last_trigger = monthly_events[-1].strftime('%Y-%m-%d') if len(monthly_events) else ''
                    directional = bool((r.Close > r.ema200 and r.st_dir == 1 and r.rsi > 55 and r.pdi > r.mdi) if side == 'CE' else (r.Close < r.ema200 and r.st_dir == -1 and r.rsi < 45 and r.mdi > r.pdi))
                    monthly_trend = 'BULLISH' if (r.Close > r.ema50 and mtd > 0 and r.st_dir == 1) else ('BEARISH' if (r.Close < r.ema50 and mtd < 0 and r.st_dir == -1) else 'SIDEWAYS/MIXED')
                    consistent = (monthly_trend == ('BULLISH' if side == 'CE' else 'BEARISH'))
                    hard_blocks = []
                    if not volok or not bool(r.Volume > r.vol20): hard_blocks.append('Volume unconfirmed')
                    if pd.isna(r.cpr_width) or r.cpr_width > cpr_width: hard_blocks.append('CPR too wide')
                    if pd.isna(r.adx) or r.adx <= 22: hard_blocks.append('ADX too low')
                    if not directional: hard_blocks.append('Trend / RSI / DI disagreement')
                    if not consistent: hard_blocks.append('Monthly trend disagreement')
                    if any(abs((d.index[-1].date()-pd.Timestamp(e).date()).days)<=3 for e in blackouts): hard_blocks.append('Event blackout')
                    status = 'FRESH ENTRY' if eligible and not hard_blocks else ('MONTHLY TRIGGER WATCH' if best >= 8 and last_trigger and not hard_blocks else ('STRONG WATCHLIST' if best >= 8 and directional else 'REJECTED'))
                    row = {'Symbol':x['symbol'],'Date':d.index[-1].strftime('%Y-%m-%d'),
                           'Side':side,'Score':int(best),'Eligible':bool(status == 'FRESH ENTRY'),'Status':status,'Monthly trend':monthly_trend,'MTD %':round(mtd,2),'Last monthly trigger':last_trigger,'Monthly trigger':bool(last_trigger),'Hard blocks':'; '.join(hard_blocks),'FII net ₹Cr (market)':flows['fii_net'] if flows else None,'DII net ₹Cr (market)':flows['dii_net'] if flows else None,'Flow date':flows['date'] if flows else '', 'FII 5D ₹Cr':flows['fii_5d'] if flows else None,'DII 5D ₹Cr':flows['dii_5d'] if flows else None,'FII MTD ₹Cr':flows['fii_mtd'] if flows else None,'DII MTD ₹Cr':flows['dii_mtd'] if flows else None,'Institutional sentiment':flows['sentiment'] if flows else 'UNAVAILABLE', 'Close':entry,
                           'Underlying SL':entry-direction*risk,'Target 1 (1R)':entry+direction*risk,
                           'Target 2 (2R)':entry+direction*2*risk,
                           'ADX':float(r.adx),'RSI':float(r.rsi),'Volume OK':volok,
                           'Weekly CPR width %':float(r.cpr_width)*100 if pd.notna(r.cpr_width) else None,
                           'Reason': 'Eligible daily signal' if status == 'FRESH ENTRY' else '; '.join(reasons),
                           'Failed gates':len(reasons), 'Load seconds':round(time.monotonic()-stock_started,2)}
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
                st.session_state.scan['Rank'] = st.session_state.scan.Status.map({'FRESH ENTRY':0,'MONTHLY TRIGGER WATCH':1,'STRONG WATCHLIST':2,'REJECTED':3}).fillna(4)
                st.session_state.scan = st.session_state.scan.sort_values(['Rank','Score','ADX'],ascending=[True,False,False]).drop(columns=['Rank']).reset_index(drop=True)
                st.session_state.history = pd.concat([st.session_state.history,pd.DataFrame(history)],ignore_index=True).drop_duplicates(['Symbol','Date','Side']) if history else st.session_state.history
                st.success(f'Scanned {len(results)} instruments in {time.monotonic()-started:.1f}s; {int(new.Eligible.sum())} eligible. Previous batches retained in this session.')
            st.session_state.scan_errors = pd.DataFrame(errors)
            if errors: st.warning(f'{len(errors)} instruments failed'); st.dataframe(pd.DataFrame(errors),hide_index=True)
            if errors: st.download_button('Download API errors CSV',pd.DataFrame(errors).to_csv(index=False),file_name='dhan_api_errors.csv')
    st.subheader('Moneycontrol institutional cash flows')
    if flows:
        st.caption(f"Published as of {flows['date']}; refreshes on every Scan selected batch. Not live intraday data. Market-wide, not individual-stock flows.")
        f1,f2,f3 = st.columns(3)
        f1.metric('FII net ₹Cr (latest)', f"{flows['fii_net']:+,.2f}")
        f2.metric('DII net ₹Cr (latest)', f"{flows['dii_net']:+,.2f}")
        f3.metric('5-session sentiment', flows['sentiment'])
        st.dataframe(pd.DataFrame(flows['rows']), use_container_width=True, hide_index=True)
        st.link_button('Moneycontrol source', flows['source'])
    else:
        st.warning('Institutional data unavailable. This is not a zero-flow reading. ' + (flow_error or ''))
    st.caption('Institutional cash flows provide contextual market sentiment; they do not override technical entry gates or establish stock-specific buying.')
    if not st.session_state.scan.empty:
        st.caption('Only the latest completed daily candle is eligible for a new entry; prior signals are historical.')
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
        st.subheader('Monthly-trigger candidates (score ≥ 8)')
        st.dataframe(d[d.Status == 'MONTHLY TRIGGER WATCH'],use_container_width=True,hide_index=True)
        st.subheader('Strong watchlist (score ≥ 8)')
        st.dataframe(d[d.Status == 'STRONG WATCHLIST'],use_container_width=True,hide_index=True)
        with st.expander('Rejected candidates and failed conditions'):
            st.dataframe(d[d.Status == 'REJECTED'],use_container_width=True,hide_index=True)
        st.info('Monthly-trigger watchlist means a crossover/breakout occurred this month, NOT that entry is safe now. Fresh entry additionally needs current confirmation. Option-chain expiry, IV, spread and OI must be checked manually before paper option entry.')
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
            api = DhanAPI(token, client_id=client_id)
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
