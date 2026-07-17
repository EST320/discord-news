import html
import json
import os
import re
import time
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

import deepl
import requests
from playwright.sync_api import sync_playwright


# =========================
# 配置
# =========================

DATA_URL = "https://ix.cnn.io/data/truth-social/truth_archive.json"

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_TRUMP"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]

STATE_FILE = Path("seen_trump.json")
SCREENSHOT_DIR = Path("trump_screenshots")

MAX_SEND_PER_RUN = 50
DISCORD_DELAY_SECONDS = 0.55

# 已发送帖子 ID 保存 7 天，用于防重复发送。
RETENTION_SECONDS = 7 * 24 * 3600

# 只首次发送发布时间在最近两小时内的帖子。
# API 中重新出现的旧帖，即使 seen 里没有，也不会补发。
MAX_POST_AGE_SECONDS = 2 * 3600

# 首次运行时不补发：只写入 seen_trump.json。
# 改成 True 时，首次运行会发送近期帖子。
SEND_ON_FIRST_RUN = False

# 截图页面等待时间。
SCREENSHOT_WAIT_MS = 4000

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

MEDIA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://truthsocial.com/",
}

MAX_TRANSLATED_LEN = 3900

translator = deepl.Translator(DEEPL_API_KEY)


# =========================
# seen 状态
# =========================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        seen = payload.get("seen", {})
        return seen if isinstance(seen, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取 seen 状态失败，将按空状态处理：{exc}")
        return {}


def save_state(seen):
    cutoff = time.time() - RETENTION_SECONDS

    pruned = {
        str(post_id): sent_at
        for post_id, sent_at in seen.items()
        if isinstance(sent_at, (int, float)) and sent_at > cutoff
    }

    STATE_FILE.write_text(
        json.dumps({"seen": pruned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# =========================
# CNN / Truth Social 数据
# =========================

def fetch_posts():
    response = requests.get(DATA_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()

    data = response.json()

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        return data.get("posts", [])

    raise RuntimeError(f"数据格式异常：{type(data)}")


def parse_timestamp(value):
    if not value:
        return None, None

    if isinstance(value, (int, float)):
        ts = float(value)
        return ts, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    if not isinstance(value, str):
        return None, None

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.timestamp(), dt.astimezone(timezone.utc).isoformat()

    except ValueError:
        return None, None


def clean_html_content(value):
    text = str(value or "")
    text = html.unescape(text)

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)

    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def extract_media_url(item):
    media = item.get("media")

    if not isinstance(media, list) or not media:
        return None

    first = media[0]

    if isinstance(first, str):
        url = first
    elif isinstance(first, dict):
        url = first.get("url") or first.get("preview_url")
    else:
        return None

    if not isinstance(url, str) or not url.strip():
        return None

    url = url.strip().replace("&amp;", "&")

    # 视频不能直接作为 Discord embed image。
    if url.lower().split("?")[0].endswith((".mp4", ".mov", ".webm")):
        return None

    return url


def post_to_news(item):
    post_id = str(item.get("id", "")).strip()
    if not post_id:
        return None

    content = clean_html_content(item.get("content", ""))
    media_url = extract_media_url(item)

    if not content and not media_url:
        return None

    created_ts, timestamp = parse_timestamp(item.get("created_at"))

    post_url = str(item.get("url") or "").strip()
    if not post_url.startswith("http"):
        post_url = f"https://truthsocial.com/@realDonaldTrump/{post_id}"

    return {
        "id": post_id,
        "content": content[:3900],
        "url": post_url,
        "created_ts": created_ts,
        "timestamp": timestamp,
        "media_url": media_url,
    }


def collect_new_posts(raw_posts, seen_ids):
    now = time.time()
    collected = {}

    for item in raw_posts:
        post = post_to_news(item)

        if post is None:
            continue

        if post["id"] in seen_ids:
            continue

        # 无法解析时间时，不发，避免历史帖误推送。
        if post["created_ts"] is None:
            continue

        age_seconds = now - post["created_ts"]

        # 时间未来超过 10 分钟，视为异常。
        if age_seconds < -600:
            continue

        # 超过两小时的帖子不补发。
        if age_seconds > MAX_POST_AGE_SECONDS:
            continue

        collected[post["id"]] = post

    return sorted(
        collected.values(),
        key=lambda post: post["created_ts"],
    )


# =========================
# DeepL 翻译
# =========================

def translate_text(text):
    """英文翻译为简体中文；失败时返回 None，由外层回退原文。"""
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
        print(f"DeepL 翻译失败，将回退原文：{exc}")
        return None

    except Exception as exc:
        print(f"翻译出现未知错误，将回退原文：{exc}")
        return None


def build_description(content):
    """
    Embed 内仅显示中文翻译。
    如果原帖是纯链接，截图中会保留链接，因此 Embed 不额外显示链接，
    防止 Discord 再生成不可控的链接预览。
    """
    if not content:
        return None

    translated = translate_text(content)

    # DeepL 失败时回退原文，避免整条帖子丢失。
    return (translated or content)[:MAX_TRANSLATED_LEN]


# =========================
# 截图
# =========================

def screenshot_truth_post(post):
    """
    打开 Truth Social 原帖 URL 并截图。
    优先尝试截图帖文卡片；找不到卡片时退回整页截图。
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    screenshot_path = SCREENSHOT_DIR / f"truth_{post['id']}.png"

    selectors = [
        "article",
        '[data-testid="status"]',
        '[data-testid="status-content"]',
        "main article",
    ]

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            page = browser.new_page(
                viewport={"width": 980, "height": 1300},
                device_scale_factor=1,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
            )

            try:
                page.goto(
                    post["url"],
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                page.wait_for_timeout(SCREENSHOT_WAIT_MS)

                # 尝试只截取帖子卡片，避免截到多余页面内容。
                for selector in selectors:
                    locator = page.locator(selector).first

                    try:
                        if locator.count() and locator.is_visible(timeout=1500):
                            locator.screenshot(
                                path=str(screenshot_path),
                                timeout=15000,
                            )
                            break
                    except Exception:
                        continue

                # 未找到卡片时，截图整个页面作为后备。
                if not screenshot_path.exists():
                    page.screenshot(
                        path=str(screenshot_path),
                        full_page=True,
                    )

            finally:
                browser.close()

        if screenshot_path.exists() and screenshot_path.stat().st_size > 0:
            return screenshot_path

    except Exception as exc:
        print(f"帖子截图失败：{post['id']}，{exc}")

    screenshot_path.unlink(missing_ok=True)
    return None


def download_media(url):
    """截图失败时，使用帖子自带图片作为后备。"""
    try:
        response = requests.get(
            url,
            headers=MEDIA_HEADERS,
            timeout=30,
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()

        if "png" in content_type:
            extension = "png"
        elif "webp" in content_type:
            extension = "webp"
        elif "gif" in content_type:
            extension = "gif"
        else:
            extension = "jpg"

        return response.content, f"media.{extension}", content_type

    except requests.RequestException as exc:
        print(f"下载帖子媒体失败：{exc}")
        return None, None, None


# =========================
# Discord 发送
# =========================

def post_to_discord(post):
    description = build_description(post["content"])

    embed = {
        "color": 15158332,
    }

    # 纯图片帖允许没有 description。
    if description:
        embed["description"] = description

    if post["timestamp"]:
        embed["timestamp"] = post["timestamp"]

    screenshot_path = screenshot_truth_post(post)

    payload = {
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    # 重点：
    # 1. 不传 username / avatar_url，使用 Discord webhook 后台设置。
    # 2. 不传 author，因此没有 Embed 内部作者行。
    # 3. 不传 content，因此不会在消息顶部出现单独的 URL。
    # 4. 截图作为 attachment 进入 Embed 的 image 区域。

    try:
        if screenshot_path:
            embed["image"] = {
                "url": f"attachment://{screenshot_path.name}"
            }

            with screenshot_path.open("rb") as image_file:
                response = requests.post(
                    WEBHOOK_URL,
                    data={
                        "payload_json": json.dumps(
                            payload,
                            ensure_ascii=False,
                        )
                    },
                    files={
                        "file": (
                            screenshot_path.name,
                            image_file,
                            "image/png",
                        )
                    },
                    timeout=90,
                )

        else:
            # Truth Social 页面截图失败时，尝试上传原帖自带媒体。
            media_bytes = None
            filename = None
            content_type = None

            if post["media_url"]:
                media_bytes, filename, content_type = download_media(
                    post["media_url"]
                )

            if media_bytes:
                embed["image"] = {
                    "url": f"attachment://{filename}"
                }

                response = requests.post(
                    WEBHOOK_URL,
                    data={
                        "payload_json": json.dumps(
                            payload,
                            ensure_ascii=False,
                        )
                    },
                    files={
                        "file": (
                            filename,
                            media_bytes,
                            content_type or "image/jpeg",
                        )
                    },
                    timeout=60,
                )

            else:
                # 完全没有图时，只发送中文译文。
                response = requests.post(
                    WEBHOOK_URL,
                    json=payload,
                    timeout=30,
                )

        if response.status_code == 429:
            retry_after = float(response.json().get("retry_after", 2))
            time.sleep(retry_after + 1)
            return post_to_discord(post)

        response.raise_for_status()

    finally:
        if screenshot_path:
            screenshot_path.unlink(missing_ok=True)


# =========================
# 主程序
# =========================

def main():
    seen = load_state()
    seen_ids = set(seen)

    raw_posts = fetch_posts()
    new_posts = collect_new_posts(raw_posts, seen_ids)

    # 首次部署只建档，不补发旧内容。
    if not seen_ids and not SEND_ON_FIRST_RUN:
        now = time.time()

        for post in new_posts:
            seen[post["id"]] = now

        save_state(seen)

        print(
            f"首次初始化：记录 {len(new_posts)} 条近期帖子，"
            "未发送历史内容。"
        )
        return

    posts_to_send = list(islice(new_posts, MAX_SEND_PER_RUN))

    sent_count = 0

    for post in posts_to_send:
        post_to_discord(post)
        seen[post["id"]] = time.time()
        sent_count += 1
        time.sleep(DISCORD_DELAY_SECONDS)

    save_state(seen)

    print(
        f"检测到 {len(new_posts)} 条新帖子，"
        f"已发送 {sent_count} 条。"
    )


if __name__ == "__main__":
    main()
