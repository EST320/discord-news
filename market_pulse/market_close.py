"""US market summary for a Chinese-language channel: posted after each close,
and on demand as the reply to the /market slash command.

One image in Chinese: index tiles with intraday sparklines, a sector heat map
and macro tiles with 52-week ranges. The message text is just the session date.

Usage:
    python -m market_pulse.market_close

Set DRY_RUN=true to fetch and render without posting.

On demand: with DISCORD_APPLICATION_ID and INTERACTION_TOKEN set, the summary
is rendered whatever the time of day, labelled with the session status, and
sent as the reply to that slash-command interaction instead of to the webhook.
"""

import os
import time
from datetime import datetime, time as clock, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import requests

from market_pulse.discord import edit_interaction_response, post_webhook
from market_pulse.theme import BG, GREEN, MUTED, PANEL, RED, RULE, TEXT, Fonts, blend, tile

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_MARKET"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")
APPLICATION_ID_ENV = "DISCORD_APPLICATION_ID"
INTERACTION_TOKEN_ENV = "INTERACTION_TOKEN"

# Yahoo Finance's chart endpoint: closes for up to 20 symbols per request.
SPARK_URL = "https://query1.finance.yahoo.com/v8/finance/spark"
SPARK_BATCH_SIZE = 10
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

# (symbol, Chinese name, ticker shown on the tile)
INDICES = [
    ("^GSPC", "标普500", "S&P 500"),
    ("^IXIC", "纳斯达克", "NASDAQ"),
    ("^DJI", "道琼斯", "DOW"),
    ("RSP", "标普等权", "RSP"),
    ("IWM", "罗素2000", "IWM"),
    ("SOXX", "半导体", "SOXX"),
]

# Sectors are tracked through the Select Sector SPDR ETFs.
SECTORS = [
    ("XLK", "科技"),
    ("XLC", "通信服务"),
    ("XLY", "可选消费"),
    ("XLF", "金融"),
    ("XLI", "工业"),
    ("XLV", "医疗保健"),
    ("XLP", "必需消费"),
    ("XLE", "能源"),
    ("XLB", "原材料"),
    ("XLU", "公用事业"),
    ("XLRE", "房地产"),
]

# (symbol, Chinese name, value format, kind). ^TNX quotes the yield itself in
# percent, so its move is shown in basis points, not as a percentage of a percentage.
MACRO = [
    ("^VIX", "VIX 恐慌指数", "{:.2f}", "pct"),
    ("^TNX", "10年期美债收益率", "{:.2f}%", "bps"),
    ("DX-Y.NYB", "美元指数", "{:.2f}", "pct"),
    ("GC=F", "黄金", "${:,.0f}", "pct"),
    ("CL=F", "WTI 原油", "${:.2f}", "pct"),
    ("BTC-USD", "比特币", "${:,.0f}", "pct"),
]

ALL_SYMBOLS = [row[0] for row in INDICES + SECTORS + MACRO]
INTRADAY_SYMBOLS = [row[0] for row in INDICES]
RANGE_SYMBOLS = [row[0] for row in MACRO]

WEEKDAYS = "一二三四五六日"

# US market convention: green means up, red means down. Set RED_UP to True
# for the mainland Chinese convention (red up, green down). Every number
# also carries an arrow and a sign, so the chart reads the same either way.
RED_UP = False
UP, DOWN = (RED, GREEN) if RED_UP else (GREEN, RED)
FLAT = "#8a8f98"

# A move of this size (in percent) gets a fully saturated tile.
INDEX_HEAT_CAP = 2.0
SECTOR_HEAT_CAP = 2.0


# ============================================================
# Data
# ============================================================

def fetch_spark(symbols, range_, interval):
    """Fetch close series for the symbols, in batches. Returns {symbol: series}."""
    merged = {}
    for start in range(0, len(symbols), SPARK_BATCH_SIZE):
        batch = symbols[start:start + SPARK_BATCH_SIZE]
        params = {"symbols": ",".join(batch), "range": range_, "interval": interval}
        last_exc = None
        for attempt in range(1, FETCH_MAX_RETRIES + 1):
            try:
                response = requests.get(SPARK_URL, params=params, headers=HEADERS, timeout=30)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError(f"Unexpected quote data format: {type(payload)}")
                merged.update(payload)
                last_exc = None
                break
            except (requests.exceptions.RequestException, ValueError, RuntimeError) as exc:
                last_exc = exc
                if attempt < FETCH_MAX_RETRIES:
                    wait = FETCH_RETRY_BACKOFF_SECONDS * attempt
                    print(f"Quote fetch attempt {attempt} failed ({exc!r}), retrying in {wait}s")
                    time.sleep(wait)
        if last_exc:
            raise last_exc
    return merged


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
        "previous": previous,
        "change": last - previous,
        "change_pct": (last - previous) / previous * 100,
        "bar_ts": last_ts,
    }


