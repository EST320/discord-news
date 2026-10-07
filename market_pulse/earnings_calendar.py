"""Weekly earnings calendar: one image with next week's reports, a column per day.

Usage:
    python -m market_pulse.earnings_calendar

Set DRY_RUN=true to fetch and render without posting.
"""

import os
from datetime import datetime, timedelta, timezone

from market_pulse import nasdaq
from market_pulse.discord import post_webhook
from market_pulse.theme import (
    AMBER, GREEN, INDIGO, MUTED, RED, RULE, Fonts, box, canvas, label, save, text_width,
)

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_EARNINGS"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

OUTPUT_FILE = "earnings_calendar.png"

DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri")

# Which companies make the card. In a quiet week the floor decides; at the
# peak of earnings season, when 80+ companies above the floor can report on
# one day, the per-day limit keeps only the largest.
MIN_MARKET_CAP = 20_000_000_000
MAX_COMPANIES_PER_DAY = 12

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
MORE_HEIGHT = 0.40
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


# ============================================================
# Data
# ============================================================

def select_companies(companies):
    """The day's largest companies above the floor, and how many more were left out.

    Returns (shown, hidden): shown is at most MAX_COMPANIES_PER_DAY items,
    largest first; hidden counts the other companies that cleared the floor.
    """
    eligible = sorted(
        (c for c in companies if c["market_cap"] >= MIN_MARKET_CAP),
        key=lambda c: c["market_cap"],
        reverse=True,
    )
    shown = [{**c, "ticker": f"${c['symbol']}"} for c in eligible[:MAX_COMPANIES_PER_DAY]]
    return shown, len(eligible) - len(shown)


def load_week(monday):
    """Fetch Monday to Friday. Returns ({day: [company, ...]}, {day: hidden count})."""
    grouped, hidden = {}, {}
    for offset, day in enumerate(DAY_LABELS):
        companies = nasdaq.fetch_earnings(monday + timedelta(days=offset))
        grouped[day], hidden[day] = select_companies(companies)
    return grouped, hidden


# ============================================================
# Formatting
# ============================================================

def format_market_cap(value):
    """245_000_000_000 -> '$245B'; 1_200_000_000_000 -> '$1.2T'."""
    if value >= 1e12:
        return f"${value / 1e12:.1f}T"
    return f"${value / 1e9:.0f}B"


def format_eps(value):
    """5.94 -> '$5.94'; -0.12 -> '-$0.12'."""
    return f"-${abs(value):.2f}" if value < 0 else f"${value:.2f}"


def eps_trend(item):
    """+1, -1 or 0: is the consensus estimate above or below the same quarter last year?"""
    forecast, last_year = item.get("eps_forecast"), item.get("last_year_eps")
    if forecast is None or last_year is None or abs(forecast - last_year) < 0.005:
        return 0
    return 1 if forecast > last_year else -1


def split_sessions(day_items):
    """[(session key, items)] for the sessions that have reports, in display order."""
    sessions = []
    for key in SESSIONS:
        items = [item for item in day_items if (item["hour"] if item["hour"] in SESSIONS else "") == key]
        if items:
            sessions.append((key, items))
    return sessions


def day_content_height(day_items, hidden=0):
    sessions = split_sessions(day_items)
    height = sum(SESSION_HEIGHT + len(items) * ROW_HEIGHT for _, items in sessions) if sessions else ROW_HEIGHT
    return height + (MORE_HEIGHT if hidden else 0)


# ============================================================
# Chart
# ============================================================

