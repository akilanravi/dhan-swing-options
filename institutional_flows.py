"""Read-only institutional cash-flow context. Amounts are ₹ crore."""
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo
import math
import re
import pandas as pd
import requests
import streamlit as st

REPORT='https://www.nseindia.com/reports/fii-dii'
HOLDINGS='https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern'
IST=ZoneInfo('Asia/Kolkata')
COLS=['Date','FII/FPI','DII']
TEMPLATE='Date,FII net (Cr),DII net (Cr)\n'

def key(label):
    return re.sub(r'[^a-z0-9]','',str(label).lower())

def money_num(value):
    try:
        v=float(str(value).strip().replace('−','-').replace(',','').replace('₹',''))
        return v if math.isfinite(v) else float('nan')
    except (TypeError,ValueError):
        return float('nan')

def normalize(raw,today=None):
    """Accept NSE category rows or a date/FII/DII archive, with dates paired strictly."""
    today=today or datetime.now(IST).date()
    f=raw.copy()
    f.columns=[key(k) for k in f.columns]
    if f.columns.duplicated().any():raise ValueError('Duplicate CSV column names.')
    if 'date' not in f:raise ValueError('Expected a Date column.')
    # Parse ISO dates separately to avoid interpreting YYYY-MM-DD as YYYY-DD-MM.
    text=f.date.astype(str).str.strip()
    iso=text.str.fullmatch(r'\d{4}-\d{2}-\d{2}')
    d=pd.to_datetime(text.where(iso),format='%Y-%m-%d',errors='coerce').fillna(
        pd.to_datetime(text.where(~iso),format='mixed',dayfirst=True,errors='coerce'))
    if d.isna().any() or (d.dt.date>today).any():raise ValueError('Archive has missing, invalid or future dates.')
    f['Date']=d.dt.normalize()
    if 'category' in f:
        net=next((x for x in f if x.startswith('netvalue') or x=='net'),None)
        if not net:raise ValueError('Expected NSE Net Value in crore.')
        mapping={'fii':'FII/FPI','fpi':'FII/FPI','fiifpi':'FII/FPI','dii':'DII'}
        out=f.assign(Group=f.category.map(lambda s:mapping.get(key(s))),Value=f[net].map(money_num))
        if out.Group.isna().any() or out.duplicated(['Date','Group']).any():
            raise ValueError('Expected one FII/FPI and one DII row per date. Do not mix the two NSE reports.')
        out=out.pivot(index='Date',columns='Group',values='Value').reset_index()
    else:
        fii=next((k for k in ('fiinetcr','fiinet','fiifpi','fii') if k in f),None)
        dii=next((k for k in ('diinetcr','diinet','dii') if k in f),None)
        if not fii or not dii:raise ValueError('Expected Date, FII net (Cr), DII net (Cr), or the NSE Category export.')
        if f.Date.duplicated().any():raise ValueError('Repeated dates in archive. Keep one complete row per date.')
        out=pd.DataFrame({'Date':f.Date,'FII/FPI':f[fii].map(money_num),'DII':f[dii].map(money_num)})
    out=out.reindex(columns=COLS).dropna().sort_values('Date').reset_index(drop=True)
    if out.empty:raise ValueError('No complete date with both FII and DII net values.')
    return out

@st.cache_data(ttl=300,show_spinner=False)
def latest():
    """NSE endpoint, best effort. No retries or access-control workarounds."""
    url='https://www.nseindia.com/api/fiidiiTradeReact'
    with requests.Session() as s:
        r=s.get(url,headers={'Accept':'application/json','Referer':REPORT},timeout=(4,7))
        if not r.ok:raise RuntimeError(f'NSE returned HTTP {r.status_code}. Use the official CSV upload.')
        data=r.json()
    if not isinstance(data,list):raise ValueError('Unexpected NSE response. Use the official CSV upload.')
    return normalize(pd.DataFrame(data))

def merge(prior,new):
    return pd.concat([prior,new],ignore_index=True).drop_duplicates('Date',keep='last').sort_values('Date').reset_index(drop=True)

