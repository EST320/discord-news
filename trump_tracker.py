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
from urllib.parse import quote, urlparse

import deepl
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ============================================================
# 配置
# ============================================================

TEST_MODE = False
# 仅在 TEST_MODE=True 时生效：None / "image" / "video"
TEST_MEDIA_TYPE = "image"
IMAGE_DELIVERY = "direct"

DATA_URL = "https://ix.cnn.io/data/truth-social/truth_archive.json"

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL_TRUMP"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]

STATE_FILE = Path("seen_trump.json")
CARD_DIR = Path("trump_cards")
LOCAL_AVATAR_PATH = Path("assets/trump_avatar.jpg")

MAX_SEND_PER_RUN = 20
DISCORD_DELAY_SECONDS = 0.8
MAX_POST_AGE_SECONDS = 12 * 3600
RETENTION_SECONDS = 30 * 24 * 3600
SEND_ON_FIRST_RUN = False
MAX_DISCORD_RETRIES = 5

MAX_TRANSLATED_LEN = 4000
MAX_CARD_TEXT_LEN = 6000
MAX_BODY_LINES = 60

CARD_DISPLAY_NAME = "Donald J. Trump"
CARD_HANDLE = "@realDonaldTrump"

# ------------------------------------------------------------
# 媒体投递策略（核心开关）
#
# "direct" —— embed 里直接填 Truth Social 的原始 URL，由 Discord 自己
#             的服务器去抓取。我们的 runner 完全不碰媒体文件。
# "proxy"  —— embed 里填 images.weserv.nl 的代理 URL。weserv 去抓
#             Truth Social，Discord 再去抓 weserv。如果 Truth Social
#             的 CDN 连 Discord 的抓取服务也一起挡了，就切到这个。
#
# 先用 direct 测；图片如果还是不显示，把这一行改成 "proxy" 再测一次。
# 注意 weserv 只处理图片，视频缩略图同样是图片所以也适用。
# ------------------------------------------------------------
IMAGE_DELIVERY = "direct"

IMAGE_PROXY_TEMPLATE = "https://images.weserv.nl/?url={}"

# Discord 免费版单文件上传上限 10MB，留余量给 multipart 的其他开销。
# 视频超过这个大小就放弃附件上传，退回缩略图 + 链接。
MAX_VIDEO_ATTACHMENT_BYTES = 9 * 1024 * 1024

# 是否尝试为视频下载原始字节并作为附件上传（成功的话 Discord 里就是
# 一个真正可播放的播放器）。如果确认 runner 一定抓不到，可以关掉它
# 省掉每次的无效下载尝试和等待时间。
TRY_VIDEO_ATTACHMENT = True

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

MEDIA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://truthsocial.com/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}

translator = deepl.Translator(DEEPL_API_KEY)

# ------------------------------------------------------------
# curl_cffi：只在"需要我们自己下载字节"时才用得上（目前只剩视频附件
# 这一条路径）。图片已经改成交给 Discord 抓，不再依赖它。
# 没装也不影响图片显示，只会让视频附件那条路走不通。
# ------------------------------------------------------------
try:
    from curl_cffi.requests import Session as _CurlSession

    _CURL_SESSION = _CurlSession(impersonate="chrome124")
    HAS_CURL_CFFI = True
except ImportError:
    _CURL_SESSION = None
    HAS_CURL_CFFI = False
    print("提示：未安装 curl_cffi，视频附件下载将使用普通 requests（成功率更低）。")

# ============================================================
# 状态：ID 去重 + 内容哈希去重 + HTTP 条件请求缓存
# ============================================================

