
"""Update BTC closing prices and Fear & Greed data.

Preserves historical observations and dashboard indicator rules.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
HTML = Path("index.html")

CMC_ENDPOINT = (
    "https://pro-api.coinmarketcap.com/v3/"
    "cryptocurrency/quotes/historical"
)

FG_ENDPOINT = "https://api.alternative.me/fng/"


def get_json(endpoint, params, headers=None):
    url = endpoint + "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            **(headers or {}),
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        message = exc.read(500).decode("utf-8", "replace")
        raise RuntimeError(
            f"HTTP {exc.code} from {endpoint}: {message}"
        ) from exc


def utc_datetime(value):
    return datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    ).astimezone(UTC)


def cmc_prices(first, last, api_key):
    start = datetime.combine(
        first, time(23, 59), UTC
    )

    end = datetime.combine(
        last + timedelta(days=1),
        time(1, 0),
        UTC,
    )

    end = min(end, datetime.now(UTC))

    payload = get_json(
        CMC_ENDPOINT,
        {
            "id": "1",
            "convert": "USD",
            "interval": "24h",
            "time_start": start.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "time_end": end.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "aux": "search_interval",
        },
        {"X-CMC_PRO_API_KEY": api_key},
    )

    status = payload.get("status") or {}

    if status.get("error_code") not in (0, None):
        raise RuntimeError(
            "CMC API error: "
            + str(status.get("error_message"))
        )

    data = payload.get("data")

    if not isinstance(data, dict):
        raise RuntimeError(
            "Unexpected CMC response: data is not an object"
        )

    btc = data.get("1")

    if not isinstance(btc, dict):
        raise RuntimeError(
            "Unexpected CMC response: BTC object missing"
        )

    snapshots = btc.get("quotes")

    if not isinstance(snapshots, list):
        raise RuntimeError(
            "Unexpected CMC response: quotes array missing"
        )

    print("CMC snapshots returned:", len(snapshots))

    if not snapshots:
        raise RuntimeError(
            "CMC returned zero BTC snapshots. "
            "Check API access, date range, and endpoint. "
            "Dashboard was not changed."
        )

    result = {}

    for item in snapshots:
        stamp = utc_datetime(item["timestamp"])

        label = item.get("search_interval")
        interval_stamp = (
            utc_datetime(label) if label else stamp
        )

        candidate_days = (
            interval_stamp.date(),
            interval_stamp.date() - timedelta(days=1),
        )

        closest = min(
            candidate_days,
            key=lambda d: abs(
                (
                    interval_stamp
                    - datetime.combine(
                        d, time(23, 59), UTC
                    )
                ).total_seconds()
            ),
        )

        difference = abs(
            (
                interval_stamp
                - datetime.combine(
                    closest, time(23, 59), UTC
                )
            ).total_seconds()
        )

        if difference > 3600:
            continue

        if not first <= closest <= last:
            continue

        usd = (
            (item.get("quote") or {}).get("USD") or {}
        )

        value = usd.get("price")

        if (
            not isinstance(value, (int, float))
            or value <= 0
        ):
            raise RuntimeError(
                f"Invalid CMC BTC price for {closest}"
            )

        if closest in result:
            raise RuntimeError(
                f"Duplicate CMC quote for {closest}"
            )

        result[closest] = round(float(value), 2)

    print(
        "CMC usable dates:",
        ", ".join(map(str, sorted(result))) or "(none)",
    )

    return result


def fg_prices(first, last):
    days = max(35, (last - first).days + 7)

    payload = get_json(
        FG_ENDPOINT,
        {
            "limit": str(days),
            "format": "json",
        },
    )

    records = payload.get("data")

    if not isinstance(records, list):
        raise RuntimeError(
            "Unexpected Alternative.me response"
        )

    result = {}

    for entry in records:
        day = datetime.fromtimestamp(
            int(entry["timestamp"]), UTC
        ).date()

        value = int(entry["value"])

        if not 0 <= value <= 100:
            raise RuntimeError(
                f"Invalid Fear & Greed value for {day}"
            )

        result[day] = value

    return result


def main():
    api_key = os.environ.get("CMC_API_KEY")

    if not api_key:
        raise RuntimeError(
            "CMC_API_KEY is missing from GitHub Secrets"
        )

    html = HTML.read_text(encoding="utf-8")

    match = re.search(
        r"const\s+D\s*=\s*(\{[^\n]*\})\s*;",
        html,
    )

    if not match:
        raise RuntimeError(
            "Dashboard data object not found. "
            "HTML was not changed."
        )

    data = json.loads(match.group(1))

    closes = data["c"]
    fear = data["g"]

    if (
        not isinstance(closes, list)
        or not isinstance(fear, list)
        or len(closes) != len(fear)
        or not closes
    ):
        raise RuntimeError(
            "Invalid dashboard data arrays"
        )

    first_record = date.fromisoformat(
        data["start"]
    )

    first = first_record + timedelta(
        days=len(closes)
    )

    last = datetime.now(UTC).date() - timedelta(
        days=1
    )

    if first > last:
        print("Already up to date")
        return

    if (last - first).days > 25:
        raise RuntimeError(
            "More than 26 days missing. "
            "Manual review required."
        )

    prices = cmc_prices(first, last, api_key)
    sentiments = fg_prices(first, last)

    added = 0
    day = first

    while day <= last:
        if day not in prices:
            raise RuntimeError(
                f"CMC BTC quote missing for {day}. "
                "Dashboard unchanged."
            )

        if day not in sentiments:
            raise RuntimeError(
                f"Alternative.me F&G missing for {day}. "
                "Dashboard unchanged."
            )

        closes.append(prices[day])
        fear.append(sentiments[day])

        print(
            f"Added {day}: "
            f"BTC ${prices[day]:,.2f}, "
            f"F&G {sentiments[day]}"
        )

        added += 1
        day += timedelta(days=1)

    data["c"] = closes
    data["g"] = fear

    replacement = json.dumps(
        data,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    new_html = (
        html[:match.start(1)]
        + replacement
        + html[match.end(1):]
    )

    HTML.write_text(
        new_html,
        encoding="utf-8",
    )

    print(
        f"Updated index.html: "
        f"{added} day(s) appended"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "UPDATE FAILED:",
            exc,
            file=sys.stderr,
        )
        sys.exit(1)
        
