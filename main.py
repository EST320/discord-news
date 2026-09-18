import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# 华尔街见闻前端已迁移到 awtmt.com 这个 API 域名；旧的 api-prod.wallstreetcn.com
# 从 2026-09-17 起对机房 IP（含 GitHub Actions）直接丢包，连不上。
# 新域名路径、参数、返回结构与旧接口完全一致。
API_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
LIVE_URL = "https://wallstreetcn.com/live/us-stock"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = Path("seen.json")

CHANNEL = "us-stock-channel"
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_SEND_PER_RUN = 100
FIRST_RUN_SEND = 10
DISCORD_DELAY_SECONDS = 0.55
RETENTION_SECONDS = 12 * 3600
MAX_NEWS_AGE_SECONDS = 15 * 60
API_MAX_RETRIES = 3
API_RETRY_BACKOFF_SECONDS = 3
DISCORD_MAX_RETRIES = 5

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": LIVE_URL,
    "Origin": "https://wallstreetcn.com",
}

# 单个 Session 复用 TCP/TLS 连接：一次运行里要发 1 次 API 请求 + N 条 Discord 消息，
# 复用连接比每次重新握手快，也更不容易被限流。
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("seen", {})
    except (json.JSONDecodeError, OSError) as exc:
        # 状态文件损坏时不要直接崩掉；当成空状态继续跑，本轮会走首次运行逻辑
        print(f"状态文件读取失败，按空状态处理：{exc!r}")
        return {}


def save_state(seen):
    cutoff = time.time() - RETENTION_SECONDS
    pruned = {k: v for k, v in seen.items() if v > cutoff}
    STATE_FILE.write_text(
        json.dumps({"seen": pruned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def api_get(cursor=0):
    params = {"channel": CHANNEL, "client": "pc", "cursor": cursor, "limit": PAGE_SIZE}
    last_exc = None
    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            response = SESSION.get(API_URL, params=params, timeout=20)
            response.raise_for_status()
            payload = response.json().get("data", {})
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise RuntimeError(f"API items 格式异常：{type(items)}")
            return items, payload.get("next_cursor", 0)
        except (requests.exceptions.RequestException, ValueError, RuntimeError) as exc:
            last_exc = exc
            if attempt < API_MAX_RETRIES:
                wait = API_RETRY_BACKOFF_SECONDS * attempt
                print(f"api_get 第 {attempt} 次失败（{exc!r}），{wait}s 后重试")
                time.sleep(wait)
    raise last_exc


def item_to_news(item):
    news_id = str(item.get("id", "")).strip()
    if not news_id:
        return None

    title = str(item.get("title", "")).strip()
    content = str(item.get("content_text", "")).strip()
    content = re.sub(r"\s*[（(]来自华尔街见闻APP[）)]\s*$", "", content).strip()

    if not title:
        bracket_match = re.match(r"^【([^】]+)】\s*", content)
        if bracket_match:
            title = bracket_match.group(1)
            content = content[bracket_match.end():].strip()
        else:
            lines = content.split("\n", 1)
            title = lines[0].strip() or "华尔街见闻快讯"
            content = lines[1].strip() if len(lines) > 1 else ""

    title = re.sub(r"\s{2,}", " ", title.replace("\n", " ").replace("\r", " ")).strip()[:250]
    content = content[:3900]

    display_time = item.get("display_time")
    display_ts = display_time if isinstance(display_time, (int, float)) else None
    timestamp = (
        datetime.fromtimestamp(display_ts, tz=timezone.utc).isoformat()
        if display_ts is not None
        else None
    )

    return {
        "id": news_id,
        "title": title,
        "content": content,
        "timestamp": timestamp,
        "display_ts": display_ts,
    }


def collect_new_items(seen_ids):
    collected = {}
    cursor = 0
    now = time.time()

    for _ in range(MAX_PAGES):
        raw_items, next_cursor = api_get(cursor)
        if not raw_items:
            break

        page_news = [n for n in (item_to_news(x) for x in raw_items) if n]
        for news in page_news:
            if news["display_ts"] is None:
                continue
            if now - news["display_ts"] > MAX_NEWS_AGE_SECONDS:
                continue
            if news["id"] not in seen_ids:
                collected[news["id"]] = news

        if any(n["id"] in seen_ids for n in page_news):
            break

        # 本页最旧的一条都已超出时间窗口，再往后翻只会更旧，没有必要继续请求
        oldest_ts = min(
            (n["display_ts"] for n in page_news if n["display_ts"] is not None),
            default=None,
        )
        if oldest_ts is not None and now - oldest_ts > MAX_NEWS_AGE_SECONDS:
            break

        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return list(reversed(collected.values()))


def post_to_discord(news, attempt=1):
    safe_title = re.sub(r"\s{2,}", " ", news["title"].replace("\n", " ").replace("\r", " ")).strip()
    text = f"[{safe_title}]({LIVE_URL})"

    if news["content"]:
        body = re.sub(r"\n{2,}", "\n", news["content"].replace("\r\n", "\n").replace("\r", "\n"))
        text += "\n" + body.replace("\n", "\n\n")

    embed = {
        "color": 5793266,
        "author": {"name": "wallstreetcn · us-stock", "url": LIVE_URL},
        "description": text[:4096],
    }

    if news["timestamp"]:
        embed["timestamp"] = news["timestamp"]

    response = SESSION.post(
        WEBHOOK_URL,
        json={"embeds": [embed], "allowed_mentions": {"parse": []}},
        timeout=30,
    )

    if response.status_code == 429:
        # 原来是无限递归，Discord 持续限流时会把栈打爆；这里限定重试次数
        if attempt >= DISCORD_MAX_RETRIES:
            raise RuntimeError(f"Discord 连续 {attempt} 次返回 429，放弃这条")
        try:
            retry_after = float(response.json().get("retry_after", 2))
        except (ValueError, TypeError):
            retry_after = 2.0
        time.sleep(retry_after + 1)
        return post_to_discord(news, attempt + 1)

    response.raise_for_status()


def main():
    seen = load_state()
    seen_ids = set(seen)
    first_run = not seen_ids

    try:
        new_items = collect_new_items(seen_ids)
    except (requests.exceptions.RequestException, ValueError, RuntimeError) as exc:
        # 抓取失败就跳过这一轮，两分钟后的下一次触发会补上，不让整个 job 报红
        print(f"抓取失败，跳过本次运行：{exc!r}")
        return

    to_send = new_items[-FIRST_RUN_SEND:] if first_run else new_items[:MAX_SEND_PER_RUN]

    sent = 0
    try:
        for news in to_send:
            post_to_discord(news)
            # 发一条记一条：中途若抛异常，已发出的不会在下一轮被重复推送
            seen[news["id"]] = time.time()
            sent += 1
            time.sleep(DISCORD_DELAY_SECONDS)
    finally:
        if first_run:
            # 首次运行只补发最近几条，其余历史标记为已读，避免下一轮被当成新消息
            now = time.time()
            for news in new_items:
                seen.setdefault(news["id"], now)
        # 即使上面中途失败也要落盘，保证去重状态不丢
        save_state(seen)
        print(f"检测到 {len(new_items)} 条，已发送 {sent} 条。")


if __name__ == "__main__":
    main()
