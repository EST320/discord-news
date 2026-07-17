import hashlib
import html
import io
import json
import os
import re
import textwrap
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

DATA_URL = "https://ix.cnn.io/data/truth-social/truth_archive.json"

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_TRUMP"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]

STATE_FILE = Path("seen_trump.json")
CARD_DIR = Path("trump_cards")

MAX_SEND_PER_RUN = 20
DISCORD_DELAY_SECONDS = 0.8

# CNN 归档可能有延迟。12 小时内首次出现的帖子允许发出；
# 超过 12 小时的历史帖子，即使不在 seen 中也绝不补发。
MAX_POST_AGE_SECONDS = 12 * 3600

# ID 和正文哈希保留 30 天，避免 archive 重复返回时再次发送。
RETENTION_SECONDS = 30 * 24 * 3600

# 首次部署时：
# False = 不补发当前历史帖子，只建立初始去重状态。
# True  = 会推送 MAX_POST_AGE_SECONDS 时间窗口内的帖子。
SEND_ON_FIRST_RUN = False

# Discord Embed 描述最大长度低于 Discord 4096 上限，留出安全空间。
MAX_TRANSLATED_LEN = 3800

# 生成原帖卡片时的正文长度，防止卡片无限拉长。
MAX_CARD_TEXT_LEN = 1400

# 这是生成卡片所显示的账号信息，而不是 Discord webhook 名称。
# Discord 顶部名称/头像依然完全使用 Discord 后台 webhook 设置。
CARD_DISPLAY_NAME = "Donald J. Trump"
CARD_HANDLE = "@realDonaldTrump"

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

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


# ============================================================
# 状态：ID 去重 + 内容哈希去重
# ============================================================

def load_state():
    """
    兼容旧格式：
    旧文件：
    {
      "seen": {
        "123": 1710000000
      }
    }

    新文件：
    {
      "seen": {"123": 1710000000},
      "hashes": {"abcdef...": 1710000000}
    }
    """
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

        return {
            "seen": seen,
            "hashes": hashes,
        }

    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取状态文件失败，将以空状态启动：{exc}")
        return {"seen": {}, "hashes": {}}


