"""Backfill tool: re-send flash news from the last N hours that never reached Discord.

It reuses the parsing and posting code of market_pulse.wallstreetcn, so backfilled
messages look identical to live ones. For each requested channel it:
  1. fetches every item published in the last N hours
  2. skips IDs already recorded in the channel's state file (no duplicates)
  3. posts the rest oldest-first and records them in the state file

Environment variables:
    HOURS      how far back to look (default 6)
    CHANNELS   comma-separated subset of us,a,hk (default all three)
    DRY_RUN    true = only list what would be sent (default false)

Usage (local):
    HOURS=6 CHANNELS=us,hk DRY_RUN=true python -m market_pulse.backfill
or trigger backfill.yml manually from the GitHub Actions page.
"""

import os
import time
from datetime import datetime, timezone

from market_pulse.wallstreetcn import (
    CHANNELS,
    DISCORD_DELAY_SECONDS,
    fetch_page,
    item_to_news,
    load_seen,
    post_to_discord,
    save_seen,
)

HOURS = float(os.environ.get("HOURS", "6"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")
WANTED = [c.strip() for c in os.environ.get("CHANNELS", "us,a,hk").split(",") if c.strip()]

MAX_PAGES = 15


def fetch_window(channel, since_ts):
    """Page backwards until items are older than since_ts. Returns newest first."""
    collected = {}
    cursor = 0

    for _ in range(MAX_PAGES):
        raw_items, next_cursor = fetch_page(channel, cursor)
        if not raw_items:
            break

        page_news = [n for n in (item_to_news(x) for x in raw_items) if n]
        oldest = None
        for news in page_news:
            if news["display_ts"] is None:
                continue
            oldest = news["display_ts"] if oldest is None else min(oldest, news["display_ts"])
            if news["display_ts"] >= since_ts:
                collected[news["id"]] = news

        if oldest is not None and oldest < since_ts:
            break
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return list(collected.values())


def run_channel(key):
    channel = CHANNELS[key]
    since_ts = time.time() - HOURS * 3600

    webhook_url = os.environ.get(channel.webhook_env, "")
    if not webhook_url and not DRY_RUN:
        print(f"[{key}] Skipped: environment variable {channel.webhook_env} is not set")
        return

    seen = load_seen(channel.state_file)
    seen_ids = set(seen)

    window = fetch_window(channel, since_ts)
    pending = [n for n in window if n["id"] not in seen_ids]
    # Send oldest first, same order as the live tracker.
    pending.sort(key=lambda n: n["display_ts"] or 0)

    print(f"[{key}] {len(window)} item(s) in the last {HOURS} hours, {len(pending)} not yet sent")

    if DRY_RUN:
        for n in pending:
            when = datetime.fromtimestamp(n["display_ts"], tz=timezone.utc).astimezone()
            print(f"      [dry run] {when:%m-%d %H:%M}  {n['title'][:60]}")
        return

    sent = 0
    try:
        for news in pending:
            post_to_discord(channel, webhook_url, news)
            seen[news["id"]] = time.time()
            sent += 1
            time.sleep(DISCORD_DELAY_SECONDS)
    finally:
        save_seen(channel.state_file, seen)
        print(f"[{key}] Backfilled {sent} item(s), state written to {channel.state_file.name}")


def main():
    mode = "dry run (nothing is sent)" if DRY_RUN else "live backfill"
    print(f"=== Backfill window: last {HOURS} hours | channels: {','.join(WANTED)} | {mode} ===")
    for key in WANTED:
        if key not in CHANNELS:
            print(f"Unknown channel {key}; choose from: {' / '.join(CHANNELS)}")
            continue
        run_channel(key)


if __name__ == "__main__":
    main()
