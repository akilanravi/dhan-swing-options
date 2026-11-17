"""Swing breakouts and one-click historical direction comparison. Read-only."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
import streamlit as st

from swing_breakout import DAILY_METHODS, METHODS, WEEK, compare, summarize
from monthly_breakouts import month_labels, provider_filtered, session_sources
from underlying_backtest import load_bars

LABELS = dict(ema200='Price vs EMA200',ema50='EMA50 vs EMA200',st='Supertrend (10,3)',
              adx='ADX > 22',adx_rise='ADX rising',di='+DI / −DI',trigger='Original EMA / CPR trigger',
              rsi='RSI (14)',volume='Volume vs 20-session avg',ema21='Price vs EMA21')
AUDIT = Path(__file__).with_name('breakout_reference')


def render_swing_breakout(snapshot, stocks, expected, token, client_id, make_api, quote_for=None,
                          *, errors=None, scanning=False, scanned=False, next_session=None, review=None):
    st.subheader('⚡ Monthly Swing Breakouts')
    live, history, next_history = st.tabs(['Monthly breakout log', 'Historical comparison', 'Next-session research'])
    with live:
        st.caption('One row per symbol, signal date and selected method. Dates are daily-close confirmations, not the time price first crossed intraday. A daily breakout is known after the 15:30 IST close. For a new same-day scan, the app waits until 15:45 IST.')
        latest=snapshot.get('breakout_snapshot_date')
        events=snapshot.get('breakout_events',[])
        coverage=snapshot.get('breakout_coverage',[])
        month=st.selectbox('Calendar month',month_labels(expected),
                           format_func=lambda v:pd.Timestamp(v+'-01').strftime('%B %Y'),key='month_swing_month')
        method=st.selectbox('Breakout method',DAILY_METHODS,key='month_swing_method')
        direction=st.radio('Direction',('Both','CE','PE'),horizontal=True,key='month_swing_side')
        include=st.checkbox('Include Yahoo fallback for missing Dhan sessions (watchlist only)',key='month_swing_fallback',
                            help='Dhan takes priority per stock and date. Unchecked means Dhan-only.')
        origin=st.radio('Session filter',('Entire month','Latest completed session only'),horizontal=True,key='month_swing_window')
        if scanning:
            st.info('Scan running. Any events below are from the last available scan.')
        elif latest and latest!=expected:
            st.info(f'Showing saved history from the scan for {latest:%d %b %Y}. Refresh to check the expected {expected:%d %b %Y} session.')
        size=snapshot.get('breakout_requested')
        feeds=[({**{k:v for k,v in c.items() if k!='days'},
                 'Sessions checkable this month':sum(t.startswith(month) for t in c['days'].get(method,set()))}) for c in coverage]
        ds={c['Symbol'] for c in feeds if c['Daily source']=='Dhan' and c['Sessions checkable this month']}
        ys={c['Symbol'] for c in feeds if c['Daily source']=='Yahoo Finance' and c['Sessions checkable this month']}
        st.caption(f"Expected latest session {expected:%d %b %Y} · Dhan coverage this month {len(ds)} · Yahoo coverage this month {len(ys)}"+(f" · Requested {size} instruments" if size is not None else ''))
        with st.expander('Scan data status',expanded=not coverage and (scanning or scanned)):
            methods=[]
            for m in DAILY_METHODS:
                dh,ya=session_sources(coverage,m,expected.isoformat())
                de=provider_filtered(events,coverage,m,expected.isoformat(),False)
                com=provider_filtered(events,coverage,m,expected.isoformat(),True)
                methods.append({'Method':m,'Date':expected.isoformat(),'Dhan ready':len(dh),'Yahoo-only ready':len(ya),
                                'Dhan breakouts':len(de),'Yahoo-only breakouts':len(com)-len(de)})
            st.dataframe(pd.DataFrame(methods),width='stretch',hide_index=True)
            if feeds:
                st.dataframe(pd.DataFrame(feeds),width='stretch',hide_index=True)
            if errors is not None and not errors.empty:
                st.caption('Market Scanner exclusions can include symbols with too little history for EMA200. They may still appear in this breakout log.')
                fields=['Symbol','Status','Expected date','Dhan candle date','Dhan outcome','Yahoo status','Error']
                st.dataframe(errors[[k for k in fields if k in errors]],width='stretch',hide_index=True)
            if not feeds and (errors is None or errors.empty):
                st.caption('No history has been confirmed by this scan.')
        visible=provider_filtered(events,coverage,method,month,include)
        if direction!='Both': visible=[v for v in visible if v['Side']==direction]
        expected_iso=expected.isoformat()
        dates={t for c in coverage for t in c['days'].get(method,set()) if t.startswith(month)}
        if expected_iso.startswith(month): dates.add(expected_iso)
        date_to_check=expected_iso if expected_iso.startswith(month) else None
        if origin=='Entire month':
            selected_date=st.selectbox('Signal date', ['All dates',*sorted(dates,reverse=True)],key='month_swing_signal_day')
            if selected_date!='All dates':
                visible=[v for v in visible if v['Session date']==selected_date]
                date_to_check=selected_date
        else:
            date_to_check=max(dates,default=None)
            visible=[v for v in visible if v['Session date']==date_to_check]
        date_restrict=False
        if date_to_check:
            dh,ya=session_sources(coverage,method,date_to_check)
            date_visible=provider_filtered(events,coverage,method,date_to_check,include)
            if direction!='Both':
                date_visible=[e for e in date_visible if e['Side']==direction]
            st.caption(f'{date_to_check} · Dhan ready {len(dh)} · Yahoo-only ready {len(ya)} · {len(date_visible)} matching events before the symbol filter.')
            date_restrict=not dh and not (include and ya) and (origin!='Entire month' or selected_date!='All dates')
            if not dh and ya and not include:
                st.warning(f'{date_to_check} has no usable Dhan candles for this method. Enable the Yahoo fallback above to view the available same-date data.')
            elif not dh and not ya:
                st.warning(f'{date_to_check} has no verified candles with sufficient history for this method yet. Older rows below are not signals for this date.')
            elif not dh and ya and include:
                st.info(f'{date_to_check} is using labelled Yahoo watchlist data, not Dhan candles.')
            elif size and len(dh)<size and not include:
                st.caption(f'Dhan coverage for this session is partial ({len(dh)} of {size}). An empty list is not a market-wide result.')
        stock_names=sorted({v['Symbol'] for v in visible})
        picked=st.multiselect('Filter symbols (optional)',stock_names,key='month_swing_symbols')
        if picked: visible=[v for v in visible if v['Symbol'] in picked]
        if not visible:
            if not coverage:
                if not scanning:
                    st.info('No breakout history loaded. Click Refresh monthly breakouts or use SCAN NOW in Market Scanner.')
            elif not ds and ys and not include:
                st.warning('Only Yahoo has usable history for this month. You can opt in to the labelled fallback watchlist or keep the view Dhan-only.')
            elif not (ds or (ys and include)):
                st.info('No candles with enough range history in this month for the selected method and source.')
            elif not date_restrict:
                st.info('History was checked. No breakout matches these filters.')
        else:
            visible.sort(key=lambda e:(e['Session date'],abs(e['Beyond level %'])),reverse=True)
            records=[]
            for e in visible:
                row={k:v for k,v in e.items() if k!='Checks'}
                for k,label in LABELS.items():
                    v=e['Checks'].get(k)
                    row[label]='✓' if v is True else '✗' if v is False else '—'
                records.append(row)
            frame=pd.DataFrame(records)
            left,right,total=st.columns(3)
            left.metric('↑ Upside events',int(frame.Side.eq('CE').sum()))
            right.metric('↓ Downside events',int(frame.Side.eq('PE').sum()))
            total.metric('Unique instruments',int(frame.Symbol.nunique()))
            styles=pd.DataFrame('',index=frame.index,columns=frame.columns)
            up=frame.Side.eq('CE')
            for selected,color in ((up,'#a5f3ca;background-color:#123e35'),(~up,'#ffb3ba;background-color:#51232d')):
                styles.loc[selected,['Breakout','Symbol','Close ₹']]='color:'+color+';font-weight:bold'
            for col in LABELS.values():
                styles.loc[frame[col].eq('✓'),col]='color:#a5f3ca;background-color:#123e35'
                styles.loc[frame[col].eq('✗'),col]='color:#ffb3ba;background-color:#51232d'
            st.caption('✓ Passed · ✗ Not met · — Not enough indicator history. Checks are measured on the signal date. Price, stop and target are underlying references. A later cross by the same symbol is shown on its own date.')
            st.dataframe(frame.style.apply(lambda _:styles,axis=None),width='stretch',hide_index=True,
                         column_order=['Session date','Symbol','Breakout','Side','Method','Daily source','Open ₹','High ₹','Low ₹',
                                       'Close ₹','Volume','Level ₹','Beyond level %','Range high ₹','Range low ₹',
                                       'Reference stop ₹','Reference 2R ₹','Existing score','Passed checks',
                                       'Unavailable checks',*LABELS.values()],
                         column_config={'Session date':st.column_config.TextColumn(pinned=True),
                                        'Symbol':st.column_config.TextColumn(pinned=True),
                                        **{x:st.column_config.NumberColumn(format='%.2f') for x in frame if '₹' in x or '%' in x}})
            st.download_button('Download monthly breakout CSV',frame.to_csv(index=False),f'swing_breakouts_{month}_{method.split(" · ")[-1].replace(" ","_")}.csv',key='month_swing_csv')
            options={f"{e['Session date']} · {e['Symbol']} · {e['Side']}":e for e in visible}
            choice=st.selectbox('Inspect a breakout',list(options),key='month_swing_inspect')
            event=options[choice]
            st.markdown(f"**{event['Breakout']} · {choice} · ₹{event['Close ₹']:,.2f} · broke ₹{event['Level ₹']:,.2f}**")
            st.caption(f"Candle source · {event['Daily source']}")
            if token and next_session and review:
                render_opening_review(event,next_session,review)
            if event['Session date']==expected.isoformat() and token and quote_for:
                if st.button('Check current Dhan option quote',key='month_swing_quote'):
                    try:
                        quote=quote_for(event)
                        st.session_state['month_swing_quote_result']=(choice,method,pd.Timestamp.now(tz='Asia/Kolkata').strftime('%d %b %H:%M:%S IST'),quote)
                    except Exception:
                        st.warning('Dhan quote unavailable. Check your connection and try again.')
                saved=st.session_state.get('month_swing_quote_result')
                if saved and saved[:2]==(choice,method):
                    q=saved[3]
                    if q.get('Contract'):
                        st.write(f"{q['Contract']} · Bid ₹{q['Bid']:,.2f} · Ask ₹{q['Ask']:,.2f} · Expiry {q['Expiry']} · {saved[2]}")
                        st.caption('Current Dhan snapshot, not the premium on a historical signal.')
                    else: st.info(q.get('Note') or 'No matching option found.')
    with history:
        render_history(stocks,expected,token,client_id,make_api)
    with next_history:
        render_next_reference()


def render_opening_review(event,next_session,review):
    day=next_session(date.fromisoformat(event['Session date']))
    ist=ZoneInfo('Asia/Kolkata')
    now=datetime.now(ist)
    cutoff=datetime.combine(day,time(9,30),tzinfo=ist)
    keys=(event['Symbol'],event['Session date'],event['Method'],event['Side'],event['Daily source'],event['Close ₹'],event['Level ₹'])
    with st.expander(f'Next-session opening review · {day:%d %b %Y} IST'):
        st.caption('Watchlist context only. This does not calculate an option return, place a trade, or trigger a paper entry.')
        if now<cutoff:
            st.info(f'Wait for the {day:%d %b} 09:15–09:30 IST candle to complete. No confirmation yet.')
            return
        if st.button('Load first 15-minute Dhan candle',key='swing_open_review'):
            try:
                result=review(event,day)
                if result is None:
                    st.session_state['opening_review']=(keys,None,'Dhan did not return the exact 09:15 IST candle for this session.')
                else:
                    st.session_state['opening_review']=(keys,result,'')
            except Exception:
                st.session_state['opening_review']=(keys,None,'Opening candle unavailable from Dhan. Check access or retry.')
        saved=st.session_state.get('opening_review')
        if not saved or saved[0]!=keys:
            return
        r=saved[1]
        if r is None:
            st.info(saved[2])
            return
        for col, (title,value) in zip(st.columns(3),
                                      [('Opening gap',r['gap_pct']),('09:15 → 09:30',r['first_15m_pct']),
                                       ('09:30 vs signal close',r['vs_prior_pct'])]):
            col.metric(title,f'{value:+.2f}%')
        st.caption(f"Dhan underlying candle · Open ₹{r['open']:,.2f} · Close ₹{r['close']:,.2f}")
        for title,ok in [('First candle agrees with the signal',r['first_candle_agrees']),
                         ('Beyond the prior signal close',r['beyond_prior_close']),
                         ('Still beyond the broken level',r['broken_level_holds']),
                         ('Retest and move back beyond the broken level',r['retest'])]:
            st.write(('✓ ' if ok else '✗ ')+title)
        if event['Method']==WEEK and event['Side']=='CE' and r['confirm']:
            st.info('Matches the prior-week upside + 09:30 confirmation research filter. It did not beat its matched comparison in the November holdout.')


def render_next_reference():
    f=AUDIT/'next_session_summary.csv'
    if not f.exists():
        st.info('Install the breakout_reference folder from this release to load the completed research comparison.')
        return
    st.markdown((AUDIT/'NEXT_SESSION.md').read_text(encoding='utf-8'))
    summary=pd.read_csv(f)
    cols=st.columns(3)
    period=cols[0].selectbox('Sample',['Nov holdout','Sep–Oct'],key='nextref_sample')
    horizon=cols[1].selectbox('Exit time (IST)',['10:30','EOD'],key='nextref_exit')
    side=cols[2].selectbox('Signal direction',['CE','PE','Both'],key='nextref_side')
    view=summary[summary.Period.eq(period)&summary.Exit.eq(horizon)]
    if side!='Both':
        view=view[view.Side.eq(side)]
    st.dataframe(view.style.format(precision=3,na_rep='—'),width='stretch',hide_index=True)
    st.download_button('Download next-session backtest summary',f.read_bytes(),'next_session_summary.csv',key='nextref_download')
    trades=AUDIT/'next_session_trades.csv'
    if trades.exists():
        st.download_button('Download next-session trade log',trades.read_bytes(),'next_session_trades.csv',key='nextref_trades')


def render_history(stocks,expected,token,client_id,make_api):
    st.caption('Compare daily closes versus 10-session, 20-session, and prior-week ranges, plus a full 60-minute close versus the same prior 20-session range. All four enter at the next session open so the comparison tests swing direction, not intraday entry speed.')
    st.caption('Common days only when including 60m, which requires all six full hours. One non-overlapping trade per symbol and method. Stop 1.5× daily ATR, target 2R, stop first if both are touched in the same daily candle. Gap stops fill at the open. Windows touching an unexplained overnight move of 18% or more are excluded. Direction hit measures the final horizon close versus entry, irrespective of the stop or target. No option premiums or options P&L.')
    if (AUDIT/'summary.csv').exists():
        with st.expander('Reference backtest · completed historical run',expanded=True):
            st.markdown((AUDIT/'README.md').read_text(encoding='utf-8'))
            ref=pd.read_csv(AUDIT/'summary.csv')
            st.dataframe(ref.style.format(precision=2,na_rep='—'),width='stretch',hide_index=True)
            st.download_button('Download reference trades', (AUDIT/'trades.csv').read_bytes(), 'swing_reference_trades.csv',key='sw_bo_ref_csv')
            st.download_button('Download reference data coverage', (AUDIT/'coverage.csv').read_bytes(), 'swing_reference_coverage.csv',key='sw_bo_ref_coverage')
    if not token or stocks.empty:
        st.info('Configure Dhan in Streamlit Secrets and load the instrument master to run your own comparison.')
        return
    universe={x['symbol']:x for x in stocks.to_dict('records')}
    universe['NIFTY']={'symbol':'NIFTY','sid':'13','segment':'IDX_I','instrument':'INDEX'}
    with st.form('sw_bo_hist_form'):
        symbols=st.multiselect('Underlyings', sorted(universe), default=[x for x in ('NIFTY','RELIANCE','HDFCBANK','INFY','SBIN') if x in universe],max_selections=12)
        a,b,c=st.columns(3)
        start=a.date_input('Start',date(max(2024,expected.year-2),1,1),max_value=expected)
        end=b.date_input('End',expected,max_value=expected)
        split=c.date_input('Holdout starts',date(max(2025,expected.year-1),1,1),max_value=expected)
        d,e,f=st.columns(3)
        hold=d.selectbox('Maximum trading sessions held',(2,3,5,7,10),index=2)
        cost=e.number_input('Round-trip underlying cost assumption (bps)',0,100,10)
        use_hour=f.checkbox('Compare 60m on matching dates',True)
        run=st.form_submit_button('Run Dhan breakout comparison',type='primary')
    if run:
        st.session_state.pop('sw_bo_results',None)
        if not symbols or not start < split <= end:
            st.error('Select at least one underlying and dates with Start < Holdout starts ≤ End.')
        else:
            output,coverage,errors=[],[],[]
            progress=st.progress(0.0,text='Downloading Dhan historical candles…')
            api=make_api(token,client_id)
            try:
                for i,symbol in enumerate(symbols,1):
                    progress.progress((i-1)/len(symbols),text=f'{symbol} · {i}/{len(symbols)}')
                    try:
                        x=universe[symbol]
                        daily=load_bars(api,x,start,end,0)
                        hours=load_bars(api,x,start,end,60) if use_hour else None
                        rows,cov=compare(symbol,daily,hours,pd.Timestamp(start),pd.Timestamp(end),pd.Timestamp(split),hold=hold,cost_bps=cost)
                        output.extend(rows); coverage.extend(cov)
                    except Exception as exc:
                        errors.append({'Symbol':symbol,'Reason':str(exc)[:170]})
            finally:
                if hasattr(api,'session'): api.session.close()
                progress.empty()
            st.session_state['sw_bo_results']=(pd.DataFrame(output),pd.DataFrame(coverage),errors,hold,start,split,end,cost,tuple(symbols),use_hour)
    if 'sw_bo_results' not in st.session_state:
        return
    trades,cov,errors,hold,start,split,end,cost,symbols,use_hour=st.session_state['sw_bo_results']
    st.caption(f"Last run · {', '.join(symbols)} · {start} to {end} · holdout {split} · {hold} sessions · {cost} bps · {'matched daily/60m dates' if use_hour else 'daily only'}")
    if errors:
        st.warning('Some instruments could not be compared. They were not silently substituted.')
        st.dataframe(pd.DataFrame(errors),hide_index=True)
    if not cov.empty:
        with st.expander('Historical data coverage'):
            st.dataframe(cov,hide_index=True)
    if trades.empty:
        st.info('No valid trades for this date range and data coverage.')
        return
    result=summarize(trades,hold)
    holdouts=result[result.Period.eq('Holdout') & result.Side.eq('Both')]
    for _,row in holdouts.iterrows():
        if row.Trades<30:
            st.caption(f"Small holdout sample · {row['Method']} had {row.Trades} trades.")
    if len(holdouts) and holdouts['Avg net R'].le(0).all():
        st.info('No tested method had positive net expectancy in the holdout.')
    if len(holdouts)==len(METHODS if use_hour else DAILY_METHODS) and (holdouts.Trades>=30).all() and not errors and cov['Comparable sessions'].sum()>0:
        best=holdouts.sort_values('Avg net R',ascending=False).iloc[0]
        st.write(f"**Highest holdout average net R in this run · {best.Method} · {best['Avg net R']:+.3f}R over {best.Trades} trades.**")
    st.dataframe(result.style.format(precision=2,na_rep='—'),width='stretch',hide_index=True)
    st.download_button('Download Dhan comparison summary',result.to_csv(index=False),'swing_dhan_breakout_summary.csv',key='sw_bo_stats')
    st.download_button('Download directional trade log',trades.to_csv(index=False),'swing_dhan_breakout_trades.csv',key='sw_bo_trades')
