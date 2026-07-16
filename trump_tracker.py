import json
import os
import re
import time
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

import requests
import deepl

DATA_URL = "https://ix.cnn.io/data/truth-social/truth_archive.json"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_TRUMP"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]
STATE_FILE = Path("seen_trump.json")

MAX_SEND_PER_RUN = 50
DISCORD_DELAY_SECONDS = 0.55
RETENTION_SECONDS = 7 * 24 * 3600

HEADERS = {"User-Agent": "Mozilla/5.0"}
MEDIA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://truthsocial.com/",
}

MAX_TRANSLATED_LEN = 1700
MAX_ORIGINAL_LEN = 1700

URL_PATTERN = re.compile(r"https?://\S+")

translator = deepl.Translator(DEEPL_API_KEY)


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


def extract_media_url(item):
    media = item.get("media")
    if not media:
        return None
    first = media[0]
    url = first if isinstance(first, str) else (first.get("url") or first.get("preview_url") if isinstance(first, dict) else None)
    if not url or url.lower().endswith((".mp4", ".mov", ".webm")):
        return None
    return url


def extract_pure_link(content):
    """如果这条帖子内容本质上只是一个链接(去掉链接后剩余文字为空),
    返回这个链接;否则返回None。纯链接帖子会被单独处理:跳过翻译,
    并放在Discord消息的顶层content字段以触发原生链接展开(视频/图片预览)。"""
    if not content:
        return None
    stripped = content.strip()
    urls = URL_PATTERN.findall(stripped)
    if not urls:
        return None
    remaining = URL_PATTERN.sub("", stripped).strip()
    return urls[0] if not remaining else None


def translate_text(text):
    """Translate English -> Chinese using DeepL. Never raises; falls back to
    the original text if the API call fails or times out, so a translation
    error can never crash the run or block the post from being sent."""
    if not text or not text.strip():
        return None
    try:
        result = translator.translate_text(
            text,
            source_lang="EN",
            target_lang="ZH",
        )
        translated = result.text.strip()
        return translated or None
    except deepl.DeepLException as exc:
        print(f"DeepL翻译失败,已回退为仅显示原文: {exc}")
        return None
    except Exception as exc:
        print(f"翻译过程中出现未知异常,已回退为仅显示原文: {exc}")
        return None


def build_description(content):
    """仅显示 DeepL 中文译文；翻译失败时回退显示原文。"""
    if not content:
        return "🖼️ [图片贴文]"

    # 纯链接不需要翻译，也没有可读的中文正文。
    if extract_pure_link(content):
        return content[:MAX_ORIGINAL_LEN]

    translated = translate_text(content)
    return (translated or content)[:3900]


def post_to_news(item):
    post_id = str(item.get("id", "")).strip()
    if not post_id:
        return None

    content = re.sub(r"<[^>]+>", " ", str(item.get("content", "") or "")).strip()
    content = re.sub(r"\s{2,}", " ", content)

    media_url = extract_media_url(item)
    if not content and not media_url:
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
        "media_url": media_url,
    }


def collect_new_posts(raw_posts, seen_ids):
    """O(n) single pass to dedupe + filter unseen posts, O(n log n) final
    sort by timestamp. Space is O(k) where k = number of *new* posts only."""
    collected = {}
    for item in raw_posts:
        post = post_to_news(item)
        if post is None or post["id"] in seen_ids:
            continue
        collected[post["id"]] = post
    return sorted(collected.values(), key=lambda p: p["timestamp"] or "")


def download_media(url):
    try:
        response = requests.get(url, headers=MEDIA_HEADERS, timeout=15)
        response.raise_for_status()
        ext = url.split(".")[-1].split("?")[0][:4] or "jpg"
        return response.content, f"media.{ext}"
    except requests.RequestException:
        return None, None


def post_to_discord(post):
    embed = {
        "color": 15158332,
        "description": build_description(post["content"]),
    }

    if post["timestamp"]:
        embed["timestamp"] = post["timestamp"]

    files = {}
    if post["media_url"]:
        media_bytes, filename = download_media(post["media_url"])
        if media_bytes:
            embed["image"] = {"url": f"attachment://{filename}"}
            files["file"] = (filename, media_bytes, "image/jpeg") 

    # 不传 username / avatar_url：
    # Discord 自动使用 webhook 后台配置的名称和头像。
    # 不传 embed.author：
    # Embed 卡片内部不会出现 “Donald J. Trump · @realDonaldTrump”。
    payload = {
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    pure_link = extract_pure_link(post["content"])
    if pure_link:
        # 纯链接贴文仍然放在顶层，保留 Discord 原生的视频/图片展开。
        payload["content"] = pure_link

    if files:
        response = requests.post(
            WEBHOOK_URL,
            data={"payload_json": json.dumps(payload, ensure_ascii=False)},
            files=files,
            timeout=30,
        )
    else:
        response = requests.post(
            WEBHOOK_URL,
            json=payload,
            timeout=30,
        )

    if response.status_code == 429:
        time.sleep(float(response.json().get("retry_after", 2)) + 1)
        return post_to_discord(post)

    response.raise_for_status()


def main():
    seen = load_state()
    is_first_run = not seen
    seen_ids = set(seen)

    raw_posts = fetch_posts()
    new_posts = collect_new_posts(raw_posts, seen_ids)

    to_send_iter = iter(()) if is_first_run else islice(new_posts, MAX_SEND_PER_RUN)

    sent_count = 0
    for post in to_send_iter:
        post_to_discord(post)
        sent_count += 1
        time.sleep(DISCORD_DELAY_SECONDS)

    now = time.time()
    for post in new_posts:
        seen[post["id"]] = now

    save_state(seen)
    print(f"检测到 {len(new_posts)} 条,已发送 {sent_count} 条。")


if __name__ == "__main__":
    main()
