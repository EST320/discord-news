import os
import time
from datetime import datetime, timedelta

import requests
import plotly.graph_objects as go

FINNHUB_KEY = os.environ["FINNHUB_API_KEY"]
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_EARNINGS"]
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
OUTPUT_FILE = "earnings_calendar.png"

DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def get_week_range():
    today = datetime.utcnow().date()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def fetch_earnings(start, end):
    params = {"from": start.isoformat(), "to": end.isoformat(), "token": FINNHUB_KEY}
    response = requests.get(FINNHUB_URL, params=params, timeout=30)
    response.raise_for_status()
    return response.json().get("earningsCalendar", [])


def fetch_company_name(symbol, cache):
    if symbol in cache:
        return cache[symbol]

    params = {"symbol": symbol, "token": FINNHUB_KEY}
    response = requests.get(PROFILE_URL, params=params, timeout=30)
    time.sleep(1.1)

    name = symbol
    if response.status_code == 200:
        name = response.json().get("name") or symbol

    cache[symbol] = name
    return name


def group_by_day(entries, monday):
    grouped = {DAY_LABELS[i]: [] for i in range(5)}
    name_cache = {}

    for entry in entries:
        date_str = entry.get("date")
        symbol = entry.get("symbol")
        hour = entry.get("hour", "")

        if not date_str or not symbol:
            continue

        entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        offset = (entry_date - monday).days

        if offset < 0 or offset > 4:
            continue

        icon = "☀️" if hour == "bmo" else "🌙" if hour == "amc" else ""
        name = fetch_company_name(symbol, name_cache)

        grouped[DAY_LABELS[offset]].append({
            "ticker": f"${symbol}",
            "name": name,
            "icon": icon,
        })

    return grouped


def build_chart(grouped, monday):
    max_rows = max((len(v) for v in grouped.values()), default=0)
    if max_rows == 0:
        return False

    header_vals = [f"{day} {(monday + timedelta(days=i)).strftime('%b %d')}" for i, day in enumerate(DAY_LABELS)]

    def format_cell(item):
        if not item:
            return ""
        return f"<b>{item['ticker']} {item['icon']}</b><br>{item['name']}"

    cell_vals = []
    for i in range(max_rows):
        row = []
        for day in DAY_LABELS:
            items = grouped[day]
            row.append(format_cell(items[i]) if i < len(items) else "")
        cell_vals.append(row)

    cols = list(zip(*cell_vals))

    fig = go.Figure(data=[go.Table(
        columnwidth=[100] * len(header_vals),
        header=dict(
            values=[f"<b>{d}</b>" for d in header_vals],
            fill_color="#1f2430",
            font=dict(color="white", size=15, family="Arial"),
            align="left",
            height=38,
        ),
        cells=dict(
            values=cols,
            fill_color="#2a2f3a",
            font=dict(color="#E8E8E8", size=13, family="Arial"),
            align="left",
            height=56,
            line_color="#3a3f4a",
        ),
    )])

    fig.update_layout(
        title={"text": f"Weekly US Earnings Calendar (Week of {monday.strftime('%b %d, %Y')})<br><span style='font-size: 16px; font-weight: normal;'>☀️ Before Market Open · 🌙 After Market Close</span>"},
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
        retry_after = response.json().get("retry_after", 2)
        time.sleep(float(retry_after) + 1)
        return post_to_discord()

    response.raise_for_status()


def main():
    monday, friday = get_week_range()
    entries = fetch_earnings(monday, friday)

    if not entries:
        print("本周没有财报数据。")
        return

    grouped = group_by_day(entries, monday)
    has_data = build_chart(grouped, monday)

    if not has_data:
        print("分组后没有可显示的条目。")
        return

    post_to_discord()
    print(f"已发送 {monday} 至 {friday} 财报日历。")


if __name__ == "__main__":
    main()