def load_state():
    default = {"seen": {}, "hashes": {}, "etag": None, "last_modified": None}
    if not STATE_FILE.exists():
        return default
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
            "etag": data.get("etag"),
            "last_modified": data.get("last_modified"),
        }
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取状态文件失败，将以空状态启动：{exc}")
        return default


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
            {
                "seen": pruned_seen,
                "hashes": pruned_hashes,
                "etag": state.get("etag"),
                "last_modified": state.get("last_modified"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

# ============================================================
# 数据抓取与帖子解析
# ============================================================

def fetch_posts(state):
    """
    带 ETag / Last-Modified 条件请求的抓取。

    cron 每 2 分钟一次、一天 720 次，而 Trump 不可能每次都发新内容。
    远端支持条件请求时，绝大多数运行会直接收到 304，跳过 JSON 解析 +
    全量扫描 + 去重。远端不支持时自动退化成普通全量请求，无副作用。

    返回 None 表示"数据未变化，本次无需继续处理"。
    """
    headers = dict(HEADERS)
    if state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    if state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]

    response = requests.get(DATA_URL, headers=headers, timeout=30)

    if response.status_code == 304:
        state["etag"] = response.headers.get("ETag") or state.get("etag")
        state["last_modified"] = response.headers.get("Last-Modified") or state.get("last_modified")
        return None

    response.raise_for_status()

    state["etag"] = response.headers.get("ETag") or state.get("etag")
    state["last_modified"] = response.headers.get("Last-Modified") or state.get("last_modified")

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


def get_all_media(item):
    """
    返回帖子里的全部媒体（不再只取第一张）。

    Discord 单条消息最多 10 个 embed，多图帖子（数据源里常见 2-4 张）
    现在可以全部显示出来，而不是像以前那样只保留第一张。
    """
    media_list = item.get("media")
    if not isinstance(media_list, list) or not media_list:
        return []

    results = []
    for entry in media_list:
        if isinstance(entry, str):
            url = entry.strip().replace("&amp;", "&")
            preview_url = None
        elif isinstance(entry, dict):
            url = str(entry.get("url") or "").strip().replace("&amp;", "&")
            preview_url = str(
                entry.get("preview_url")
                or entry.get("preview")
                or entry.get("thumbnail_url")
                or ""
            ).strip().replace("&amp;", "&") or None
        else:
            continue

        if not url:
            continue

        path = urlparse(url).path.lower()
        if path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
            media_type = "image"
        elif path.endswith((".mp4", ".mov", ".webm", ".m3u8")):
            media_type = "video"
        else:
            media_type = "unknown"

        results.append({"url": url, "type": media_type, "preview_url": preview_url})

    return results


def derive_video_thumbnail(video_url):
    """
    推导视频缩略图 URL。

    Truth Social 是 Mastodon 的 fork，媒体附件通常同时存在 original/
    和 small/ 两个变体，small/ 是服务端自动生成的预览帧。数据源本身
    不提供 preview_url 字段，所以这里按 Mastodon 的路径约定推导。

    这是基于 Mastodon 惯例的推测，不保证一定存在；推导不出来或实际
    404 时，视频那条 embed 只会少一张预览图，不影响其他内容。
    """
    if not video_url or "/original/" not in video_url:
        return None
    prefix = video_url.rsplit("/original/", 1)[0]
    stem = Path(urlparse(video_url).path).stem
    if not stem:
        return None
    return f"{prefix}/small/{stem}.jpg"


def to_delivery_url(url):
    """按 IMAGE_DELIVERY 策略，把原始媒体 URL 转成交给 Discord 的 URL。"""
    if not url:
        return None
    if IMAGE_DELIVERY == "proxy":
        return IMAGE_PROXY_TEMPLATE.format(quote(url, safe=""))
    return url


def make_content_hash(content, media_list):
    normalized = re.sub(r"\s+", " ", content or "").strip().lower()
    media_key = ""
    if media_list:
        first = media_list[0]
        media_key = (first.get("preview_url") or first.get("url") or "").strip().lower()
    raw = f"{normalized}|{media_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def item_to_post(item):
    post_id = str(item.get("id") or item.get("status_id") or "").strip()
    if not post_id:
        return None

    content = clean_html_content(
        item.get("content") or item.get("text") or item.get("body") or ""
    )
    media_list = get_all_media(item)

    if not content and not media_list:
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
        "media_list": media_list,
        "content_hash": make_content_hash(content, media_list),
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


def first_media_type(post):
    return post["media_list"][0]["type"] if post["media_list"] else None


def build_description(post):
    if post["content"]:
        translated = translate_text(post["content"])
        return (translated or post["content"])[:MAX_TRANSLATED_LEN]

    kind = first_media_type(post)
    if kind == "video":
        return "特朗普发布了一段视频。"
    if kind is not None:
        return "特朗普发布了一张图片。"
    return "特朗普发布了一条帖子。"


def split_text(text, limit):
    if len(text) <= limit:
        return [text]
    return [text[i:i + limit] for i in range(0, len(text), limit)]

# ============================================================
# 生成"原帖卡片图"（纯文字，不含媒体）
#
# 关键改动：卡片不再把图片合成进去。合成图片要求 runner 能下载到
# 媒体文件，而那正是一直失败的环节。现在卡片只画文字，是纯本地
# 渲染、不联网、永不失败的操作；媒体交给 Discord 单独去抓。
# 副作用是好的：图片以原始分辨率显示，不再被压进卡片里。
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


def _fetch_bytes(url, timeout=20):
    """
    抓取任意 URL 的原始字节，成功返回 bytes，失败返回 None。

    失败时打印详细诊断（状态码、server / cf-ray 响应头、异常类型、
    响应体前 200 字符）—— 这些会出现在 GitHub Actions 日志里，方便
    判断到底是被明确拒绝（403/406）、连接被重置，还是超时。
    """
    try:
        if HAS_CURL_CFFI:
            response = _CURL_SESSION.get(url, headers=MEDIA_HEADERS, timeout=timeout)
        else:
            response = requests.get(url, headers=MEDIA_HEADERS, timeout=timeout)

        if response.status_code != 200:
            try:
                body_preview = response.text[:200]
            except Exception:
                body_preview = "<binary or undecodable body>"
            print(
                f"[抓取失败] HTTP {response.status_code} url={url} "
                f"server={response.headers.get('server', '')!r} "
                f"cf-ray={response.headers.get('cf-ray', '')!r} "
                f"body_head={body_preview!r}"
            )
            return None

        return response.content
    except Exception as exc:
        print(f"[抓取异常] {type(exc).__name__}: {exc} url={url}")
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


_AVATAR_IMAGE = None


def get_avatar():
    """
    头像固定使用本地文件 assets/trump_avatar.jpg，不请求 CDN。
    只在首次调用时读盘并缓存到内存，避免重复 I/O。
    """
    global _AVATAR_IMAGE
    if _AVATAR_IMAGE is not None:
        return _AVATAR_IMAGE

    if LOCAL_AVATAR_PATH.exists():
        try:
            source = Image.open(LOCAL_AVATAR_PATH).convert("RGB")
        except Exception as exc:
            print(f"本地头像读取失败，使用默认头像：{exc}")
            source = draw_default_avatar()
    else:
        print(f"未找到本地头像文件 {LOCAL_AVATAR_PATH}，使用默认头像")
        source = draw_default_avatar()

    avatar = ImageOps.fit(source, (72, 72), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (72, 72), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 71, 71), fill=255)
    result = Image.new("RGBA", (72, 72), (0, 0, 0, 0))
    result.paste(avatar.convert("RGBA"), (0, 0), mask)
    _AVATAR_IMAGE = result
    return result


