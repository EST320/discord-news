import hashlib
import html
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from urllib.parse import urlparse

import deepl
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps


# ============================================================
# 配置
# ============================================================

TEST_MODE = False
DATA_URL = "https://ix.cnn.io/data/truth-social/truth_archive.json"

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_TRUMP"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]

STATE_FILE = Path("seen_trump.json")
CARD_DIR = Path("trump_cards")

MAX_SEND_PER_RUN = 20
DISCORD_DELAY_SECONDS = 0.8

MAX_POST_AGE_SECONDS = 12 * 3600
RETENTION_SECONDS = 30 * 24 * 3600
SEND_ON_FIRST_RUN = False

MAX_TRANSLATED_LEN = 4000
MAX_CARD_TEXT_LEN = 6000
MAX_BODY_LINES = 60

CARD_DISPLAY_NAME = "Donald J. Trump"
CARD_HANDLE = "@realDonaldTrump"

FALLBACK_AVATAR_URL = (
    "https://static-assets-1.truthsocial.com/tmtg:prime-ts-assets/"
    "accounts/avatars/107/780/257/626/128/497/original/454286ac07a6f6e6.jpeg"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

MEDIA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://truthsocial.com/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}

translator = deepl.Translator(DEEPL_API_KEY)


# ============================================================
# 状态：ID 去重 + 内容哈希去重
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {"seen": {}, "hashes": {}}

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        seen = data.get("seen", {})
        hashes = data.get("hashes", {})

        if not isinstance(seen, dict):
            seen = {}
        if not isinstance(hashes, dict):
            hashes = {}

        return {"seen": seen, "hashes": hashes}

    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取状态文件失败，将以空状态启动：{exc}")
        return {"seen": {}, "hashes": {}}


def save_state(state):
    cutoff = time.time() - RETENTION_SECONDS

    pruned_seen = {
        k: v for k, v in state["seen"].items()
        if isinstance(v, (int, float)) and v > cutoff
    }
    pruned_hashes = {
        k: v for k, v in state["hashes"].items()
        if isinstance(v, (int, float)) and v > cutoff
    }

    STATE_FILE.write_text(
        json.dumps(
            {"seen": pruned_seen, "hashes": pruned_hashes},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# 数据抓取与帖子解析
# ============================================================

def fetch_posts():
    response = requests.get(DATA_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        posts = payload.get("posts", [])
        return posts if isinstance(posts, list) else []

    raise RuntimeError(f"CNN 数据格式异常：{type(payload)}")


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
        dt_utc = dt.astimezone(timezone.utc)
        return dt_utc.timestamp(), dt_utc.isoformat()
    except ValueError:
        return None, None


def clean_html_content(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_first_media(item):
    media = item.get("media")
    if not isinstance(media, list) or not media:
        return None

    first = media[0]

    if isinstance(first, str):
        url = first.strip().replace("&amp;", "&")
        preview_url = None
    elif isinstance(first, dict):
        url = str(first.get("url") or "").strip().replace("&amp;", "&")
        preview_url = str(
            first.get("preview_url")
            or first.get("preview")
            or first.get("thumbnail_url")
            or ""
        ).strip().replace("&amp;", "&")
    else:
        return None

    if not url:
        return None

    path = urlparse(url).path.lower()

    if path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
        media_type = "image"
    elif path.endswith((".mp4", ".mov", ".webm", ".m3u8")):
        media_type = "video"
    else:
        media_type = "unknown"

    return {"url": url, "type": media_type, "preview_url": preview_url or None}


def get_avatar_url(item):
    account = item.get("account") or item.get("user") or {}
    if not isinstance(account, dict):
        account = {}

    candidates = (
        item.get("avatar"),
        item.get("avatar_url"),
        account.get("avatar"),
        account.get("avatar_url"),
        account.get("profile_image_url"),
    )

    for value in candidates:
        if isinstance(value, str) and value.strip().startswith("http"):
            return value.strip()

    return FALLBACK_AVATAR_URL


def make_content_hash(content, media):
    normalized = re.sub(r"\s+", " ", content or "").strip().lower()
    media_key = ""
    if media:
        media_key = (media.get("preview_url") or media.get("url") or "").strip().lower()
    raw = f"{normalized}|{media_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def item_to_post(item):
    post_id = str(item.get("id") or item.get("status_id") or "").strip()
    if not post_id:
        return None

    content = clean_html_content(
        item.get("content") or item.get("text") or item.get("body") or ""
    )
    media = get_first_media(item)

    if not content and not media:
        return None

    created_ts, timestamp = parse_timestamp(
        item.get("created_at")
        or item.get("createdAt")
        or item.get("published_at")
        or item.get("timestamp")
    )

    post_url = str(
        item.get("url") or item.get("status_url") or item.get("permalink") or ""
    ).strip()

    if not post_url.startswith("http"):
        post_url = f"https://truthsocial.com/@realDonaldTrump/{post_id}"

    return {
        "id": post_id,
        "content": content,
        "url": post_url,
        "created_ts": created_ts,
        "timestamp": timestamp,
        "media": media,
        "avatar_url": get_avatar_url(item),
        "content_hash": make_content_hash(content, media),
    }


def collect_new_posts(raw_posts, state):
    now = time.time()
    seen_ids = set(state["seen"])
    seen_hashes = set(state["hashes"])
    collected = {}

    for raw_item in raw_posts:
        post = item_to_post(raw_item)
        if post is None:
            continue
        if post["id"] in seen_ids:
            continue
        if post["content_hash"] in seen_hashes:
            print(f"跳过重复正文：{post['id']}")
            continue
        if post["created_ts"] is None:
            print(f"跳过无法解析时间的帖子：{post['id']}")
            continue

        age_seconds = now - post["created_ts"]

        if age_seconds < -600:
            print(f"跳过时间异常帖子：{post['id']}")
            continue
        if age_seconds > MAX_POST_AGE_SECONDS:
            continue

        collected[post["id"]] = post

    return sorted(collected.values(), key=lambda post: post["created_ts"])


# ============================================================
# DeepL 中文翻译
# ============================================================

def translate_text(text):
    if not text or not text.strip():
        return None

    try:
        result = translator.translate_text(text, source_lang="EN", target_lang="ZH")
        return result.text.strip() or None
    except deepl.DeepLException as exc:
        print(f"DeepL 翻译失败，回退英文原文：{exc}")
        return None
    except Exception as exc:
        print(f"翻译未知错误，回退英文原文：{exc}")
        return None


def build_description(post):
    if post["content"]:
        translated = translate_text(post["content"])
        return (translated or post["content"])[:MAX_TRANSLATED_LEN]
    if post["media"] and post["media"]["type"] == "video":
        return "特朗普发布了一段视频。"
    if post["media"]:
        return "特朗普发布了一张图片。"
    return "特朗普发布了一条帖子。"


def split_text(text, limit):
    if len(text) <= limit:
        return [text]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


# ============================================================
# 生成"原帖卡片图"
# ============================================================

def get_font(size, bold=False):
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
    )

    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


FONT_NAME = get_font(28, bold=True)
FONT_HANDLE = get_font(21, bold=False)
FONT_BODY = get_font(26, bold=False)
FONT_META = get_font(19, bold=False)
FONT_VIDEO = get_font(26, bold=True)


def download_image(url, timeout=20):
    if not url:
        return None
    try:
        response = requests.get(url, headers=MEDIA_HEADERS, timeout=timeout)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception as exc:
        print(f"图片下载失败：{exc}")
        return None


def draw_default_avatar(size=72):
    canvas = Image.new("RGB", (size, size), "#F7F7F7")
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((0, 0, size - 1, size - 1), fill="#EAEAEA")
    draw.pieslice((0, 0, size - 1, size - 1), start=90, end=270, fill="#1E5AA8")
    draw.pieslice((0, 0, size - 1, size - 1), start=270, end=90, fill="#C9272C")
    draw.ellipse((size * 0.34, size * 0.22, size * 0.66, size * 0.55), fill="#F2C6A0")
    draw.rectangle((size * 0.27, size * 0.50, size * 0.73, size * 0.83), fill="#1E3F79")
    return canvas


_AVATAR_CACHE = {}

def get_avatar(post):
    url = post["avatar_url"]

    if url in _AVATAR_CACHE:
        source = _AVATAR_CACHE[url]
    else:
        source = download_image(url) or draw_default_avatar()
        _AVATAR_CACHE[url] = source

    avatar = ImageOps.fit(source, (72, 72), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (72, 72), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 71, 71), fill=255)
    result = Image.new("RGBA", (72, 72), (0, 0, 0, 0))
    result.paste(avatar.convert("RGBA"), (0, 0), mask)
    return result


def wrap_text(draw, text, font, max_width):
    lines = []

    for paragraph in (text or "").splitlines() or [""]:
        words = paragraph.split(" ")
        if not words:
            lines.append("")
            continue

        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = word
                continue

            segment = ""
            for char in word:
                candidate = segment + char
                if draw.textlength(candidate, font=font) <= max_width:
                    segment = candidate
                else:
                    if segment:
                        lines.append(segment)
                    segment = char
            current = segment

        if current:
            lines.append(current)

    return lines


def rounded_rectangle(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def create_post_card(post):
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    card_path = CARD_DIR / f"trump_{post['id']}.png"

    card_width = 980
    padding = 42
    content_width = card_width - padding * 2
    top_height = 126

    original_text = (post["content"] or "").strip()
    if not original_text:
        if post["media"] and post["media"]["type"] == "video":
            original_text = "Video post"
        elif post["media"]:
            original_text = "Image post"
        else:
            original_text = "Truth Social post"

    original_text = original_text[:MAX_CARD_TEXT_LEN]

    scratch_draw = ImageDraw.Draw(Image.new("RGB", (card_width, 10), "white"))
    body_lines = wrap_text(scratch_draw, original_text, FONT_BODY, content_width)

    if len(body_lines) > MAX_BODY_LINES:
        body_lines = body_lines[:MAX_BODY_LINES]
        body_lines[-1] = body_lines[-1][:max(0, len(body_lines[-1]) - 3)] + "..."

    body_line_height = 39
    body_height = max(1, len(body_lines)) * body_line_height

    source_image = None
    video_preview = None

    if post["media"]:
        if post["media"]["type"] == "image":
            source_image = download_image(post["media"]["url"])
        elif post["media"]["type"] == "video":
            video_preview = download_image(post["media"]["preview_url"])

    media_height = 0
    if source_image or video_preview:
        media_height = min(560, int(content_width * 0.63)) + 28
    elif post["media"] and post["media"]["type"] == "video":
        media_height = min(460, int(content_width * 0.52)) + 28

    footer_height = 82
    card_height = top_height + body_height + media_height + footer_height + padding

    card = Image.new("RGB", (card_width, card_height), "#FFFFFF")
    draw = ImageDraw.Draw(card)

    rounded_rectangle(draw, (1, 1, card_width - 2, card_height - 2), radius=18,
                       fill="#FFFFFF", outline="#E4E4E4", width=2)

    avatar = get_avatar(post)
    card.paste(avatar, (padding, 31), avatar)

    name_x = padding + 92
    draw.text((name_x, 36), CARD_DISPLAY_NAME, font=FONT_NAME, fill="#1F2430")
    name_width = draw.textlength(CARD_DISPLAY_NAME, font=FONT_NAME)
    badge_x = int(name_x + name_width + 14)

    draw.ellipse((badge_x, 43, badge_x + 22, 65), fill="#E969A7")
    draw.text((badge_x + 5, 43), "check", font=FONT_META, fill="#FFFFFF")
    draw.text((name_x, 74), CARD_HANDLE, font=FONT_HANDLE, fill="#6B7280")

    y = top_height
    for line in body_lines:
        draw.text((padding, y), line, font=FONT_BODY, fill="#30323A")
        y += body_line_height

    if source_image or video_preview:
        media = source_image or video_preview
        image_top = y + 12
        image_height = media_height - 28

        fitted = ImageOps.fit(media, (content_width, image_height), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (content_width, image_height), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, content_width, image_height), radius=18, fill=255)
        card.paste(fitted, (padding, image_top), mask)

        if post["media"]["type"] == "video":
            center_x = padding + content_width // 2
            center_y = image_top + image_height // 2
            draw.ellipse((center_x - 47, center_y - 47, center_x + 47, center_y + 47), fill="#111827")
            draw.polygon(
                [(center_x - 12, center_y - 22), (center_x - 12, center_y + 22), (center_x + 25, center_y)],
                fill="#FFFFFF",
            )

        y = image_top + image_height + 28

    elif post["media"] and post["media"]["type"] == "video":
        video_top = y + 12
        video_height = media_height - 28

        rounded_rectangle(draw, (padding, video_top, padding + content_width, video_top + video_height),
                           radius=18, fill="#111827")

        center_x = padding + content_width // 2
        center_y = video_top + video_height // 2
        draw.ellipse((center_x - 50, center_y - 50, center_x + 50, center_y + 50), fill="#272C37")
        draw.polygon(
            [(center_x - 12, center_y - 23), (center_x - 12, center_y + 23), (center_x + 28, center_y)],
            fill="#FFFFFF",
        )
        draw.text((padding + 24, video_top + video_height - 48), "VIDEO", font=FONT_VIDEO, fill="#FFFFFF")

        y = video_top + video_height + 28

    created = "Truth Social"
    if post["created_ts"]:
        dt = datetime.fromtimestamp(post["created_ts"], tz=timezone.utc)
        created = dt.strftime("%b %d, %Y . %I:%M %p UTC")

    draw.line((padding, y + 4, card_width - padding, y + 4), fill="#ECECEC", width=1)
    draw.text((padding, y + 26), created, font=FONT_META, fill="#6B7280")
    draw.text((card_width - padding - 150, y + 26), "Truth Social", font=FONT_META, fill="#6B7280")

    card.save(card_path, format="PNG", optimize=True)
    return card_path


# ============================================================
# Discord 推送
# ============================================================

def build_embeds(post, translated_text, card_filename):
    chunks = split_text(translated_text, MAX_TRANSLATED_LEN)
    embeds = []

    for index, chunk in enumerate(chunks):
        embed = {"color": 5763719, "description": chunk}

        if index == 0:
            embed["title"] = "在 Truth Social 查看原帖"
            embed["url"] = post["url"]
            embed["image"] = {"url": f"attachment://{card_filename}"}

        if post["timestamp"]:
            embed["timestamp"] = post["timestamp"]

        embeds.append(embed)

    return embeds[:10]


def post_to_discord(post):
    translated_text = build_description(post)
    card_path = create_post_card(post)

    payload = {
        "embeds": build_embeds(post, translated_text, card_path.name),
        "allowed_mentions": {"parse": []},
    }

    try:
        with card_path.open("rb") as card_file:
            response = requests.post(
                WEBHOOK_URL,
                data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                files={"file": (card_path.name, card_file, "image/png")},
                timeout=60,
            )

        if response.status_code == 429:
            retry_after = float(response.json().get("retry_after", 2))
            time.sleep(retry_after + 1)
            return post_to_discord(post)

        response.raise_for_status()

    finally:
        card_path.unlink(missing_ok=True)


# ============================================================
# 主程序
# ============================================================

def main():
    state = load_state()

    if TEST_MODE:
        now = time.time()
        test_post = {
            "id": f"test-{int(now)}",
            "content": (
                "This is a test message from the Trump Truth Tracker. "
                "It verifies the Discord webhook, DeepL translation, "
                "and the generated post card."
            ),
            "url": "https://truthsocial.com/@realDonaldTrump",
            "created_ts": now,
            "timestamp": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "media": None,
            "avatar_url": FALLBACK_AVATAR_URL,
            "content_hash": hashlib.sha256(f"discord-test-{int(now)}".encode("utf-8")).hexdigest(),
        }

        post_to_discord(test_post)
        print("测试成功：已发送模拟帖子。未读取 CNN，未修改 seen_trump.json。")
        return

    raw_posts = fetch_posts()
    new_posts = collect_new_posts(raw_posts, state)

    is_first_run = not state["seen"] and not state["hashes"]

    if is_first_run and not SEND_ON_FIRST_RUN:
        now = time.time()
        for post in new_posts:
            state["seen"][post["id"]] = now
            state["hashes"][post["content_hash"]] = now

        save_state(state)
        print(f"首次初始化完成：记录 {len(new_posts)} 条近期帖子，没有补发历史内容。")
        return

    posts_to_send = list(islice(new_posts, MAX_SEND_PER_RUN))
    sent_count = 0

    for post in posts_to_send:
        post_to_discord(post)

        sent_at = time.time()
        state["seen"][post["id"]] = sent_at
        state["hashes"][post["content_hash"]] = sent_at

        sent_count += 1
        save_state(state)

        time.sleep(DISCORD_DELAY_SECONDS)

    save_state(state)
    print(f"检测到 {len(new_posts)} 条候选新帖子，已发送 {sent_count} 条。")


if __name__ == "__main__":
    main()
