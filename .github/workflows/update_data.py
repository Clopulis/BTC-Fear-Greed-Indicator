#!/usr/bin/env python3
"""Poll verified UTC daily opens and sentiment until both are available."""
import datetime as dt
import json
import math
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / 'index.html'
UTC = dt.timezone.utc
START = dt.date(2018, 2, 1)
POLL = 30

def fetch_json(url):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'BTC-FG-Indicator/1.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)

def coinbase_opens(first, end):
    out = {}
    current = first
    while current <= end:
        stop = min(current + dt.timedelta(days=200), end + dt.timedelta(days=1))
        query = urllib.parse.urlencode({'granularity':86400,
            'start':dt.datetime.combine(current,dt.time(),UTC).isoformat(),
            'end':dt.datetime.combine(stop,dt.time(),UTC).isoformat()})
        rows = fetch_json('https://api.exchange.coinbase.com/products/BTC-USD/candles?' + query)
        if not isinstance(rows,list):
            raise RuntimeError('Coinbase response was not a candle list')
        for row in rows:
            if not isinstance(row,list) or len(row)<6:
                raise RuntimeError('Malformed Coinbase candle')
            ts=int(row[0]); opening=float(row[3])
            if ts%86400 or not math.isfinite(opening) or opening<=0:
                raise RuntimeError('Invalid Coinbase UTC daily open')
            day=dt.datetime.fromtimestamp(ts,UTC).date()
            if first<=day<=end:
                if day in out and out[day]!=opening:
                    raise RuntimeError(f'Conflicting Coinbase candle: {day}')
                out[day]=opening
        current=stop
    return out

def fear_greed(limit):
    payload=fetch_json(f'https://api.alternative.me/fng/?limit={limit}&format=json')
    if not isinstance(payload,dict) or not isinstance(payload.get('data'),list) or payload.get('metadata',{}).get('error'):
        raise RuntimeError('Invalid Alternative.me response')
    out={}
    for item in payload['data']:
        day=dt.datetime.fromtimestamp(int(item['timestamp']),UTC).date()
        value=int(item['value'])
        if not 0<=value<=100 or (day in out and out[day]!=value):
            raise RuntimeError(f'Invalid or conflicting Fear & Greed reading: {day}')
        out[day]=value
    return out

def complete(data, day):
    i=(day-START).days
    return 0<=i<len(data['c']) and i<len(data['g']) and data['c'][i] is not None and data['g'][i] is not None

def publish(original, match, old, end, btc, fg):
    dates=[START+dt.timedelta(days=i) for i in range((end-START).days+1)]
    new=dict(old)
    for key, values in [('c',btc),('g',fg)]:
        new[key]=[values.get(d,old[key][i] if i<len(old[key]) else None) for i,d in enumerate(dates)]
    if not complete(new,end):
        raise RuntimeError('Refusing incomplete daily publication')
    new['s']=[]; new['thr']=[]; new['rej']=[]
    updated=original[:match.start()]+'const D='+json.dumps(new,separators=(',',':'),allow_nan=False)+';'+original[match.end():]
    updated=re.sub(r'(<script id="btc-update-status" type="application/json">).*?(</script>)',lambda m:m.group(1)+'{"waiting":false}'+m.group(2),updated,count=1)
    updated=re.sub(r'<div class="sub">Updated through .*?</div>',f'<div class="sub">Updated through {end.isoformat()} (UTC daily opens). BTC/USD: Coinbase Exchange 1-day candles (UTC), open. Fear &amp; Greed: Alternative.me dated historical API. Missing sentiment dates remain unavailable. Signals recalculated in the browser from these inputs.</div>',updated,count=1)
    marker='(function recalculateSignals(){'
    if original.count(marker)!=1 or updated.count(marker)!=1 or original.split(marker,1)[1].split('})();',1)[0]!=updated.split(marker,1)[1].split('})();',1)[0]:
        raise RuntimeError('Signal calculation code changed unexpectedly')
    HTML.write_text(updated,encoding='utf-8')
    print(f'Both verified daily observations published locally for {end}',flush=True)

def main():
    original=HTML.read_text(encoding='utf-8')
    match=re.search(r'(?m)^const D=(\{[^\n]*\});$',original)
    if not match or 'id="btc-update-status"' not in original:
        raise RuntimeError('Missing dashboard data or status marker')
    old=json.loads(match.group(1))
    if old.get('start')!=START.isoformat() or not all(k in old for k in ('c','g','s')):
        raise RuntimeError('Unexpected dashboard data structure')
    now=dt.datetime.now(UTC); target=now.date()
    if complete(old,target):
        if now.hour<22:
            print('Today already complete; no API requests'); return
        target+=dt.timedelta(days=1)
    midnight=dt.datetime.combine(target,dt.time(),UTC)
    wait=(midnight-now).total_seconds()
    if wait>0:
        print(f'Waiting until {midnight.isoformat()}',flush=True); time.sleep(wait)
    deadline=time.monotonic()+110*60
    first=START+dt.timedelta(days=min(len(old['c']),len(old['g'])))
    first=min(first,target)
    # Include trailing incomplete days; preserve existing verified history.
    while first>START and not complete(old,first-dt.timedelta(days=1)):
        first-=dt.timedelta(days=1)
    btc={}; fg={}
    with ThreadPoolExecutor(max_workers=2) as pool:
        while time.monotonic()<deadline:
            tick=time.monotonic()
            tasks=[]
            if target not in btc: tasks.append((btc,pool.submit(coinbase_opens,first,target)))
            if target not in fg: tasks.append((fg,pool.submit(fear_greed,max(3,(target-first).days+2))))
            for store,task in tasks:
                try: store.update(task.result())
                except Exception as exc: print(f'Retryable source error: {exc}',flush=True)
            if target in btc and target in fg:
                publish(original,match,old,target,btc,fg); return
            print(f'{target}: Coinbase={target in btc}, Alternative.me={target in fg}; retrying',flush=True)
            time.sleep(max(0,POLL-(time.monotonic()-tick)))
    raise RuntimeError('Daily data still unavailable; next scheduled run resumes polling')

if __name__=='__main__':
    main()
