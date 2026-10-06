"""Fear & Greed tracker: the CNN stock-market index and the alternative.me
crypto index, posted together as one image.

Usage:
    python -m market_pulse.fear_greed

Set DRY_RUN=true to fetch and render without posting or touching state.
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import LinearSegmentedColormap
import numpy as np
import requests

from market_pulse.discord import post_webhook
from market_pulse.paths import STATE_DIR
from market_pulse.theme import BG, GREEN, MUTED, RED, RULE, TEXT, Fonts, tile

# ============================================================
# Config
# ============================================================

TEST_MODE = False
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CRYPTO_URL = "https://api.alternative.me/fng/?limit=35"

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_FEARGREED"

STATE_FILE = STATE_DIR / "seen_feargreed.json"
OUTPUT_FILE = Path("fear_greed.png")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.cnn.com/markets/fear-and-greed",
}

# Extreme Fear -> Extreme Greed, anchored on the theme's red and green.
BAND_COLORS = [RED, "#f08c3a", "#e3c341", "#7cc66a", GREEN]


# ============================================================
# State management
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {"cnn_last": None, "crypto_last": None}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"cnn_last": None, "crypto_last": None}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ============================================================
# Generic rating helpers
# ============================================================

def rating_band(value):
    if value < 25:
        return 0
    if value < 45:
        return 1
    if value < 55:
        return 2
    if value < 75:
        return 3
    return 4


def rating_label(value):
    return ("Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed")[rating_band(value)]


def rating_color(value):
    return BAND_COLORS[rating_band(value)]


def build_commentary(value, prev_value):
    label = rating_label(value)

    if prev_value is None:
        trend = ""
    else:
        diff = value - prev_value
        if abs(diff) < 0.5:
            trend = " (flat)"
        elif diff > 0:
            trend = f" (+{diff:.1f})"
        else:
            trend = f" (-{abs(diff):.1f})"

    return f"{label} ({value:.1f}){trend}."


# ============================================================
# CNN Fear & Greed Index
# ============================================================

def fetch_cnn_data():
    response = requests.get(CNN_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def build_cnn_commentary(score, prev_value):
    return build_commentary(score, prev_value)


def get_cnn_history_value(series, days_ago):
    """Find the CNN historical value closest to N days ago."""
    if not series:
        return None

    target = datetime.now(timezone.utc) - timedelta(days=days_ago)
    target_ts = target.timestamp() * 1000  # CNN timestamps are in milliseconds

    best_point = None
    best_diff = None
    for point in series:
        ts = point.get("x")
        val = point.get("y")
        if ts is None or val is None:
            continue
        diff = abs(ts - target_ts)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_point = val

    return best_point


def build_cnn_history(data):
    fg = data.get("fear_and_greed_historical", {})
    series = fg.get("data", [])

    current_fg = data.get("fear_and_greed", {})
    now_value = float(current_fg.get("score", 0))

    history = []
    for label, days_ago in (("Now", 0), ("Yesterday", 1), ("Last week", 7), ("Last month", 30)):
        if days_ago == 0:
            value = now_value
        else:
            value = get_cnn_history_value(series, days_ago)
            if value is None:
                value = now_value
        history.append((label, float(value)))

    return history


def load_cnn(state):
    """One panel's worth of data: value, text commentary and history."""
    data = fetch_cnn_data()
    score = float(data.get("fear_and_greed", {}).get("score", 0))
    return {
        "key": "cnn_last",
        # Kept short: side-by-side embed fields are narrow, and a longer
        # heading wraps onto a second line and knocks the two columns out of line.
        "heading": "CNN Market Sentiment",
        "title": "Stock Market",
        "source": "CNN Business",
        "value": score,
        "commentary": build_cnn_commentary(score, state.get("cnn_last")),
        "history": build_cnn_history(data),
    }


# ============================================================
# Crypto Fear & Greed Index
# ============================================================

