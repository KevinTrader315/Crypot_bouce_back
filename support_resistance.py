"""
Support & Resistance Level Detection for Bounce-Back Bot

Fetches Binance spot candles and identifies key price levels where BTC/ETH
are likely to bounce. Used as a filter: only take bounce-back trades when
spot price is near a support (for YES buys) or resistance (for NO buys).

Levels are derived from:
  1. Swing highs/lows (pivot points from 5m and 1h candles)
  2. Round numbers ($500 increments for BTC, $25 for ETH)
  3. Liquidity zones (clusters of equal highs/lows)
  4. Prior session high/low (last 4h, 24h)

Levels are refreshed every 15 minutes and saved to data/sr_levels.json.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger("SR")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BINANCE_BASE = "https://api.binance.us/api/v3"
BINANCE_SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}

# How close spot must be to a level to count as "at support/resistance"
# As a fraction of price (0.001 = 0.1%)
PROXIMITY_PCT = 0.0015  # 0.15% — ~$100 for BTC at $68K, ~$3 for ETH at $2K

# Round number increments per asset
ROUND_INCREMENTS = {"btc": 500, "eth": 25, "sol": 5}

SR_FILE = "data/sr_levels.json"
REFRESH_INTERVAL = 900  # 15 minutes


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def range_size(self) -> float:
        return self.high - self.low


@dataclass
class Level:
    """A support or resistance price level."""
    price: float
    type: str          # "support" or "resistance"
    source: str        # "swing", "round", "liquidity", "session"
    strength: int      # 1-5, higher = more touches or confluence
    asset: str
    timestamp: int = 0  # when the level was created/last touched


# ---------------------------------------------------------------------------
# Binance data fetching
# ---------------------------------------------------------------------------

def _fetch_klines(asset: str, interval: str, limit: int) -> list[Candle]:
    """Fetch klines from Binance.US. No auth needed."""
    symbol = BINANCE_SYMBOLS.get(asset, "BTCUSDT")
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        return [Candle(
            timestamp=int(c[0]) // 1000,
            open=float(c[1]), high=float(c[2]),
            low=float(c[3]), close=float(c[4]),
            volume=float(c[5]),
        ) for c in resp.json() if float(c[2]) > 0]
    except Exception as e:
        logger.warning("Fetch klines %s %s: %s", asset, interval, e)
        return []


def fetch_spot_price(asset: str) -> Optional[float]:
    """Get current spot price for an asset."""
    symbol = BINANCE_SYMBOLS.get(asset)
    if not symbol:
        return None
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/ticker/price",
            params={"symbol": symbol},
            timeout=10,
        )
        return float(resp.json()["price"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Level detection algorithms
# ---------------------------------------------------------------------------

def _detect_swing_levels(candles: list[Candle], asset: str,
                         pivot_len: int = 3) -> list[Level]:
    """Find swing highs (resistance) and swing lows (support)."""
    levels = []
    for i in range(pivot_len, len(candles) - pivot_len):
        c = candles[i]

        # Swing high = resistance
        is_high = all(c.high >= candles[i - j].high for j in range(1, pivot_len + 1))
        is_high = is_high and all(c.high >= candles[i + j].high for j in range(1, pivot_len + 1))
        if is_high:
            levels.append(Level(
                price=c.high, type="resistance", source="swing",
                strength=1, asset=asset, timestamp=c.timestamp,
            ))

        # Swing low = support
        is_low = all(c.low <= candles[i - j].low for j in range(1, pivot_len + 1))
        is_low = is_low and all(c.low <= candles[i + j].low for j in range(1, pivot_len + 1))
        if is_low:
            levels.append(Level(
                price=c.low, type="support", source="swing",
                strength=1, asset=asset, timestamp=c.timestamp,
            ))

    return levels


def _detect_round_numbers(spot_price: float, asset: str) -> list[Level]:
    """Generate round number support/resistance near current price."""
    increment = ROUND_INCREMENTS.get(asset, 500)
    levels = []
    # Find round numbers within 2% of current price
    base = int(spot_price / increment) * increment
    for offset in range(-5, 6):
        price = base + offset * increment
        if abs(price - spot_price) / spot_price <= 0.02:
            lvl_type = "support" if price < spot_price else "resistance"
            levels.append(Level(
                price=price, type=lvl_type, source="round",
                strength=2, asset=asset,
            ))
    return levels


def _detect_liquidity_clusters(candles: list[Candle], asset: str,
                               tolerance_pct: float = 0.001) -> list[Level]:
    """Find clusters of equal highs/lows (liquidity pools)."""
    levels = []
    if not candles:
        return levels

    ref_price = candles[-1].close

    # Equal highs → resistance
    highs = sorted(c.high for c in candles)
    i = 0
    while i < len(highs):
        cluster = [highs[i]]
        j = i + 1
        while j < len(highs) and (highs[j] - highs[i]) / ref_price < tolerance_pct:
            cluster.append(highs[j])
            j += 1
        if len(cluster) >= 3:
            avg_price = sum(cluster) / len(cluster)
            levels.append(Level(
                price=avg_price, type="resistance", source="liquidity",
                strength=min(len(cluster), 5), asset=asset,
            ))
        i = j

    # Equal lows → support
    lows = sorted(c.low for c in candles)
    i = 0
    while i < len(lows):
        cluster = [lows[i]]
        j = i + 1
        while j < len(lows) and (lows[j] - lows[i]) / ref_price < tolerance_pct:
            cluster.append(lows[j])
            j += 1
        if len(cluster) >= 3:
            avg_price = sum(cluster) / len(cluster)
            levels.append(Level(
                price=avg_price, type="support", source="liquidity",
                strength=min(len(cluster), 5), asset=asset,
            ))
        i = j

    return levels


def _detect_session_levels(candles: list[Candle], asset: str) -> list[Level]:
    """Prior session (4h, 24h) high/low as S/R."""
    levels = []
    if not candles:
        return levels

    now = time.time()

    # Last 4 hours
    recent_4h = [c for c in candles if c.timestamp > now - 4 * 3600]
    if recent_4h:
        h4_high = max(c.high for c in recent_4h)
        h4_low = min(c.low for c in recent_4h)
        levels.append(Level(
            price=h4_high, type="resistance", source="session",
            strength=3, asset=asset,
        ))
        levels.append(Level(
            price=h4_low, type="support", source="session",
            strength=3, asset=asset,
        ))

    # Last 24 hours
    recent_24h = [c for c in candles if c.timestamp > now - 24 * 3600]
    if recent_24h:
        d_high = max(c.high for c in recent_24h)
        d_low = min(c.low for c in recent_24h)
        levels.append(Level(
            price=d_high, type="resistance", source="session",
            strength=4, asset=asset,
        ))
        levels.append(Level(
            price=d_low, type="support", source="session",
            strength=4, asset=asset,
        ))

    return levels


# ---------------------------------------------------------------------------
# Cluster and deduplicate levels
# ---------------------------------------------------------------------------

def _cluster_levels(levels: list[Level], tolerance_pct: float = 0.001) -> list[Level]:
    """Merge nearby levels, keeping the strongest."""
    if not levels:
        return []

    sorted_levels = sorted(levels, key=lambda l: l.price)
    clustered = []
    i = 0

    while i < len(sorted_levels):
        cluster = [sorted_levels[i]]
        j = i + 1
        ref = sorted_levels[i].price
        while j < len(sorted_levels) and ref > 0 and \
              (sorted_levels[j].price - ref) / ref < tolerance_pct:
            cluster.append(sorted_levels[j])
            j += 1

        # Merge: use weighted average price, sum strengths, keep strongest type
        total_strength = sum(l.strength for l in cluster)
        avg_price = sum(l.price * l.strength for l in cluster) / total_strength
        best = max(cluster, key=lambda l: l.strength)
        sources = list(set(l.source for l in cluster))

        clustered.append(Level(
            price=round(avg_price, 2),
            type=best.type,
            source="+".join(sorted(sources)),
            strength=min(total_strength, 5),
            asset=best.asset,
            timestamp=best.timestamp,
        ))
        i = j

    return clustered


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def compute_levels(asset: str) -> list[Level]:
    """Compute all S/R levels for an asset. Returns clustered, deduplicated list."""
    all_levels = []

    # Fetch candles at multiple timeframes
    candles_5m = _fetch_klines(asset, "5m", 200)   # ~16 hours
    candles_1h = _fetch_klines(asset, "1h", 48)    # 2 days

    # Spot price for round numbers
    spot = fetch_spot_price(asset)

    # 1. Swing levels from 5m candles (short-term S/R)
    if candles_5m:
        swings_5m = _detect_swing_levels(candles_5m, asset, pivot_len=3)
        all_levels.extend(swings_5m)

    # 2. Swing levels from 1h candles (stronger S/R)
    if candles_1h:
        swings_1h = _detect_swing_levels(candles_1h, asset, pivot_len=2)
        # 1h swings are stronger
        for s in swings_1h:
            s.strength = 3
        all_levels.extend(swings_1h)

    # 3. Round numbers
    if spot:
        all_levels.extend(_detect_round_numbers(spot, asset))

    # 4. Liquidity clusters from 5m
    if candles_5m:
        all_levels.extend(_detect_liquidity_clusters(candles_5m, asset))

    # 5. Session highs/lows from 1h data
    if candles_1h:
        all_levels.extend(_detect_session_levels(candles_1h, asset))

    # Cluster nearby levels together
    support = _cluster_levels([l for l in all_levels if l.type == "support"])
    resistance = _cluster_levels([l for l in all_levels if l.type == "resistance"])

    combined = support + resistance
    # Sort by proximity to current price (most relevant first)
    if spot:
        combined.sort(key=lambda l: abs(l.price - spot))

    return combined


# ---------------------------------------------------------------------------
# Level checker — used by bounce bot at signal time
# ---------------------------------------------------------------------------

class SRTracker:
    """Manages S/R levels for all assets. Refreshes periodically."""

    def __init__(self, assets: list[str], save_path: str = SR_FILE):
        self.assets = assets
        self.save_path = save_path
        self._levels: dict[str, list[Level]] = {}
        self._spot_cache: dict[str, float] = {}
        self._last_refresh: float = 0
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        """Load cached levels from disk."""
        try:
            with open(self.save_path) as f:
                data = json.load(f)
            for asset, lvls in data.get("levels", {}).items():
                self._levels[asset] = [Level(**l) for l in lvls]
            self._last_refresh = data.get("refreshed_at", 0)
            logger.info("Loaded %d S/R levels from cache",
                        sum(len(v) for v in self._levels.values()))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self):
        """Persist levels to disk."""
        os.makedirs(os.path.dirname(self.save_path) or ".", exist_ok=True)
        data = {
            "refreshed_at": self._last_refresh,
            "refreshed_at_str": datetime.fromtimestamp(
                self._last_refresh, tz=timezone.utc).isoformat(),
            "levels": {
                asset: [asdict(l) for l in lvls]
                for asset, lvls in self._levels.items()
            },
        }
        with open(self.save_path, "w") as f:
            json.dump(data, f, indent=2)

    def refresh(self, force: bool = False):
        """Recompute levels for all assets."""
        now = time.time()
        if not force and now - self._last_refresh < REFRESH_INTERVAL:
            return

        with self._lock:
            for asset in self.assets:
                try:
                    levels = compute_levels(asset)
                    self._levels[asset] = levels
                    spot = fetch_spot_price(asset)
                    if spot:
                        self._spot_cache[asset] = spot
                    logger.info("%s: %d S/R levels (spot=$%.2f)",
                                asset.upper(), len(levels), spot or 0)
                except Exception as e:
                    logger.warning("S/R refresh %s: %s", asset, e)

            self._last_refresh = now
            self._save()

    def get_spot(self, asset: str) -> Optional[float]:
        """Get cached spot price (refreshed with levels)."""
        return self._spot_cache.get(asset)

    def nearest_support(self, asset: str, price: Optional[float] = None) -> Optional[Level]:
        """Find the nearest support level AT or BELOW the given price."""
        if price is None:
            price = self._spot_cache.get(asset)
        if price is None:
            return None
        with self._lock:
            supports = [l for l in self._levels.get(asset, [])
                        if l.type == "support" and l.price <= price]
        if not supports:
            return None
        return max(supports, key=lambda l: l.price)  # closest below

    def nearest_resistance(self, asset: str, price: Optional[float] = None) -> Optional[Level]:
        """Find the nearest resistance level AT or ABOVE the given price."""
        if price is None:
            price = self._spot_cache.get(asset)
        if price is None:
            return None
        with self._lock:
            resistances = [l for l in self._levels.get(asset, [])
                           if l.type == "resistance" and l.price >= price]
        if not resistances:
            return None
        return min(resistances, key=lambda l: l.price)  # closest above

    def is_near_support(self, asset: str, price: Optional[float] = None) -> tuple[bool, Optional[Level]]:
        """Check if current price is near a support level.

        Returns (True, level) if within PROXIMITY_PCT of a support.
        """
        if price is None:
            price = self._spot_cache.get(asset)
        if price is None:
            return False, None

        with self._lock:
            supports = [l for l in self._levels.get(asset, [])
                        if l.type == "support"]

        for level in supports:
            dist = abs(price - level.price) / price
            if dist <= PROXIMITY_PCT:
                return True, level
        return False, None

    def is_near_resistance(self, asset: str, price: Optional[float] = None) -> tuple[bool, Optional[Level]]:
        """Check if current price is near a resistance level.

        Returns (True, level) if within PROXIMITY_PCT of a resistance.
        """
        if price is None:
            price = self._spot_cache.get(asset)
        if price is None:
            return False, None

        with self._lock:
            resistances = [l for l in self._levels.get(asset, [])
                           if l.type == "resistance"]

        for level in resistances:
            dist = abs(price - level.price) / price
            if dist <= PROXIMITY_PCT:
                return True, level
        return False, None

    def check_bounce_signal(self, asset: str, entry_side: str) -> tuple[bool, Optional[Level], Optional[float]]:
        """Check if spot price is at a level that supports a bounce.

        For YES buys (price dropped): check if spot is near SUPPORT
        For NO buys (price rallied): check if spot is near RESISTANCE

        Returns (should_trade, level, spot_price)
        """
        spot = fetch_spot_price(asset)
        if spot is None:
            # Can't verify — allow trade but log warning
            return True, None, None

        self._spot_cache[asset] = spot

        if entry_side == "yes":
            # Price dropped → we want spot to be near support (likely to bounce up)
            at_level, level = self.is_near_support(asset, spot)
        else:
            # Price rallied → we want spot to be near resistance (likely to reverse down)
            at_level, level = self.is_near_resistance(asset, spot)

        return at_level, level, spot