def save_state(state):
    cutoff = time.time() - RETENTION_SECONDS

    pruned_seen = {
        str(key): value
        for key, value in state["seen"].items()
        if isinstance(value, (int, float)) and value > cutoff
    }

    pruned_hashes = {
        str(key): value
        for key, value in state["hashes"].items()
        if isinstance(value, (int, float)) and value > cutoff
    }

    STATE_FILE.write_text(
        json.dumps(
            {
                "seen": pruned_seen,
                "hashes": pruned_hashes,
            },
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
        timestamp = float(value)
        return (
            timestamp,
            datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
        )

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
    """
    返回：
    {
      "url": "...",
      "type": "image" / "video" / "unknown",
      "preview_url": "..."
    }

    重点：视频帖子不再被直接丢弃。
    """
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

    return {
        "url": url,
        "type": media_type,
        "preview_url": preview_url or None,
    }


def get_avatar_url(item):
    """
    尝试从 CNN JSON 不同可能字段中提取头像。
    如果取不到，生成卡片时自动绘制红蓝默认头像。
    """
    account = item.get("account") or item.get("user") or {}

    if not isinstance(account, dict):
        account = {}

    candidates = [
        item.get("avatar"),
        item.get("avatar_url"),
        account.get("avatar"),
        account.get("avatar_url"),
        account.get("profile_image_url"),
    ]

    for value in candidates:
        if isinstance(value, str) and value.strip().startswith("http"):
            return value.strip()

    return None


def make_content_hash(content, media):
    """
    内容哈希是第二道去重：
    同一个正文即使被 CNN 用不同 id 返回，也只会发送一次。
    """
    normalized = re.sub(r"\s+", " ", content or "").strip().lower()

    media_key = ""
    if media:
        media_key = (
            media.get("preview_url")
            or media.get("url")
            or ""
        ).strip().lower()

    raw = f"{normalized}|{media_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def item_to_post(item):
    post_id = str(item.get("id") or item.get("status_id") or "").strip()

    if not post_id:
        return None

    content = clean_html_content(
        item.get("content")
        or item.get("text")
        or item.get("body")
        or ""
    )

    media = get_first_media(item)

    # 没文字且没媒体才丢弃。
    # 空文字视频帖子现在会保留并做视频占位卡片。
    if not content and not media:
        return None

    created_ts, timestamp = parse_timestamp(
        item.get("created_at")
        or item.get("createdAt")
        or item.get("published_at")
        or item.get("timestamp")
    )

    post_url = str(
        item.get("url")
        or item.get("status_url")
        or item.get("permalink")
        or ""
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

        # ID 去重。
        if post["id"] in seen_ids:
            continue

        # 内容去重。
        if post["content_hash"] in seen_hashes:
            print(f"跳过重复正文：{post['id']}")
            continue

        # 没有可解析发布时间的帖子不推送，避免历史数据误发。
        if post["created_ts"] is None:
            print(f"跳过无法解析时间的帖子：{post['id']}")
            continue

        age_seconds = now - post["created_ts"]

        # 未来超过十分钟，数据时间异常。
        if age_seconds < -600:
            print(f"跳过时间异常帖子：{post['id']}")
            continue

        # 防止 CNN archive 旧记录在状态过期后被重新发送。
        if age_seconds > MAX_POST_AGE_SECONDS:
            continue

        collected[post["id"]] = post

    return sorted(
        collected.values(),
        key=lambda post: post["created_ts"],
    )


# ============================================================
# DeepL 中文翻译
# ============================================================

def translate_text(text):
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
        print(f"DeepL 翻译失败，回退英文原文：{exc}")
        return None

    except Exception as exc:
        print(f"翻译未知错误，回退英文原文：{exc}")
        return None


def build_description(post):
    """
    Discord Embed 内只显示中文。
    纯图片/视频帖子会有简短说明。
    """
    if post["content"]:
        translated = translate_text(post["content"])
        return (translated or post["content"])[:MAX_TRANSLATED_LEN]

    if post["media"] and post["media"]["type"] == "video":
        return "特朗普发布了一段视频。"

    if post["media"]:
        return "特朗普发布了一张图片。"

    return "特朗普发布了一条帖子。"


# ============================================================
# 生成“原帖卡片图”
# 不访问 Truth Social，不受 Cloudflare 影响
# ============================================================

def get_font(size, bold=False):
    """
    ubuntu-latest 一般有 DejaVu 字体。
    英文原帖卡片使用该字体；中文在 Discord Embed 显示，
    所以卡片本身不依赖中文字体。
    """
    candidates = []

    if bold:
        candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ])
    else:
        candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ])

    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


FONT_NAME = get_font(28, bold=True)
FONT_HANDLE = get_font(21, bold=False)
FONT_BODY = get_font(26, bold=False)
FONT_META = get_font(19, bold=False)
FONT_VIDEO = get_font(26, bold=True)
FONT_ICON = get_font(34, bold=True)


def download_image(url, timeout=20):
    if not url:
        return None

    try:
        response = requests.get(
            url,
            headers=MEDIA_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()

        image = Image.open(io.BytesIO(response.content))
        return image.convert("RGB")

    except Exception as exc:
        print(f"图片下载失败：{exc}")
        return None


def draw_default_avatar(size=72):
    """
    CNN 未提供头像或头像无法下载时使用的简洁红蓝默认头像。
    """
    canvas = Image.new("RGB", (size, size), "#F7F7F7")
    draw = ImageDraw.Draw(canvas)

    draw.ellipse((0, 0, size - 1, size - 1), fill="#EAEAEA")

    draw.pieslice(
        (0, 0, size - 1, size - 1),
        start=90,
        end=270,
        fill="#1E5AA8",
    )

    draw.pieslice(
        (0, 0, size - 1, size - 1),
        start=270,
        end=90,
        fill="#C9272C",
    )

    draw.ellipse(
        (size * 0.34, size * 0.22, size * 0.66, size * 0.55),
        fill="#F2C6A0",
    )

    draw.rectangle(
        (size * 0.27, size * 0.50, size * 0.73, size * 0.83),
        fill="#1E3F79",
    )

    return canvas


def get_avatar(post):
    avatar = download_image(post["avatar_url"])

    if avatar is None:
        avatar = draw_default_avatar()

    avatar = ImageOps.fit(
        avatar,
        (72, 72),
        method=Image.Resampling.LANCZOS,
    )

    mask = Image.new("L", (72, 72), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 71, 71), fill=255)

    result = Image.new("RGBA", (72, 72), (0, 0, 0, 0))
    result.paste(avatar.convert("RGBA"), (0, 0), mask)

    return result


