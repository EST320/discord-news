"""Weekly earnings calendar: one image with next week's reports, a column per day.

Usage:
    python -m market_pulse.earnings_calendar

Set DRY_RUN=true to fetch and render without posting.
"""

import os
from datetime import datetime, timedelta, timezone

from market_pulse import nasdaq, yahoo
from market_pulse.discord import post_webhook
from market_pulse.theme import (
    AMBER, GREEN, INDIGO, MUTED, RED, RULE, Fonts, box, canvas, label, save, text_width, wrap,
)

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_EARNINGS"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

OUTPUT_FILE = "earnings_calendar.png"

DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri")

# Which companies make the card: the most actively traded, not the largest.
# Ranking by average daily dollar volume keeps closely followed mid caps
# (Coinbase, MicroStrategy) and drops large caps that barely trade in the US
# (Shell, TotalEnergies, AB InBev). The market-cap floor only keeps out small
# stocks having one wild month.
MIN_MARKET_CAP = 5_000_000_000
MAX_COMPANIES_PER_DAY = 12
DOLLAR_VOLUME_DAYS = 20
# In a quiet week fewer than 12 companies report on a day, so the per-day
# limit never bites; this floor is what keeps thinly traded names off then.
# Calibrated on real weeks: familiar names such as Fastenal, State Street and
# Interactive Brokers average $345M-$490M a day, while names like Rambus, F5
# and FTAI Aviation sit at $240M-$250M.
MIN_DOLLAR_VOLUME = 300_000_000
# The 20-day average costs one request per company, so each day is first cut
# down to this many candidates using a single session's volume.
SHORTLIST_PER_DAY = 30

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
ROW_HEIGHT = 0.56          # a company whose name fits on one line
NAME_LINE_HEIGHT = 0.19    # each further line of a wrapped name
COLUMN_INSET = 0.16
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

def eligible_companies(companies):
    return [c for c in companies if c["market_cap"] >= MIN_MARKET_CAP]


def shortlist(companies, one_day_volume):
    """The day's candidates for the 20-day lookup: the most traded in the latest session.

    Without any volume data (the screener request failed) it falls back to the largest.
    """
    ranked = sorted(
        eligible_companies(companies),
        key=lambda c: (one_day_volume.get(c["symbol"], 0.0), c["market_cap"]),
        reverse=True,
    )
    return ranked[:SHORTLIST_PER_DAY]


def select_companies(companies, average_volume, one_day_volume=None):
    """The day's most actively traded companies, and how many others cleared the cap floor.

    Returns (shown, hidden). shown holds at most MAX_COMPANIES_PER_DAY items,
    most traded first, each with its "dollar_volume". A company's 20-day
    average is used when known and the latest session's volume otherwise.
    Companies known to trade less than MIN_DOLLAR_VOLUME a day are left out.
    A company with no volume data at all is kept and ranked last by market
    cap, so that losing the volume sources degrades to a largest-first list
    instead of an empty one.
    """
    one_day_volume = one_day_volume or {}
    eligible = eligible_companies(companies)

    def activity(company):
        symbol = company["symbol"]
        return average_volume.get(symbol) or one_day_volume.get(symbol, 0.0)

    ranked = sorted(
        (c for c in eligible if not 0 < activity(c) < MIN_DOLLAR_VOLUME),
        key=lambda c: (activity(c), c["market_cap"]),
        reverse=True,
    )
    shown = [
        {**c, "ticker": f"${c['symbol']}", "dollar_volume": activity(c)}
        for c in ranked[:MAX_COMPANIES_PER_DAY]
    ]
    return shown, len(eligible) - len(shown)


def load_week(monday):
    """Fetch Monday to Friday. Returns ({day: [company, ...]}, {day: hidden count})."""
    try:
        one_day_volume = nasdaq.fetch_dollar_volumes()
    except Exception as exc:
        print(f"Volume screener unavailable, shortlisting by market cap instead: {exc!r}")
        one_day_volume = {}

    by_day = {
        day: nasdaq.fetch_earnings(monday + timedelta(days=offset))
        for offset, day in enumerate(DAY_LABELS)
    }
    candidates = {c["symbol"] for companies in by_day.values() for c in shortlist(companies, one_day_volume)}
    average_volume = yahoo.average_dollar_volumes(sorted(candidates), DOLLAR_VOLUME_DAYS)
    print(f"{DOLLAR_VOLUME_DAYS}-day dollar volume found for {len(average_volume)} of {len(candidates)} candidates")

    grouped, hidden = {}, {}
    for day, companies in by_day.items():
        candidates_today = shortlist(companies, one_day_volume)
        shown, _ = select_companies(candidates_today, average_volume, one_day_volume)
        grouped[day] = shown
        hidden[day] = len(eligible_companies(companies)) - len(shown)
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


