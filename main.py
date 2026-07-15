import json
import os
from pathlib import Path

import requests

API_URL = "https://api-prod.wallstreetcn.com/apiv1/content/lives"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = Path("seen.json")

PARAMS = {
    "channel": "global-channel",
    "client": "pc",
    "cursor": 0,
    "limit": 100,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://wallstreetcn.com/live/global",
}


def load_seen():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return []


def save_seen(seen):
    STATE_FILE.write_text(
        json.dumps(seen[-5000:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_items():
    response = requests.get(
        API_URL,
        params=PARAMS,
        headers=HEADERS,
        timeout=30,
    )

    print("API URL:", response.url)
    print("API status:", response.status_code)
    print("API preview:", response.text[:500])

    response.raise_for_status()
    data = response.json()

    # 正常返回结构：data.data.items
    items = data.get("data", {}).get("items", [])

    if not items:
        raise RuntimeError(
            f"API 没有返回 items。顶层 keys: {list(data.keys())}"
        )

    result = []

    for item in items:
        news_id = str(item.get("id", ""))
        title = item.get("title", "").strip()
        content = item.get("content_text", "").strip()

        # 某些条目只有 content，没有 title
        if title and content:
            text = f"{title}\n{content}"
        else:
            text = title or content

        if news_id and text:
            result.append((news_id, text))

    print("Parsed items:", len(result))
    return result


def post_to_discord(text):
    content = f"**华尔街见闻｜全球快讯**\n{text}\nhttps://wallstreetcn.com/live/global"

    # Discord 单条消息上限为 2,000 字符
    if len(content) > 1950:
        content = content[:1940] + "…"

    response = requests.post(
        WEBHOOK_URL,
        json={
            "username": "华尔街见闻快讯",
            "content": content,
        },
        timeout=30,
    )

    print("Discord status:", response.status_code)
    response.raise_for_status()


def main():
    seen = load_seen()
    seen_set = set(seen)
    items = get_items()

    # 你已确认 Webhook 工作，因此第一次运行先推最近 5 条，方便你直接验证。
    if not seen:
        recent_items = items[:5]

        for news_id, text in reversed(recent_items):
            post_to_discord(text)
            seen.append(news_id)

        # 其余当前条目写入状态，不在下一轮重复推送旧消息
        seen.extend(
            news_id for news_id, _ in items
            if news_id not in set(seen)
        )

        save_seen(seen)
        print(f"首次运行：已推送最近 {len(recent_items)} 条快讯。")
        return

    new_items = [
        (news_id, text)
        for news_id, text in items
        if news_id not in seen_set
    ]

    # API 结果通常是新到旧，反过来发送使 Discord 内呈现旧到新
    for news_id, text in reversed(new_items):
        post_to_discord(text)
        seen.append(news_id)

    save_seen(seen)
    print(f"本次推送新快讯：{len(new_items)} 条。")


if __name__ == "__main__":
    main()
