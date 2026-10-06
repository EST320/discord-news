"""Weekly earnings calendar: one image with next week's reports, a column per day.

Usage:
    python -m market_pulse.earnings_calendar

Set DRY_RUN=true to fetch and render without posting.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import requests

from market_pulse.discord import post_webhook
from market_pulse.theme import AMBER, INDIGO, MUTED, RULE, Fonts, box, canvas, label, save

FINNHUB_KEY_ENV = "FINNHUB_API_KEY"
WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_EARNINGS"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
OUTPUT_FILE = "earnings_calendar.png"

DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri")
TIME_ORDER = {"bmo": 0, "amc": 1, "": 2}
MIN_MARKET_CAP = 10_000_000_000
MAX_COMPANIES_PER_DAY = 15
PROFILE_REQUEST_DELAY = 1.1

# Reporting session -> (heading, accent colour), in display order.
SESSIONS = {
    "bmo": ("Before Open", AMBER),
    "amc": ("After Close", INDIGO),
    "": ("Time TBD", MUTED),
}

# Card layout, in inches.
CARD_WIDTH = 12.0
MARGIN = 0.36
COLUMN_GAP = 0.12
TOP = 0.74                 # where the day columns start
DAY_HEADER_HEIGHT = 0.66
SESSION_HEIGHT = 0.40
ROW_HEIGHT = 0.56
COLUMN_PADDING = 0.14
FOOTER_HEIGHT = 0.50


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


# ============================================================
# Chart
# ============================================================

def format_market_cap(value):
    """245_000_000_000 -> '$245B'; 1_200_000_000_000 -> '$1.2T'."""
    if value >= 1e12:
        return f"${value / 1e12:.1f}T"
    return f"${value / 1e9:.0f}B"


def split_sessions(day_items):
    """[(session key, items)] for the sessions that have reports, in display order."""
    sessions = []
    for key in SESSIONS:
        items = [item for item in day_items if (item["hour"] if item["hour"] in SESSIONS else "") == key]
        if items:
            sessions.append((key, items))
    return sessions


def day_content_height(day_items):
    sessions = split_sessions(day_items)
    if not sessions:
        return ROW_HEIGHT
    return sum(SESSION_HEIGHT + len(items) * ROW_HEIGHT for _, items in sessions)


def draw_day(ax, font, x, width, height, day, date, day_items):
    box(ax, x, TOP, width, height)
    inner_left, inner_right = x + 0.16, x + width - 0.16

    label(ax, inner_left, TOP + 0.33, day, font(15, bold=True))
    label(ax, inner_right, TOP + 0.33, date.strftime("%b %d"), font(11), MUTED, ha="right")
    ax.plot([inner_left, inner_right], [TOP + DAY_HEADER_HEIGHT - 0.04] * 2, color=RULE, linewidth=1)

    y = TOP + DAY_HEADER_HEIGHT
    sessions = split_sessions(day_items)
    if not sessions:
        label(ax, x + width / 2, y + ROW_HEIGHT / 2, "No reports", font(11), MUTED, ha="center")
        return

    for key, items in sessions:
        heading, accent = SESSIONS[key]
        box(ax, inner_left, y + 0.13, 0.05, 0.18, accent)
        label(ax, inner_left + 0.13, y + 0.22, heading, font(10, bold=True), accent)
        y += SESSION_HEIGHT

        for item in items:
            cap = format_market_cap(item["market_cap"])
            label(ax, inner_left, y + 0.17, item["ticker"], font(12.5, bold=True))
            label(ax, inner_right, y + 0.17, cap, font(10), MUTED, ha="right")
            label(ax, inner_left, y + 0.38, item["name"], font(9.5), MUTED, max_width=inner_right - inner_left - 0.05)
            y += ROW_HEIGHT


def draw_card(grouped, monday, out_path=OUTPUT_FILE):
    """Render the week. Returns False (and draws nothing) when no company made the cut."""
    if not any(grouped.values()):
        return False

    font = Fonts()
    content_height = max(day_content_height(items) for items in grouped.values())
    column_height = DAY_HEADER_HEIGHT + content_height + COLUMN_PADDING
    fig, ax = canvas(CARD_WIDTH, TOP + column_height + FOOTER_HEIGHT)

    friday = monday + timedelta(days=4)
    label(ax, MARGIN, 0.40, "Earnings Calendar", font(13), MUTED)
    label(ax, CARD_WIDTH - MARGIN, 0.40, f"{monday.strftime('%b %d')} – {friday.strftime('%b %d, %Y')}",
          font(13), MUTED, ha="right")

    width = (CARD_WIDTH - 2 * MARGIN - COLUMN_GAP * (len(DAY_LABELS) - 1)) / len(DAY_LABELS)
    for i, day in enumerate(DAY_LABELS):
        draw_day(ax, font, MARGIN + i * (width + COLUMN_GAP), width, column_height,
                 day, monday + timedelta(days=i), grouped[day])

    footer = f"Market cap above {format_market_cap(MIN_MARKET_CAP)} · largest first · Data: Finnhub"
    label(ax, CARD_WIDTH - MARGIN, TOP + column_height + FOOTER_HEIGHT / 2, footer, font(10), MUTED, ha="right")

    save(fig, out_path)
    return True


# ============================================================
# Entry point
# ============================================================

def post_to_discord():
    with open(OUTPUT_FILE, "rb") as f:
        post_webhook(os.environ[WEBHOOK_ENV], file=(OUTPUT_FILE, f.read()))


def main():
    # Fail fast on missing configuration instead of after the Finnhub calls.
    os.environ[FINNHUB_KEY_ENV]
    if not DRY_RUN:
        os.environ[WEBHOOK_ENV]

    monday, friday = get_next_week_range()
    entries = fetch_earnings(monday, friday)

    if not entries:
        print(f"No earnings data for {monday} to {friday}.")
        return

    grouped = group_by_day(entries, monday)

    if not draw_card(grouped, monday):
        print("No companies above the market-cap threshold after filtering.")
        return

    total = sum(len(v) for v in grouped.values())
    if DRY_RUN:
        print(f"[dry run] {total} companies for {monday} to {friday} -> {OUTPUT_FILE}")
        return

    post_to_discord()
    print(f"Posted earnings calendar for {monday} to {friday}: {total} companies.")


if __name__ == "__main__":
    main()
