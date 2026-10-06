"""Daily US market close summary: one image with indices, sectors and macro gauges.

Usage:
    python -m market_pulse.market_close

Set DRY_RUN=true to fetch and render without posting.
"""

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import requests

from market_pulse.discord import post_webhook

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_MARKET"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

# Yahoo Finance's chart endpoint: daily closes for many symbols in one request.
SPARK_URL = "https://query1.finance.yahoo.com/v8/finance/spark"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
FETCH_MAX_RETRIES = 3
FETCH_RETRY_BACKOFF_SECONDS = 5

OUTPUT_FILE = Path("market_close.png")

# A daily bar is stamped with its session open (13:30 UTC for US equities), so
# after the close today's bar is ~8 hours old and the previous session's is 32+.
MAX_BAR_AGE_SECONDS = 20 * 3600

INDICES = [
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq Composite"),
    ("^DJI", "Dow Jones"),
]

# Sectors are tracked through the Select Sector SPDR ETFs.
SECTORS = [
    ("XLK", "Technology"),
    ("XLC", "Communication"),
    ("XLY", "Consumer Discretionary"),
    ("XLF", "Financials"),
    ("XLI", "Industrials"),
    ("XLV", "Health Care"),
    ("XLP", "Consumer Staples"),
    ("XLE", "Energy"),
    ("XLB", "Materials"),
    ("XLU", "Utilities"),
    ("XLRE", "Real Estate"),
]

# (symbol, label, value format, kind). ^TNX quotes the yield itself in percent,
# so its move is shown in basis points rather than as a percentage of a percentage.
MACRO = [
    ("^VIX", "VIX", "{:.2f}", "pct"),
    ("^TNX", "US 10Y Yield", "{:.2f}%", "bps"),
    ("DX-Y.NYB", "Dollar Index", "{:.2f}", "pct"),
    ("GC=F", "Gold", "${:,.0f}", "pct"),
    ("CL=F", "WTI Crude", "${:.2f}", "pct"),
    ("BTC-USD", "Bitcoin", "${:,.0f}", "pct"),
]

ALL_SYMBOLS = [s for s, *_ in INDICES + SECTORS] + [s for s, *_ in MACRO]

BG = "#1f2430"
PANEL = "#2a2f3a"
TEXT = "#E8E8E8"
MUTED = "#8a8f98"
UP = "#3fb950"
DOWN = "#f85149"
FLAT = "#8a8f98"


# ============================================================
# Data
# ============================================================

def fetch_spark(symbols):
    params = {"symbols": ",".join(symbols), "range": "5d", "interval": "1d"}
    last_exc = None
    for attempt in range(1, FETCH_MAX_RETRIES + 1):
        try:
            response = requests.get(SPARK_URL, params=params, headers=HEADERS, timeout=30)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError(f"Unexpected quote data format: {type(payload)}")
            return payload
        except (requests.exceptions.RequestException, ValueError, RuntimeError) as exc:
            last_exc = exc
            if attempt < FETCH_MAX_RETRIES:
                wait = FETCH_RETRY_BACKOFF_SECONDS * attempt
                print(f"Quote fetch attempt {attempt} failed ({exc!r}), retrying in {wait}s")
                time.sleep(wait)
    raise last_exc


def parse_quote(entry):
    """Turn one symbol's daily series into its last close and the move from the close before."""
    if not isinstance(entry, dict):
        return None

    bars = [
        (ts, close)
        for ts, close in zip(entry.get("timestamp") or [], entry.get("close") or [])
        if isinstance(close, (int, float))
    ]
    if len(bars) < 2:
        return None

    (_, previous), (last_ts, last) = bars[-2], bars[-1]
    if not previous:
        return None

    return {
        "price": last,
        "change": last - previous,
        "change_pct": (last - previous) / previous * 100,
        "bar_ts": last_ts,
    }


def load_quotes():
    payload = fetch_spark(ALL_SYMBOLS)
    quotes = {symbol: parse_quote(payload.get(symbol)) for symbol in ALL_SYMBOLS}
    return {symbol: quote for symbol, quote in quotes.items() if quote}


def market_traded_today(quotes, now=None):
    """False on weekends and holidays, when the newest S&P 500 bar is a previous session's."""
    sp500 = quotes.get("^GSPC")
    if not sp500:
        return False
    now = time.time() if now is None else now
    return now - sp500["bar_ts"] < MAX_BAR_AGE_SECONDS


# ============================================================
# Formatting
# ============================================================

def change_color(value):
    if abs(value) < 0.005:
        return FLAT
    return UP if value > 0 else DOWN


def format_pct(value):
    return f"{value:+.2f}%"


def format_macro_change(quote, kind):
    if kind == "bps":
        return f"{quote['change'] * 100:+.1f} bps"
    return format_pct(quote["change_pct"])


def build_summary(quotes):
    parts = [
        f"{label} {format_pct(quotes[symbol]['change_pct'])}"
        for symbol, label in INDICES
        if symbol in quotes
    ]
    return " · ".join(parts)


# ============================================================
# Chart
# ============================================================

def panel(fig, rect):
    ax = fig.add_axes(rect)
    ax.set_facecolor(PANEL)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    return ax


def draw_index_tile(fig, rect, label, quote):
    ax = panel(fig, rect)
    ax.text(0.07, 0.78, label, fontsize=13, color=MUTED, ha="left", va="center")
    if not quote:
        ax.text(0.07, 0.38, "n/a", fontsize=24, color=MUTED, ha="left", va="center")
        return
    color = change_color(quote["change_pct"])
    ax.text(0.07, 0.46, f"{quote['price']:,.2f}", fontsize=25, color=TEXT,
            fontweight="bold", ha="left", va="center")
    ax.text(0.07, 0.16, f"{format_pct(quote['change_pct'])}   {quote['change']:+,.2f}",
            fontsize=14, color=color, fontweight="bold", ha="left", va="center")
    ax.add_patch(mpatches.Rectangle((0, 0), 0.012, 1, color=color))


