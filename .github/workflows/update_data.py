
import os
import json
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

FILE = Path("index.html")
CMC_URL = "https://pro-api.coinmarketcap.com/v3/cryptocurrency/quotes/historical"
FG_URL = "https://api.alternative.me/fng/"
UTC = timezone.utc


def fetch(url, params, headers=None):
    url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers=headers or {"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.load(response)


def parse_time(value):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(UTC)


def main():
    key = os.environ.get("CMC_API_KEY")
    if not key:
        raise RuntimeError("CMC_API_KEY secret is missing")

    html = FILE.read_text(encoding="utf-8")

    match = re.search(
        r"const D=(\{[^\n]*\});",
        html
    )
    if not match:
        raise RuntimeError("Dashboard data object not found")

    data = json.loads(match.group(1))
    closes = data["c"]
    fear = data["g"]

    if len(closes) != len(fear):
        raise RuntimeError("BTC and F&G arrays differ")

    start = datetime.strptime(
        data["start"], "%Y-%m-%d"
    ).date()

    last = start + timedelta(days=len(closes) - 1)
    today = datetime.now(UTC).date()
    final_day = today - timedelta(days=1)

    if last >= final_day:
        print("Already up to date")
        return

    first = last + timedelta(days=1)

    if (final_day - first).days > 25:
        raise RuntimeError(
            "More than 26 days missing; manual review required"
        )

    # CoinMarketCap end-of-day quote, 23:59 UTC.
    params = {
        "id": "1",
        "convert": "USD",
        "interval": "24h",
        "time_start": first.isoformat() + "T23:59:00Z",
        "time_end": final_day.isoformat() + "T23:59:59Z",
    }

    cmc = fetch(
        CMC_URL,
        params,
        {
            "Accept": "application/json",
            "X-CMC_PRO_API_KEY": key,
        },
    )

    status = cmc.get("status", {})
    if status.get("error_code", 0) != 0:
        raise RuntimeError(
            "CMC API error: " +
            str(status.get("error_message"))
        )

    print("CMC response data type:", type(cmc.get("data")).__name__)
    print("CMC response data keys:", list(cmc.get("data", {}).keys()) if isinstance(cmc.get("data"), dict) else "not a dictionary")
    
    quotes = cmc.get("data", {}).get("1", {}).get(
        "quotes", []
    )

    print("CMC quotes returned:", len(quotes))
    for item in quotes[-5:]:
        print("CMC timestamp:", item.get("timestamp"))
        
    prices = {}

    for item in quotes:
        stamp = parse_time(item["timestamp"])

        # Associate a quote with its nearest
        # 23:59 UTC daily boundary.
        day = stamp.date()
        target = datetime.combine(
            day,
            datetime.min.time(),
            tzinfo=UTC
        ).replace(hour=23, minute=59)

        if abs((stamp - target).total_seconds()) > 1800:
            previous = target - timedelta(days=1)
            if abs((stamp - previous).total_seconds()) > 1800:
                continue
            target = previous
            day = target.date()

        price = float(item["quote"]["USD"]["price"])
        if price <= 0:
            raise RuntimeError("Invalid BTC price")

        if first <= day <= final_day:
            prices[day] = round(price, 2)

    # Official dated Fear & Greed history.
    fg_response = fetch(
        FG_URL,
        {"limit": "35", "format": "json"}
    )

    fg_values = {}
    for item in fg_response["data"]:
        day = datetime.fromtimestamp(
            int(item["timestamp"]), UTC
        ).date()
        fg_values[day] = int(item["value"])

    added = 0
    day = first

    while day <= final_day:
        
        if day not in prices or day not in fg_values:
            print(f"CMC BTC price available: {day in prices}")
            print(f"Alternative.me F&G available: {day in fg_values}")
            print(f"Waiting for matching BTC and F&G: {day}")
            break

        closes.append(prices[day])
        fear.append(fg_values[day])

        print(
            f"Added {day}: BTC ${prices[day]:,.2f}, "
            f"F&G {fg_values[day]}"
        )

        added += 1
        day += timedelta(days=1)

    if not added:
        print("No new complete matching days")
        return

    # Preserve all existing indicator logic.
    # The dashboard recalculates signals on load.
    data["c"] = closes
    data["g"] = fear

    updated = json.dumps(
        data, separators=(",", ":")
    )

    html = (
        html[:match.start(1)]
        + updated
        + html[match.end(1):]
    )

    FILE.write_text(html, encoding="utf-8")
    print(f"Saved {added} new daily observations")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Update failed: {error}", file=sys.stderr)
        sys.exit(1)
      