def parse_intraday(entry):
    """The session's intraday closes, for the sparkline. Empty if unusable."""
    if not isinstance(entry, dict):
        return []
    closes = [c for c in entry.get("close") or [] if isinstance(c, (int, float))]
    return closes if len(closes) >= 5 else []


def load_quotes():
    payload = fetch_spark(ALL_SYMBOLS, "5d", "1d")
    quotes = {symbol: parse_quote(payload.get(symbol)) for symbol in ALL_SYMBOLS}
    return {symbol: quote for symbol, quote in quotes.items() if quote}


def parse_range(entry):
    """(low, high) of a year of daily closes, or None if unusable."""
    if not isinstance(entry, dict):
        return None
    closes = [c for c in entry.get("close") or [] if isinstance(c, (int, float))]
    return (min(closes), max(closes)) if len(closes) >= 20 else None


def load_intraday():
    """Sparklines are decoration: a failure here must not block the summary."""
    try:
        payload = fetch_spark(INTRADAY_SYMBOLS, "1d", "5m")
    except Exception as exc:
        print(f"Intraday fetch failed, drawing tiles without sparklines: {exc!r}")
        return {}
    return {symbol: parse_intraday(payload.get(symbol)) for symbol in INTRADAY_SYMBOLS}


def load_ranges():
    """52-week ranges for the macro tiles. Decoration too: never blocks the summary."""
    try:
        payload = fetch_spark(RANGE_SYMBOLS, "1y", "1d")
    except Exception as exc:
        print(f"52-week range fetch failed, drawing macro tiles without ranges: {exc!r}")
        return {}
    ranges = {symbol: parse_range(payload.get(symbol)) for symbol in RANGE_SYMBOLS}
    return {symbol: value for symbol, value in ranges.items() if value}


def market_traded_today(quotes, now=None):
    """False on weekends and holidays, when the newest S&P 500 bar is a previous session's."""
    sp500 = quotes.get("^GSPC")
    if not sp500:
        return False
    now = time.time() if now is None else now
    return now - sp500["bar_ts"] < MAX_BAR_AGE_SECONDS


def new_york_time(utc_now):
    """Convert an aware UTC datetime to New York wall-clock time (naive).

    US daylight time runs from the second Sunday of March to the first Sunday
    of November, switching at 2 am local. Computed here rather than through a
    time zone database so that it behaves the same on every machine.
    """
    def nth_sunday(year, month, n):
        first = datetime(year, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))

    standard = utc_now.replace(tzinfo=None) - timedelta(hours=5)
    dst_start = nth_sunday(standard.year, 3, 2) + timedelta(hours=2)
    dst_end = nth_sunday(standard.year, 11, 1) + timedelta(hours=1)  # 2 am daylight = 1 am standard
    return standard + timedelta(hours=1) if dst_start <= standard < dst_end else standard


def session_status(session_date, utc_now=None):
    """How the newest S&P 500 session relates to now, for the on-demand summary."""
    now = new_york_time(utc_now or datetime.now(timezone.utc))
    if session_date == now.date():
        if now.time() < clock(16, 0):
            return f"盘中 {now:%H:%M} 纽约时间"
        return "已收盘"
    if now.weekday() < 5 and now.time() < clock(9, 30):
        return "未开盘 · 上一交易日"
    return "休市 · 上一交易日"


# ============================================================
# Formatting
# ============================================================

def direction(value):
    if abs(value) < 0.005:
        return 0
    return 1 if value > 0 else -1


def change_color(value):
    return {1: UP, -1: DOWN, 0: FLAT}[direction(value)]


def arrow(value):
    return {1: "▲", -1: "▼", 0: "—"}[direction(value)]


def format_pct(value):
    return f"{value:+.2f}%"


