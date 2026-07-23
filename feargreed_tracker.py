import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import LinearSegmentedColormap
import numpy as np
import requests

# ============================================================
# Config
# ============================================================

TEST_MODE = False

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CRYPTO_URL = "https://api.alternative.me/fng/?limit=35"

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_FEARGREED"]

STATE_FILE = Path("seen_feargreed.json")
CHART_DIR = Path("feargreed_charts")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.cnn.com/markets/fear-and-greed",
}


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

def rating_label(value):
    if value < 25:
        return "Extreme Fear"
    if value < 45:
        return "Fear"
    if value < 55:
        return "Neutral"
    if value < 75:
        return "Greed"
    return "Extreme Greed"


def rating_color(value):
    if value < 25:
        return "#d9534f"
    if value < 45:
        return "#e8974e"
    if value < 55:
        return "#e0c341"
    if value < 75:
        return "#8bc34a"
    return "#4caf50"


# ============================================================
# CNN Fear & Greed Index
# ============================================================

def fetch_cnn_data():
    response = requests.get(CNN_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def build_cnn_commentary(score, prev_value):
    label = rating_label(score)

    if prev_value is None:
        trend = ""
    else:
        diff = score - prev_value
        if abs(diff) < 0.5:
            trend = " (flat)"
        elif diff > 0:
            trend = f" (+{diff:.1f})"
        else:
            trend = f" (-{abs(diff):.1f})"

    return f"{label} ({score:.1f}){trend}."


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


# ============================================================
# Crypto Fear & Greed Index
# ============================================================

def fetch_crypto_data():
    response = requests.get(CRYPTO_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()
    return payload.get("data", [])


def build_crypto_commentary(current_value, prev_value):
    label = rating_label(current_value)

    if prev_value is None:
        trend = ""
    else:
        diff = current_value - prev_value
        if abs(diff) < 0.5:
            trend = " (flat)"
        elif diff > 0:
            trend = f" (+{diff:.1f})"
        else:
            trend = f" (-{abs(diff):.1f})"

    return f"{label} ({current_value:.1f}){trend}."


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


# ============================================================
# Gauge chart: gradient dial + Historical Values panel
# ============================================================

def draw_full_card(value, title, subtitle, icon_label, history, source_label, updated_at, out_path):
    fig = plt.figure(figsize=(12, 5.4))
    fig.patch.set_facecolor("white")

    # ---- Left: gauge card ----
    ax = fig.add_axes([0.03, 0.07, 0.50, 0.86])
    ax.set_facecolor("white")
    ax.set_xlim(-1.4, 1.4)
    ax.set_ylim(-0.85, 1.35)
    ax.set_aspect("equal")
    ax.axis("off")

    ax.add_patch(plt.Circle((-1.22, 1.18), 0.09, facecolor="#f7931a", zorder=5))
    ax.text(-1.22, 1.18, icon_label, fontsize=11, ha="center", va="center",
            color="white", fontweight="bold", zorder=6)
    ax.text(-1.05, 1.18, title, fontsize=18, ha="left", va="center", color="#2b2b2b", fontweight="bold")
    ax.text(-1.25, 0.93, subtitle, fontsize=10, ha="left", va="center", color="#8a8f98")
    ax.plot([-1.35, 1.35], [0.80, 0.80], color="#e5e5e5", linewidth=1)

    ax.text(-1.25, 0.60, "Now:", fontsize=12, ha="left", va="center", color="#555555")
    ax.text(-1.25, 0.40, rating_label(value), fontsize=15, ha="left", va="center",
            color=rating_color(value), fontweight="bold")

    cmap = LinearSegmentedColormap.from_list(
        "fg", ["#d9534f", "#e8974e", "#e0c341", "#8bc34a", "#4caf50"]
    )
    r_outer = 0.60
    r_inner = 0.40
    n_seg = 200
    cy = -0.08

    for i in range(n_seg):
        t0 = i / n_seg
        t1 = (i + 1) / n_seg
        theta1 = 180 - t0 * 180
        theta2 = 180 - t1 * 180
        color = cmap(t0)
        wedge = mpatches.Wedge((0, cy), r_outer, theta2, theta1, width=r_outer - r_inner,
                                facecolor=color, edgecolor="none")
        ax.add_patch(wedge)

    for tick in [0, 25, 50, 75, 100]:
        angle = np.radians(180 - (tick / 100) * 180)
        ox1, oy1 = (r_outer + 0.02) * np.cos(angle), cy + (r_outer + 0.02) * np.sin(angle)
        ox2, oy2 = (r_outer + 0.08) * np.cos(angle), cy + (r_outer + 0.08) * np.sin(angle)
        ax.plot([ox1, ox2], [oy1, oy2], color="#999999", linewidth=1.3)
        tx, ty = (r_outer + 0.18) * np.cos(angle), cy + (r_outer + 0.18) * np.sin(angle)
        ax.text(tx, ty, str(tick), ha="center", va="center", fontsize=9.5, color="#888888")

    for tick in range(0, 101, 5):
        if tick % 25 == 0:
            continue
        angle = np.radians(180 - (tick / 100) * 180)
        ox1, oy1 = (r_outer + 0.01) * np.cos(angle), cy + (r_outer + 0.01) * np.sin(angle)
        ox2, oy2 = (r_outer + 0.04) * np.cos(angle), cy + (r_outer + 0.04) * np.sin(angle)
        ax.plot([ox1, ox2], [oy1, oy2], color="#c7c7c7", linewidth=0.8)

    needle_angle = np.radians(180 - (value / 100) * 180)
    needle_len = r_inner - 0.02
    nx, ny = needle_len * np.cos(needle_angle), cy + needle_len * np.sin(needle_angle)
    ax.plot([0, nx], [cy, ny], color="#aeaeae", linewidth=5, solid_capstyle="round", zorder=5)

    badge_r = 0.13
    badge_dist = r_inner + 0.30
    bx = badge_dist * np.cos(needle_angle)
    by = cy + badge_dist * np.sin(needle_angle)
    ax.add_patch(plt.Circle((bx, by), badge_r, color=rating_color(value), zorder=6))
    ax.text(bx, by, str(int(round(value))), ha="center", va="center", fontsize=17,
            color="white", fontweight="bold", zorder=7)

    icon_r = 0.07
    icon_dist = r_inner * 0.5
    icx, icy = icon_dist * np.cos(needle_angle), cy + icon_dist * np.sin(needle_angle)
    ax.add_patch(plt.Circle((icx, icy), icon_r, facecolor="#f7931a", edgecolor="white", linewidth=1.5, zorder=8))
    ax.text(icx, icy, icon_label, ha="center", va="center", fontsize=9, color="white", fontweight="bold", zorder=9)

    ax.plot([-1.35, 1.35], [-0.72, -0.72], color="#e5e5e5", linewidth=1)
    ax.text(0, -0.82, source_label, fontsize=9, color="#a5a9af", ha="center")

    # ---- Right: Historical Values ----
    ax2 = fig.add_axes([0.57, 0.07, 0.41, 0.86])
    ax2.set_facecolor("white")
    ax2.set_xlim(0, 1)
    ax2.set_ylim(0, 1)
    ax2.axis("off")

    ax2.text(0, 0.97, "Historical Values", fontsize=17, fontweight="bold", color="#2b2b2b", ha="left", va="top")

    n_items = len(history)
    row_h = 0.82 / n_items
    top_y = 0.80

    for i, (label, val) in enumerate(history):
        y = top_y - i * row_h
        ax2.text(0, y, label, fontsize=12.5, color="#555555", ha="left", va="top")
        ax2.text(0, y - row_h * 0.42, rating_label(val), fontsize=13.5, color=rating_color(val),
                 fontweight="bold", ha="left", va="top")

        badge_r2 = 0.055
        bcx, bcy = 0.90, y - row_h * 0.20
        ax2.add_patch(plt.Circle((bcx, bcy), badge_r2, color=rating_color(val), zorder=5))
        ax2.text(bcx, bcy, str(int(round(val))), ha="center", va="center", fontsize=13,
                 color="white", fontweight="bold", zorder=6)

        if i < n_items - 1:
            line_y = y - row_h + row_h * 0.10
            ax2.plot([0, 1], [line_y, line_y], color="#eeeeee", linewidth=1)

    CHART_DIR.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=150, facecolor="white", bbox_inches="tight")
    plt.close()

    return out_path


# ============================================================
# Discord posting
# ============================================================

def post_to_discord(title, commentary, value, updated_at, chart_path, color):
    embed = {
        "title": title,
        "description": commentary,
        "color": color,
        "fields": [
            {"name": "Current Value", "value": f"{value:.2f}", "inline": True},
        ],
        "image": {"url": f"attachment://{chart_path.name}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}

    with chart_path.open("rb") as chart_file:
        response = requests.post(
            WEBHOOK_URL,
            data={"payload_json": json.dumps(payload, ensure_ascii=False)},
            files={"file": (chart_path.name, chart_file, "image/png")},
            timeout=60,
        )

    if response.status_code == 429:
        retry_after = float(response.json().get("retry_after", 2))
        time.sleep(retry_after + 1)
        return post_to_discord(title, commentary, value, updated_at, chart_path, color)

    response.raise_for_status()
    chart_path.unlink(missing_ok=True)


# ============================================================
# Main: CNN section
# ============================================================

def run_cnn(state):
    data = fetch_cnn_data()
    fg = data.get("fear_and_greed", {})
    score = float(fg.get("score", 0))
    prev_value = state.get("cnn_last")

    commentary = build_cnn_commentary(score, prev_value)
    history = build_cnn_history(data)
    updated_at = datetime.now().strftime("Last updated %b %d, %Y")

    chart_path = draw_full_card(
        score, "Fear & Greed Index", "CNN Business Stock Market Sentiment",
        "$", history, "cnn.com", updated_at, CHART_DIR / "cnn_gauge.png"
    )

    post_to_discord("CNN Market Sentiment Tracker", commentary, score, updated_at, chart_path, color=15105642)

    state["cnn_last"] = score
    print(f"CNN index posted: {score:.2f} ({rating_label(score)})")


# ============================================================
# Main: Crypto section
# ============================================================

def run_crypto(state):
    entries = fetch_crypto_data()
    if not entries:
        print("Crypto data empty, skipping.")
        return

    current_value = float(entries[0]["value"])
    prev_value = float(entries[1]["value"]) if len(entries) > 1 else None

    commentary = build_crypto_commentary(current_value, prev_value)
    history = build_crypto_history(entries)

    updated_at = datetime.fromtimestamp(
        int(entries[0]["timestamp"]), tz=timezone.utc
    ).strftime("Last updated %b %d, %Y")

    chart_path = draw_full_card(
        current_value, "Fear & Greed Index", "Multifactorial Crypto Market Sentiment Analysis",
        "B", history, "alternative.me", updated_at, CHART_DIR / "crypto_gauge.png"
    )

    post_to_discord("Crypto Market Sentiment Tracker", commentary, current_value, updated_at, chart_path, color=15844367)

    state["crypto_last"] = current_value
    print(f"Crypto index posted: {current_value:.2f} ({rating_label(current_value)})")


# ============================================================
# Entry point
# ============================================================

def main():
    state = load_state()

    if TEST_MODE:
        history = [("Now", 37.51), ("Yesterday", 38.6), ("Last week", 41.2), ("Last month", 35.0)]
        updated_at = datetime.now().strftime("Last updated %b %d, %Y")
        chart_path = draw_full_card(
            37.51, "Fear & Greed Index", "CNN Business Stock Market Sentiment",
            "$", history, "cnn.com", updated_at, CHART_DIR / "test_gauge.png"
        )
        post_to_discord(
            "CNN Market Sentiment Tracker (Test)",
            "This is a test message to verify the webhook, chart generation, and posting pipeline.",
            37.51, updated_at, chart_path, color=15105642,
        )
        print("Test succeeded: sample gauge message sent.")
        return

    try:
        run_cnn(state)
    except Exception as exc:
        print(f"CNN fetch/post failed: {exc}")

    try:
        run_crypto(state)
    except Exception as exc:
        print(f"Crypto fetch/post failed: {exc}")

    save_state(state)


if __name__ == "__main__":
    main()
