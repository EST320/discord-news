import json
import re
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = "https://api-prod.wallstreetcn.com/apiv1/content/lives"
LIVE_URL = "https://wallstreetcn.com/live/a-stock"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_A"]

STATE_FILE = Path("seen_a.json")

CHANNEL = "a-stock-channel"
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
    state["seen_ids"] = state["seen_ids"][-1000:]
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

    if not title:
        title = content[:120] if content else "华尔街见闻快讯"
        content = content[120:] if len(content) > 120 else ""

    title = title.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
    title = re.sub(r"\s{2,}", " ", title)

    title = title[:250]
    content = content[:3900]

    display_time = item.get("display_time", "")
    timestamp = None

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

        unseen = [x for x in page_news if x["id"] not in seen_set]
        collected.extend(unseen)

        if any(x["id"] in seen_set for x in page_news):
            break

        if not next_cursor or next_cursor == cursor:
            break

        cursor = next_cursor

    unique = {}
    for news in collected:
        unique[news["id"]] = news

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

    text = text[:4096]

    embed = {
        "color": 15548997,
        "author": {
            "name": "wallstreetcn · a-stock",
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
    print(f"检测到 {len(new_items)} 条A股未推送快讯。")

    if not seen_ids:
        to_send = new_items[-FIRST_RUN_SEND:]
        all_current_ids = [x["id"] for x in new_items]
    else:
        to_send = new_items[:MAX_SEND_PER_RUN]
        all_current_ids = [x["id"] for x in new_items]

    for news in to_send:
        post_to_discord(news)
        time.sleep(DISCORD_DELAY_SECONDS)

    sent_ids = {x["id"] for x in to_send}
    old_seen = set(seen_ids)

    updated_seen = list(old_seen | sent_ids)

    if not seen_ids:
        updated_seen = list(set(all_current_ids))

    state["seen_ids"] = updated_seen
    save_state(state)

    print(f"已发送 {len(to_send)} 条A股快讯。")


if __name__ == "__main__":
    main()
