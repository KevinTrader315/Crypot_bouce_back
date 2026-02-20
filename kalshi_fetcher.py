"""
Kalshi BTC Market Data Fetcher

Fetches live market data from Kalshi's public REST API for Bitcoin
range/bracket markets across multiple timeframes (15m, hourly, daily).
"""

import requests
from typing import Optional

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Known Kalshi series tickers by asset and timeframe
SERIES_TICKERS = {
    "15m": "KXBTC15M",
    "hourly": "KXBTC",
    "daily": "KXBTC",
}

ASSET_SERIES = {
    "btc": {"15m": "KXBTC15M", "hourly": "KXBTC", "daily": "KXBTC"},
    "eth": {"15m": "KXETH15M", "hourly": "KXETH", "daily": "KXETH"},
    "sol": {"15m": "KXSOL15M", "hourly": "KXSOL", "daily": "KXSOL"},
}


def fetch_markets(
    series_ticker: str = "KXBTC",
    status: str = "open",
    limit: int = 200,
    cursor: Optional[str] = None,
) -> dict:
    """
    Fetch markets from Kalshi API.

    Args:
        series_ticker: The series to filter by (e.g., KXBTC, KXBTC15M)
        status: Market status filter (open, closed, settled)
        limit: Max results per page (1-1000)
        cursor: Pagination cursor

    Returns:
        API response dict with 'markets' list and 'cursor' for pagination.
    """
    params = {
        "series_ticker": series_ticker,
        "status": status,
        "limit": limit,
    }
    if cursor:
        params["cursor"] = cursor

    resp = requests.get(f"{KALSHI_API}/markets", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_all_markets_for_series(
    series_ticker: str = "KXBTC", status: str = "open"
) -> list[dict]:
    """Fetch all markets for a series, handling pagination."""
    all_markets = []
    cursor = None

    while True:
        data = fetch_markets(series_ticker, status, limit=1000, cursor=cursor)
        markets = data.get("markets", [])
        all_markets.extend(markets)

        cursor = data.get("cursor")
        if not cursor or not markets:
            break

    return all_markets


def fetch_event(event_ticker: str) -> dict:
    """Fetch a specific event and its nested markets."""
    resp = requests.get(
        f"{KALSHI_API}/events/{event_ticker}", timeout=15
    )
    resp.raise_for_status()
    return resp.json()


def fetch_orderbook(ticker: str, depth: int = 10) -> Optional[dict]:
    """
    Fetch the order book for a specific market ticker.

    Returns dict with 'orderbook' containing 'yes' and 'no' bid arrays.
    Each entry is [price_cents, quantity].
    The 'yes' array = YES-side bids (demand for YES).
    The 'no' array = NO-side bids (demand for NO = effective YES asks).
    """
    try:
        resp = requests.get(
            f"{KALSHI_API}/markets/{ticker}/orderbook",
            params={"depth": depth},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("orderbook")
    except Exception:
        return None


def compute_orderbook_imbalance(orderbook: dict) -> Optional[float]:
    """Compute YES-side demand imbalance from Kalshi orderbook.

    Sums contract depth on both sides of the book.
    Returns 0.0-1.0 where >0.5 = more YES demand, <0.5 = more NO demand.
    Returns None if orderbook is empty or unavailable.
    """
    if not orderbook:
        return None

    yes_bids = orderbook.get("yes", [])
    no_bids = orderbook.get("no", [])

    yes_depth = sum(level[1] for level in yes_bids) if yes_bids else 0
    no_depth = sum(level[1] for level in no_bids) if no_bids else 0

    total = yes_depth + no_depth
    if total == 0:
        return None

    return yes_depth / total


def group_markets_by_event(markets: list[dict]) -> dict[str, list[dict]]:
    """Group a flat list of markets by their event_ticker."""
    events = {}
    for m in markets:
        et = m.get("event_ticker", "unknown")
        events.setdefault(et, []).append(m)
    return events


def parse_kalshi_bracket(market: dict) -> dict:
    """
    Parse a single Kalshi bracket/market into a normalized structure.

    Returns:
        {
            "ticker": str,
            "event_ticker": str,
            "title": str,
            "floor_strike": float or None,
            "cap_strike": float or None,
            "yes_bid": float,
            "yes_ask": float,
            "no_bid": float,
            "no_ask": float,
            "last_price": float,
            "volume": int,
            "open_interest": int,
            "close_time": str (ISO),
            "status": str,
        }
    """
    return {
        "ticker": market.get("ticker", ""),
        "event_ticker": market.get("event_ticker", ""),
        "title": market.get("title", ""),
        "subtitle": market.get("subtitle", ""),
        "floor_strike": market.get("floor_strike"),
        "cap_strike": market.get("cap_strike"),
        "yes_bid": _cents_to_dollars(market.get("yes_bid")),
        "yes_ask": _cents_to_dollars(market.get("yes_ask")),
        "no_bid": _cents_to_dollars(market.get("no_bid")),
        "no_ask": _cents_to_dollars(market.get("no_ask")),
        "last_price": _cents_to_dollars(market.get("last_price")),
        "volume": market.get("volume", 0),
        "volume_24h": market.get("volume_24h", 0),
        "open_interest": market.get("open_interest", 0),
        "close_time": market.get("close_time", ""),
        "expiration_time": market.get("expiration_time", ""),
        "status": market.get("status", ""),
    }


def _cents_to_dollars(val) -> Optional[float]:
    """Convert Kalshi cent-denominated price to dollars."""
    if val is None:
        return None
    return val / 100.0


def parse_kalshi_event(event_ticker: str, markets: list[dict]) -> dict:
    """
    Parse a Kalshi event (collection of brackets) into a normalized structure.

    Returns:
        {
            "platform": "kalshi",
            "event_ticker": str,
            "brackets": list of parsed bracket dicts,
            "total_volume": int,
            "close_time": str,
            "bracket_count": int,
            "implied_prob_sum": float (should be ~1.0),
        }
    """
    brackets = [parse_kalshi_bracket(m) for m in markets]
    brackets.sort(key=lambda b: b["floor_strike"] or 0)

    total_vol = sum(b["volume"] for b in brackets)

    # Sum of midpoint prices (implied probabilities) should ~= 1.0
    prob_sum = 0.0
    for b in brackets:
        if b["yes_bid"] is not None and b["yes_ask"] is not None:
            mid = (b["yes_bid"] + b["yes_ask"]) / 2.0
            prob_sum += mid
        elif b["last_price"] is not None:
            prob_sum += b["last_price"]

    close_time = brackets[0]["close_time"] if brackets else ""

    return {
        "platform": "kalshi",
        "event_ticker": event_ticker,
        "brackets": brackets,
        "total_volume": total_vol,
        "close_time": close_time,
        "bracket_count": len(brackets),
        "implied_prob_sum": prob_sum,
    }


def get_kalshi_btc_snapshot(timeframe: str = "15m") -> list[dict]:
    """
    Get a full snapshot of current Kalshi BTC range markets.

    Args:
        timeframe: "15m", "hourly", or "daily"

    Returns:
        List of normalized event dicts, each containing bracket arrays.
    """
    series = SERIES_TICKERS.get(timeframe, "KXBTC")
    markets = fetch_all_markets_for_series(series, status="open")
    grouped = group_markets_by_event(markets)

    events = []
    for event_ticker, bracket_markets in grouped.items():
        parsed = parse_kalshi_event(event_ticker, bracket_markets)
        events.append(parsed)

    # Sort by close time
    events.sort(key=lambda e: e["close_time"])
    return events


def calculate_kalshi_fee(contracts: int, price: float) -> float:
    """
    Calculate Kalshi trading fee.

    Formula: 0.07 * min(price, 1 - price)
    Capped at $0.035 per contract ($3.50 per 100 contracts).
    """
    if price <= 0 or price >= 1:
        return 0.0
    per_contract = 0.07 * min(price, 1.0 - price)
    per_contract = min(per_contract, 0.035)
    return per_contract * contracts


def find_highest_volume_brackets(event: dict, top_n: int = 5) -> list[dict]:
    """Return the top N brackets by volume for an event."""
    return sorted(event["brackets"], key=lambda b: b["volume"], reverse=True)[:top_n]


def find_brackets_near_price(
    event: dict, btc_price: float, n: int = 3
) -> list[dict]:
    """Find the N brackets closest to a given BTC price."""
    scored = []
    for b in event["brackets"]:
        if b["floor_strike"] is not None and b["cap_strike"] is not None:
            mid = (b["floor_strike"] + b["cap_strike"]) / 2.0
            scored.append((abs(mid - btc_price), b))
    scored.sort(key=lambda x: x[0])
    return [b for _, b in scored[:n]]


if __name__ == "__main__":
    print("=== Kalshi BTC Range Markets ===\n")

    for tf_label, tf_key in [("15-Minute", "15m"), ("Hourly", "hourly")]:
        print(f"--- {tf_label} Markets ---")
        try:
            events = get_kalshi_btc_snapshot(tf_key)
        except Exception as e:
            print(f"  Error fetching {tf_label}: {e}\n")
            continue

        if not events:
            print(f"  No open {tf_label} BTC markets found.\n")
            continue

        for ev in events[:3]:
            print(f"  Event: {ev['event_ticker']}")
            print(f"    Brackets: {ev['bracket_count']}")
            print(f"    Volume: {ev['total_volume']:,} contracts")
            print(f"    Implied Prob Sum: {ev['implied_prob_sum']:.4f}")
            print(f"    Closes: {ev['close_time']}")

            top = find_highest_volume_brackets(ev, 3)
            for b in top:
                floor = b["floor_strike"] or 0
                cap = b["cap_strike"] or 0
                print(
                    f"      ${floor:,.0f}-${cap:,.0f}  "
                    f"bid={b['yes_bid']}  ask={b['yes_ask']}  "
                    f"vol={b['volume']}"
                )
            print()
