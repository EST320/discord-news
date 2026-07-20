import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import plotly.graph_objects as go
import requests

# ============================================================
# 配置
# ============================================================

TEST_MODE = False

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CRYPTO_URL = "https://api.alternative.me/fng/?limit=2"

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_FEARGREED"]

STATE_FILE = Path("seen_feargreed.json")
CHART_DIR = Path("feargreed_charts")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.cnn.com/markets/fear-and-greed",
}

MIN_CHANGE_TO_POST = 0.5  # 数值变动小于此幅度则跳过本次推送，避免刷屏


# ============================================================
# 状态管理
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
# CNN 恐慌与贪婪指数
# ============================================================

RATING_ZH = {
    "extreme fear": "极度恐慌",
    "fear": "恐慌",
    "neutral": "中性",
    "greed": "贪婪",
    "extreme greed": "极度贪婪",
}

RATING_RANGE = {
    "extreme fear": "0-24",
    "fear": "25-44",
    "neutral": "45-55",
    "greed": "56-75",
    "extreme greed": "76-100",
}


def fetch_cnn_data():
    response = requests.get(CNN_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def component_phrase(name, rating, score):
    rating = (rating or "").lower()

    templates = {
        "market_momentum_sp500": {
            "extreme fear": "标普指数深陷极度弱势",
            "fear": "标普指数动能疲软",
            "neutral": "标普指数走势平稳",
            "greed": "标普指数延续反弹态势",
            "extreme greed": "标普指数强势拉升",
        },
        "stock_price_breadth": {
            "extreme fear": "市场广度深陷极度恐慌区间",
            "fear": "市场广度受窄幅上涨影响偏弱",
            "neutral": "市场广度保持中性",
            "greed": "市场广度稳步扩大",
            "extreme greed": "市场广度呈现极度乐观",
        },
        "market_volatility_vix_50": {
            "extreme fear": "波动率处于极度恐慌水平",
            "fear": "波动率略显紧张",
            "neutral": "波动率保持中性",
            "greed": "波动率保持低位平稳",
            "extreme greed": "波动率处于极度乐观区间",
        },
        "junk_bond_demand": {
            "extreme fear": "垂圾债需求极度萎缩",
            "fear": "垂圾债需求疲软",
            "neutral": "垂圾债需求保持中性",
            "greed": "垂圾债需求走强",
            "extreme greed": "垂圾债需求异常火热",
        },
        "safe_haven_demand": {
            "extreme fear": "避险需求急剧升温",
            "fear": "避险需求偏高",
            "neutral": "避险需求保持中性",
            "greed": "避险需求走弱",
            "extreme greed": "避险需求几近消失",
        },
    }

    phrases = templates.get(name, {})
    return phrases.get(rating)


def build_cnn_commentary(data):
    fg = data.get("fear_and_greed", {})
    score = fg.get("score")
    rating = (fg.get("rating") or "").lower()

    parts = []
    for key in (
        "market_momentum_sp500",
        "stock_price_breadth",
        "market_volatility_vix_50",
        "junk_bond_demand",
        "safe_haven_demand",
    ):
        comp = data.get(key, {})
        phrase = component_phrase(key, comp.get("rating"), comp.get("score"))
        if phrase:
            parts.append(phrase)

    summary = "，".join(parts[:3]) if parts else "市场情绪指标暂无明显变化"
    rating_zh = RATING_ZH.get(rating, rating or "未知")

    return f"{summary}，恐慌与贪婪指数处于{rating_zh}区间，最新读数为 {score:.2f}。"


# ============================================================
# 加密货币恐慌与贪婪指数
# ============================================================

CRYPTO_RATING_MAP = {
    "Extreme Fear": ("极度恐慌", "0-24"),
    "Fear": ("恐慌", "25-44"),
    "Neutral": ("中性", "45-55"),
    "Greed": ("贪婪", "56-75"),
    "Extreme Greed": ("极度贪婪", "76-100"),
}


def fetch_crypto_data():
    response = requests.get(CRYPTO_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()
    return payload.get("data", [])


def build_crypto_commentary(current_value, prev_value, classification):
    rating_zh, _ = CRYPTO_RATING_MAP.get(classification, (classification, ""))

    if prev_value is None:
        trend = "暂无历史对比数据"
    else:
        diff = current_value - prev_value
        if abs(diff) < 0.5:
            trend = "较上一期基本持平"
        elif diff > 0:
            trend = f"较上一期上升了 {diff:.1f}"
        else:
            trend = f"较上一期下降了 {abs(diff):.1f}"

    return f"加密货币市场当前情绪为「{rating_zh}」，{trend}。"


# ============================================================
# 仪表盘图表
# ============================================================

def make_gauge_chart(title, value, rating_zh, rating_range, filename):
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    chart_path = CHART_DIR / filename

    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=value,
            number={"font": {"size": 48}},
            title={"text": f"{title}<br><span style='font-size:0.7em'>当前情绪: {rating_zh} ({rating_range})</span>"},
            gauge={
                "axis": {"range": [0, 100], "tickwidth": 1},
                "bar": {"color": "black", "thickness": 0.25},
                "steps": [
                    {"range": [0, 25], "color": "#F4C2C2"},
                    {"range": [25, 45], "color": "#FADBAA"},
                    {"range": [45, 55], "color": "#F5F0C4"},
                    {"range": [55, 75], "color": "#C9E4C5"},
                    {"range": [75, 100], "color": "#8FCB8F"},
                ],
            },
        )
    )

    fig.update_layout(
        width=880,
        height=560,
        margin=dict(l=40, r=40, t=100, b=40),
        paper_bgcolor="white",
        font={"color": "#1F2430", "family": "Arial"},
    )

    fig.write_image(str(chart_path))
    return chart_path