def draw_company(ax, font, y, left, right, item):
    upper, lower = y + 0.17, y + 0.38

    # First line: ticker, and the consensus EPS with an arrow for its
    # direction against the same quarter last year.
    label(ax, left, upper, item["ticker"], font(12.5, bold=True))
    if item.get("eps_forecast") is not None:
        trend = eps_trend(item)
        arrow = {1: "▲", -1: "▼", 0: ""}[trend]
        arrow_font = font(8)
        eps_right = right - (text_width(arrow, arrow_font) + 0.05 if arrow else 0)
        if arrow:
            label(ax, right, upper + 0.005, arrow, arrow_font, GREEN if trend > 0 else RED, ha="right")
        label(ax, eps_right, upper, f"est {format_eps(item['eps_forecast'])}", font(10), MUTED, ha="right")

    # Second line: company name, and the market cap the day is sorted by.
    cap, cap_font = format_market_cap(item["market_cap"]), font(9.5)
    label(ax, right, lower, cap, cap_font, MUTED, ha="right")
    label(ax, left, lower, item["name"], font(9.5), MUTED,
          max_width=right - left - text_width(cap, cap_font) - 0.14)


def draw_day(ax, font, x, width, height, day, date, day_items, hidden):
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
            draw_company(ax, font, y, inner_left, inner_right, item)
            y += ROW_HEIGHT

    if hidden:
        label(ax, inner_left, y + MORE_HEIGHT / 2, f"+{hidden} more above {format_market_cap(MIN_MARKET_CAP)}",
              font(9.5), MUTED)


def draw_card(grouped, monday, hidden=None, out_path=OUTPUT_FILE):
    """Render the week. Returns False (and draws nothing) when no company made the cut."""
    if not any(grouped.values()):
        return False
    hidden = hidden or {}

    font = Fonts()
    content_height = max(day_content_height(grouped[day], hidden.get(day, 0)) for day in DAY_LABELS)
    column_height = DAY_HEADER_HEIGHT + content_height + COLUMN_PADDING
    fig, ax = canvas(CARD_WIDTH, TOP + column_height + FOOTER_HEIGHT)

    friday = monday + timedelta(days=4)
    label(ax, MARGIN, 0.40, "Earnings Calendar", font(13), MUTED)
    label(ax, CARD_WIDTH - MARGIN, 0.40, f"{monday.strftime('%b %d')} – {friday.strftime('%b %d, %Y')}",
          font(13), MUTED, ha="right")

    width = (CARD_WIDTH - 2 * MARGIN - COLUMN_GAP * (len(DAY_LABELS) - 1)) / len(DAY_LABELS)
    for i, day in enumerate(DAY_LABELS):
        draw_day(ax, font, MARGIN + i * (width + COLUMN_GAP), width, column_height,
                 day, monday + timedelta(days=i), grouped[day], hidden.get(day, 0))

    footer_y = TOP + column_height + FOOTER_HEIGHT / 2
    label(ax, MARGIN, footer_y, "est = consensus EPS · ▲▼ vs. the same quarter last year", font(10), MUTED)
    label(ax, CARD_WIDTH - MARGIN, footer_y,
          f"Top {MAX_COMPANIES_PER_DAY} per day above {format_market_cap(MIN_MARKET_CAP)} market cap · Data: Nasdaq",
          font(10), MUTED, ha="right")

    save(fig, out_path)
    return True


# ============================================================
# Entry point
# ============================================================

def post_to_discord():
    with open(OUTPUT_FILE, "rb") as f:
        post_webhook(os.environ[WEBHOOK_ENV], file=(OUTPUT_FILE, f.read()))


def main():
    if not DRY_RUN:
        os.environ[WEBHOOK_ENV]  # fail fast on missing configuration

    monday, friday = get_next_week_range()
    grouped, hidden = load_week(monday)

    if not draw_card(grouped, monday, hidden):
        print(f"No companies above the market-cap floor report between {monday} and {friday}.")
        return

    total = sum(len(v) for v in grouped.values())
    if DRY_RUN:
        print(f"[dry run] {total} companies for {monday} to {friday} -> {OUTPUT_FILE}")
        return

    post_to_discord()
    print(f"Posted earnings calendar for {monday} to {friday}: {total} companies.")


if __name__ == "__main__":
    main()
