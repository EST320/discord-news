import json
import time

import requests

API_URL = "https://api-prod.wallstreetcn.com/apiv1/content/lives"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://wallstreetcn.com/live",
    "Origin": "https://wallstreetcn.com",
}

CHANNELS = {
    "美股": "us-stock-channel",
    "A股": "a-stock-channel",
    "港股": "hk-stock-channel",
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
        raise RuntimeError(f"{channel} 返回的 items 不是列表：{type(items)}")

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
    print(f"来源频道：{source_market}")
    print(f"新闻 ID：{news_id}")
    print(f"标题：{title}")
    print(f"时间戳：{display_time}")
    print(f"正文前 300 字：{content[:300]}")

    # 先输出列表接口的完整 item，确认列表层是否已经有 tags/symbols/channel 等字段。
    print_json("列表接口完整 item", item)

    if not news_id:
        print("没有新闻 ID，跳过详情查询。")
        return

    try:
        detail = get_live_detail(news_id)
    except requests.RequestException as exc:
        print(f"详情接口请求失败：{exc}")
        return

    # 重点输出我们可能用来做市场识别的字段。
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

    print_json("详情接口候选分类字段", candidate_fields)
    print_json("详情接口完整 data", detail)


def main():
    for market_name, channel in CHANNELS.items():
        print("\n" + "#" * 100)
        print(f"开始检查：{market_name} / {channel}")
        print("#" * 100)

        try:
            items = get_live_items(channel)
        except requests.RequestException as exc:
            print(f"列表接口请求失败：{exc}")
            continue

        print(f"该频道本次拿到 {len(items)} 条列表消息。")

        for item in items:
            inspect_item(market_name, item)
            time.sleep(DETAIL_DELAY_SECONDS)

    print("\n检查结束。请把日志中每个“详情接口候选分类字段”贴回来。")


if __name__ == "__main__":
    main()
