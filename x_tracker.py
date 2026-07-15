import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import snscrape.modules.twitter as sntwitter

WEBHOOK_MAP = {
    "TrendSpider": os.environ["DISCORD_WEBHOOK_URL_TRENDSPIDER"],
    "StockOptionCole": os.environ["DISCORD_WEBHOOK_URL_COLE"],
    "ArtofSpecuycky": os.environ["DISCORD_WEBHOOK_URL_SPECUYCKY"],
}

STATE_FILE = Path("seen_x.json")

MAX_POSTS_PER_USER = 5
MAX_SEND_PER_RUN_PER_USER = 30
DISCORD_DELAY_SECONDS = 0.55
RETENTION_SECONDS = 7 * 24 * 3600


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


def fetch_user_posts(username):
    scraper = sntwitter.TwitterUserScraper(username)
    posts = []
    try:
        for i, tweet in enumerate(scraper.get_items()):
            if i >= MAX_POSTS_PER_USER:
                break
            posts.append({
                "id": str(tweet.id),
                "username": username,
                "display_name": tweet.user.displayname,
                "avatar": tweet.user.profileImageUrl,
                "content": tweet.rawContent,
                "url": tweet.url,
                "timestamp": tweet.date.astimezone(timezone.utc).isoformat(),
                "likes": tweet.likeCount or 0,
                "retweets": tweet.retweetCount or 0,
                "replies": tweet.replyCount or 0,
                "media_url": tweet.media[0].fullUrl if tweet.media else None,
            })
    except Exception as e:
        print(f"抓取 @{username} 失败: {e}")
    return posts


def collect_new_posts_by_user(username, seen_ids):
    collected = {}
    for post in fetch_user_posts(username):
        if post["id"] not in seen_ids:
            collected[post["id"]] = post
    return sorted(collected.values(), key=lambda p: p["timestamp"])


def post_to_discord(webhook_url, post):
    embed = {
        "color": 1942002,
        "author": {
            "name": f"{post['display_name']} · @{post['username']}",
            "url": post["url"],
            "icon_url": post["avatar"],
        },
        "description": post["content"][:4000],
        "footer": {
            "text": f"💬 {post['replies']}   🔁 {post['retweets']}   ❤️ {post['likes']}   ·  X",
        },
        "url": post["url"],
        "timestamp": post["timestamp"],
    }

    if post["media_url"]:
        embed["image"] = {"url": post["media_url"]}

    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    response = requests.post(webhook_url, json=payload, timeout=30)

    if response.status_code == 429:
        time.sleep(float(response.json().get("retry_after", 2)) + 1)
        return post_to_discord(webhook_url, post)

    response.raise_for_status()


def process_user(username, webhook_url, seen, is_first_run):
    seen_ids = set(seen)
    new_posts = collect_new_posts_by_user(username, seen_ids)
    to_send = [] if is_first_run else new_posts[:MAX_SEND_PER_RUN_PER_USER]

    for post in to_send:
        post_to_discord(webhook_url, post)
        time.sleep(DISCORD_DELAY_SECONDS)

    now = time.time()
    for post in new_posts:
        seen[post["id"]] = now

    print(f"@{username}: 检测到 {len(new_posts)} 条,已发送 {len(to_send)} 条。")


def main():
    seen = load_state()
    is_first_run = not seen

    for username, webhook_url in WEBHOOK_MAP.items():
        process_user(username, webhook_url, seen, is_first_run)

    save_state(seen)


if __name__ == "__main__":
    main()