def wrap_text(draw, text, font, max_width):
    """
    按像素宽度断行，而不是只按字符数断行。
    """
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

            # 极少数超长 URL / 单词，强制分段。
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
    draw.rounded_rectangle(
        box,
        radius=radius,
        fill=fill,
        outline=outline,
        width=width,
    )


def create_post_card(post):
    """
    生成白底的“Truth Social 风格原帖卡片”：
    - 英文原文
    - 账号名与头像
    - 原帖图片（有图片时）
    - 视频占位（视频帖）
    - 时间与互动信息样式
    """
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

    scratch = Image.new("RGB", (card_width, 100), "white")
    scratch_draw = ImageDraw.Draw(scratch)

    body_lines = wrap_text(
        scratch_draw,
        original_text,
        FONT_BODY,
        content_width,
    )

    max_body_lines = 12

    if len(body_lines) > max_body_lines:
        body_lines = body_lines[:max_body_lines]
        body_lines[-1] = body_lines[-1][: max(0, len(body_lines[-1]) - 3)] + "..."

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
        media_height = min(560, int(content_width * 0.63))
        media_height += 28

    elif post["media"] and post["media"]["type"] == "video":
        media_height = min(460, int(content_width * 0.52))
        media_height += 28

    footer_height = 82
    card_height = (
        top_height
        + body_height
        + media_height
        + footer_height
        + padding
    )

    card = Image.new("RGB", (card_width, card_height), "#FFFFFF")
    draw = ImageDraw.Draw(card)

    # 背景卡片。
    rounded_rectangle(
        draw,
        (1, 1, card_width - 2, card_height - 2),
        radius=18,
        fill="#FFFFFF",
        outline="#E4E4E4",
        width=2,
    )

    # 顶部账号区。
    avatar = get_avatar(post)
    card.paste(avatar, (padding, 31), avatar)

    name_x = padding + 92
    draw.text(
        (name_x, 36),
        CARD_DISPLAY_NAME,
        font=FONT_NAME,
        fill="#1F2430",
    )

    name_width = draw.textlength(
        CARD_DISPLAY_NAME,
        font=FONT_NAME,
    )

    badge_x = int(name_x + name_width + 14)

    draw.ellipse(
        (badge_x, 43, badge_x + 22, 65),
        fill="#E969A7",
    )
    draw.text(
        (badge_x + 5, 43),
        "✓",
        font=FONT_META,
        fill="#FFFFFF",
    )

    draw.text(
        (name_x, 74),
        CARD_HANDLE,
        font=FONT_HANDLE,
        fill="#6B7280",
    )

    # 英文正文。
    y = top_height

    for line in body_lines:
        draw.text(
            (padding, y),
            line,
            font=FONT_BODY,
            fill="#30323A",
        )
        y += body_line_height

    # 自带图片或视频预览。
    if source_image or video_preview:
        media = source_image or video_preview

        image_top = y + 12
        image_height = media_height - 28

        fitted = ImageOps.fit(
            media,
            (content_width, image_height),
            method=Image.Resampling.LANCZOS,
        )

        mask = Image.new(
            "L",
            (content_width, image_height),
            0,
        )
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, content_width, image_height),
            radius=18,
            fill=255,
        )

        card.paste(
            fitted,
            (padding, image_top),
            mask,
        )

        if post["media"]["type"] == "video":
            # 视频预览上叠加播放键。
            center_x = padding + content_width // 2
            center_y = image_top + image_height // 2

            draw.ellipse(
                (
                    center_x - 47,
                    center_y - 47,
                    center_x + 47,
                    center_y + 47,
                ),
                fill="#111827",
            )

            draw.polygon(
                [
                    (center_x - 12, center_y - 22),
                    (center_x - 12, center_y + 22),
                    (center_x + 25, center_y),
                ],
                fill="#FFFFFF",
            )

        y = image_top + image_height + 28

    elif post["media"] and post["media"]["type"] == "video":
        # 无法取得视频封面时，生成视频占位卡片。
        video_top = y + 12
        video_height = media_height - 28

        rounded_rectangle(
            draw,
            (
                padding,
                video_top,
                padding + content_width,
                video_top + video_height,
            ),
            radius=18,
            fill="#111827",
        )

        center_x = padding + content_width // 2
        center_y = video_top + video_height // 2

        draw.ellipse(
            (
                center_x - 50,
                center_y - 50,
                center_x + 50,
                center_y + 50,
            ),
            fill="#272C37",
        )

        draw.polygon(
            [
                (center_x - 12, center_y - 23),
                (center_x - 12, center_y + 23),
                (center_x + 28, center_y),
            ],
            fill="#FFFFFF",
        )

        draw.text(
            (padding + 24, video_top + video_height - 48),
            "VIDEO",
            font=FONT_VIDEO,
            fill="#FFFFFF",
        )

        y = video_top + video_height + 28

    # 底部信息。
    created = "Truth Social"

    if post["created_ts"]:
        dt = datetime.fromtimestamp(
            post["created_ts"],
            tz=timezone.utc,
        )
        created = dt.strftime("%b %d, %Y · %I:%M %p UTC")

    draw.line(
        (padding, y + 4, card_width - padding, y + 4),
        fill="#ECECEC",
        width=1,
    )

    draw.text(
        (padding, y + 26),
        created,
        font=FONT_META,
        fill="#6B7280",
    )

    draw.text(
        (card_width - padding - 150, y + 26),
        "Truth Social",
        font=FONT_META,
        fill="#6B7280",
    )

    card.save(card_path, format="PNG", optimize=True)

    return card_path


