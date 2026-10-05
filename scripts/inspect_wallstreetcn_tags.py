"""Debug helper: dump raw Wallstreetcn list and detail payloads for a few items
per channel, to see which fields could be used to classify news by market.

Usage:
    python scripts/inspect_wallstreetcn_tags.py
"""

import json
import time

import requests

API_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://wallstreetcn.com/live",
    "Origin": "https://wallstreetcn.com",
}

CHANNELS = {
    "US": "us-stock-channel",
    "A-share": "a-stock-channel",
    "HK": "hk-stock-channel",
}

ITEMS_PER_CHANNEL = 3
DETAIL_DELAY_SECONDS = 0.6


def get_live_items(channel):
    response = requests.get(
        API_URL,
        params={
            "channel": channel,
            "client": "pc",
            "cursor": 0,
            "limit": ITEMS_PER_CHANNEL,
        },
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json().get("data", {})
    items = data.get("items", [])

    if not isinstance(items, list):
        raise RuntimeError(f"{channel} returned non-list items: {type(items)}")

    return items


def get_live_detail(news_id):
    response = requests.get(
        f"{API_URL}/{news_id}",
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    return payload.get("data", payload)


def print_json(label, value):
    print(f"\n--- {label} ---")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def inspect_item(source_market, item):
    news_id = str(item.get("id", "")).strip()
    title = str(item.get("title", "")).strip()
    content = str(item.get("content_text", "")).strip()
    display_time = item.get("display_time")

    print("\n" + "=" * 100)
    print(f"Source channel: {source_market}")
    print(f"News ID: {news_id}")
    print(f"Title: {title}")
    print(f"Timestamp: {display_time}")
    print(f"First 300 chars of content: {content[:300]}")

    # Dump the full list-endpoint item first, to see whether tags/symbols/channel
    # fields are already present at the list level.
    print_json("Full list-endpoint item", item)

    if not news_id:
        print("No news ID, skipping the detail lookup.")
        return

    try:
        detail = get_live_detail(news_id)
    except requests.RequestException as exc:
        print(f"Detail request failed: {exc}")
        return

    # Highlight the fields that might identify the market.
    candidate_fields = {
        key: detail.get(key)
        for key in (
            "id",
            "title",
            "content_text",
            "display_time",
            "channel",
            "channels",
            "category",
            "categories",
            "asset_tags",
            "tags",
            "symbols",
            "assets",
            "stocks",
            "stock",
            "market",
            "markets",
            "exchange",
            "exchanges",
            "uri",
            "related_assets",
        )
        if key in detail
    }

    print_json("Detail-endpoint candidate classification fields", candidate_fields)
    print_json("Full detail-endpoint data", detail)


def main():
    for market_name, channel in CHANNELS.items():
        print("\n" + "#" * 100)
        print(f"Inspecting: {market_name} / {channel}")
        print("#" * 100)

        try:
            items = get_live_items(channel)
        except requests.RequestException as exc:
            print(f"List request failed: {exc}")
            continue

        print(f"Got {len(items)} list item(s) for this channel.")

        for item in items:
            inspect_item(market_name, item)
            time.sleep(DETAIL_DELAY_SECONDS)

    print("\nDone. See each \"Detail-endpoint candidate classification fields\" block above.")


if __name__ == "__main__":
    main()
