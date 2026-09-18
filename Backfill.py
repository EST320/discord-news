"""一次性补发脚本：把最近 N 小时内、还没推送过的快讯补发到 Discord。

这个文件完全独立，不修改也不依赖 main.py / main_a.py / main_hk.py 的任何逻辑。
它只做三件事：
  1. 按频道抓取最近 N 小时的快讯
  2. 跳过 seen*.json 里已经记录过的 ID（不会重复推送）
  3. 用与主脚本完全一致的格式推送，并把补发的 ID 写回 seen*.json

用法（本地）：
    HOURS=6 CHANNELS=us,hk DRY_RUN=true python backfill.py
或者直接在 GitHub 上用 backfill.yml 手动触发。

跑完之后这个文件可以删掉，也可以留着，下次万一再断档直接用。
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"

# 每个频道的配置，与三个主脚本里的常量逐项对应（颜色 / author / 状态文件 / webhook 变量）
CHANNELS = {
    "us": {
        "channel": "us-stock-channel",
        "live_url": "https://wallstreetcn.com/live/us-stock",
        "state_file": "seen.json",
        "webhook_env": "DISCORD_WEBHOOK_URL",
        "color": 5793266,
        "author": "wallstreetcn · us-stock",
    },
    "a": {
        "channel": "a-stock-channel",
        "live_url": "https://wallstreetcn.com/live/a-stock",
        "state_file": "seen_a.json",
        "webhook_env": "DISCORD_WEBHOOK_URL_A",
        "color": 15548997,
        "author": "wallstreetcn · a-stock",
    },
    "hk": {
        "channel": "hk-stock-channel",
        "live_url": "https://wallstreetcn.com/live/hk-stock",
        "state_file": "seen_hk.json",
        "webhook_env": "DISCORD_WEBHOOK_URL_HK",
        "color": 3066993,
        "author": "wallstreetcn · hk-stock",
    },
}

HOURS = float(os.environ.get("HOURS", "6"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")
WANTED = [c.strip() for c in os.environ.get("CHANNELS", "us,a,hk").split(",") if c.strip()]

PAGE_SIZE = 100
MAX_PAGES = 15
DISCORD_DELAY_SECONDS = 0.55
DISCORD_MAX_RETRIES = 5
RETENTION_SECONDS = 12 * 3600

SESSION = requests.Session()


def headers_for(live_url):
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Referer": live_url,
        "Origin": "https://wallstreetcn.com",
    }


def load_seen(state_file):
    path = Path(state_file)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("seen", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_seen(state_file, seen):
    cutoff = time.time() - RETENTION_SECONDS
    pruned = {k: v for k, v in seen.items() if v > cutoff}
    Path(state_file).write_text(
        json.dumps({"seen": pruned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def item_to_news(item):
    """与主脚本 item_to_news 完全相同的解析逻辑，保证输出格式一致。"""
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


def fetch_window(cfg, since_ts):
    """翻页抓取，直到时间早于 since_ts 为止。返回窗口内的全部快讯（新->旧）。"""
    collected = {}
    cursor = 0
    headers = headers_for(cfg["live_url"])

    for _ in range(MAX_PAGES):
        params = {
            "channel": cfg["channel"],
            "client": "pc",
            "cursor": cursor,
            "limit": PAGE_SIZE,
        }
        response = SESSION.get(API_URL, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json().get("data", {})
        raw_items = payload.get("items", [])
        if not raw_items:
            break

        page_news = [n for n in (item_to_news(x) for x in raw_items) if n]
        oldest = None
        for news in page_news:
            if news["display_ts"] is None:
                continue
            oldest = news["display_ts"] if oldest is None else min(oldest, news["display_ts"])
            if news["display_ts"] >= since_ts:
                collected[news["id"]] = news

        if oldest is not None and oldest < since_ts:
            break
        next_cursor = payload.get("next_cursor", 0)
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return list(collected.values())


def post_to_discord(cfg, webhook_url, news, attempt=1):
    """payload 构造与主脚本 post_to_discord 逐字段一致。"""
    live_url = cfg["live_url"]
    safe_title = re.sub(r"\s{2,}", " ", news["title"].replace("\n", " ").replace("\r", " ")).strip()
    text = f"[{safe_title}]({live_url})"

    if news["content"]:
        body = re.sub(r"\n{2,}", "\n", news["content"].replace("\r\n", "\n").replace("\r", "\n"))
        text += "\n" + body.replace("\n", "\n\n")

    embed = {
        "color": cfg["color"],
        "author": {"name": cfg["author"], "url": live_url},
        "description": text[:4096],
    }

    if news["timestamp"]:
        embed["timestamp"] = news["timestamp"]

    response = SESSION.post(
        webhook_url,
        json={"embeds": [embed], "allowed_mentions": {"parse": []}},
        timeout=30,
    )

    if response.status_code == 429:
        if attempt >= DISCORD_MAX_RETRIES:
            raise RuntimeError(f"Discord 连续 {attempt} 次返回 429，放弃这条")
        try:
            retry_after = float(response.json().get("retry_after", 2))
        except (ValueError, TypeError):
            retry_after = 2.0
        time.sleep(retry_after + 1)
        return post_to_discord(cfg, webhook_url, news, attempt + 1)

    response.raise_for_status()


def run_channel(key):
    cfg = CHANNELS[key]
    since_ts = time.time() - HOURS * 3600

    webhook_url = os.environ.get(cfg["webhook_env"], "")
    if not webhook_url and not DRY_RUN:
        print(f"[{key}] 跳过：环境变量 {cfg['webhook_env']} 未设置")
        return

    seen = load_seen(cfg["state_file"])
    seen_ids = set(seen)

    window = fetch_window(cfg, since_ts)
    pending = [n for n in window if n["id"] not in seen_ids]
    # 由旧到新发送，和主脚本的顺序一致
    pending.sort(key=lambda n: n["display_ts"] or 0)

    print(f"[{key}] 最近 {HOURS} 小时共 {len(window)} 条，其中未推送 {len(pending)} 条")

    if DRY_RUN:
        for n in pending:
            when = datetime.fromtimestamp(n["display_ts"], tz=timezone.utc).astimezone()
            print(f"      [试运行] {when:%m-%d %H:%M}  {n['title'][:60]}")
        return

    sent = 0
    try:
        for news in pending:
            post_to_discord(cfg, webhook_url, news)
            seen[news["id"]] = time.time()
            sent += 1
            time.sleep(DISCORD_DELAY_SECONDS)
    finally:
        save_seen(cfg["state_file"], seen)
        print(f"[{key}] 已补发 {sent} 条，状态已写回 {cfg['state_file']}")


def main():
    mode = "试运行（不实际发送）" if DRY_RUN else "正式补发"
    print(f"=== 补发窗口：最近 {HOURS} 小时 | 频道：{','.join(WANTED)} | {mode} ===")
    for key in WANTED:
        if key not in CHANNELS:
            print(f"未知频道 {key}，可选：us / a / hk")
            continue
        run_channel(key)


if __name__ == "__main__":
    main()
