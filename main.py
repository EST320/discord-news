import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

LIVE_URL = "https://wallstreetcn.com/live"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = Path("seen.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    )
}


def load_seen():
    if not STATE_FILE.exists():
        return []
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_seen(seen):
    STATE_FILE.write_text(
        json.dumps(seen[-1000:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def send_discord(text):
    payload = {
        "username": "华尔街见闻快讯",
        "content": f"**华尔街见闻｜快讯**\n{text}\n{LIVE_URL}",
    }
    response = requests.post(WEBHOOK_URL, json=payload, timeout=20)
    response.raise_for_status()


def clean_text(text):
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_live_items():
    response = requests.get(LIVE_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    texts = []

    for tag in soup.select("article, li, div"):
        text = clean_text(tag.get_text(" ", strip=True))

        if 25 <= len(text) <= 500 and re.match(r"^\d{1,2}:\d{2}", text):
            texts.append(text)

    unique = []
    known = set()

    for text in texts:
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if key not in known:
            unique.append((key, text))
            known.add(key)

    return unique[:30]


def main():
    seen = load_seen()
    seen_set = set(seen)

    items = get_live_items()
    new_items = [(key, text) for key, text in items if key not in seen_set]

    if not seen:
        save_seen([key for key, _ in items])
        print("首次运行：已建立基线，不推送旧快讯。")
        return

    for key, text in reversed(new_items[:10]):
        send_discord(text)
        seen.append(key)

    save_seen(seen)
    print(f"推送 {min(len(new_items), 10)} 条新快讯。")


if __name__ == "__main__":
    main()
