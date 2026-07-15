import hashlib
import json
import os
import re
from pathlib import Path

import requests

LIVE_URL = "https://wallstreetcn.com/live/global"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = Path("seen.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def load_seen():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return []


def save_seen(seen):
    STATE_FILE.write_text(
        json.dumps(seen[-3000:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def post_to_discord(text):
    content = f"**华尔街见闻｜全球快讯**\n{text}\n{LIVE_URL}"
    if len(content) > 2000:
        content = content[:1950] + "…"

    response = requests.post(
        WEBHOOK_URL,
        json={"username": "华尔街见闻快讯", "content": content},
        timeout=20,
    )
    response.raise_for_status()


def get_items():
    response = requests.get(LIVE_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()

    html = response.text
    print("Page status:", response.status_code)
    print("Page length:", len(html))

    # 从 SSR / Nuxt 页面内容中提取带时间的快讯文本
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = (
        html.replace("&nbsp;", " ")
        .replace("&quot;", '"')
        .replace("&#x2F;", "/")
        .replace("&amp;", "&")
    )
    html = re.sub(r"\s+", " ", html)

    pattern = r"(?=(\d{2}:\d{2}\s.{15,1200}?)(?=\s\d{2}:\d{2}\s|$))"
    raw_items = re.findall(pattern, html)

    items = []
    used = set()

    for text in raw_items:
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) < 25:
            continue

        if any(x in text[:100] for x in ["登录", "下载APP", "实时行情", "财经日历"]):
            continue

        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if key not in used:
            items.append((key, text))
            used.add(key)

    print("Parsed items:", len(items))
    return items[:100]


def main():
    seen = load_seen()
    seen_set = set(seen)
    items = get_items()

    # 为了你现在能立刻看到效果：首次直接发最近 5 条
    if not seen:
        first_batch = items[:5]

        for key, text in reversed(first_batch):
            post_to_discord(text)
            seen.append(key)

        # 同时写入当前所有项目，防止下次再刷旧新闻
        seen.extend(key for key, _ in items if key not in set(seen))
        save_seen(seen)
        print(f"首次测试：推送 {len(first_batch)} 条。")
        return

    new_items = [(key, text) for key, text in items if key not in seen_set]

    for key, text in reversed(new_items):
        post_to_discord(text)
        seen.append(key)

    save_seen(seen)
    print(f"推送新快讯：{len(new_items)} 条。")


if __name__ == "__main__":
    main()