def layout_name(name, cap, name_font, cap_font, width):
    """Wrap the company name to the column and place the market cap.

    Returns (lines, cap_inline). The full name is always shown, on as many
    lines as it needs. The cap sits at the right of the last line when there
    is room, and otherwise gets a line of its own.
    """
    # Measured widths run slightly under the rendered ones, so wrap a little early.
    lines = wrap(name, name_font, width - 0.08)
    room = width - text_width(lines[-1], name_font) - 0.14
    return lines, bool(text_width(cap, cap_font) <= room)


def row_height(item):
    lines, cap_inline = item.get("layout") or ([item["name"]], True)
    return ROW_HEIGHT + (len(lines) - 1 + (0 if cap_inline else 1)) * NAME_LINE_HEIGHT


def day_content_height(day_items, hidden=0):
    sessions = split_sessions(day_items)
    if sessions:
        height = sum(SESSION_HEIGHT + sum(row_height(item) for item in items) for _, items in sessions)
    else:
        height = ROW_HEIGHT
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

    # Below: the full company name, wrapped, with the market cap at the end of
    # its last line (or on its own line if it won't fit).
    lines, cap_inline = item.get("layout") or ([item["name"]], True)
    for i, line in enumerate(lines):
        label(ax, left, lower + i * NAME_LINE_HEIGHT, line, font(9.5), MUTED)
    cap_line = len(lines) - 1 if cap_inline else len(lines)
    label(ax, right, lower + cap_line * NAME_LINE_HEIGHT, format_market_cap(item["market_cap"]),
          font(9.5), MUTED, ha="right")


def draw_day(ax, font, x, width, height, day, date, day_items, hidden):
    box(ax, x, TOP, width, height)
    inner_left, inner_right = x + COLUMN_INSET, x + width - COLUMN_INSET

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
            y += row_height(item)

    if hidden:
        label(ax, inner_left, y + MORE_HEIGHT / 2, f"+{hidden} less traded above {format_market_cap(MIN_MARKET_CAP)}",
              font(9.5), MUTED)


def draw_card(grouped, monday, hidden=None, out_path=OUTPUT_FILE):
    """Render the week. Returns False (and draws nothing) when no company made the cut."""
    if not any(grouped.values()):
        return False
    hidden = hidden or {}

    font = Fonts()
    width = (CARD_WIDTH - 2 * MARGIN - COLUMN_GAP * (len(DAY_LABELS) - 1)) / len(DAY_LABELS)
    for day_items in grouped.values():
        for item in day_items:
            item["layout"] = layout_name(item["name"], format_market_cap(item["market_cap"]),
                                         font(9.5), font(9.5), width - 2 * COLUMN_INSET)
    content_height = max(day_content_height(grouped[day], hidden.get(day, 0)) for day in DAY_LABELS)
    column_height = DAY_HEADER_HEIGHT + content_height + COLUMN_PADDING
    fig, ax = canvas(CARD_WIDTH, TOP + column_height + FOOTER_HEIGHT)

    friday = monday + timedelta(days=4)
    label(ax, MARGIN, 0.40, "Earnings Calendar", font(13), MUTED)
    label(ax, CARD_WIDTH - MARGIN, 0.40, f"{monday.strftime('%b %d')} – {friday.strftime('%b %d, %Y')}",
          font(13), MUTED, ha="right")

    for i, day in enumerate(DAY_LABELS):
        draw_day(ax, font, MARGIN + i * (width + COLUMN_GAP), width, column_height,
                 day, monday + timedelta(days=i), grouped[day], hidden.get(day, 0))

    footer_y = TOP + column_height + FOOTER_HEIGHT / 2
    label(ax, MARGIN, footer_y, "est = consensus EPS · ▲▼ vs. the same quarter last year", font(10), MUTED)
    label(ax, CARD_WIDTH - MARGIN, footer_y,
          f"Most actively traded, up to {MAX_COMPANIES_PER_DAY} a day ({DOLLAR_VOLUME_DAYS}-day avg. dollar volume) · "
          "Data: Nasdaq, Yahoo Finance",
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