def fetch_crypto_data():
    response = requests.get(CRYPTO_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()
    return payload.get("data", [])


def build_crypto_commentary(current_value, prev_value):
    return build_commentary(current_value, prev_value)


def build_crypto_history(entries):
    """
    Entries are ordered newest to oldest (as returned by alternative.me).
    Index 0 = today, 1 = yesterday, 7 = a week ago, 30 = a month ago
    (falls back to the oldest available entry if data is insufficient).
    """
    def pick(idx):
        if idx < len(entries):
            return float(entries[idx]["value"])
        return float(entries[-1]["value"])

    return [
        ("Now", pick(0)),
        ("Yesterday", pick(1)),
        ("Last week", pick(7)),
        ("Last month", pick(30)),
    ]


def load_crypto(state):
    entries = fetch_crypto_data()
    if not entries:
        raise RuntimeError("Crypto index returned no data")

    current_value = float(entries[0]["value"])
    prev_value = float(entries[1]["value"]) if len(entries) > 1 else None
    return {
        "key": "crypto_last",
        "heading": "Crypto Market Sentiment",
        "title": "Crypto Market",
        "source": "alternative.me",
        "value": current_value,
        "commentary": build_crypto_commentary(current_value, prev_value),
        "history": build_crypto_history(entries),
    }


# ============================================================
# Chart: both indices side by side, each a gauge plus history
# ============================================================

def draw_gauge(fig, font, rect, value):
    ax = fig.add_axes(rect)
    ax.set_xlim(-1.40, 1.40)
    ax.set_ylim(-0.62, 1.33)
    ax.set_aspect("equal")
    ax.axis("off")

    cmap = LinearSegmentedColormap.from_list("fear_greed", BAND_COLORS)
    r_outer, width, segments = 1.0, 0.20, 200
    for i in range(segments):
        t0, t1 = i / segments, (i + 1) / segments
        ax.add_patch(mpatches.Wedge((0, 0), r_outer, 180 - t1 * 180, 180 - t0 * 180, width=width,
                                    facecolor=cmap(t0), edgecolor="none"))

    # Scale: a tick every 5 points just outside the arc, longer and labelled every 25.
    for tick in range(0, 101, 5):
        angle = np.radians(180 - tick / 100 * 180)
        cos, sin = np.cos(angle), np.sin(angle)
        major = tick % 25 == 0
        r0, r1 = 1.035, (1.105 if major else 1.07)
        ax.plot([r0 * cos, r1 * cos], [r0 * sin, r1 * sin], color=TEXT if major else MUTED,
                linewidth=1.8 if major else 1.0, solid_capstyle="butt")
        if major:
            ax.text(1.23 * cos, 1.23 * sin, str(tick), fontproperties=font(10),
                    color=MUTED, ha="center", va="center")

    angle = np.radians(180 - max(0, min(value, 100)) / 100 * 180)
    ax.plot([0, 0.70 * np.cos(angle)], [0, 0.70 * np.sin(angle)], color=TEXT, linewidth=4.5,
            solid_capstyle="round", zorder=5)
    ax.add_patch(plt.Circle((0, 0), 0.075, color=TEXT, zorder=6))

    color = rating_color(value)
    ax.text(0, -0.27, f"{value:.0f}", fontproperties=font(34, bold=True), color=color, ha="center", va="center")
    ax.text(0, -0.52, rating_label(value), fontproperties=font(14, bold=True), color=color, ha="center", va="center")


def draw_history(fig, font, rect, history):
    """Yesterday / last week / last month, each with its value and rating."""
    left, bottom, width, height = rect
    past = history[1:]
    gap = 0.012
    cell_width = (width - gap * (len(past) - 1)) / len(past)
    for i, (label, value) in enumerate(past):
        ax = tile(fig, [left + i * (cell_width + gap), bottom, cell_width, height], BG)
        color = rating_color(value)
        ax.text(0.5, 0.80, label, fontproperties=font(10.5), color=MUTED, ha="center", va="center")
        ax.text(0.5, 0.47, f"{value:.0f}", fontproperties=font(19, bold=True), color=color, ha="center", va="center")
        ax.text(0.5, 0.17, rating_label(value), fontproperties=font(10, bold=True), color=color, ha="center", va="center")


def draw_panel(fig, font, rect, panel):
    left, bottom, width, height = rect
    ax = tile(fig, rect)
    ax.text(0.05, 0.93, panel["title"], fontproperties=font(16, bold=True), color=TEXT, ha="left", va="center")
    ax.text(0.95, 0.93, panel["source"], fontproperties=font(10.5), color=MUTED, ha="right", va="center")
    ax.plot([0.05, 0.95], [0.865, 0.865], color=RULE, linewidth=1)

    if panel.get("value") is None:
        ax.text(0.5, 0.45, "No data", fontproperties=font(15), color=MUTED, ha="center", va="center")
        return

    draw_gauge(fig, font, [left + width * 0.08, bottom + height * 0.27, width * 0.84, height * 0.58], panel["value"])
    draw_history(fig, font, [left + width * 0.05, bottom + height * 0.045, width * 0.90, height * 0.215],
                 panel["history"])


def draw_card(panels, out_path=OUTPUT_FILE):
    font = Fonts()
    fig = plt.figure(figsize=(12, 6.3))
    fig.patch.set_facecolor(BG)

    fig.text(0.03, 0.948, "Fear & Greed Index", fontproperties=font(13), color=MUTED, ha="left", va="center")
    fig.text(0.97, 0.948, datetime.now(timezone.utc).strftime("%b %d, %Y"), fontproperties=font(13),
             color=MUTED, ha="right", va="center")

    gap = 0.012
    width = (0.94 - gap) / 2
    for i, panel in enumerate(panels):
        draw_panel(fig, font, [0.03 + i * (width + gap), 0.045, width, 0.865], panel)

    plt.savefig(out_path, dpi=110, facecolor=BG)
    plt.close(fig)
    return out_path


# ============================================================
# Discord posting
# ============================================================

def build_embed(panels, image_name):
    """One embed: each index keeps its heading, commentary and current value, side by side."""
    available = [panel for panel in panels if panel.get("value") is not None]
    return {
        "color": int(rating_color(available[0]["value"]).lstrip("#"), 16),
        "fields": [
            {
                "name": panel["heading"],
                "value": f"{panel['commentary']}\n**Current Value**\n{panel['value']:.2f}",
                "inline": True,
            }
            for panel in available
        ],
        "image": {"url": f"attachment://{image_name}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def post_to_discord(panels, chart_path):
    payload = {"embeds": [build_embed(panels, chart_path.name)], "allowed_mentions": {"parse": []}}
    post_webhook(
        os.environ[WEBHOOK_ENV],
        payload,
        file=(chart_path.name, chart_path.read_bytes()),
        timeout=60,
    )
    chart_path.unlink(missing_ok=True)


# ============================================================
# Entry point
# ============================================================

def load_panels(state):
    """Fetch both indices. One failing leaves a 'No data' panel; it does not block the other."""
    panels = []
    for loader, title, source in ((load_cnn, "Stock Market", "CNN Business"),
                                  (load_crypto, "Crypto Market", "alternative.me")):
        try:
            panels.append(loader(state))
        except Exception as exc:
            print(f"{title} index fetch failed: {exc!r}")
            panels.append({"title": title, "source": source, "value": None})
    return panels


def sample_panels():
    return [
        {"key": "cnn_last", "heading": "CNN Market Sentiment (Test)", "title": "Stock Market",
         "source": "CNN Business", "value": 37.51, "commentary": "This is a test message.",
         "history": [("Now", 37.51), ("Yesterday", 38.6), ("Last week", 41.2), ("Last month", 35.0)]},
        {"key": "crypto_last", "heading": "Crypto Market Sentiment (Test)", "title": "Crypto Market",
         "source": "alternative.me", "value": 70.0, "commentary": "This is a test message.",
         "history": [("Now", 70.0), ("Yesterday", 65.0), ("Last week", 74.0), ("Last month", 73.0)]},
    ]


def main():
    if not DRY_RUN:
        os.environ[WEBHOOK_ENV]  # fail fast on missing configuration
    state = load_state()

    if TEST_MODE:
        panels = sample_panels()
        post_to_discord(panels, draw_card(panels))
        print("Test succeeded: sample gauge message sent.")
        return

    panels = load_panels(state)
    available = [panel for panel in panels if panel.get("value") is not None]
    if not available:
        raise RuntimeError("Neither index could be fetched, nothing to post.")

    chart_path = draw_card(panels)
    if DRY_RUN:
        print(f"[dry run] rendered {chart_path} with {len(available)}/{len(panels)} indices")
        return

    post_to_discord(panels, chart_path)

    for panel in available:
        state[panel["key"]] = panel["value"]
        print(f"{panel['title']} index posted: {panel['value']:.2f} ({rating_label(panel['value'])})")
    save_state(state)


if __name__ == "__main__":
    main()