def draw_sectors(fig, rect, quotes):
    ax = panel(fig, rect)
    ax.text(0.04, 0.945, "Sectors", fontsize=15, color=TEXT, fontweight="bold", ha="left", va="center")

    rows = sorted(
        ((label, quotes[symbol]["change_pct"]) for symbol, label in SECTORS if symbol in quotes),
        key=lambda row: row[1],
        reverse=True,
    )
    if not rows:
        ax.text(0.5, 0.5, "n/a", fontsize=20, color=MUTED, ha="center", va="center")
        return

    scale = max(max(abs(pct) for _, pct in rows), 0.5)
    row_height = 0.86 / len(SECTORS)
    zero_x, half_width = 0.62, 0.17

    ax.plot([zero_x, zero_x], [0.02, 0.89], color="#3a3f4a", linewidth=1)
    for i, (label, pct) in enumerate(rows):
        y = 0.87 - (i + 0.5) * row_height
        color = change_color(pct)
        width = pct / scale * half_width
        ax.add_patch(mpatches.Rectangle((zero_x, y - row_height * 0.28), width, row_height * 0.56, color=color))
        ax.text(0.04, y, label, fontsize=12, color=TEXT, ha="left", va="center")
        ax.text(0.97, y, format_pct(pct), fontsize=12, color=color, fontweight="bold", ha="right", va="center")


def draw_macro(fig, rect, quotes):
    ax = panel(fig, rect)
    ax.text(0.06, 0.945, "Rates, Volatility & Commodities", fontsize=15, color=TEXT,
            fontweight="bold", ha="left", va="center")

    row_height = 0.86 / len(MACRO)
    for i, (symbol, label, value_format, kind) in enumerate(MACRO):
        y = 0.87 - (i + 0.5) * row_height
        quote = quotes.get(symbol)
        ax.text(0.06, y, label, fontsize=13, color=MUTED, ha="left", va="center")
        if quote:
            ax.text(0.66, y, value_format.format(quote["price"]), fontsize=15, color=TEXT,
                    fontweight="bold", ha="right", va="center")
            ax.text(0.95, y, format_macro_change(quote, kind), fontsize=12.5,
                    color=change_color(quote["change"]), fontweight="bold", ha="right", va="center")
        else:
            ax.text(0.95, y, "n/a", fontsize=13, color=MUTED, ha="right", va="center")
        if i < len(MACRO) - 1:
            line_y = y - row_height / 2
            ax.plot([0.06, 0.95], [line_y, line_y], color="#3a3f4a", linewidth=1)


def draw_card(quotes, session_date, out_path=OUTPUT_FILE):
    fig = plt.figure(figsize=(12, 7.6))
    fig.patch.set_facecolor(BG)

    fig.text(0.035, 0.945, "US Market Close", fontsize=22, color=TEXT, fontweight="bold", ha="left", va="center")
    fig.text(0.965, 0.945, session_date.strftime("%a, %b %d, %Y"), fontsize=14, color=MUTED, ha="right", va="center")

    tile_width, gap = 0.30, 0.015
    for i, (symbol, label) in enumerate(INDICES):
        left = 0.035 + i * (tile_width + gap)
        draw_index_tile(fig, [left, 0.715, tile_width, 0.17], label, quotes.get(symbol))

    draw_sectors(fig, [0.035, 0.06, 0.52, 0.63], quotes)
    draw_macro(fig, [0.57, 0.06, 0.395, 0.63], quotes)

    fig.text(0.965, 0.025, "Change vs. previous close · Sectors: Select Sector SPDR ETFs · Data: Yahoo Finance",
             fontsize=9, color=MUTED, ha="right", va="center")

    plt.savefig(out_path, dpi=110, facecolor=BG)
    plt.close(fig)
    return out_path


# ============================================================
# Entry point
# ============================================================

def main():
    if not DRY_RUN:
        os.environ[WEBHOOK_ENV]  # fail fast on missing configuration

    quotes = load_quotes()
    missing = [symbol for symbol in ALL_SYMBOLS if symbol not in quotes]
    if missing:
        print(f"No data for: {', '.join(missing)}")
    if "^GSPC" not in quotes or len(missing) > len(ALL_SYMBOLS) // 2:
        raise RuntimeError("Too much quote data is missing, not posting a summary.")

    if not market_traded_today(quotes):
        print("The US market did not trade today, nothing to post.")
        if not DRY_RUN:
            return

    session_date = datetime.fromtimestamp(quotes["^GSPC"]["bar_ts"], tz=timezone.utc).date()
    chart_path = draw_card(quotes, session_date)
    summary = build_summary(quotes)

    if DRY_RUN:
        print(f"[dry run] {summary} -> {chart_path}")
        return

    sp500_pct = quotes["^GSPC"]["change_pct"]
    embed = {
        "title": "US Market Close",
        "description": summary,
        "color": 4176208 if sp500_pct >= 0 else 16273737,
        "image": {"url": f"attachment://{chart_path.name}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_webhook(
        os.environ[WEBHOOK_ENV],
        {"embeds": [embed], "allowed_mentions": {"parse": []}},
        file=(chart_path.name, chart_path.read_bytes()),
        timeout=60,
    )
    chart_path.unlink(missing_ok=True)
    print(f"Posted market close summary for {session_date}: {summary}")


if __name__ == "__main__":
    main()
