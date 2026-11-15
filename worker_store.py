"""One durable journal writer per strategy. Dashboards only read in worker mode."""
import io
import json
import re
import os
import requests
import pandas as pd

def enabled(value):
    return str(value).strip().lower() in ('1','true','yes','on')

class CloudJournal:
    def __init__(self, token, gist, strategy):
        if not token or not re.fullmatch(r'[a-fA-F0-9]{16,64}', str(gist or '')):
            raise ValueError('A GitHub token and an existing Gist ID are required')
        self.url=f'https://api.github.com/gists/{gist}'
        self.headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json'}
        self.csv={'intraday':'intraday_paper_trades.csv','swing':'paper_trades.csv'}[strategy]
        self.state=f'{strategy}_worker_status.json'

    def read(self):
        r=requests.get(self.url,headers=self.headers,timeout=12)
        if not r.ok:raise RuntimeError(f'GitHub journal read HTTP {r.status_code}')
        files=r.json()['files']
        def content(name):
            f=files.get(name)
            if not f:return ''
            if f.get('truncated'):raise RuntimeError(f'{name} too large for inline journal read; archive before resuming')
            return f.get('content','')
        raw=content(self.csv)
        frame=pd.read_csv(io.StringIO(raw),dtype={'id':str}) if raw.strip() else pd.DataFrame()
        status=content(self.state)
        return frame,json.loads(status) if status else {}

    def write(self, frame, state):
        files={self.state:{'content':json.dumps(state,ensure_ascii=False)}}
        if frame is not None and len(frame.columns):
            files[self.csv]={'content':frame.to_csv(index=False)}
        r=requests.patch(self.url,headers=self.headers,json={'files':files},timeout=12)
        if not r.ok:raise RuntimeError(f'GitHub journal write HTTP {r.status_code}')

def cloud_journal(token,gist,strategy):
    return CloudJournal(token,gist,strategy)

def market_open(now, env):
    holidays={x.strip() for x in env.get('NSE_CLOSED_DATES','').split(',')}
    return (now.weekday()<5 and now.date().isoformat() not in holidays
            and (9,15)<=(now.hour,now.minute)<(15,30))

def load_env(path):
    """Read simple KEY=value lines on each cycle so token changes take effect."""
    from pathlib import Path
    return {**os.environ, **dict(line.strip().split('=',1) for line in Path(path).read_text().splitlines()
                                 if line.strip() and not line.lstrip().startswith('#') and '=' in line)}
