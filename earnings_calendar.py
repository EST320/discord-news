import os
import time
from datetime import datetime, timedelta
from itertools import zip_longest

import requests
import plotly.graph_objects as go

FINNHUB_KEY = os.environ["FINNHUB_API_KEY"]
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_EARNINGS"]
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
OUTPUT_FILE = "earnings_calendar.png"

DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri")
TIME_ORDER = {"bmo": 0, "amc": 1, "": 2}
ICON_MAP = {"bmo": "☀️", "amc": "🌙"}
MIN_MARKET_CAP = 5_000_000_000
MAX_COMPANIES_PER_DAY = 25


def get_week_range():
    today = datetime.utcnow().date()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=4)


def fetch_earnings(start, end):
    response = requests.get(
        FINNHUB_URL,
        params={"from": start.isoformat(), "to": end.isoformat(), "token": FINNHUB_KEY},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("earningsCalendar", [])


def fetch_profile(symbol, cache):
    if symbol in cache:
        return cache[symbol]

    response = requests.get(PROFILE_URL, params={"symbol": symbol, "token": FINNHUB_KEY}, timeout=30)
    time.sleep(1.1)

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
    grouped = {day: [] for day in DAY_LABELS}
    profile_cache = {}

    for entry in entries:
        date_str, symbol, hour = entry.get("date"), entry.get("symbol"), entry.get("hour", "")
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
    return f"<b>{item['ticker']} {icon}</b>".strip() + f"<br>{item['name']}"


def build_chart(grouped, monday):
    columns = list(zip_longest(*grouped.values(), fillvalue=None))
    if not columns:
        return False

    header_vals = [f"{day} {(monday + timedelta(days=i)).strftime('%b %d')}" for i, day in enumerate(DAY_LABELS)]
    cell_vals = [[format_cell(item) for item in col] for col in zip(*columns)]

    fig = go.Figure(data=[go.Table(
        columnwidth=[150] * len(DAY_LABELS),
        header=dict(
            values=[f"<b>{d}</b>" for d in header_vals],
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
        height=len(columns) * 56 + 38,
    )

    fig.write_image(OUTPUT_FILE)
    return True


def post_to_discord():
    with open(OUTPUT_FILE, "rb") as f:
        response = requests.post(
            WEBHOOK_URL,
            data={"username": "华尔街见闻财报日历"},
            files={"file": (OUTPUT_FILE, f, "image/png")},
            timeout=30,
        )

    if response.status_code == 429:
        time.sleep(float(response.json().get("retry_after", 2)) + 1)
        return post_to_discord()

    response.raise_for_status()


def main():
    monday, friday = get_week_range()
    entries = fetch_earnings(monday, friday)

    if not entries:
        print("本周没有财报数据。")
        return

    grouped = group_by_day(entries, monday)

    if not build_chart(grouped, monday):
        print("筛选后没有市值达标的公司。")
        return

    post_to_discord()
    print(f"已发送 {monday} 至 {friday} 财报日历,共{sum(len(v) for v in grouped.values())}家公司。")


if __name__ == "__main__":
    main()
