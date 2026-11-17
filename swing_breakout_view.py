"""Swing breakouts and one-click historical direction comparison. Read-only."""
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from swing_breakout import DAILY_METHODS, METHODS, compare, summarize
from underlying_backtest import load_bars

LABELS = dict(ema200='Price vs EMA200',ema50='EMA50 vs EMA200',st='Supertrend (10,3)',
              adx='ADX > 22',adx_rise='ADX rising',di='+DI / −DI',trigger='Original EMA / CPR trigger',
              rsi='RSI (14)',volume='Volume vs 20-session avg',ema21='Price vs EMA21')
AUDIT = Path(__file__).with_name('breakout_reference')


def render_swing_breakout(scan, stocks, expected, token, client_id, make_api, quote_for=None):
    st.subheader('⚡ Swing breakouts')
    live, history = st.tabs(['Latest daily scan', 'Historical comparison'])
    with live:
        st.caption('A completed daily candle must open on the original side of the level and close beyond it. Wick-only moves and candles that open beyond the level are not counted. The breakout is independent of your original setup filters and cannot trigger an order or a paper entry.')
        entries=[e for bundle in scan.get('_swing_breakouts',[]) if isinstance(bundle,list) for e in bundle]
        verified=int(scan.get('Daily source',pd.Series(dtype=str)).eq('Dhan').sum())
        st.caption(f'Daily candle date {expected:%d %b %Y} IST · {verified} Dhan-verified instruments in the scan. This tab does not use fallback candles.')
        method=st.selectbox('Breakout method',DAILY_METHODS,key='sw_bo_method')
        direction=st.radio('Direction',('Both','CE','PE'),horizontal=True,key='sw_bo_dir')
        st.caption('Click SCAN NOW in Market scanner to refresh the existing universe. During market hours, this tab shows the previous completed daily session.')
        visible=[e for e in entries if e['Method']==method and (direction=='Both' or e['Side']==direction)]
        if not visible:
            st.info('No matching breakouts in the verified scan.' if verified else 'Waiting for a Dhan-verified scan.')
        else:
            visible.sort(key=lambda e:-abs(e['Beyond level %']))
            records=[]
            for e in visible:
                row={k:v for k,v in e.items() if k not in ('Checks','Method','Session')}
                for k,label in LABELS.items():
                    v=e['Checks'].get(k)
                    row[label]='✓' if v is True else '✗' if v is False else '—'
                records.append(row)
            frame=pd.DataFrame(records)
            left,right=st.columns(2)
            left.metric('↑ Bullish breakouts',int(frame.Side.eq('CE').sum()))
            right.metric('↓ Bearish breakouts',int(frame.Side.eq('PE').sum()))
            styles=pd.DataFrame('',index=frame.index,columns=frame.columns)
            up=frame.Side.eq('CE')
            for selected,color in ((up,'#a5f3ca;background-color:#123e35'),(~up,'#ffb3ba;background-color:#51232d')):
                styles.loc[selected,['Breakout','Symbol','Close ₹']]='color:'+color+';font-weight:bold'
            for col in LABELS.values():
                styles.loc[frame[col].eq('✓'),col]='color:#a5f3ca;background-color:#123e35'
                styles.loc[frame[col].eq('✗'),col]='color:#ffb3ba;background-color:#51232d'
            st.caption('✓ Passed · ✗ Not met · — Unavailable. Original conditions are measured on this breakout candle. Prices and levels are for the underlying. The stop and 2R values are references from the signal close, not option-premium levels.')
            st.dataframe(frame.style.apply(lambda _: styles, axis=None),width='stretch',hide_index=True,
                         column_config={'Breakout':st.column_config.TextColumn(pinned=True),'Symbol':st.column_config.TextColumn(pinned=True),
                                        **{x:st.column_config.NumberColumn(format='%.2f') for x in frame if '₹' in x or '%' in x}})
            st.download_button('Download swing breakout CSV',frame.to_csv(index=False),f'swing_breakouts_{expected}.csv',key='sw_bo_live_csv')
            options={f"{e['Symbol']} · {e['Side']}":e for e in visible}
            choice=st.selectbox('Inspect a breakout',list(options),key='sw_bo_inspect')
            event=options[choice]
            st.markdown(f"**{event['Breakout']} · {choice} · ₹{event['Close ₹']:,.2f} · broke ₹{event['Level ₹']:,.2f}**")
            if token and quote_for and st.button('Check current Dhan option quote',key='sw_bo_quote'):
                try:
                    quote=quote_for(event['Symbol'],event['Side'])
                    st.session_state['sw_bo_quote_result']=(str(expected),choice,pd.Timestamp.now(tz='Asia/Kolkata').strftime('%d %b %H:%M:%S IST'),quote)
                except Exception:
                    st.warning('Dhan quote unavailable. Check your connection and try again.')
            saved=st.session_state.get('sw_bo_quote_result')
            if saved and saved[:2]==(str(expected),choice):
                q=saved[3]
                if q.get('Contract'):
                    st.write(f"{q['Contract']} · Bid ₹{q['Bid']:,.2f} · Ask ₹{q['Ask']:,.2f} · Expiry {q['Expiry']} · {saved[2]}")
                    st.caption('Dhan quote snapshot, not the premium at breakout time. It may be the last exchange quote outside market hours.')
                else: st.info(q.get('Note') or 'No matching option found.')
    with history:
        render_history(stocks,expected,token,client_id,make_api)


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
