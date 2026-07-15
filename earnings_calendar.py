import os
import time
from datetime import datetime, timedelta
from itertools import zip_longest

import requests
import plotly.graph_objects as go

FMP_KEY = os.environ["FMP_API_KEY"]
EODHD_KEY = os.environ["EODHD_API_KEY"]
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_EARNINGS"]

FMP_CALENDAR_URL = "https://financialmodelingprep.com/stable/earnings-calendar"
FMP_PROFILE_URL = "https://financialmodelingprep.com/stable/profile"
EODHD_URL = "https://eodhd.com/api/calendar/earnings"
OUTPUT_FILE = "earnings_calendar.png"

DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri")
TIME_ORDER = {"bmo": 0, "amc": 1, "": 2}
ICON_MAP = {"bmo": "☀️", "amc": "🌙"}
MAX_PAGES = 20
MIN_MARKET_CAP = 5_000_000_000
MAX_COMPANIES_PER_DAY = 20


def get_week_range():
    today = datetime.utcnow().date()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=4)


def fetch_fmp_dates(start, end):
    entries = []
    for page in range(MAX_PAGES):
        response = requests.get(
            FMP_CALENDAR_URL,
            params={"from": start.isoformat(), "to": end.isoformat(), "page": page, "apikey": FMP_KEY},
            timeout=30,
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list) or not batch:
            break
        entries.extend(batch)
    return entries


def fetch_eodhd_timing(start, end):
    response = requests.get(
        EODHD_URL,
        params={"from": start.isoformat(), "to": end.isoformat(), "api_token": EODHD_KEY, "fmt": "json"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json().get("earnings", [])

    timing_map = {}
    for item in data:
        code = str(item.get("code", "")).split(".")[0]
        timing = str(item.get("before_after_market") or "").lower()
        if timing == "beforemarket":
            timing_map[code] = "bmo"
        elif timing == "aftermarket":
            timing_map[code] = "amc"
    return timing_map


def fetch_profile(symbol, cache):
    if symbol in cache:
        return cache[symbol]

    response = requests.get(FMP_PROFILE_URL, params={"symbol": symbol, "apikey": FMP_KEY}, timeout=30)
    time.sleep(0.3)

    profile = {"name": symbol, "market_cap": 0}
    if response.status_code == 200:
        data = response.json()
        if isinstance(data, list) and data:
            profile = {
                "name": data[0].get("companyName") or symbol,
                "market_cap": data[0].get("marketCap") or 0,
            }

    cache[symbol] = profile
    return profile


def group_by_day(fmp_entries, timing_map, monday):
    grouped = {day: [] for day in DAY_LABELS}
    profile_cache = {}
    seen_symbols = set()

    for entry in fmp_entries:
        date_str = entry.get("date")
        symbol = entry.get("symbol")

        if not date_str or not symbol or symbol in seen_symbols:
            continue

        try:
            entry_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue

        offset = (entry_date - monday).days
        if not 0 <= offset <= 4:
            continue

        profile = fetch_profile(symbol, profile_cache)
        if profile["market_cap"] < MIN_MARKET_CAP:
            continue

        seen_symbols.add(symbol)
        grouped[DAY_LABELS[offset]].append({
            "ticker": f"${symbol}",
            "name": profile["name"],
            "hour": timing_map.get(symbol, ""),
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
            data={"username": "财报鸡"},
            files={"file": (OUTPUT_FILE, f, "image/png")},
            timeout=30,
        )

    if response.status_code == 429:
        time.sleep(float(response.json().get("retry_after", 2)) + 1)
        return post_to_discord()

    response.raise_for_status()


def main():
    monday, friday = get_week_range()

    fmp_entries = fetch_fmp_dates(monday, friday)
    if not fmp_entries:
        print("本周没有财报数据。")
        return

    timing_map = fetch_eodhd_timing(monday, friday)
    grouped = group_by_day(fmp_entries, timing_map, monday)

    if not build_chart(grouped, monday):
        print("筛选后没有市值达标的公司。")
        return

    post_to_discord()
    print(f"已发送 {monday} 至 {friday} 财报日历,共{sum(len(v) for v in grouped.values())}家重点公司。")


if __name__ == "__main__":
    main()
