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

TEST_MODE =True
# None / "image" / "video" —— 仅在 TEST_MODE=True 时生效
TEST_MEDIA_TYPE = "video"  
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
 
# 图片直连失败时的兜底代理：用 images.weserv.nl 自己的服务器去抓图，
# 出口 IP 和 GitHub Actions 不一样，能绕开"按数据中心 IP 段拉黑"这类
# 直连抓不到的封锁方式（这是免费公共服务，没有可用性保证，只是兜底）。
IMAGE_PROXY_TEMPLATE = "https://images.weserv.nl/?url={}"
 
# Discord 免费版单文件上传上限是 10MB，这里留点余量给 multipart 的
# 其他开销。视频超过这个大小就放弃直接附件上传，退回纯链接。
MAX_VIDEO_ATTACHMENT_BYTES = 9 * 1024 * 1024
 
MAX_TRANSLATED_LEN = 4000
MAX_CARD_TEXT_LEN = 6000
MAX_BODY_LINES = 60
 
CARD_DISPLAY_NAME = "Donald J. Trump"
CARD_HANDLE = "@realDonaldTrump"
 
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
# 图片下载客户端
#
# static-assets-1.truthsocial.com 这个 CDN 会在 TLS 握手层面（而不是
# 靠 User-Agent / Referer 这类应用层 header）拦截非浏览器请求 —— 这也是
# 之前头像必须改用本地文件的原因。普通 requests/urllib3 的 TLS 指纹和
# 真实浏览器不同，直接抓帖子里的图片大概率会被同样拦截。这里改用
# curl_cffi 模拟 Chrome 的 TLS/HTTP2 指纹来绕开。
#
# 需要先安装：pip install curl_cffi
# ------------------------------------------------------------
try:
    from curl_cffi.requests import Session as _CurlSession
 
    _CURL_SESSION = _CurlSession(impersonate="chrome124")
    HAS_CURL_CFFI = True
except ImportError:
    _CURL_SESSION = None
    HAS_CURL_CFFI = False
    print(
        "警告：未安装 curl_cffi，图片抓取很可能被 Truth Social CDN 的反爬机制"
        "在 TLS 层拦截。建议执行 `pip install curl_cffi` 后重新运行。"
    )
 
