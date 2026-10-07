"""Weekly IPO calendar: one image listing the US deals expected to price from
today through the end of next week, grouped by day.

Usage:
    python -m market_pulse.ipo_calendar

Set DRY_RUN=true to fetch and render without posting.
"""

import os
from datetime import datetime, timezone

from market_pulse import nasdaq
from market_pulse.discord import post_webhook
from market_pulse.earnings_calendar import get_next_week_range
from market_pulse.theme import AMBER, MUTED, RULE, TEXT, Fonts, box, canvas, label, save, text_width, wrap

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_IPO"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

OUTPUT_FILE = "ipo_calendar.png"
MAX_ROWS = 25

# Card layout, in inches. Each listing is one row; the *_X values are the
# left edge (or right edge, for the right-aligned deal size) of each column.
CARD_WIDTH = 12.0
MARGIN = 0.36
TOP = 0.74                 # where the column captions sit
CAPTION_HEIGHT = 0.36
DAY_HEIGHT = 0.52
ROW_HEIGHT = 0.66          # a deal whose company name fits on one line
NAME_LINE_HEIGHT = 0.22    # each further line of a wrapped name
ROW_GAP = 0.07
FOOTER_HEIGHT = 0.50
TICKER_X = MARGIN + 0.22
COMPANY_X = MARGIN + 1.50
PRICE_X = 7.20
SHARES_X = 9.05
DEAL_RIGHT_X = CARD_WIDTH - MARGIN - 0.22
COMPANY_WIDTH = PRICE_X - COMPANY_X - 0.25


def to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def format_amount(value, prefix=""):
    """1234567 -> '1.2M'; zero or missing -> '-'."""
    value = to_number(value)
    if value <= 0:
        return "-"
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= threshold:
            return f"{prefix}{value / threshold:.1f}{suffix}"
    return f"{prefix}{value:.0f}"


def format_price(value):
    """The proposed price arrives as text: a single price or a 'low-high' range."""
    text = str(value or "").strip()
    if not text:
        return "-"
    parts = [f"{to_number(part):.2f}" for part in text.split("-") if part.strip()]
    return "$" + " - ".join(parts) if parts else "-"


def is_spac(name):
    """Blank-check companies are named '... Acquisition Corp' almost without exception."""
    return "acquisition" in name.lower()


def listing_window(today=None):
    """Today through next week's Friday.

    IPO dates are usually fixed only a week or so ahead, so a window limited
    to next week is often still empty; the rest of this week is included too.
    """
    today = today or datetime.now(timezone.utc).date()
    _, next_friday = get_next_week_range(today)
    return today, next_friday


def months_in(start, end):
    """The 'YYYY-MM' months the window touches; the calendar is served a month at a time."""
    months = [start.strftime("%Y-%m")]
    if end.strftime("%Y-%m") != months[0]:
        months.append(end.strftime("%Y-%m"))
    return months


def fetch_deals(start, end):
    deals = []
    for month in months_in(start, end):
        deals.extend(nasdaq.fetch_upcoming_ipos(month))
    return deals


def select_listings(deals, start, end):
    """Keep the window's deals, by date then largest first, without duplicates."""
    listings, seen = [], set()
    for deal in deals:
        if not start <= deal["date"] <= end:
            continue
        if not deal["name"] and not deal["symbol"]:
            continue
        key = (deal["date"], deal["symbol"], deal["name"])
        if key in seen:
            continue
        seen.add(key)

        name = deal["name"]
        listings.append({**deal, "name": name.title() if name.isupper() else name, "spac": is_spac(name)})

    listings.sort(key=lambda item: (item["date"], -item["deal_size"], item["symbol"]))
    return listings


def group_by_date(listings):
    """[(date, [listing, ...])] in the order given, which is already by date."""
    groups = []
    for item in listings:
        if groups and groups[-1][0] == item["date"]:
            groups[-1][1].append(item)
        else:
            groups.append((item["date"], [item]))
    return groups


def row_height(item):
    return ROW_HEIGHT + (len(item.get("name_lines") or [""]) - 1) * NAME_LINE_HEIGHT


def card_height(groups):
    rows = sum(row_height(item) + ROW_GAP for _, items in groups for item in items)
    return TOP + CAPTION_HEIGHT + len(groups) * DAY_HEIGHT + rows + FOOTER_HEIGHT


