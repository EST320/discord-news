"""Weekly IPO calendar: a table image of the expected US listings from today
through the end of next week.

Usage:
    python -m market_pulse.ipo_calendar

Set DRY_RUN=true to fetch and render without posting.
"""

import os
from datetime import datetime, timezone

import requests
import plotly.graph_objects as go

from market_pulse.discord import post_webhook
from market_pulse.earnings_calendar import get_next_week_range

FINNHUB_KEY_ENV = "FINNHUB_API_KEY"
WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_IPO"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/ipo"
OUTPUT_FILE = "ipo_calendar.png"

# Finnhub also lists deals that were only filed (no date commitment) or pulled.
SHOWN_STATUSES = {"expected", "priced"}
MAX_ROWS = 25

COLUMNS = ("Date", "Ticker", "Company", "Exchange", "Price", "Shares", "Deal Size")
COLUMN_WIDTHS = (95, 80, 330, 190, 115, 95, 105)


def fetch_ipos(start, end):
    response = requests.get(
        FINNHUB_URL,
        params={"from": start.isoformat(), "to": end.isoformat(), "token": os.environ[FINNHUB_KEY_ENV]},
        timeout=30,
    )
    response.raise_for_status()
    entries = response.json().get("ipoCalendar", [])
    return entries if isinstance(entries, list) else []


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
    """Finnhub sends a single price or a 'low-high' range, as text."""
    text = str(value or "").strip()
    if not text:
        return "-"
    parts = [f"{to_number(part):.2f}" for part in text.split("-") if part.strip()]
    # A single dollar sign per cell: Plotly renders text between two as LaTeX.
    return "$" + " - ".join(parts) if parts else "-"


def listing_window(today=None):
    """Today through next week's Friday.

    IPO dates are usually fixed only a week or so ahead, so a window limited
    to next week is often still empty; the rest of this week is included too.
    """
    today = today or datetime.now(timezone.utc).date()
    _, next_friday = get_next_week_range(today)
    return today, next_friday


def select_listings(entries, start, end):
    """Keep the window's expected or priced deals, by date then largest first."""
    listings = []
    for entry in entries:
        if str(entry.get("status") or "").lower() not in SHOWN_STATUSES:
            continue
        try:
            date = datetime.strptime(entry.get("date") or "", "%Y-%m-%d").date()
        except ValueError:
            continue
        if not start <= date <= end:
            continue

        name = str(entry.get("name") or "").strip()
        symbol = str(entry.get("symbol") or "").strip()
        if not name and not symbol:
            continue

        listings.append({
            "date": date,
            "symbol": symbol,
            "name": name.title() if name.isupper() else name,
            "exchange": str(entry.get("exchange") or "").strip(),
            "price": entry.get("price"),
            "shares": to_number(entry.get("numberOfShares")),
            "deal_size": to_number(entry.get("totalSharesValue")),
        })

    listings.sort(key=lambda item: (item["date"], -item["deal_size"], item["symbol"]))
    return listings


def build_rows(listings):
    """Table rows as text; the date is only printed on the first listing of each day."""
    rows = []
    previous_date = None
    for item in listings:
        rows.append((
            f"<b>{item['date'].strftime('%a %b %d')}</b>" if item["date"] != previous_date else "",
            f"<b>${item['symbol']}</b>" if item["symbol"] else "-",
            item["name"] or "-",
            item["exchange"] or "-",
            format_price(item["price"]),
            format_amount(item["shares"]),
            format_amount(item["deal_size"], "$"),
        ))
        previous_date = item["date"]
    return rows


def build_chart(listings):
    shown = listings[:MAX_ROWS]
    rows = build_rows(shown)

    fig = go.Figure(data=[go.Table(
        columnwidth=list(COLUMN_WIDTHS),
        header=dict(
            values=[f"<b>{name}</b>" for name in COLUMNS],
            fill_color="#1f2430",
            font=dict(color="white", size=15, family="Arial"),
            align="left",
            height=38,
        ),
        cells=dict(
            values=[list(column) for column in zip(*rows)],
            fill_color="#2a2f3a",
            font=dict(color="#E8E8E8", size=13, family="Arial"),
            align="left",
            height=34,
            line_color="#3a3f4a",
        ),
    )])

    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0),
        width=sum(COLUMN_WIDTHS),
        height=len(rows) * 34 + 38,
    )

    fig.write_image(OUTPUT_FILE)


def main():
    # Fail fast on missing configuration instead of after the Finnhub call.
    os.environ[FINNHUB_KEY_ENV]
    if not DRY_RUN:
        os.environ[WEBHOOK_ENV]

    start, end = listing_window()
    entries = fetch_ipos(start, end)
    listings = select_listings(entries, start, end)
    title = f"IPO Calendar · {start.strftime('%b %d')} - {end.strftime('%b %d')}"

    if not listings:
        print(f"No expected IPOs for {start} to {end} ({len(entries)} calendar entries).")
        if not DRY_RUN:
            # Say so in the channel: silence would look the same as a failed run.
            post_webhook(os.environ[WEBHOOK_ENV], {
                "embeds": [{"title": title, "description": "No IPOs are scheduled in this period yet.", "color": 5793266}],
                "allowed_mentions": {"parse": []},
            })
        return

    build_chart(listings)
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