def _split_long_word(draw, word, font, max_width):
    """
    用二分查找切分单独就超宽的词（比如一长串没有空格的 URL）。
    逐字符线性扫描在这种情况下是 O(word_len^2)；二分后每次切分是
    O(log word_len) 次宽度测量，总体降到 O(word_len log word_len)。
    """
    pieces = []
    start = 0
    n = len(word)
    while start < n:
        lo, hi = start + 1, n
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if draw.textlength(word[start:mid], font=font) <= max_width:
                lo = mid
            else:
                hi = mid - 1
        # lo 至少是 start+1：即使单字符本身就超宽也强制吞掉一个，避免死循环
        pieces.append(word[start:lo])
        start = lo
    return pieces


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
                current = ""

            if draw.textlength(word, font=font) <= max_width:
                current = word
                continue

            pieces = _split_long_word(draw, word, font, max_width)
            for piece in pieces[:-1]:
                lines.append(piece)
            current = pieces[-1] if pieces else ""

        if current:
            lines.append(current)
    return lines


def rounded_rectangle(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def create_post_card(post):
    """纯本地渲染的文字卡片，不联网、不会失败。"""
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    card_path = CARD_DIR / f"trump_{post['id']}.png"

    card_width = 980
    padding = 42
    content_width = card_width - padding * 2
    top_height = 126

    original_text = (post["content"] or "").strip()
    if not original_text:
        kind = first_media_type(post)
        if kind == "video":
            original_text = "Video post"
        elif kind is not None:
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

    footer_height = 82
    card_height = top_height + body_height + footer_height + padding

    card = Image.new("RGB", (card_width, card_height), "#FFFFFF")
    draw = ImageDraw.Draw(card)
    rounded_rectangle(draw, (1, 1, card_width - 2, card_height - 2), radius=18,
                      fill="#FFFFFF", outline="#E4E4E4", width=2)

    avatar = get_avatar()
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
# 视频附件（唯一能做出"真正可播放播放器"的办法）
# ============================================================

def download_video_bytes(url, timeout=60):
    """
    下载视频原始字节，用于作为 Discord 附件上传。

    把裸链接放进消息正文对 Discord 是无效的 —— Discord 只对认识
    oEmbed 元数据的站点（YouTube/Twitch 等）自动生成播放器，对第三方
    直链只当普通文字链接处理（已实测确认）。要真正可播放只能传字节。
    """
    content = _fetch_bytes(url, timeout=timeout)
    if content is None:
        return None
    if len(content) > MAX_VIDEO_ATTACHMENT_BYTES:
        print(
            f"视频 {len(content) / 1024 / 1024:.1f}MB 超过附件上限"
            f"（{MAX_VIDEO_ATTACHMENT_BYTES / 1024 / 1024:.0f}MB），改用缩略图 + 链接：{url}"
        )
        return None
    return content

# ============================================================
# Discord 推送
# ============================================================

def build_embeds(post, translated_text, card_filename):
    """
    组装 embed 列表。

    结构：
      embed[0]  标题 + 中文翻译 + 文字卡片（attachment）
      embed[1:] 每个媒体一个 embed，image.url 直接指向远端 —— 由
                Discord 服务器自己去抓，我们的 runner 完全不参与。

    所有 embed 共用同一个 url（原帖链接），Discord 会把它们合并成
    一个视觉整体，看起来就是"一条卡片 + 若干张图"。
    """
    chunks = split_text(translated_text, MAX_TRANSLATED_LEN)
    embeds = []

    for index, chunk in enumerate(chunks):
        embed = {"color": 5763719, "description": chunk, "url": post["url"]}
        if index == 0:
            embed["title"] = "Original Post on Truth Social"
            embed["image"] = {"url": f"attachment://{card_filename}"}
            if post["timestamp"]:
                embed["timestamp"] = post["timestamp"]
        embeds.append(embed)

    for media in post["media_list"]:
        if len(embeds) >= 10:
            break

        if media["type"] == "image":
            target = to_delivery_url(media["url"])
        elif media["type"] == "video":
            # 视频没能作为附件传上去时，退而求其次显示一张预览帧
            thumb = media.get("preview_url") or derive_video_thumbnail(media["url"])
            target = to_delivery_url(thumb)
        else:
            target = None

        if target:
            embeds.append({"color": 5763719, "url": post["url"], "image": {"url": target}})

    return embeds[:10]


def _send_discord_payload(payload, card_path, video_attachments):
    """
    只负责把准备好的 payload + 附件发出去，并处理 429 重试。
    卡片渲染、翻译、视频下载都在调用方一次性做完 —— 429 重试时不该
    把这些开销再付一遍，这里只重发 HTTP 请求本身。
    """
    for _ in range(MAX_DISCORD_RETRIES):
        with card_path.open("rb") as card_file:
            files = {"files[0]": (card_path.name, card_file, "image/png")}
            for index, (filename, data) in enumerate(video_attachments, start=1):
                files[f"files[{index}]"] = (filename, data, "video/mp4")

            response = requests.post(
                WEBHOOK_URL,
                data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                files=files,
                timeout=120,
            )
        if response.status_code == 429:
            retry_after = float(response.json().get("retry_after", 2))
            time.sleep(retry_after + 1)
            continue
        if response.status_code >= 400:
            print(f"[Discord 拒绝] HTTP {response.status_code} body={response.text[:400]!r}")
        response.raise_for_status()
        return
    raise RuntimeError(f"Discord 429 重试 {MAX_DISCORD_RETRIES} 次后仍未发送成功")


def post_to_discord(post):
    translated_text = build_description(post)
    card_path = create_post_card(post)

    video_attachments = []
    leftover_video_links = []

    for media in post["media_list"]:
        if media["type"] != "video":
            continue

        data = download_video_bytes(media["url"]) if TRY_VIDEO_ATTACHMENT else None
        if data is not None:
            filename = Path(urlparse(media["url"]).path).name or f"{post['id']}.mp4"
            video_attachments.append((filename, data))
            print(f"视频已下载（{len(data) / 1024 / 1024:.1f}MB），作为附件上传 → Discord 内可直接播放")
        else:
            # 附件传不上去时保留链接，让用户至少能点开看
            leftover_video_links.append(media["url"])

    payload = {
        "embeds": build_embeds(post, translated_text, card_path.name),
        "allowed_mentions": {"parse": []},
    }
    if leftover_video_links:
        payload["content"] = "\n".join(leftover_video_links)

    try:
        _send_discord_payload(payload, card_path, video_attachments)
    finally:
        card_path.unlink(missing_ok=True)

# ============================================================
# 主程序
# ============================================================

_TEST_MEDIA_URLS = {
    "image": "https://static-assets-1.truthsocial.com/tmtg:prime-ts-assets/media_attachments/"
             "files/117/016/718/777/732/128/original/89b7243307a5d02f.jpg",
    "video": "https://static-assets-1.truthsocial.com/tmtg:prime-ts-assets/media_attachments/"
             "files/117/016/402/090/566/111/original/21c1796c935ea564.mp4",
}


def main():
    state = load_state()

    if TEST_MODE:
        now = time.time()

        test_media_list = []
        if TEST_MEDIA_TYPE in _TEST_MEDIA_URLS:
            test_media_list = [{
                "url": _TEST_MEDIA_URLS[TEST_MEDIA_TYPE],
                "type": TEST_MEDIA_TYPE,
                "preview_url": None,
            }]

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
            "media_list": test_media_list,
            "content_hash": hashlib.sha256(f"discord-test-{int(now)}".encode("utf-8")).hexdigest(),
        }
        post_to_discord(test_post)
        print(
            f"测试完成：media={TEST_MEDIA_TYPE}，投递策略={IMAGE_DELIVERY}。"
            "未读取 CNN，未修改 seen_trump.json。"
        )
        return

    raw_posts = fetch_posts(state)
    if raw_posts is None:
        print("CNN 数据自上次抓取后未变化（HTTP 304），本次跳过后续处理。")
        save_state(state)
        return

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