def format_macro_change(quote, kind):
    if kind == "bps":
        return f"{quote['change'] * 100:+.1f} 基点"
    return format_pct(quote["change_pct"])


def format_date(session_date):
    return f"{session_date.year}年{session_date.month}月{session_date.day}日 周{WEEKDAYS[session_date.weekday()]}"


def sector_rows(quotes):
    """(name, pct) for every sector with data, best first."""
    rows = [(name, quotes[symbol]["change_pct"]) for symbol, name in SECTORS if symbol in quotes]
    return sorted(rows, key=lambda row: row[1], reverse=True)


def breadth(rows):
    up = sum(1 for _, pct in rows if direction(pct) > 0)
    down = sum(1 for _, pct in rows if direction(pct) < 0)
    return up, down


# ============================================================
# Discord embed
# ============================================================

def build_embed(quotes, session_date, image_name, title="美股收盘"):
    """The image carries all the numbers; the embed only captions it with the date, in small text."""
    sp500 = quotes.get("^GSPC")
    return {
        # Description rather than title: regular-size text instead of a bold heading.
        "description": f"{title} · {format_date(session_date)}",
        "color": int(change_color(sp500["change_pct"] if sp500 else 0).lstrip("#"), 16),
        "image": {"url": f"attachment://{image_name}"},
    }


# ============================================================
# Chart
# ============================================================

def heat_color(pct, cap):
    """Tile background: the panel colour shifted toward up/down by the size of the move."""
    if direction(pct) == 0:
        return PANEL
    strength = min(abs(pct) / cap, 1.0)
    return blend(PANEL, change_color(pct), 0.18 + 0.62 * strength)


def draw_sparkline(fig, rect, closes, previous, color):
    ax = fig.add_axes(rect)
    ax.axis("off")
    low, high = min(closes + [previous]), max(closes + [previous])
    pad = (high - low) * 0.12 or 1
    ax.set_xlim(0, len(closes) - 1)
    ax.set_ylim(low - pad, high + pad)
    xs = range(len(closes))
    ax.fill_between(xs, closes, previous, color=color, alpha=0.18, linewidth=0)
    ax.plot(xs, closes, color=color, linewidth=2.0, solid_capstyle="round")
    ax.axhline(previous, color=MUTED, linewidth=0.9, linestyle=(0, (3, 3)))


def draw_index_tile(fig, font, rect, name, ticker, quote, closes):
    """A wide tile: the numbers on the left, the session's intraday path on the right."""
    left, bottom, width, height = rect
    ax = tile(fig, rect)
    ax.text(0.05, 0.83, name, fontproperties=font(16, bold=True), color=TEXT, ha="left", va="center")
    ax.text(0.95, 0.83, ticker, fontproperties=font(10), color=MUTED, ha="right", va="center")
    if not quote:
        ax.text(0.5, 0.42, "暂无数据", fontproperties=font(13), color=MUTED, ha="center", va="center")
        return

    pct = quote["change_pct"]
    color = change_color(pct)
    ax.add_patch(mpatches.Rectangle((0, 0.965), 1, 0.035, color=color))
    ax.text(0.05, 0.53, f"{arrow(pct)} {format_pct(pct)}", fontproperties=font(23, bold=True),
            color=color, ha="left", va="center")
    ax.text(0.05, 0.27, f"{quote['price']:,.2f}", fontproperties=font(14.5, bold=True),
            color=TEXT, ha="left", va="center")
    ax.text(0.05, 0.11, f"{quote['change']:+,.2f}", fontproperties=font(12),
            color=color, ha="left", va="center")

    if closes:
        draw_sparkline(fig, [left + width * 0.56, bottom + height * 0.10, width * 0.40, height * 0.58],
                       closes, quote["previous"], color)


def draw_sector_tile(fig, font, rect, name, pct):
    ax = tile(fig, rect, heat_color(pct, SECTOR_HEAT_CAP))
    ax.text(0.5, 0.66, name, fontproperties=font(11.5, bold=True), color=TEXT, ha="center", va="center")
    ax.text(0.5, 0.29, f"{arrow(pct)} {format_pct(pct)}", fontproperties=font(11, bold=True),
            color="#FFFFFF", ha="center", va="center")


