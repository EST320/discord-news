import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_URL = "https://ix.cnn.io/data/truth-social/truth_archive.json"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_TRUMP"]
STATE_FILE = Path("seen_trump.json")

MAX_SEND_PER_RUN = 50
FIRST_RUN_SEND = 10
DISCORD_DELAY_SECONDS = 0.55
RETENTION_SECONDS = 7 * 24 * 3600

HEADERS = {"User-Agent": "Mozilla/5.0"}


def load_state():
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("seen", {})


def save_state(seen):
    cutoff = time.time() - RETENTION_SECONDS
    pruned = {k: v for k, v in seen.items() if v > cutoff}
    STATE_FILE.write_text(
        json.dumps({"seen": pruned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_posts():
    response = requests.get(DATA_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else data.get("posts", [])


def post_to_news(item):
    post_id = str(item.get("id", "")).strip()
    if not post_id:
        return None

    content = re.sub(r"<[^>]+>", " ", str(item.get("content", "") or "")).strip()
    content = re.sub(r"\s{2,}", " ", content)
    if not content:
        return None

    url = item.get("url") or f"https://truthsocial.com/@realDonaldTrump/{post_id}"
    created_at = item.get("created_at")

    timestamp = None
    if created_at:
        try:
            timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass

    return {
        "id": post_id,
        "content": content[:3900],
        "url": url,
        "timestamp": timestamp,
        "replies": item.get("replies_count", 0),
        "reblogs": item.get("reblogs_count", 0),
        "favourites": item.get("favourites_count", 0),
    }


def collect_new_posts(raw_posts, seen_ids):
    collected = {}
    for item in raw_posts:
        post = post_to_news(item)
        if post and post["id"] not in seen_ids:
            collected[post["id"]] = post
    return sorted(collected.values(), key=lambda p: p["timestamp"] or "")


def post_to_discord(post):
    text = f"[查看原贴]({post['url']})\n{post['content']}"
    text += f"\n\n💬 {post['replies']} · 🔁 {post['reblogs']} · ❤️ {post['favourites']}"

    embed = {
        "color": 15158332,
        "author": {"name": "Trump · Truth Social", "url": post["url"]},
        "description": text[:4096],
    }
    if post["timestamp"]:
        embed["timestamp"] = post["timestamp"]

    response = requests.post(
        WEBHOOK_URL,
        json={"username": "Trump Truth Tracker", "embeds": [embed], "allowed_mentions": {"parse": []}},
        timeout=30,
    )

    if response.status_code == 429:
        time.sleep(float(response.json().get("retry_after", 2)) + 1)
        return post_to_discord(post)

    response.raise_for_status()


def main():
    seen = load_state()
    seen_ids = set(seen)

    raw_posts = fetch_posts()
    new_posts = collect_new_posts(raw_posts, seen_ids)

    to_send = new_posts[-FIRST_RUN_SEND:] if not seen_ids else new_posts[:MAX_SEND_PER_RUN]

    for post in to_send:
        post_to_discord(post)
        time.sleep(DISCORD_DELAY_SECONDS)

    now = time.time()
    for post in (new_posts if not seen_ids else to_send):
        seen[post["id"]] = now

    save_state(seen)
    print(f"检测到 {len(new_posts)} 条,已发送 {len(to_send)} 条。")


if __name__ == "__main__":
    main()
