import json
import re
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = "https://api-prod.wallstreetcn.com/apiv1/content/lives"
LIVE_URL = "https://wallstreetcn.com/live/us-stock"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

STATE_FILE = Path("seen.json")

CHANNEL = "us-stock-channel"
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_SEND_PER_RUN = 100
FIRST_RUN_SEND = 10
DISCORD_DELAY_SECONDS = 0.55

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": LIVE_URL,
    "Origin": "https://wallstreetcn.com",
}


def load_state():
    if not STATE_FILE.exists():
        return {"seen_ids": []}

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if isinstance(state, list):
        return {"seen_ids": state}

    return state


def save_state(state):
    state["seen_ids"] = state["seen_ids"][-10000:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def api_get(cursor=0):
    params = {
        "channel": CHANNEL,
        "client": "pc",
        "cursor": cursor,
        "limit": PAGE_SIZE,
    }

    response = requests.get(
        API_URL,
        params=params,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    payload = data.get("data", {})

    items = payload.get("items", [])
    next_cursor = payload.get("next_cursor", 0)

    if not isinstance(items, list):
        raise RuntimeError(f"API items 格式异常：{type(items)}")

    return items, next_cursor


def item_to_news(item):
    news_id = str(item.get("id", "")).strip()
    title = str(item.get("title", "")).strip()
    content = str(item.get("content_text", "")).strip()

    if not news_id:
        return None

    # 有些快讯只有正文；标题为空时直接以正文做标题
    if not title:
        title = content[:120] if content else "华尔街见闻快讯"
        content = content[120:] if len(content) > 120 else ""

    # 标题必须是单行，否则会破坏 Markdown 链接语法 [标题](链接)
    title = title.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
    title = re.sub(r"\s{2,}", " ", title)

    # Discord Embed 的 title 最大 256 字符、description 最大 4096 字符
    title = title[:250]
    content = content[:3900]

    display_time = item.get("display_time", "")
    timestamp = None

    # API 有时给 Unix 时间戳
    if isinstance(display_time, (int, float)):
        timestamp = datetime.fromtimestamp(display_time, tz=timezone.utc).isoformat()

    return {
        "id": news_id,
        "title": title,
        "content": content,
        "timestamp": timestamp,
    }

def collect_new_items(seen_ids):
    seen_set = set(seen_ids)
    collected = []
    cursor = 0

    for page in range(MAX_PAGES):
        raw_items, next_cursor = api_get(cursor)

        if not raw_items:
            break

        page_news = [item_to_news(x) for x in raw_items]
        page_news = [x for x in page_news if x]

        # 接口由新到旧返回
        unseen = [x for x in page_news if x["id"] not in seen_set]
        collected.extend(unseen)

        # 只要这一页遇到已见的新闻，说明更老的页面都已处理，不再翻页
        if any(x["id"] in seen_set for x in page_news):
            break

        if not next_cursor or next_cursor == cursor:
            break

        cursor = next_cursor

    # 按 ID 去重，避免跨页重复
    unique = {}
    for news in collected:
        unique[news["id"]] = news

    # API 是新到旧；反过来投递使 Discord 从旧到新阅读
    return list(reversed(list(unique.values())))


def post_to_discord(news):
    safe_title = news["title"].replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    safe_title = re.sub(r"\s{2,}", " ", safe_title).strip()

    headline_link = f"[{safe_title}]({LIVE_URL})"

    text = headline_link

    if news["content"]:
        formatted_content = news["content"].replace("\r\n", "\n").replace("\r", "\n")
        formatted_content = re.sub(r"\n{2,}", "\n", formatted_content)
        formatted_content = formatted_content.replace("\n", "\n\n")
        text += f"\n{formatted_content}"

    # Discord Embed field value 最大 4096 字符
    text = text[:4096]

    embed = {
        "color": 5793266,
        "author": {
            "name": "wallstreetcn · us-stock",
            "url": LIVE_URL,
        },
        "description": text,
    }

    if news["timestamp"]:
        embed["timestamp"] = news["timestamp"]

    response = requests.post(
        WEBHOOK_URL,
        json={
            "username": "华尔街见闻快讯",
            "embeds": [embed],
            "allowed_mentions": {"parse": []},
        },
        timeout=30,
    )

    if response.status_code == 429:
        retry_after = response.json().get("retry_after", 2)
        time.sleep(float(retry_after) + 1)
        return post_to_discord(news)

    response.raise_for_status()

def main():
    state = load_state()
    seen_ids = state.get("seen_ids", [])

    new_items = collect_new_items(seen_ids)
    print(f"检测到 {len(new_items)} 条未推送快讯。")

    # 第一次执行：只验证视觉效果，避免将历史页面全部刷屏
    if not seen_ids:
        to_send = new_items[-FIRST_RUN_SEND:]
        all_current_ids = [x["id"] for x in new_items]
    else:
        to_send = new_items[:MAX_SEND_PER_RUN]
        all_current_ids = [x["id"] for x in new_items]

    for news in to_send:
        post_to_discord(news)
        time.sleep(DISCORD_DELAY_SECONDS)

    # 即使一次积压超过上限，也不把未发送的项目标记为已见
    sent_ids = {x["id"] for x in to_send}
    old_seen = set(seen_ids)

    updated_seen = list(old_seen | sent_ids)

    # 首轮仅将当前全部条目入库，后续避免重复
    if not seen_ids:
        updated_seen = list(set(all_current_ids))

    state["seen_ids"] = updated_seen
    save_state(state)

    print(f"已发送 {len(to_send)} 条。")


if __name__ == "__main__":
    main()