# ============================================================
# Discord 推送
# ============================================================

def post_to_discord(post):
    """
    Discord 顶部名字和头像：
    - 不传 username
    - 不传 avatar_url
    因此完全采用 Discord webhook 后台配置。

    Embed 内部：
    - 只显示中文翻译
    - 不显示 author
    - 不显示顶层 URL
    - 下方显示生成的英文原帖卡片图
    """
    translated_text = build_description(post)
    card_path = create_post_card(post)

    embed = {
        "color": 5763719,
        "description": translated_text,
        "image": {
            "url": f"attachment://{card_path.name}",
        },
    }

    if post["timestamp"]:
        embed["timestamp"] = post["timestamp"]

    payload = {
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    try:
        with card_path.open("rb") as card_file:
            response = requests.post(
                WEBHOOK_URL,
                data={
                    "payload_json": json.dumps(
                        payload,
                        ensure_ascii=False,
                    ),
                },
                files={
                    "file": (
                        card_path.name,
                        card_file,
                        "image/png",
                    ),
                },
                timeout=60,
            )

        if response.status_code == 429:
            retry_after = float(
                response.json().get("retry_after", 2)
            )
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

    raw_posts = fetch_posts()
    new_posts = collect_new_posts(raw_posts, state)

    is_first_run = not state["seen"] and not state["hashes"]

    # 第一次使用时不发旧消息，只建立去重状态。
    if is_first_run and not SEND_ON_FIRST_RUN:
        now = time.time()

        for post in new_posts:
            state["seen"][post["id"]] = now
            state["hashes"][post["content_hash"]] = now

        save_state(state)

        print(
            f"首次初始化完成：记录 {len(new_posts)} 条近期帖子，"
            "没有补发历史内容。"
        )
        return

    posts_to_send = list(
        islice(new_posts, MAX_SEND_PER_RUN)
    )

    sent_count = 0

    for post in posts_to_send:
        # 只有 Discord 返回成功后，才写入状态。
        post_to_discord(post)

        sent_at = time.time()
        state["seen"][post["id"]] = sent_at
        state["hashes"][post["content_hash"]] = sent_at

        sent_count += 1
        save_state(state)

        time.sleep(DISCORD_DELAY_SECONDS)

    # 未发送的帖子不会被标记 seen：
    # 下次运行会继续发送，不会漏消息。
    save_state(state)

    print(
        f"检测到 {len(new_posts)} 条候选新帖子，"
        f"已发送 {sent_count} 条。"
    )


if __name__ == "__main__":
    main()
