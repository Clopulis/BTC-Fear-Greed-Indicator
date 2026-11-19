#!/usr/bin/env python3
"""Rebuild BTC/F&G embedded data using verified completed UTC Coinbase candles."""
import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / 'index.html'
UTC = dt.timezone.utc
START = dt.date(2018, 2, 1)
END = dt.datetime.now(UTC).date() - dt.timedelta(days=1)

def fetch_json(url):
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'BTC-FG-Indicator/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=35) as response:
                return json.load(response)
        except Exception:
            if attempt == 4: raise
            time.sleep(2 ** attempt + 1)

def coinbase_closes():
    out = {}
    current = START
    # Coinbase limits candle requests to 300 points. 200-day windows leave room.
    while current <= END:
        stop = min(current + dt.timedelta(days=200), END + dt.timedelta(days=1))
        start_ts = dt.datetime.combine(current, dt.time(), UTC).isoformat().replace('+00:00', 'Z')
        end_ts = dt.datetime.combine(stop, dt.time(), UTC).isoformat().replace('+00:00', 'Z')
        query = urllib.parse.urlencode({'granularity': 86400, 'start': start_ts, 'end': end_ts})
        url = 'https://api.exchange.coinbase.com/products/BTC-USD/candles?' + query
        rows = fetch_json(url)
        if not isinstance(rows, list): raise RuntimeError('Coinbase response was not candle list')
        for row in rows:
            if not isinstance(row, list) or len(row) < 5: raise RuntimeError('Malformed Coinbase candle')
            ts = int(row[0]); close = float(row[4])
            if ts % 86400: raise RuntimeError(f'Non-UTC-midnight candle: {ts}')
            date = dt.datetime.fromtimestamp(ts, UTC).date()
            if START <= date <= END and close > 0:
                if date in out and out[date] != close: raise RuntimeError(f'Conflicting Coinbase candle: {date}')
                out[date] = close
        current = stop
        time.sleep(0.18)
    return out

def fear_greed():
    data = fetch_json('https://api.alternative.me/fng/?limit=0&format=json')
    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
        raise RuntimeError('Invalid Alternative.me response')
    out = {}
    for item in data['data']:
        date = dt.datetime.fromtimestamp(int(item['timestamp']), UTC).date()
        value = int(item['value'])
        if not 0 <= value <= 100: raise RuntimeError(f'Invalid Fear & Greed: {date}')
        if date in out and out[date] != value: raise RuntimeError(f'Conflicting Fear & Greed: {date}')
        out[date] = value
    return out

def main():
    if END < START: raise RuntimeError('No completed UTC day')
    original = HTML.read_text(encoding='utf-8')
    match = re.search(r'(?m)^const D=(\{[^\n]*\});$', original)
    if not match: raise RuntimeError('Expected single-line const D=...; not found; refusing to modify')
    old = json.loads(match.group(1))
    if old.get('start') != START.isoformat() or not all(k in old for k in ('c','g','s')):
        raise RuntimeError('Unexpected dashboard data structure; refusing to modify')
    btc = coinbase_closes()
    fg = fear_greed()
    dates = [START + dt.timedelta(days=i) for i in range((END - START).days + 1)]
    missing_btc = [d.isoformat() for d in dates if d not in btc]
    if missing_btc:
        raise RuntimeError(f'Coinbase history incomplete ({len(missing_btc)} missing), first: {missing_btc[:10]}. Refusing to mix sources.')
    # Missing F&G observations remain null, never fabricated.
    new = dict(old)
    new['c'] = [btc[d] for d in dates]
    new['g'] = [fg.get(d) for d in dates]
    # The existing browser script recalculates D.s from D.c and D.g.
    new['s'] = []
    new['thr'] = []
    new['rej'] = []
    replacement = 'const D=' + json.dumps(new, separators=(',', ':'), allow_nan=False) + ';'
    updated = original[:match.start()] + replacement + original[match.end():]
    updated = re.sub(r'<div class="sub">Updated through .*?</div>',
        f'<div class="sub">Updated through {END.isoformat()} (completed UTC days). BTC/USD: Coinbase Exchange 1-day candles (UTC), close. Fear &amp; Greed: Alternative.me dated historical API. Missing sentiment dates are shown as unavailable. Signals recalculated in the browser from these inputs.</div>', updated, count=1)
    # Guard: chart calculation source is unchanged.
    if original[match.end():].split('(function recalculateSignals(){',1)[1].split('})();',1)[0] != updated[updated.index(replacement)+len(replacement):].split('(function recalculateSignals(){',1)[1].split('})();',1)[0]:
        raise RuntimeError('Signal calculation code changed unexpectedly')
    HTML.write_text(updated, encoding='utf-8')
    print(f'Updated {len(dates)} Coinbase UTC daily closes, {sum(d in fg for d in dates)} Alternative.me readings, through {END}')

if __name__ == '__main__': main()
