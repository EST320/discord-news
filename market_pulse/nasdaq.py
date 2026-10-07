"""Client for the public JSON API behind nasdaq.com's calendar pages.

No API key and no quota, but it is the website's own backend rather than a
documented service: it expects browser-like headers and can change without
notice.

Run as a script for a connectivity check that posts nothing:
    python -m market_pulse.nasdaq
"""

import time
from datetime import datetime, timedelta, timezone

import requests

API_URL = "https://api.nasdaq.com/api"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5

# Nasdaq's reporting-time codes -> the session keys used by the earnings card.
SESSION_CODES = {"time-pre-market": "bmo", "time-after-hours": "amc"}


def get_data(path, params):
    """GET one endpoint and return its "data" object, retrying transient failures."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(f"{API_URL}/{path}", params=params, headers=HEADERS, timeout=30)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError(f"Unexpected Nasdaq response type: {type(payload)}")
            code = (payload.get("status") or {}).get("rCode")
            if code not in (None, 200):
                raise RuntimeError(f"Nasdaq API returned status {code} for {path}")
            return payload.get("data") or {}
        except (requests.exceptions.RequestException, ValueError, RuntimeError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * attempt
                print(f"Nasdaq request attempt {attempt} failed ({exc!r}), retrying in {wait}s")
                time.sleep(wait)
    raise last_exc


def parse_number(text):
    """'$883,527,910,000' -> 883527910000.0, '($0.12)' -> -0.12, '' or 'N/A' -> None."""
    cleaned = str(text or "").strip().replace("$", "").replace(",", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    try:
        value = float(cleaned.strip("()"))
    except ValueError:
        return None
    return -value if negative else value


def fetch_earnings(day):
    """Companies reporting on one date, as normalised dicts."""
    rows = get_data("calendar/earnings", {"date": day.isoformat()}).get("rows") or []
    companies = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        companies.append({
            "symbol": symbol,
            "name": str(row.get("name") or symbol).strip(),
            "market_cap": parse_number(row.get("marketCap")) or 0.0,
            "hour": SESSION_CODES.get(row.get("time"), ""),
            "eps_forecast": parse_number(row.get("epsForecast")),
            "last_year_eps": parse_number(row.get("lastYearEPS")),
        })
    return companies


def fetch_upcoming_ipos(month):
    """Deals with an expected pricing date in the given month ('YYYY-MM'), as normalised dicts."""
    data = get_data("ipo/calendar", {"date": month})
    rows = ((data.get("upcoming") or {}).get("upcomingTable") or {}).get("rows") or []
    deals = []
    for row in rows:
        try:
            date = datetime.strptime(str(row.get("expectedPriceDate") or ""), "%m/%d/%Y").date()
        except ValueError:
            continue
        deals.append({
            "date": date,
            "symbol": str(row.get("proposedTickerSymbol") or "").strip(),
            "name": str(row.get("companyName") or "").strip(),
            "exchange": str(row.get("proposedExchange") or "").strip(),
            "price": str(row.get("proposedSharePrice") or "").strip(),
            "shares": parse_number(row.get("sharesOffered")) or 0.0,
            "deal_size": parse_number(row.get("dollarValueOfSharesOffered")) or 0.0,
        })
    return deals


def self_check():
    """Fetch a little of each calendar and fail loudly if either comes back unusable."""
    today = datetime.now(timezone.utc).date()

    # Some weekday in the next two weeks always has reports; a single empty day proves nothing.
    reports = 0
    for offset in range(1, 15):
        day = today + timedelta(days=offset)
        if day.weekday() < 5:
            companies = fetch_earnings(day)
            reports += len(companies)
            print(f"earnings {day}: {len(companies)} companies")
            if reports >= 20:
                break
    if reports == 0:
        raise RuntimeError("Nasdaq earnings calendar returned no companies for two weeks")

    deals = fetch_upcoming_ipos(today.strftime("%Y-%m"))
    print(f"ipo calendar {today:%Y-%m}: {len(deals)} upcoming deals")
    print("Nasdaq API reachable.")


if __name__ == "__main__":
    self_check()
