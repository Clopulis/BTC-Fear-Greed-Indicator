
#!/usr/bin/env python3
"""Update BTC opening prices and Alternative.me F&G by UTC date."""

import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / "index.html"
UTC = dt.timezone.utc
START = dt.date(2018, 2, 1)


def fetch_json(url):
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "BTC-FG-Indicator/1.0",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=35) as response:
                return json.load(response)
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt + 1)


def coinbase_opens(end_date):
    out = {}
    current = START

    while current <= end_date:
        stop = min(
            current + dt.timedelta(days=200),
            end_date + dt.timedelta(days=1),
        )

        start_ts = dt.datetime.combine(
            current, dt.time(), UTC
        ).isoformat().replace("+00:00", "Z")

        end_ts = dt.datetime.combine(
            stop, dt.time(), UTC
        ).isoformat().replace("+00:00", "Z")

        query = urllib.parse.urlencode({
            "granularity": 86400,
            "start": start_ts,
            "end": end_ts,
        })

        url = (
            "https://api.exchange.coinbase.com/products/"
            "BTC-USD/candles?" + query
        )

        rows = fetch_json(url)

        if not isinstance(rows, list):
            raise RuntimeError(
                "Coinbase response was not a candle list"
            )

        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                raise RuntimeError("Malformed Coinbase candle")

            ts = int(row[0])
            opening = float(row[3])

            if ts % 86400:
                raise RuntimeError(
                    f"Non-UTC-midnight candle: {ts}"
                )

            date = dt.datetime.fromtimestamp(ts, UTC).date()

            if START <= date <= end_date:
                if not 0 < opening < float("inf"):
                    raise RuntimeError(
                        f"Invalid Coinbase open: {date}"
                    )

                if date in out and out[date] != opening:
                    raise RuntimeError(
                        f"Conflicting Coinbase candle: {date}"
                    )

                out[date] = opening

        current = stop
        time.sleep(0.18)

    return out


def fear_greed():
    data = fetch_json(
        "https://api.alternative.me/fng/"
        "?limit=0&format=json"
    )

    if (
        not isinstance(data, dict)
        or not isinstance(data.get("data"), list)
        or data.get("metadata", {}).get("error")
    ):
        raise RuntimeError(
            "Invalid Alternative.me response"
        )

    out = {}

    for item in data["data"]:
        timestamp = int(item["timestamp"])
        date = dt.datetime.fromtimestamp(
            timestamp, UTC
        ).date()
        value = int(item["value"])

        if not 0 <= value <= 100:
            raise RuntimeError(
                f"Invalid Fear & Greed: {date}"
            )

        if date in out and out[date] != value:
            raise RuntimeError(
                f"Conflicting Fear & Greed: {date}"
            )

        out[date] = value

    return out


def main():
    today = dt.datetime.now(UTC).date()

    if today < START:
        raise RuntimeError("Invalid UTC date")

    original = HTML.read_text(encoding="utf-8")

    match = re.search(
        r"(?m)^const D=(\{[^\n]*\});$",
        original,
    )

    if not match:
        raise RuntimeError(
            "Expected single-line const D=...; "
            "refusing to modify"
        )

    old = json.loads(match.group(1))

    if (
        old.get("start") != START.isoformat()
        or not all(k in old for k in ("c", "g", "s"))
    ):
        raise RuntimeError(
            "Unexpected dashboard data structure"
        )

    btc = coinbase_opens(today)
    fg = fear_greed()

    # Only publish through the latest date for which
    # both sources have a valid reading.
    end = today

    while end >= START and (
        end not in btc or end not in fg
    ):
        end -= dt.timedelta(days=1)

    if end < START:
        raise RuntimeError(
            "No matching BTC/F&G dates available"
        )

    if end < today:
        print(
            f"Today's data not ready for {today}. "
            f"Latest matched date: {end}."
        )

    dates = [
        START + dt.timedelta(days=i)
        for i in range((end - START).days + 1)
    ]

    missing_btc = [
        d.isoformat() for d in dates if d not in btc
    ]

    if missing_btc:
        raise RuntimeError(
            f"Coinbase history incomplete "
            f"({len(missing_btc)} missing), "
            f"first: {missing_btc[:10]}. "
            "Refusing to publish incomplete prices."
        )

    # If today's reading isn't ready, preserve the
    # existing dashboard instead of truncating it.
    old_length = len(old["c"])

    if end < today and old_length >= len(dates):
        print(
            "Waiting for both current-day readings. "
            "Keeping existing dashboard unchanged."
        )
        return

    new = dict(old)

    new["c"] = [btc[d] for d in dates]
    new["g"] = [fg.get(d) for d in dates]

    # The browser recalculates the existing signals.
    new["s"] = []
    new["thr"] = []
    new["rej"] = []

    replacement = (
        "const D="
        + json.dumps(
            new,
            separators=(",", ":"),
            allow_nan=False,
        )
        + ";"
    )

    updated = (
        original[:match.start()]
        + replacement
        + original[match.end():]
    )

    updated = re.sub(
        r'<div class="sub">Updated through .*?</div>',
        (
            f'<div class="sub">Updated through '
            f'{end.isoformat()} (UTC dates). '
            'BTC/USD: Coinbase Exchange 1-day '
            'candles (UTC), open. '
            'Fear &amp; Greed: Alternative.me '
            'dated historical API. '
            'Missing historical sentiment dates '
            'are shown as unavailable. '
            'Signals recalculated in the browser '
            'from these inputs.</div>'
        ),
        updated,
        count=1,
    )

    marker = "(function recalculateSignals(){"

    if (
        original.count(marker) != 1
        or updated.count(marker) != 1
    ):
        raise RuntimeError(
            "Signal calculation block not found uniquely"
        )

    old_signal_code = original.split(
        marker, 1
    )[1].split("})();", 1)[0]

    new_signal_code = updated.split(
        marker, 1
    )[1].split("})();", 1)[0]

    if old_signal_code != new_signal_code:
        raise RuntimeError(
            "Signal calculation code changed unexpectedly"
        )

    if updated == original:
        print("Dashboard already up to date.")
        return

    HTML.write_text(updated, encoding="utf-8")

    print(
        f"Updated {len(dates)} Coinbase UTC daily opens, "
        f"{sum(d in fg for d in dates)} Alternative.me "
        f"readings, through {end}"
    )


if __name__ == "__main__":
    main()
    