def draw_breadth(fig, font, right, y, rows):
    """Sectors up versus down, on the section's title line: counts and a proportional bar."""
    up, down = breadth(rows)
    total = max(len(rows), 1)
    fig.text(right, y, f"{down} 跌", fontproperties=font(12.5, bold=True), color=DOWN, ha="right", va="center")
    fig.text(right - 0.050, y, f"{up} 涨", fontproperties=font(12.5, bold=True), color=UP, ha="right", va="center")

    bar_width, bar_height = 0.130, 0.012
    bar_left = right - 0.100 - bar_width
    ax = fig.add_axes([bar_left, y - bar_height / 2, bar_width, bar_height])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.add_patch(mpatches.Rectangle((0, 0), 1, 1, color=RULE))
    ax.add_patch(mpatches.Rectangle((0, 0), up / total, 1, color=UP))
    ax.add_patch(mpatches.Rectangle((1 - down / total, 0), down / total, 1, color=DOWN))


def draw_range(ax, font, value_format, price, low, high):
    """52-week low and high with a marker for where the latest close sits between them."""
    low, high = min(low, price), max(high, price)
    bar_left, bar_right, bar_y = 0.10, 0.90, 0.265
    position = (price - low) / (high - low) if high > low else 0.5

    ax.plot([bar_left, bar_right], [bar_y, bar_y], color=RULE, linewidth=3.5, solid_capstyle="round")
    ax.plot([bar_left + (bar_right - bar_left) * position], [bar_y], marker="o", markersize=7,
            color=TEXT, markeredgecolor=PANEL, markeredgewidth=1.2)

    # Two dollar signs in one string would switch Matplotlib into math mode.
    label = f"52周  {value_format.format(low)} – {value_format.format(high)}".replace("$", r"\$")
    ax.text(0.5, 0.135, label, fontproperties=font(9.5), color=MUTED, ha="center", va="center")


def draw_macro_tile(fig, font, rect, name, value_format, kind, quote, year_range=None):
    ax = tile(fig, rect)
    ax.text(0.5, 0.88, name, fontproperties=font(11.5), color=MUTED, ha="center", va="center")
    if not quote:
        ax.text(0.5, 0.50, "暂无数据", fontproperties=font(12), color=MUTED, ha="center", va="center")
        return
    color = change_color(quote["change"])
    ax.text(0.5, 0.66, value_format.format(quote["price"]), fontproperties=font(19, bold=True),
            color=TEXT, ha="center", va="center")
    ax.text(0.5, 0.445, f"{arrow(quote['change'])} {format_macro_change(quote, kind)}",
            fontproperties=font(12.5, bold=True), color=color, ha="center", va="center")
    if year_range:
        draw_range(ax, font, value_format, quote["price"], *year_range)
    ax.add_patch(mpatches.Rectangle((0, 0), 1, 0.03, color=color))


def grid(left, right, columns, gap):
    width = (right - left - gap * (columns - 1)) / columns
    return [left + i * (width + gap) for i in range(columns)], width


# Card layout, in inches from the top. Indices get the most room; the eleven
# sectors share a single row.
CARD_WIDTH = 12.0
CARD_HEIGHT = 9.00
HEADER_Y = 0.36
INDEX_TOP, INDEX_HEIGHT, INDEX_COLUMNS = 0.66, 1.62, 3
SECTOR_TITLE_Y, SECTOR_TOP, SECTOR_HEIGHT = 4.40, 4.66, 0.86
MACRO_TITLE_Y, MACRO_TOP, MACRO_HEIGHT = 5.86, 6.12, 2.08
FOOTER_Y = 8.62
TILE_GAP = 0.14