def summarize(frame, requested):
    view=frame.sort_values('Date').tail(requested).copy()
    x,y=float(view['FII/FPI'].sum()),float(view['DII'].sum())
    label='Broad net buying' if x>0 and y>0 else 'Broad net selling' if x<0 and y<0 else 'Mixed / flat'
    return view,x,y,label

def show_flows(symbol,prefix,is_index=False):
    with st.expander('Institutional flows · FII/FPI and DII'):
        st.caption('Foreign (FII/FPI) and domestic (DII) are both institutional groups. These provisional combined cash-market figures are market-wide, not purchases of the selected stock.')
        n=st.number_input('Recent reported sessions',min_value=1,max_value=252,value=5,step=1,key=prefix+'_lookback')
        st.markdown(f'[NSE combined cash-market report]({REPORT})')
        st.caption('Use the combined NSE/BSE/MSEI table only. Download dated files and upload them together, or upload a saved archive.')
        files=st.file_uploader('Add or restore flow CSVs',type=['csv'],accept_multiple_files=True,key=prefix+'_input')
        state_key=prefix+'_archive'
        st.session_state.setdefault(state_key,pd.DataFrame(columns=COLS))
        if st.button('Import selected CSVs', key=prefix+'_import',disabled=not files):
            history=st.session_state[state_key]
            errors=[]
            processed=0
            for file in files:
                try:
                    parsed=normalize(pd.read_csv(BytesIO(file.getvalue()),encoding='utf-8-sig'))
                    history=merge(history,parsed)
                    processed+=1
                except Exception as exc:errors.append(f'{file.name}: {str(exc)[:170]}')
            st.session_state[state_key]=history
            if processed:st.success(f'Imported {processed} file(s).')
            if errors:st.warning('\n\n'.join(errors))
        if st.button('Fetch latest from NSE',key=prefix+'_refresh'):
            try:
                st.session_state[state_key]=merge(st.session_state[state_key],latest())
                st.caption('Latest available NSE observation merged by date.')
            except (ValueError,RuntimeError,requests.RequestException) as exc:
                st.warning(f'Latest report unavailable. Saved observations are unchanged. {str(exc)[:160]}')
        f=st.session_state[state_key]
        if f.empty:
            st.info('No flow observations yet. Fetch the latest report or import dated CSV files.')
            st.download_button('Download blank archive template',data=TEMPLATE,file_name='institutional_flow_template.csv',key=prefix+'_template')
        else:
            v,x,y,label=summarize(f,n)
            st.caption(f"{len(v)} of {n} requested observations · {v.Date.iloc[0]:%d %b %Y} to {v.Date.iloc[-1]:%d %b %Y}. Missing dates are not treated as zero. The archive may not contain consecutive sessions.")
            a,b,c=st.columns(3)
            a.metric('FII/FPI net',f'₹{x:+,.2f} Cr')
            b.metric('DII net',f'₹{y:+,.2f} Cr')
            c.metric('Combined net',f'₹{x+y:+,.2f} Cr')
            st.markdown(f"**Market backdrop:** {'🟢' if label=='Broad net buying' else '🔴' if label=='Broad net selling' else '🟡'} {label}")
            st.dataframe(v.sort_values('Date',ascending=False),hide_index=True,
                         column_config={'Date':st.column_config.DatetimeColumn(format='DD MMM YYYY'),
                                        'FII/FPI':st.column_config.NumberColumn(format='%.2f'),
                                        'DII':st.column_config.NumberColumn(format='%.2f')})
            st.bar_chart(v.set_index('Date')[['FII/FPI','DII']],stack=False,color=['#69bcff','#ffbb5c'])
            archive=f.rename(columns={'FII/FPI':'FII net (Cr)','DII':'DII net (Cr)'})
            st.download_button('Download flow archive CSV',archive.to_csv(index=False),'institutional_flows.csv',key=prefix+'_export')
        st.caption('Download your archive to keep it across sessions, then re-import it. The latest fetch does not backfill earlier dates. Flows are context only and do not change the verdict or trading rules.')
        if not is_index:
            st.markdown(f"**{symbol} ownership:** Search the symbol in [NSE company shareholding filings]({HOLDINGS}) and compare two reporting periods. Filings are ownership snapshots, not a daily buy/sell feed.")
        else:
            st.caption('A stock shareholding filing does not apply to this index.')
