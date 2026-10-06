"""Daily US market close summary for a Chinese-language channel.

One image (index tiles with intraday sparklines, a sector heat map and macro
tiles) plus a text recap, both in Chinese.

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
from matplotlib.colors import to_rgb
from matplotlib.font_manager import FontProperties
import requests

from market_pulse.discord import post_webhook
from market_pulse.fonts import cjk_font_paths

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_MARKET"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

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
    ("IWM", "罗素2000", "IWM"),
    ("SOXX", "半导体", "SOXX"),
]
MAIN_INDICES = ("^GSPC", "^IXIC", "^DJI")

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

WEEKDAYS = "一二三四五六日"

# Chinese market convention: red means up, green means down. Every number
# also carries an arrow and a sign, so the chart reads the same either way.
RED_UP = True
RED, GREEN = "#f0453a", "#22b573"
UP, DOWN = (RED, GREEN) if RED_UP else (GREEN, RED)
FLAT = "#8a8f98"

BG = "#171b24"
PANEL = "#232936"
TEXT = "#EDEFF2"
MUTED = "#8d94a1"
RULE = "#333a48"

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


def load_intraday():
    """Sparklines are decoration: a failure here must not block the summary."""
    try:
        payload = fetch_spark(INTRADAY_SYMBOLS, "1d", "5m")
    except Exception as exc:
        print(f"Intraday fetch failed, drawing tiles without sparklines: {exc!r}")
        return {}
    return {symbol: parse_intraday(payload.get(symbol)) for symbol in INTRADAY_SYMBOLS}


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

def direction(value):
    if abs(value) < 0.005:
        return 0
    return 1 if value > 0 else -1


def change_color(value):
    return {1: UP, -1: DOWN, 0: FLAT}[direction(value)]


def arrow(value):
    return {1: "▲", -1: "▼", 0: "—"}[direction(value)]


def dot(value):
    """Coloured marker for Discord text, where arrows cannot be tinted."""
    up, down = ("🔴", "🟢") if RED_UP else ("🟢", "🔴")
    return {1: up, -1: down, 0: "⚪"}[direction(value)]


def format_pct(value):
    return f"{value:+.2f}%"


def format_macro_change(quote, kind):
    if kind == "bps":
        return f"{quote['change'] * 100:+.1f} 基点"
    return format_pct(quote["change_pct"])


def format_date(session_date):
    return f"{session_date.year}年{session_date.month}月{session_date.day}日 周{WEEKDAYS[session_date.weekday()]}"


def vix_mood(level):
    if level < 15:
        return "市场平静"
    if level < 20:
        return "波动正常"
    if level < 30:
        return "情绪紧张"
    return "市场恐慌"


def sector_rows(quotes):
    """(name, pct) for every sector with data, best first."""
    rows = [(name, quotes[symbol]["change_pct"]) for symbol, name in SECTORS if symbol in quotes]
    return sorted(rows, key=lambda row: row[1], reverse=True)


def breadth(rows):
    up = sum(1 for _, pct in rows if direction(pct) > 0)
    down = sum(1 for _, pct in rows if direction(pct) < 0)
    return up, down


def build_headline(quotes):
    """One sentence on how the three main indices closed."""
    moves = [direction(quotes[s]["change_pct"]) for s in MAIN_INDICES if s in quotes]
    if not moves:
        return "美股收盘"
    if all(m > 0 for m in moves):
        tone = "三大指数集体收涨"
    elif all(m < 0 for m in moves):
        tone = "三大指数集体收跌"
    else:
        tone = "三大指数涨跌不一"

    sp500 = quotes.get("^GSPC")
    return f"{tone}，标普500 {format_pct(sp500['change_pct'])}" if sp500 else tone


# ============================================================
# Text recap (Discord embed)
# ============================================================

def build_embed(quotes, session_date, image_name):
    index_lines = [
        f"{dot(q['change_pct'])} **{name}**{'' if symbol.startswith('^') else f'（{ticker}）'}　"
        f"{q['price']:,.2f}　{format_pct(q['change_pct'])}（{q['change']:+,.2f}）"
        for symbol, name, ticker in INDICES
        if (q := quotes.get(symbol))
    ]

    rows = sector_rows(quotes)
    up, down = breadth(rows)
    leaders = "、".join(f"{name} {format_pct(pct)}" for name, pct in rows[:3])
    laggards = "、".join(f"{name} {format_pct(pct)}" for name, pct in rows[-3:][::-1])
    sector_text = f"{up} 涨 {down} 跌\n最强：{leaders}\n最弱：{laggards}" if rows else "暂无数据"

    macro_lines = []
    for symbol, name, value_format, kind in MACRO:
        quote = quotes.get(symbol)
        if not quote:
            continue
        line = f"{dot(quote['change'])} **{name}**　{value_format.format(quote['price'])}　{format_macro_change(quote, kind)}"
        if symbol == "^VIX":
            line += f"（{vix_mood(quote['price'])}）"
        macro_lines.append(line)

    sp500 = quotes.get("^GSPC")
    return {
        "title": f"美股收盘 · {format_date(session_date)}",
        "description": build_headline(quotes) + "。",
        "color": int(change_color(sp500["change_pct"] if sp500 else 0).lstrip("#"), 16),
        "fields": [
            {"name": "指数", "value": "\n".join(index_lines) or "暂无数据", "inline": False},
            {"name": "板块", "value": sector_text, "inline": False},
            {"name": "利率 · 波动 · 商品 · 加密", "value": "\n".join(macro_lines) or "暂无数据", "inline": False},
        ],
        "image": {"url": f"attachment://{image_name}"},
        "footer": {"text": "涨跌幅相对上一交易日收盘 · 板块为 SPDR 行业 ETF · 数据来源 Yahoo Finance"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
# Chart
# ============================================================

def blend(base, tint, amount):
    base_rgb, tint_rgb = to_rgb(base), to_rgb(tint)
    return tuple(b + (t - b) * amount for b, t in zip(base_rgb, tint_rgb))


def heat_color(pct, cap):
    """Tile background: the panel colour shifted toward up/down by the size of the move."""
    if direction(pct) == 0:
        return PANEL
    strength = min(abs(pct) / cap, 1.0)
    return blend(PANEL, change_color(pct), 0.18 + 0.62 * strength)


class Fonts:
    def __init__(self):
        regular, bold = cjk_font_paths()
        self.regular, self.bold = regular, bold

    def __call__(self, size, bold=False):
        return FontProperties(fname=self.bold if bold else self.regular, size=size)


def tile(fig, rect, color=PANEL):
    ax = fig.add_axes(rect)
    ax.set_facecolor(color)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    return ax


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
    left, bottom, width, height = rect
    ax = tile(fig, rect)
    ax.text(0.08, 0.90, name, fontproperties=font(15, bold=True), color=TEXT, ha="left", va="center")
    ax.text(0.92, 0.90, ticker, fontproperties=font(9.5), color=MUTED, ha="right", va="center")
    if not quote:
        ax.text(0.5, 0.5, "暂无数据", fontproperties=font(13), color=MUTED, ha="center", va="center")
        return

    pct = quote["change_pct"]
    color = change_color(pct)
    ax.add_patch(mpatches.Rectangle((0, 0.965), 1, 0.035, color=color))
    ax.text(0.08, 0.70, f"{arrow(pct)} {format_pct(pct)}", fontproperties=font(21, bold=True),
            color=color, ha="left", va="center")
    ax.text(0.08, 0.525, f"{quote['price']:,.2f}", fontproperties=font(13.5, bold=True),
            color=TEXT, ha="left", va="center")
    ax.text(0.92, 0.525, f"{quote['change']:+,.2f}", fontproperties=font(11.5),
            color=color, ha="right", va="center")

    if closes:
        draw_sparkline(fig, [left + width * 0.06, bottom + height * 0.07, width * 0.88, height * 0.34],
                       closes, quote["previous"], color)


def draw_sector_tile(fig, font, rect, name, pct):
    ax = tile(fig, rect, heat_color(pct, SECTOR_HEAT_CAP))
    ax.text(0.5, 0.67, name, fontproperties=font(14, bold=True), color=TEXT, ha="center", va="center")
    ax.text(0.5, 0.30, f"{arrow(pct)} {format_pct(pct)}", fontproperties=font(15, bold=True),
            color="#FFFFFF", ha="center", va="center")


def draw_breadth_tile(fig, font, rect, rows):
    ax = tile(fig, rect, BG)
    up, down = breadth(rows)
    total = max(len(rows), 1)
    ax.text(0.5, 0.80, "板块涨跌", fontproperties=font(11.5), color=MUTED, ha="center", va="center")
    ax.text(0.27, 0.47, str(up), fontproperties=font(24, bold=True), color=UP, ha="center", va="center")
    ax.text(0.50, 0.47, ":", fontproperties=font(20, bold=True), color=MUTED, ha="center", va="center")
    ax.text(0.73, 0.47, str(down), fontproperties=font(24, bold=True), color=DOWN, ha="center", va="center")
    # Proportional bar: share of sectors up versus down.
    ax.add_patch(mpatches.Rectangle((0.08, 0.10), 0.84, 0.10, color=RULE))
    ax.add_patch(mpatches.Rectangle((0.08, 0.10), 0.84 * up / total, 0.10, color=UP))
    ax.add_patch(mpatches.Rectangle((0.92 - 0.84 * down / total, 0.10), 0.84 * down / total, 0.10, color=DOWN))


def draw_macro_tile(fig, font, rect, name, value_format, kind, quote, note=None):
    ax = tile(fig, rect)
    ax.text(0.5, 0.84, name, fontproperties=font(11.5), color=MUTED, ha="center", va="center")
    if not quote:
        ax.text(0.5, 0.45, "暂无数据", fontproperties=font(12), color=MUTED, ha="center", va="center")
        return
    color = change_color(quote["change"])
    ax.text(0.5, 0.56, value_format.format(quote["price"]), fontproperties=font(19, bold=True),
            color=TEXT, ha="center", va="center")
    ax.text(0.5, 0.30, f"{arrow(quote['change'])} {format_macro_change(quote, kind)}",
            fontproperties=font(12.5, bold=True), color=color, ha="center", va="center")
    if note:
        ax.text(0.5, 0.11, note, fontproperties=font(9.5), color=MUTED, ha="center", va="center")
    ax.add_patch(mpatches.Rectangle((0, 0), 1, 0.03, color=color))


def section_title(fig, font, y, title, note=""):
    fig.text(0.03, y, title, fontproperties=font(15, bold=True), color=TEXT, ha="left", va="center")
    if note:
        fig.text(0.97, y, note, fontproperties=font(10.5), color=MUTED, ha="right", va="center")


def grid(left, right, columns, gap):
    width = (right - left - gap * (columns - 1)) / columns
    return [left + i * (width + gap) for i in range(columns)], width


def draw_card(quotes, intraday, session_date, out_path=OUTPUT_FILE):
    font = Fonts()
    fig = plt.figure(figsize=(12, 10.4))
    fig.patch.set_facecolor(BG)
    left, right, gap = 0.03, 0.97, 0.012

    # Header
    fig.text(left, 0.957, "美股收盘", fontproperties=font(26, bold=True), color=TEXT, ha="left", va="center")
    fig.text(right, 0.965, format_date(session_date), fontproperties=font(14), color=MUTED, ha="right", va="center")
    fig.text(right, 0.937, build_headline(quotes), fontproperties=font(12), color=MUTED, ha="right", va="center")

    # Indices: one tile each, with the session's intraday path
    xs, width = grid(left, right, len(INDICES), gap)
    for x, (symbol, name, ticker) in zip(xs, INDICES):
        draw_index_tile(fig, font, [x, 0.675, width, 0.235], name, ticker,
                        quotes.get(symbol), intraday.get(symbol) or [])

    # Sectors: heat map, best to worst in reading order
    rows = sector_rows(quotes)
    section_title(fig, font, 0.640, "板块表现", "由强到弱 · 颜色越深涨跌幅越大")
    xs, width = grid(left, right, 6, gap)
    tile_height, top = 0.135, 0.612
    cells = [(xs[i % 6], top - tile_height - (i // 6) * (tile_height + gap * 1.2)) for i in range(12)]
    for (x, y), (name, pct) in zip(cells, rows):
        draw_sector_tile(fig, font, [x, y, width, tile_height], name, pct)
    draw_breadth_tile(fig, font, [*cells[11], width, tile_height], rows)

    # Macro: rates, volatility, commodities, crypto
    section_title(fig, font, 0.288, "利率 · 波动 · 商品 · 加密")
    xs, width = grid(left, right, len(MACRO), gap)
    for x, (symbol, name, value_format, kind) in zip(xs, MACRO):
        quote = quotes.get(symbol)
        note = vix_mood(quote["price"]) if quote and symbol == "^VIX" else None
        draw_macro_tile(fig, font, [x, 0.065, width, 0.195], name, value_format, kind, quote, note)

    legend = "红涨绿跌" if RED_UP else "绿涨红跌"
    fig.text(left, 0.028, f"{legend} · 虚线为上一交易日收盘", fontproperties=font(10), color=MUTED, ha="left", va="center")
    fig.text(right, 0.028, "涨跌幅相对上一交易日收盘 · 板块为 SPDR 行业 ETF · 数据来源 Yahoo Finance",
             fontproperties=font(10), color=MUTED, ha="right", va="center")

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
    chart_path = draw_card(quotes, load_intraday(), session_date)
    embed = build_embed(quotes, session_date, chart_path.name)

    if DRY_RUN:
        print(f"[dry run] {session_date}: rendered {chart_path} with {len(quotes)}/{len(ALL_SYMBOLS)} quotes")
        return

    post_webhook(
        os.environ[WEBHOOK_ENV],
        {"embeds": [embed], "allowed_mentions": {"parse": []}},
        file=(chart_path.name, chart_path.read_bytes()),
        timeout=60,
    )
    chart_path.unlink(missing_ok=True)
    print(f"Posted market close summary for {session_date}.")


if __name__ == "__main__":
    main()
