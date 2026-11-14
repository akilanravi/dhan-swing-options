import os
from datetime import date,timedelta
import pandas as pd
import streamlit as st
from dhan_api import DhanAPI
from signals import indicators,annotate

st.set_page_config(page_title='Dhan Swing Options',page_icon='📈',layout='wide')
st.title('Dhan Swing Options | Research Dashboard')
st.caption('Dhan API only · Daily CE/PE screening · No automatic orders · No Telegram · No Yahoo')
with st.sidebar:
    st.header('Connection')
    token=st.text_input('Dhan access token',value=os.getenv('DHAN_ACCESS_TOKEN',''),type='password',help='Use Streamlit secrets/environment variable on deployment; never commit tokens')
    st.header('Instrument mapping')
    symbol=st.text_input('Display name','NIFTY')
    sid=st.text_input('Dhan underlying security ID','13')
    segment=st.selectbox('Exchange segment',['IDX_I','NSE_EQ','NSE_FNO'])
    instrument=st.selectbox('Instrument type',['INDEX','EQUITY','FUTIDX','FUTSTK'])
    st.caption('Verify IDs from the current Dhan instrument master. Default 13 is an example and must be checked.')
    capital=st.number_input('Capital ₹',min_value=1000,value=100000,step=5000)
    risk_pct=st.slider('Risk per trade %',1.0,2.0,1.0,0.5)
    cpr_width=st.slider('Maximum weekly CPR width %',0.1,5.0,1.5,0.1)/100
    min_score=st.slider('Minimum signal score',7,10,7)
    st.header('Events')
    events=st.text_area('Blackout dates YYYY-MM-DD (one per line)','')
    st.caption('Maintain corporate result and major-event dates manually. ±3 calendar days are blocked.')
    run=st.button('Fetch and analyze',type='primary',use_container_width=True)

if not token:
    st.info('Enter your Dhan API access token in the sidebar. The app does not save it to a file.')
elif run:
    try:
        api=DhanAPI(token)
        today=date.today()
        with st.spinner('Fetching Dhan daily and 60-minute candles…'):
            daily=api.candles(sid,segment,instrument,(today-timedelta(days=1500)).isoformat(),(today+timedelta(days=1)).isoformat())
            hourly=api.candles(sid,segment,instrument,(today-timedelta(days=30)).strftime('%Y-%m-%d 09:15:00'),(today+timedelta(days=1)).strftime('%Y-%m-%d 15:30:00'),60)
        config={'min_score':min_score,'max_weekly_cpr_width':cpr_width,'event_blackout_dates':{symbol:[x.strip() for x in events.splitlines() if x.strip()]}}
        d=annotate(indicators(daily),config,symbol)
        latest=d.iloc[-1]
        st.subheader(f'{symbol} · Last completed daily candle: {d.index[-1]:%d %b %Y}')
        a,b,c,e=st.columns(4)
        a.metric('Underlying close',f'₹{latest.Close:,.2f}')
        b.metric('Score',f'{int(latest.score)}/10')
        c.metric('Signal',latest.signal or 'NO TRADE')
        e.metric('ADX',f'{latest.adx:.1f}')
        st.line_chart(d[['Close','ema21','ema50','ema200']].tail(150))
        st.subheader('Signal and levels')
        side=latest.signal
        if side:
            direction=1 if side=='CE' else -1
            entry=float(latest.Close); stop=entry-direction*1.5*float(latest.atr)
            target1=entry+direction*1.5*float(latest.atr)
            target2=entry+direction*3*float(latest.atr)
            st.dataframe(pd.DataFrame([{'Side':side,'Reference close':entry,'Underlying SL':stop,'Target 1 (1R)':target1,'Target 2 (2R)':target2,'Score':int(latest.score)}]),hide_index=True,use_container_width=True)
            st.warning('These are underlying reference levels, NOT executable option premium levels. Confirm option chain, delta, IV, spread, expiry and lot size before placing any trade.')
        else:
            st.info('Mandatory signal filters did not all pass. No trade recommended.')
        st.subheader('Last 60-minute candles')
        st.dataframe(hourly.tail(12).sort_index(ascending=False),use_container_width=True)
        st.caption('Hourly candles shown for timing review. V2 does not automatically execute hourly entry confirmations.')
        st.subheader('Historical daily signals')
        log=d[d.signal!=''][['Close','signal','score','atr','rsi','adx']].copy()
        st.dataframe(log.tail(50).sort_index(ascending=False),use_container_width=True)
        st.download_button('Download signal history CSV',log.to_csv().encode(),file_name='dhan_daily_signals.csv',mime='text/csv')
        st.subheader('Dhan option chain (manual inspection)')
        if st.button('Fetch available expiries'):
            st.write(api.expiries(sid,segment))
        expiry=st.text_input('Expiry YYYY-MM-DD (from Dhan expiry list)','')
        if st.button('Fetch option chain') and expiry:
            chain=api.chain(sid,segment,expiry)
            st.json(chain,expanded=False)
        st.caption('Raw chain view only; no fabricated IV percentile, delta or contract selection. Dhan access/permissions and rate limits apply.')
    except Exception as exc:
        st.error(str(exc))
else:
    st.markdown('### Workflow\n1. Enter your Dhan token and correct security ID.\n2. Fetch candles and inspect signal score.\n3. Review underlying entry, stop and targets.\n4. Inspect Dhan option chain before choosing a contract.\n5. Trade manually in Dhan or Zerodha.')
    st.warning('This initial website is a research scanner. Historical option P&L, automated option selection, portfolio risk tracking and full walk-forward validation are not implemented yet.')
