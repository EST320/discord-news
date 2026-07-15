import hashlib
import json
import os
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

LIVE_URL = "https://wallstreetcn.com/live/global"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = Path("seen.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def load_seen():
    if not STATE_FILE.exists():
        return []

    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_seen(seen):
    STATE_FILE.write_text(
        json.dumps(seen[-5000:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_items():
    response = requests.get(LIVE_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = normalize(soup.get_text(" ", strip=True))

    # 快讯格式：19:29【标题】正文，或 19:21正文
    pattern = r"(?=(?:^|\s)(\d{1,2}:\d{2})(?=\s|【))"
    matches = list(re.finditer(pattern, page_text))

    items = []
    for i, match in enumerate(matches):
        start = match.start(1)
        end = matches[i + 1].start(1) if i + 1 < len(matches) else len(page_text)
        text = normalize(page_text[start:end])

        # 过滤页面导航、行情和日历等非快讯内容
        if len(text) < 12 or len(text) > 3000:
            continue
        if text.startswith(("实时行情", "财经日历", "华尔街见闻 首页")):
            continue

        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        items.append((key, text))

    deduped = []
    used = set()
    for key, text in items:
        if key not in used:
            deduped.append((key, text))
            used.add(key)

    return deduped[:100]


def post_to_discord(text):
    content = f"**华尔街见闻｜全球快讯**\n{text}\n{LIVE_URL}"

    # Discord 单条 content 最大 2000 字符
    if len(content) > 1950:
        content = content[:1940] + "…\n" + LIVE_URL

    response = requests.post(
        WEBHOOK_URL,
        json={
            "username": "华尔街见闻快讯",
            "content": content,
        },
        timeout=20,
    )
    response.raise_for_status()


def main():
    seen = load_seen()
    seen_set = set(seen)
    items = fetch_items()

    # 第一次运行：建立基线，不把当前页面所有历史新闻全刷入 Discord
    if not seen:
        save_seen([key for key, _ in items])
        print(f"首次运行：已记录 {len(items)} 条现有快讯，之后只推送新增快讯。")
        return

    new_items = [(key, text) for key, text in items if key not in seen_set]

    # 页面通常由新到旧排列；倒序发出，Discord 中按时间从旧到新显示
    for key, text in reversed(new_items):
        post_to_discord(text)
        seen.append(key)

    save_seen(seen)
    print(f"发现并推送 {len(new_items)} 条新快讯。")


if __name__ == "__main__":
    main()
