import os
import time
from datetime import datetime, timedelta, timezone

import requests
import plotly.graph_objects as go

from market_pulse.discord import post_webhook

FINNHUB_KEY_ENV = "FINNHUB_API_KEY"
WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_EARNINGS"
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
OUTPUT_FILE = "earnings_calendar.png"

DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri")
TIME_ORDER = {"bmo": 0, "amc": 1, "": 2}
ICON_MAP = {"bmo": "☀️", "amc": "🌙"}
MIN_MARKET_CAP = 10_000_000_000
MAX_COMPANIES_PER_DAY = 15
PROFILE_REQUEST_DELAY = 1.1


def get_next_week_range(today=None):
    """
    Return next week's Monday and Friday.

    The workflow runs on Fridays, when Monday-Friday of this week is already
    the "current week". Adding 7 days to this week's Monday pins the range to
    next week regardless of the weekday or time the script actually runs.
    """
    today = today or datetime.now(timezone.utc).date()
    this_monday = today - timedelta(days=today.weekday())
    next_monday = this_monday + timedelta(days=7)
    next_friday = next_monday + timedelta(days=4)
    return next_monday, next_friday


def fetch_earnings(start, end):
    response = requests.get(
        FINNHUB_URL,
        params={"from": start.isoformat(), "to": end.isoformat(), "token": os.environ[FINNHUB_KEY_ENV]},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("earningsCalendar", [])


def fetch_profile(symbol, cache):
    """
    Fetch a company's name and market cap, caching by symbol.

    A company should appear only once in a given earnings calendar, but the
    cache guards against the API returning duplicate entries.
    """
    if symbol in cache:
        return cache[symbol]

    response = requests.get(PROFILE_URL, params={"symbol": symbol, "token": os.environ[FINNHUB_KEY_ENV]}, timeout=30)
    time.sleep(PROFILE_REQUEST_DELAY)

    profile = {"name": symbol, "market_cap": 0}
    if response.status_code == 200:
        data = response.json()
        profile = {
            "name": data.get("name") or symbol,
            "market_cap": (data.get("marketCapitalization") or 0) * 1_000_000,
        }

    cache[symbol] = profile
    return profile


def group_by_day(entries, monday):
    """
    Group entries by weekday in a single pass. Entries outside the week or
    without a symbol are skipped before any profile request is made.
    """
    grouped = {day: [] for day in DAY_LABELS}
    profile_cache = {}

    for entry in entries:
        date_str = entry.get("date")
        symbol = entry.get("symbol")
        hour = entry.get("hour", "")

        if not date_str or not symbol:
            continue

        offset = (datetime.strptime(date_str, "%Y-%m-%d").date() - monday).days
        if not 0 <= offset <= 4:
            continue

        profile = fetch_profile(symbol, profile_cache)
        if profile["market_cap"] < MIN_MARKET_CAP:
            continue

        grouped[DAY_LABELS[offset]].append({
            "ticker": f"${symbol}",
            "name": profile["name"],
            "hour": hour,
            "market_cap": profile["market_cap"],
        })

    for day_items in grouped.values():
        day_items.sort(key=lambda x: (TIME_ORDER.get(x["hour"], 2), -x["market_cap"]))
        del day_items[MAX_COMPANIES_PER_DAY:]

    return grouped


def format_cell(item):
    if not item:
        return ""
    icon = ICON_MAP.get(item["hour"], "")
    label = f"<b>{item['ticker']}</b>"
    if icon:
        label += f" {icon}"
    return f"{label}<br>{item['name']}"


def build_chart(grouped, monday):
    max_rows = max((len(v) for v in grouped.values()), default=0)
    if max_rows == 0:
        return False

    header_vals = [
        f"<b>{day} {(monday + timedelta(days=i)).strftime('%b %d')}</b>"
        for i, day in enumerate(DAY_LABELS)
    ]

    cell_vals = [
        [format_cell(item) for item in day_items] + [""] * (max_rows - len(day_items))
        for day_items in grouped.values()
    ]

    fig = go.Figure(data=[go.Table(
        columnwidth=[150] * len(DAY_LABELS),
        header=dict(
            values=header_vals,
            fill_color="#1f2430",
            font=dict(color="white", size=15, family="Arial"),
            align="left",
            height=38,
        ),
        cells=dict(
            values=cell_vals,
            fill_color="#2a2f3a",
            font=dict(color="#E8E8E8", size=13, family="Arial"),
            align="left",
            height=56,
            line_color="#3a3f4a",
        ),
    )])

    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0),
        width=1050,
        height=max_rows * 56 + 38,
    )

    fig.write_image(OUTPUT_FILE)
    return True


def post_to_discord():
    with open(OUTPUT_FILE, "rb") as f:
        post_webhook(os.environ[WEBHOOK_ENV], file=(OUTPUT_FILE, f.read()))


def main():
    # Fail fast on missing configuration instead of after the Finnhub calls.
    for name in (FINNHUB_KEY_ENV, WEBHOOK_ENV):
        os.environ[name]

    monday, friday = get_next_week_range()
    entries = fetch_earnings(monday, friday)

    if not entries:
        print(f"No earnings data for {monday} to {friday}.")
        return

    grouped = group_by_day(entries, monday)

    if not build_chart(grouped, monday):
        print("No companies above the market-cap threshold after filtering.")
        return

    post_to_discord()
    total = sum(len(v) for v in grouped.values())
    print(f"Posted earnings calendar for {monday} to {friday}: {total} companies.")


if __name__ == "__main__":
    main()