# ============================================================
# Discord 推送
# ============================================================

def post_to_discord(title, commentary, value, updated_at, chart_path, color):
    embed = {
        "title": title,
        "description": commentary,
        "color": color,
        "fields": [
            {"name": "当前数值", "value": f"{value:.2f}", "inline": True},
            {"name": "更新时间", "value": updated_at, "inline": True},
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
# 主程序：CNN 部分
# ============================================================

def run_cnn(state):
    data = fetch_cnn_data()
    fg = data.get("fear_and_greed", {})
    score = float(fg.get("score", 0))
    rating = (fg.get("rating") or "").lower()
    rating_zh = RATING_ZH.get(rating, rating)
    rating_range = RATING_RANGE.get(rating, "")

    last_score = state.get("cnn_last")
    if last_score is not None and abs(score - last_score) < MIN_CHANGE_TO_POST:
        print(f"CNN 指数变动幅度小于阈值（{score:.2f} vs {last_score:.2f}），跳过推送。")
        return

    commentary = build_cnn_commentary(data)
    chart_path = make_gauge_chart("CNN 恐慌与贪婪指数", score, rating_zh, rating_range, "cnn_gauge.png")

    updated_at = datetime.now().strftime("%b %d at %I:%M:%S %p")
    post_to_discord("CNN 市场情绪监测", commentary, score, updated_at, chart_path, color=15105642)

    state["cnn_last"] = score
    print(f"CNN 指数已推送：{score:.2f} ({rating_zh})")


# ============================================================
# 主程序：加密货币部分
# ============================================================

def run_crypto(state):
    entries = fetch_crypto_data()
    if not entries:
        print("加密货币数据为空，跳过。")
        return

    current = entries[0]
    current_value = float(current["value"])
    classification = current["value_classification"]
    rating_zh, rating_range = CRYPTO_RATING_MAP.get(classification, (classification, ""))

    prev_value = float(entries[1]["value"]) if len(entries) > 1 else None

    last_value = state.get("crypto_last")
    if last_value is not None and abs(current_value - last_value) < MIN_CHANGE_TO_POST:
        print(f"加密货币指数变动幅度小于阈值（{current_value:.2f} vs {last_value:.2f}），跳过推送。")
        return

    commentary = build_crypto_commentary(current_value, prev_value, classification)
    chart_path = make_gauge_chart(
        "加密货币恐慌与贪婪指数", current_value, rating_zh, rating_range, "crypto_gauge.png"
    )

    updated_at = datetime.fromtimestamp(
        int(current["timestamp"]), tz=timezone.utc
    ).strftime("%b %d at %I:%M:%S %p UTC")

    post_to_discord("加密货币市场情绪监测", commentary, current_value, updated_at, chart_path, color=15844367)

    state["crypto_last"] = current_value
    print(f"加密货币指数已推送：{current_value:.2f} ({rating_zh})")


# ============================================================
# 入口
# ============================================================

def main():
    state = load_state()

    if TEST_MODE:
        chart_path = make_gauge_chart("CNN 恐慌与贪婪指数", 37.51, "恐慌", "25-44", "test_gauge.png")
        post_to_discord(
            "CNN 市场情绪监测（测试）",
            "这是一条测试消息，用于验证 Webhook、图表生成与推送链路是否正常。",
            37.51,
            datetime.now().strftime("%b %d at %I:%M:%S %p"),
            chart_path,
            color=15105642,
        )
        print("测试成功：已发送模拟仪表盘消息。")
        return

    try:
        run_cnn(state)
    except Exception as exc:
        print(f"CNN 指数抓取/推送失败：{exc}")

    try:
        run_crypto(state)
    except Exception as exc:
        print(f"加密货币指数抓取/推送失败：{exc}")

    save_state(state)


if __name__ == "__main__":
    main()
