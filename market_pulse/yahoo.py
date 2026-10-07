"""Trading-activity lookups from Yahoo Finance's chart endpoint (no key, no quota)."""

import time

import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
REQUEST_DELAY_SECONDS = 0.15
MAX_RETRIES = 2

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def yahoo_symbol(symbol):
    """Share classes are written BRK/B or BRK.B elsewhere and BRK-B on Yahoo."""
    return symbol.strip().upper().replace("/", "-").replace(".", "-")


def dollar_volumes(payload):
    """Daily close x volume, oldest first, from one chart response."""
    result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    return [
        close * volume
        for close, volume in zip(quote.get("close") or [], quote.get("volume") or [])
        if isinstance(close, (int, float)) and isinstance(volume, (int, float)) and volume > 0
    ]


def fetch_average_dollar_volume(symbol, days=20):
    """Average daily dollar volume over the last `days` sessions, or None if unavailable."""
    url = CHART_URL.format(symbol=yahoo_symbol(symbol))
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Two months always covers 20 sessions, holidays included.
            response = SESSION.get(url, params={"range": "2mo", "interval": "1d"}, timeout=20)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            recent = dollar_volumes(response.json())[-days:]
            return sum(recent) / len(recent) if recent else None
        except (requests.exceptions.RequestException, ValueError):
            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)
    return None


def average_dollar_volumes(symbols, days=20):
    """{symbol: average daily dollar volume}. Symbols Yahoo cannot serve are left out."""
    averages = {}
    for symbol in symbols:
        value = fetch_average_dollar_volume(symbol, days)
        if value is not None:
            averages[symbol] = value
        time.sleep(REQUEST_DELAY_SECONDS)
    return averages