def draw_card(quotes, intraday, ranges, session_date, out_path=OUTPUT_FILE, status=None):
    font = Fonts()
    fig = plt.figure(figsize=(CARD_WIDTH, CARD_HEIGHT))
    fig.patch.set_facecolor(BG)
    left, right, gap = 0.03, 0.97, TILE_GAP / CARD_WIDTH

    def y(inches):
        """Vertical position as a figure fraction, measured down from the top."""
        return 1 - inches / CARD_HEIGHT

    def box(x, top, width, height):
        return [x, y(top + height), width, height / CARD_HEIGHT]

    def section_title(top, title):
        fig.text(left, y(top), title, fontproperties=font(15, bold=True), color=TEXT, ha="left", va="center")

    # Header
    header = format_date(session_date) + (f" · {status}" if status else "")
    fig.text(left, y(HEADER_Y), header, fontproperties=font(13), color=MUTED, ha="left", va="center")

    # Indices: two rows of wide tiles, each with the session's intraday path
    xs, width = grid(left, right, INDEX_COLUMNS, gap)
    for i, (symbol, name, ticker) in enumerate(INDICES):
        top = INDEX_TOP + (i // INDEX_COLUMNS) * (INDEX_HEIGHT + TILE_GAP)
        draw_index_tile(fig, font, box(xs[i % INDEX_COLUMNS], top, width, INDEX_HEIGHT), name, ticker,
                        quotes.get(symbol), intraday.get(symbol) or [])

    # Sectors: one row of heat tiles, strongest first, with the breadth on the title line
    rows = sector_rows(quotes)
    section_title(SECTOR_TITLE_Y, "板块表现")
    draw_breadth(fig, font, right, y(SECTOR_TITLE_Y), rows)
    xs, width = grid(left, right, len(SECTORS), gap * 0.6)
    for x, (name, pct) in zip(xs, rows):
        draw_sector_tile(fig, font, box(x, SECTOR_TOP, width, SECTOR_HEIGHT), name, pct)

    # Macro: rates, volatility, commodities, crypto
    section_title(MACRO_TITLE_Y, "利率 · 波动 · 商品 · 加密")
    xs, width = grid(left, right, len(MACRO), gap)
    for x, (symbol, name, value_format, kind) in zip(xs, MACRO):
        draw_macro_tile(fig, font, box(x, MACRO_TOP, width, MACRO_HEIGHT), name, value_format, kind,
                        quotes.get(symbol), ranges.get(symbol))

    fig.text(right, y(FOOTER_Y), "涨跌幅相对上一交易日收盘 · 板块为 SPDR 行业 ETF · 数据来源 Yahoo Finance",
             fontproperties=font(10), color=MUTED, ha="right", va="center")

    plt.savefig(out_path, dpi=110, facecolor=BG)
    plt.close(fig)
    return out_path


# ============================================================
# Entry point
# ============================================================

def main():
    interaction_token = os.environ.get(INTERACTION_TOKEN_ENV, "")
    on_demand = bool(interaction_token)
    if on_demand:
        os.environ[APPLICATION_ID_ENV]  # fail fast on missing configuration
    elif not DRY_RUN:
        os.environ[WEBHOOK_ENV]

    quotes = load_quotes()
    missing = [symbol for symbol in ALL_SYMBOLS if symbol not in quotes]
    if missing:
        print(f"No data for: {', '.join(missing)}")
    if "^GSPC" not in quotes or len(missing) > len(ALL_SYMBOLS) // 2:
        raise RuntimeError("Too much quote data is missing, not posting a summary.")

    # The scheduled post is about today's close; an on-demand one shows
    # whatever the latest session is, at any time.
    if not on_demand and not market_traded_today(quotes):
        print("The US market did not trade today, nothing to post.")
        if not DRY_RUN:
            return

    session_date = datetime.fromtimestamp(quotes["^GSPC"]["bar_ts"], tz=timezone.utc).date()
    status = session_status(session_date) if on_demand else None
    chart_path = draw_card(quotes, load_intraday(), load_ranges(), session_date, status=status)

    if DRY_RUN:
        print(f"[dry run] {session_date}: rendered {chart_path} with {len(quotes)}/{len(ALL_SYMBOLS)} quotes")
        return

    if on_demand:
        embed = build_embed(quotes, session_date, chart_path.name, title=f"美股行情 · {status}")
        edit_interaction_response(
            os.environ[APPLICATION_ID_ENV], interaction_token,
            {"embeds": [embed], "allowed_mentions": {"parse": []}},
            file=(chart_path.name, chart_path.read_bytes()),
        )
    else:
        embed = build_embed(quotes, session_date, chart_path.name)
        post_webhook(
            os.environ[WEBHOOK_ENV],
            {"embeds": [embed], "allowed_mentions": {"parse": []}},
            file=(chart_path.name, chart_path.read_bytes()),
            timeout=60,
        )
    chart_path.unlink(missing_ok=True)
    print(f"Posted market summary for {session_date}" + (f" ({status})." if status else "."))


if __name__ == "__main__":
    main()