def draw_row(ax, font, y, item):
    height = row_height(item)
    box(ax, MARGIN, y, CARD_WIDTH - 2 * MARGIN, height)
    middle = y + height / 2

    ticker = f"${item['symbol']}" if item["symbol"] else "-"
    label(ax, TICKER_X, middle, ticker, font(13.5, bold=True), max_width=COMPANY_X - TICKER_X - 0.12)

    # The full company name, wrapped onto as many lines as it needs, then the exchange.
    lines = item.get("name_lines") or [item["name"] or "-"]
    for i, line in enumerate(lines):
        label(ax, COMPANY_X, y + 0.24 + i * NAME_LINE_HEIGHT, line, font(12))
    lower = y + 0.46 + (len(lines) - 1) * NAME_LINE_HEIGHT

    exchange, exchange_font = item["exchange"] or "-", font(9.5)
    label(ax, COMPANY_X, lower, exchange, exchange_font, MUTED, max_width=COMPANY_WIDTH)
    if item.get("spac"):
        label(ax, COMPANY_X + text_width(exchange, exchange_font) + 0.12, lower, "SPAC", font(8.5, bold=True), AMBER)

    label(ax, PRICE_X, middle, format_price(item["price"]), font(12))
    label(ax, SHARES_X, middle, format_amount(item["shares"]), font(12))
    label(ax, DEAL_RIGHT_X, middle, format_amount(item["deal_size"], "$"), font(13.5, bold=True), ha="right")


def draw_card(listings, start, end, out_path=OUTPUT_FILE):
    font = Fonts()
    shown = listings[:MAX_ROWS]
    for item in shown:
        item["name_lines"] = wrap(item["name"] or "-", font(12), COMPANY_WIDTH - 0.1)
    groups = group_by_date(shown)
    height = card_height(groups)
    fig, ax = canvas(CARD_WIDTH, height)

    label(ax, MARGIN, 0.40, "IPO Calendar", font(13), MUTED)
    label(ax, CARD_WIDTH - MARGIN, 0.40, f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}",
          font(13), MUTED, ha="right")

    caption_y = TOP + CAPTION_HEIGHT / 2
    for x, text, ha in ((TICKER_X, "TICKER", "left"), (COMPANY_X, "COMPANY · EXCHANGE", "left"),
                        (PRICE_X, "PRICE RANGE", "left"), (SHARES_X, "SHARES", "left"),
                        (DEAL_RIGHT_X, "DEAL SIZE", "right")):
        label(ax, x, caption_y, text, font(9, bold=True), MUTED, ha=ha)
    ax.plot([MARGIN, CARD_WIDTH - MARGIN], [TOP + CAPTION_HEIGHT] * 2, color=RULE, linewidth=1)

    y = TOP + CAPTION_HEIGHT
    for date, items in groups:
        label(ax, MARGIN, y + DAY_HEIGHT * 0.58, date.strftime("%a, %b %d"), font(14, bold=True), TEXT)
        count = f"{len(items)} deal" + ("s" if len(items) != 1 else "")
        label(ax, CARD_WIDTH - MARGIN, y + DAY_HEIGHT * 0.58, count, font(10.5), MUTED, ha="right")
        y += DAY_HEIGHT
        for item in items:
            draw_row(ax, font, y, item)
            y += row_height(item) + ROW_GAP

    footer_y = height - FOOTER_HEIGHT / 2
    label(ax, MARGIN, footer_y, "By expected pricing date · trading usually starts the next session", font(10), MUTED)
    label(ax, CARD_WIDTH - MARGIN, footer_y, "Largest first within each day · Data: Nasdaq", font(10), MUTED, ha="right")

    return save(fig, out_path)


def main():
    if not DRY_RUN:
        os.environ[WEBHOOK_ENV]  # fail fast on missing configuration

    start, end = listing_window()
    deals = fetch_deals(start, end)
    listings = select_listings(deals, start, end)
    title = f"IPO Calendar · {start.strftime('%b %d')} - {end.strftime('%b %d')}"

    if not listings:
        print(f"No expected IPOs for {start} to {end} ({len(deals)} upcoming deals this month).")
        if not DRY_RUN:
            # Say so in the channel: silence would look the same as a failed run.
            post_webhook(os.environ[WEBHOOK_ENV], {
                "embeds": [{"title": title, "description": "No IPOs are scheduled in this period yet.", "color": 5793266}],
                "allowed_mentions": {"parse": []},
            })
        return

    draw_card(listings, start, end)
    summary = f"{len(listings)} expected IPO(s) for {start} to {end}"

    if DRY_RUN:
        print(f"[dry run] {summary} -> {OUTPUT_FILE}")
        return

    embed = {
        "title": title,
        "color": 5793266,
        "image": {"url": f"attachment://{OUTPUT_FILE}"},
    }
    if len(listings) > MAX_ROWS:
        embed["description"] = f"Showing the first {MAX_ROWS} of {len(listings)} listings."

    with open(OUTPUT_FILE, "rb") as f:
        post_webhook(
            os.environ[WEBHOOK_ENV],
            {"embeds": [embed], "allowed_mentions": {"parse": []}},
            file=(OUTPUT_FILE, f.read()),
        )
    print(f"Posted {summary}.")


if __name__ == "__main__":
    main()
