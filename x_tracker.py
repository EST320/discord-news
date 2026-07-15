import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import snscrape.modules.twitter as sntwitter

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_X"]
STATE_FILE = Path("seen_x.json")

USERNAMES = ["TrendSpider", "StockOptionCole", "ArtofSpecuycky"]
MAX_POSTS_PER_USER = 5
MAX_SEND_PER_RUN = 30
DISCORD_DELAY_SECONDS = 0.55
RETENTION_SECONDS = 7 * 24 * 3600

AVATAR_MAP = {}


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


def collect_new_posts(seen_ids):
    collected = {}
    for username in USERNAMES:
        for post in fetch_user_posts(username):
            if post["id"] not in seen_ids:
                collected[post["id"]] = post
    return sorted(collected.values(), key=lambda p: p["timestamp"])


def post_to_discord(post):
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
    response = requests.post(WEBHOOK_URL, json=payload, timeout=30)

    if response.status_code == 429:
        time.sleep(float(response.json().get("retry_after", 2)) + 1)
        return post_to_discord(post)

    response.raise_for_status()


def main():
    seen = load_state()
    is_first_run = not seen
    seen_ids = set(seen)

    new_posts = collect_new_posts(seen_ids)
    to_send = [] if is_first_run else new_posts[:MAX_SEND_PER_RUN]

    for post in to_send:
        post_to_discord(post)
        time.sleep(DISCORD_DELAY_SECONDS)

    now = time.time()
    for post in new_posts:
        seen[post["id"]] = now

    save_state(seen)
    print(f"检测到 {len(new_posts)} 条,已发送 {len(to_send)} 条。")


if __name__ == "__main__":
    main()
