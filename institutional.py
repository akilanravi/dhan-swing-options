"""Best-effort public Moneycontrol cash-market FII/DII data; not intraday live.
No login, captcha bypass, or unofficial private endpoint. Fail closed as unavailable.
"""
from datetime import datetime
import re
import requests
from bs4 import BeautifulSoup

URL = 'https://www.moneycontrol.com/stocks/marketstats/fii_dii_activity/'
DATE_RE = re.compile(r'\b(\d{1,2})[- /]([A-Za-z]{3})[- /](20\d{2})\b')
NUM_RE = re.compile(r'(?<![A-Za-z])[-+]?\d[\d,]*\.\d{2}(?!\d)')

def fetch_moneycontrol_flows():
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; MarketResearchDashboard/1.0)',
               'Accept': 'text/html,application/xhtml+xml'}
    response = requests.get(URL, headers=headers, timeout=12)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    records = []
    seen = set()
    for tr in soup.select('tr'):
        cells = [c.get_text(' ', strip=True) for c in tr.find_all(['td', 'th'], recursive=False)]
        if len(cells) < 7:
            continue
        date_match = DATE_RE.search(cells[0])
        if not date_match:
            continue
        try:
            asof = datetime.strptime(date_match.group(0).replace('/', '-').replace(' ', '-'), '%d-%b-%Y').date()
            # Six numeric columns: FII buy, sell, net; DII buy, sell, net.
            vals = []
            for cell in cells[1:]:
                m = NUM_RE.search(cell)
                if m:
                    vals.append(float(m.group().replace(',', '')))
                if len(vals) == 6:
                    break
            if len(vals) != 6 or asof in seen:
                continue
            if abs((vals[0]-vals[1])-vals[2]) > 2 or abs((vals[3]-vals[4])-vals[5]) > 2:
                continue
            seen.add(asof)
            records.append({'date': asof.isoformat(), 'fii_buy': vals[0], 'fii_sell': vals[1],
                            'fii_net': vals[2], 'dii_buy': vals[3], 'dii_sell': vals[4], 'dii_net': vals[5]})
        except (ValueError, TypeError):
            continue
    if not records:
        raise ValueError('Moneycontrol table not accessible or its HTML layout changed; institutional data unavailable')
    records.sort(key=lambda r: r['date'], reverse=True)
    return records

def summarize(records):
    latest = records[0]
    month = latest['date'][:7]
    month_rows = [r for r in records if r['date'].startswith(month)]
    recent = records[:5]
    fii5 = sum(r['fii_net'] for r in recent)
    dii5 = sum(r['dii_net'] for r in recent)
    fiim = sum(r['fii_net'] for r in month_rows)
    diim = sum(r['dii_net'] for r in month_rows)
    if fii5 > 0 and dii5 > 0: sentiment = 'BROAD BUYING'
    elif fii5 < 0 and dii5 < 0: sentiment = 'BROAD SELLING'
    else: sentiment = 'MIXED'
    return {'date': latest['date'], 'fii_net': latest['fii_net'], 'dii_net': latest['dii_net'],
            'fii_5d': fii5, 'dii_5d': dii5, 'fii_mtd': fiim, 'dii_mtd': diim,
            'sentiment': sentiment, 'source': URL, 'rows': records}
