#!/usr/bin/env python3
"""Refresh the embedded dashboard from verified UTC Coinbase opens and Alternative.me dates.

Never invent observations. Retry missing dated values on every scheduled run.
"""
import datetime as dt
import json
import math
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / 'index.html'
UTC = dt.timezone.utc
START = dt.date(2018, 2, 1)

def fetch_json(url):
    for attempt in range(5):
        try:
            request = urllib.request.Request(url, headers={
                'User-Agent': 'BTC-FG-Indicator/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(request, timeout=35) as response:
                return json.load(response)
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt + 1)

def coinbase_opens(end_date):
    out = {}
    current = START
    while current <= end_date:
        stop = min(current + dt.timedelta(days=200), end_date + dt.timedelta(days=1))
        start_ts = dt.datetime.combine(current, dt.time(), UTC).isoformat().replace('+00:00','Z')
        end_ts = dt.datetime.combine(stop, dt.time(), UTC).isoformat().replace('+00:00','Z')
        query = urllib.parse.urlencode({'granularity':86400,'start':start_ts,'end':end_ts})
        rows = fetch_json('https://api.exchange.coinbase.com/products/BTC-USD/candles?' + query)
        if not isinstance(rows,list):
            raise RuntimeError('Coinbase response was not a candle list')
        for row in rows:
            if not isinstance(row,list) or len(row) < 6:
                raise RuntimeError('Malformed Coinbase candle')
            ts = int(row[0]); opening = float(row[3])
            if ts % 86400 or not math.isfinite(opening) or opening <= 0:
                raise RuntimeError('Invalid Coinbase UTC daily open')
            day = dt.datetime.fromtimestamp(ts,UTC).date()
            if START <= day <= end_date:
                if day in out and out[day] != opening:
                    raise RuntimeError(f'Conflicting Coinbase candle: {day}')
                out[day] = opening
        current = stop
        time.sleep(0.18)
    return out

def fear_greed():
    payload = fetch_json('https://api.alternative.me/fng/?limit=0&format=json')
    if not isinstance(payload,dict) or not isinstance(payload.get('data'),list) or payload.get('metadata',{}).get('error'):
        raise RuntimeError('Invalid Alternative.me response')
    out = {}
    for item in payload['data']:
        day = dt.datetime.fromtimestamp(int(item['timestamp']),UTC).date()
        value = int(item['value'])
        if not 0 <= value <= 100:
            raise RuntimeError(f'Invalid Fear & Greed reading: {day}')
        if day in out and out[day] != value:
            raise RuntimeError(f'Conflicting Fear & Greed reading: {day}')
        out[day] = value
    return out

def main():
    # The current UTC day opening price is fixed once its daily candle starts.
    end = dt.datetime.now(UTC).date()
    if end < START:
        raise RuntimeError('No UTC day available')
    original = HTML.read_text(encoding='utf-8')
    status_tag = '<script id="btc-update-status" type="application/json">{"waiting":false}</script>'
    if 'id="btc-update-status"' not in original:
        raise RuntimeError('Missing update status marker in index.html')
    match = re.search(r'(?m)^const D=(\{[^\n]*\});$',original)
    if not match:
        raise RuntimeError('Expected embedded const D JSON; refusing to modify')
    old = json.loads(match.group(1))
    if old.get('start') != START.isoformat() or not all(k in old for k in ('c','g','s')):
        raise RuntimeError('Unexpected dashboard data structure; refusing to modify')
    dates = [START + dt.timedelta(days=i) for i in range((end-START).days+1)]
    btc = coinbase_opens(end)
    if not btc:
        raise RuntimeError('Coinbase returned no candles; preserving existing dashboard')
    fg = fear_greed()
    new = dict(old)
    # Backfill historical gaps on every run, while preserving previously verified
    # observations if a later API response omits them. Never invent values.
    previous_dates = [START + dt.timedelta(days=i) for i in range(len(old['c']))]
    previous_btc = dict(zip(previous_dates, old['c']))
    previous_fg = dict(zip(previous_dates, old['g']))
    new['c'] = [btc.get(d, previous_btc.get(d)) for d in dates]
    new['g'] = [fg.get(d, previous_fg.get(d)) for d in dates]
    unresolved = [(str(d), 'Coinbase' if new['c'][i] is None else '',
                   'Alternative.me' if new['g'][i] is None else '')
                  for i, d in enumerate(dates)
                  if new['c'][i] is None or new['g'][i] is None]
    if unresolved:
        print(f'Unresolved historical gaps: {len(unresolved)}; recent examples: {unresolved[-7:]}')
    # Signal logic in the HTML calculates signals from these arrays.
    new['s'] = []
    new['thr'] = []
    new['rej'] = []
    replacement = 'const D='+json.dumps(new,separators=(',',':'),allow_nan=False)+';'
    updated = original[:match.start()]+replacement+original[match.end():]
    # Keep the latest complete day's display when today's data is not yet available.
    waiting = new['c'][-1] is None or new['g'][-1] is None
    updated = re.sub(r'(<script id="btc-update-status" type="application/json">).*?(</script>)',
        lambda m: m.group(1) + json.dumps({'waiting':waiting},separators=(',',':')) + m.group(2), updated, count=1)
    updated = re.sub(r'<div class="sub">Updated through .*?</div>',
        f'<div class="sub">Updated through {end.isoformat()} (UTC daily opens). BTC/USD: Coinbase Exchange 1-day candles (UTC), open. Fear &amp; Greed: Alternative.me dated historical API. Missing sentiment dates remain unavailable. Signals recalculated in the browser from these inputs.</div>',updated,count=1)
    marker = '(function recalculateSignals(){'
    if original.count(marker) != 1 or updated.count(marker) != 1:
        raise RuntimeError('Signal calculation block not found uniquely')
    if original.split(marker,1)[1].split('})();',1)[0] != updated.split(marker,1)[1].split('})();',1)[0]:
        raise RuntimeError('Signal calculation code changed unexpectedly')
    if updated != original:
        HTML.write_text(updated,encoding='utf-8')
        print(f'Updated/backfilled {len(dates)} dated observations through {end}; unresolved gaps: {len(unresolved)}')
    else:
        print('Dashboard already up to date')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # A retrieval failure must not overwrite verified prices or signals.
        # Persist a small status marker so the published site can show the warning.
        print(f'Data retrieval failed; preserving verified observations: {exc}')
        if HTML.exists():
            old_html = HTML.read_text(encoding='utf-8')
            marker = r'(<script id="btc-update-status" type="application/json">).*?(</script>)'
            if re.search(marker,old_html):
                updated = re.sub(marker,lambda m:m.group(1)+'{"waiting":true}'+m.group(2),old_html,count=1)
                if updated != old_html: HTML.write_text(updated,encoding='utf-8')
            else:
                raise
        else:
            raise