# ============================================================
# 状态：ID 去重 + 内容哈希去重 + HTTP 条件请求缓存（ETag / Last-Modified）
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
 
    cron 是每 2 分钟跑一次，一天 720 次，而 Trump 不可能每次都发新内容——
    如果远端支持条件请求，绝大多数运行会直接收到 304，我们就能跳过
    JSON 解析 + 全量扫描 + 去重这一整套后续处理，只留一次很轻的 HTTP
    往返。如果远端不支持 ETag/Last-Modified（没有相关响应头），这里会
    自动退化成普通全量请求，不会有任何副作用。
 
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
 
 
def get_first_media(item):
    media = item.get("media")
    if not isinstance(media, list) or not media:
        return None
    first = media[0]
    if isinstance(first, str):
        url = first.strip().replace("&amp;", "&")
        preview_url = None
    elif isinstance(first, dict):
        # 目前 CNN 的数据源里 media 都是纯字符串 URL，走不到这个分支；
        # 保留它是为了兼容数据源未来改成带缩略图字段的结构。
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
 
 
def _fetch_bytes(url, timeout=20):
    """
    抓取任意 URL 的原始字节，成功返回 bytes，失败返回 None。
 
    失败时打印详细诊断（状态码、server / cf-ray 响应头、异常类型、
    出错响应体前 200 字符）—— 这些信息会出现在 GitHub Actions 的运行
    日志里，方便下次直接判断到底是被明确拒绝（403/406）、连接被重置，
    还是超时，而不是像现在这样只能靠猜。
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
 
 
def download_image(url, timeout=20):
    """
    下载图片，两级尝试：
 
    1) curl_cffi 直连（模拟 Chrome 的 TLS 指纹），对付基于 TLS/JA3
       指纹的反爬拦截。
    2) 直连失败就退到 images.weserv.nl 这个公共图片代理重试一次 ——
       如果拦截其实是按 IP 信誉/数据中心 IP 段做的（伪装了 TLS 指纹
       还是被拒，这是目前最像的情况），换一个出口 IP 有机会绕过去。
 
    url 为空时零成本直接返回，不发请求。
    """
    if not url:
        return None
 
    content = _fetch_bytes(url, timeout=timeout)
    source = "direct"
 
    if content is None:
        proxied_url = IMAGE_PROXY_TEMPLATE.format(quote(url, safe=""))
        print(f"直连失败，尝试通过图片代理重试：{proxied_url}")
        content = _fetch_bytes(proxied_url, timeout=timeout)
        source = "proxy"
 
    if content is None:
        print(f"图片下载彻底失败（直连和代理都失败）：{url}")
        return None
 
    try:
        image = Image.open(io.BytesIO(content)).convert("RGB")
        print(f"图片下载成功（来源：{source}）：{url}")
        return image
    except Exception as exc:
        print(f"下载到的内容不是有效图片：{exc}，来源={source}，url={url}")
        return None
 
 
def download_video_bytes(url, timeout=60):
    """
    下载视频原始字节，用于作为 Discord 附件直接上传。
 
    这是目前唯一被证实可靠的"让视频在 Discord 里可播放"的办法——
    把裸链接放进消息正文，Discord 对不认识 oEmbed 元数据的第三方直链
    并不会像对 YouTube/Twitch 那样自动生成播放器，实测已确认无效。
 
    注意：这里没有走 images.weserv.nl 那样的代理兜底，因为那个服务
    只处理图片。如果视频这里直连也被拦（和图片同样的封锁方式），说明
    真正需要的是一个能代理任意二进制内容（不限图片）的中转服务，这
    已经超出这个脚本能单独解决的范围，需要另外搭一个中转。
    """
    content = _fetch_bytes(url, timeout=timeout)
    if content is None:
        return None
    if len(content) > MAX_VIDEO_ATTACHMENT_BYTES:
        print(
            f"视频文件 {len(content) / 1024 / 1024:.1f}MB 超过附件上限"
            f"（{MAX_VIDEO_ATTACHMENT_BYTES / 1024 / 1024:.0f}MB），改为仅发送链接：{url}"
        )
        return None
    return content
 
 
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
    头像固定使用本地文件 assets/trump_avatar.jpg，
    不再请求 truthsocial.com 的 CDN（该 CDN 会在 TLS 握手层面
    拦截非浏览器请求，导致每次运行都 fallback 成默认头像）。
    只在首次调用时读取磁盘并缓存到内存，避免重复 I/O。
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
    用二分查找切分一个单独就超过 max_width 的词（比如一长串没有空格的
    URL）。原来逐字符线性扫描在这种情况下是 O(word_len^2)；这里每次
    切分是 O(log word_len) 次宽度测量，总体降到 O(word_len log word_len)。
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
        # lo 至少是 start+1：即使单个字符本身就超宽，也强制吞掉一个字符，
        # 避免死循环（和原来的逐字符实现行为一致）。
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
 
    # ------------------------------------------------------------
    # 媒体：按 media_kind（image / video / None）决定预留高度，
    # 而不是按"是否下载成功"决定 —— 这样即使抓图失败，也会画一个
    # 占位框而不是让图片区域整个消失。
    # ------------------------------------------------------------
    media_kind = post["media"]["type"] if post["media"] else None
 
    source_image = None
    video_preview = None
    if media_kind == "image":
        source_image = download_image(post["media"]["url"])
    elif media_kind == "video":
        # 目前 CNN 数据源里视频没有单独的预览图字段（preview_url 恒为
        # None），download_image 会立刻短路返回 None，不产生额外网络
        # 请求。保留这行是为了在数据源未来提供缩略图时自动生效。
        video_preview = download_image(post["media"]["preview_url"])
 
    media_height = 0
    if media_kind == "image":
        media_height = min(560, int(content_width * 0.63)) + 28
    elif media_kind == "video":
        media_height = min(460, int(content_width * 0.52)) + 28
 
    footer_height = 82
    card_height = top_height + body_height + media_height + footer_height + padding
 
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
 
    if media_kind == "image":
        image_top = y + 12
        image_height = media_height - 28
 
        if source_image is not None:
            fitted = ImageOps.fit(source_image, (content_width, image_height),
                                   method=Image.Resampling.LANCZOS)
            mask = Image.new("L", (content_width, image_height), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, content_width, image_height), radius=18, fill=255
            )
            card.paste(fitted, (padding, image_top), mask)
        else:
            # 下载失败时的占位框，而不是让这块区域悄悄消失
            rounded_rectangle(
                draw,
                (padding, image_top, padding + content_width, image_top + image_height),
                radius=18, fill="#F3F4F6", outline="#E4E4E4", width=2,
            )
            placeholder_text = "图片未能加载，请点击下方链接查看原贴"
            text_width = draw.textlength(placeholder_text, font=FONT_META)
            draw.text(
                (padding + (content_width - text_width) / 2, image_top + image_height / 2 - 10),
                placeholder_text, font=FONT_META, fill="#9CA3AF",
            )
 
        y = image_top + image_height + 28
 
    elif media_kind == "video":
        video_top = y + 12
        video_height = media_height - 28
 
        if video_preview is not None:
            fitted = ImageOps.fit(video_preview, (content_width, video_height),
                                   method=Image.Resampling.LANCZOS)
            mask = Image.new("L", (content_width, video_height), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, content_width, video_height), radius=18, fill=255
            )
            card.paste(fitted, (padding, video_top), mask)
 
            center_x = padding + content_width // 2
            center_y = video_top + video_height // 2
            draw.ellipse((center_x - 47, center_y - 47, center_x + 47, center_y + 47), fill="#111827")
            draw.polygon(
                [(center_x - 12, center_y - 22), (center_x - 12, center_y + 22), (center_x + 25, center_y)],
                fill="#FFFFFF",
            )
        else:
            rounded_rectangle(
                draw,
                (padding, video_top, padding + content_width, video_top + video_height),
                radius=18, fill="#111827",
            )
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
            embed["title"] = "Original Post on Truth Social "
            embed["url"] = post["url"]
            embed["image"] = {"url": f"attachment://{card_filename}"}
            if post["timestamp"]:
                embed["timestamp"] = post["timestamp"]
        embeds.append(embed)
    return embeds[:10]
 
 
def _send_discord_payload(payload, card_path, video_bytes=None, video_filename=None):
    """
    只负责把已经准备好的 payload + 附件发出去，并处理 429 重试。
    卡片渲染、DeepL 翻译、视频下载都在调用方一次性做完 —— 429 重试时
    不应该把这些开销再重复付一遍，这里只重发 HTTP 请求本身。
    """
    for attempt in range(MAX_DISCORD_RETRIES):
        with card_path.open("rb") as card_file:
            files = {"files[0]": (card_path.name, card_file, "image/png")}
            if video_bytes is not None:
                files["files[1]"] = (video_filename, video_bytes, "video/mp4")
 
            response = requests.post(
                WEBHOOK_URL,
                data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                files=files,
                timeout=90,
            )
        if response.status_code == 429:
            retry_after = float(response.json().get("retry_after", 2))
            time.sleep(retry_after + 1)
            continue
        response.raise_for_status()
        return
    raise RuntimeError(f"Discord 429 重试 {MAX_DISCORD_RETRIES} 次后仍未发送成功")
 
 
def post_to_discord(post):
    translated_text = build_description(post)
    card_path = create_post_card(post)
 
    payload = {
        "embeds": build_embeds(post, translated_text, card_path.name),
        "allowed_mentions": {"parse": []},
    }
 
    video_bytes = None
    video_filename = None
    if post["media"] and post["media"]["type"] == "video":
        video_url = post["media"]["url"]
        video_bytes = download_video_bytes(video_url)
        if video_bytes is not None:
            video_filename = Path(urlparse(video_url).path).name or f"{post['id']}.mp4"
            print(f"视频已下载（{len(video_bytes) / 1024 / 1024:.1f}MB），作为附件上传")
        else:
            # 下载失败或超过附件大小上限时，退回纯链接兜底 ——
            # 不保证 Discord 会自动生成播放器，但至少是个能点开看的入口。
            payload["content"] = video_url
 
    try:
        _send_discord_payload(payload, card_path, video_bytes, video_filename)
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
            "content_hash": hashlib.sha256(f"discord-test-{int(now)}".encode("utf-8")).hexdigest(),
        }
        post_to_discord(test_post)
        print("测试成功：已发送模拟帖子。未读取 CNN，未修改 seen_trump.json。")
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
