"""
Support & Resistance Level Detection — V2 Multi-Timeframe

Merged from backtest V2 findings + original bounce-bot architecture.

Key upgrades over V1:
  - Multi-timeframe S/R: 1h (3 days) + 4h (10 days) candles, not just 5m noise
  - Touch counting: levels must have 2+ historical bounces to qualify
  - Confluence scoring: levels confirmed on both 1h+4h get strength bonus
  - Confirmation signals: RSI extreme, Bollinger Band z-score, rejection wicks
  - Dual data source: tries OKX first (global liquidity), Binance.US fallback

Levels are refreshed every 15 minutes and saved to data/sr_levels.json.
"""

import json
import logging
import os
import threading
import time
import numpy as np
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import requests

logger = logging.getLogger("SR")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OKX_BASE = "https://www.okx.com/api/v5/market"
BINANCE_BASE = "https://api.binance.us/api/v3"

OKX_SYMBOLS = {"btc": "BTC-USDT", "eth": "ETH-USDT", "sol": "SOL-USDT"}
BINANCE_SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}

# How close spot must be to a level to count as "at support/resistance"
PROXIMITY_PCT = 0.0015  # 0.15%

# S/R detection parameters
SR_LOOKBACK_1H = 72     # 3 days of 1h candles
SR_LOOKBACK_4H = 60     # 10 days of 4h candles
SWING_WINDOW = 3         # Candles on each side for swing detection
MIN_TOUCHES = 2          # Minimum bounces to qualify
CLUSTER_PCT = 0.003      # 0.3% clustering for nearby levels

ROUND_INCREMENTS = {"btc": 500, "eth": 25, "sol": 5}

SR_FILE = "data/sr_levels.json"
REFRESH_INTERVAL = 900  # 15 minutes

# Confirmation thresholds
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
BB_ZSCORE_EXTREME = 1.5


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
    type: str           # "support", "resistance", or "both"
    source: str         # "swing", "round", "session", or joined "swing+round"
    strength: float     # 0-1 normalized strength score
    touches: int        # Historical bounce count
    timeframe: str      # "1h", "4h", "1h+4h"
    asset: str
    timestamp: int = 0


@dataclass
class ConfirmationResult:
    """Confirmation signals available at trade entry time."""
    rsi: float = 50.0
    rsi_confirms: bool = False
    bb_zscore: float = 0.0
    bb_confirms: bool = False
    rejection_wick: bool = False
    n_confirmations: int = 0


# ---------------------------------------------------------------------------
# Data fetching — OKX primary, Binance.US fallback
# ---------------------------------------------------------------------------

def _fetch_klines_okx(asset: str, interval: str, limit: int = 200) -> List[Candle]:
    """Fetch klines from OKX. No auth needed."""
    symbol = OKX_SYMBOLS.get(asset)
    if not symbol:
        return []

    okx_interval_map = {
        "1m": "1m", "5m": "5m", "15m": "15m",
        "1h": "1H", "4h": "4H", "1d": "1D",
    }
    okx_interval = okx_interval_map.get(interval, interval)

    try:
        all_candles = []
        after = ""
        remaining = limit

        while remaining > 0:
            params = {
                "instId": symbol,
                "bar": okx_interval,
                "limit": str(min(remaining, 100)),
            }
            if after:
                params["after"] = after

            resp = requests.get(
                f"{OKX_BASE}/history-candles",
                params=params, timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json().get("data", [])
            if not batch:
                break

            for c in batch:
                all_candles.append(Candle(
                    timestamp=int(c[0]) // 1000,
                    open=float(c[1]), high=float(c[2]),
                    low=float(c[3]), close=float(c[4]),
                    volume=float(c[5]),
                ))

            after = batch[-1][0]
            remaining -= len(batch)
            if len(batch) < 100:
                break
            time.sleep(0.1)

        all_candles.sort(key=lambda c: c.timestamp)
        return all_candles

    except Exception as e:
        logger.warning("OKX fetch %s %s: %s", asset, interval, e)
        return []


def _fetch_klines_binance(asset: str, interval: str, limit: int = 200) -> List[Candle]:
    """Fallback: fetch klines from Binance.US."""
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
        logger.warning("Binance fetch %s %s: %s", asset, interval, e)
        return []


def fetch_klines(asset: str, interval: str, limit: int = 200) -> List[Candle]:
    """Fetch candles — OKX first, Binance.US fallback."""
    candles = _fetch_klines_okx(asset, interval, limit)
    if candles:
        return candles
    logger.info("OKX unavailable for %s %s, trying Binance.US", asset, interval)
    return _fetch_klines_binance(asset, interval, limit)


def fetch_spot_price(asset: str) -> Optional[float]:
    """Get current spot price — OKX first, Binance fallback."""
    symbol = OKX_SYMBOLS.get(asset)
    if symbol:
        try:
            resp = requests.get(
                f"{OKX_BASE}/ticker",
                params={"instId": symbol}, timeout=10,
            )
            data = resp.json().get("data", [])
            if data:
                return float(data[0]["last"])
        except Exception:
            pass

    symbol = BINANCE_SYMBOLS.get(asset)
    if not symbol:
        return None
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/ticker/price",
            params={"symbol": symbol}, timeout=10,
        )
        return float(resp.json()["price"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Multi-Timeframe S/R Detection
# ---------------------------------------------------------------------------

def _detect_swing_levels(candles: List[Candle], asset: str,
                         timeframe: str, pivot_len: int = 3) -> List[Level]:
    """
    Find swing highs/lows with touch counting.
    Touches = how many times price came within CLUSTER_PCT and reversed.
    """
    n = len(candles)
    if n < pivot_len * 2 + 1:
        return []

    highs = np.array([c.high for c in candles])
    lows = np.array([c.low for c in candles])
    closes = np.array([c.close for c in candles])

    swing_highs = []
    swing_lows = []

    for i in range(pivot_len, n - pivot_len):
        if highs[i] == max(highs[i - pivot_len:i + pivot_len + 1]):
            swing_highs.append((highs[i], candles[i].timestamp))
        if lows[i] == min(lows[i - pivot_len:i + pivot_len + 1]):
            swing_lows.append((lows[i], candles[i].timestamp))

    levels = []

    for swings, ltype in [(swing_highs, "resistance"), (swing_lows, "support")]:
        if not swings:
            continue

        sorted_s = sorted(swings, key=lambda x: x[0])
        clusters = [[sorted_s[0]]]
        mean_price = sorted_s[0][0]

        for s in sorted_s[1:]:
            if mean_price > 0 and abs(s[0] - mean_price) / mean_price <= CLUSTER_PCT:
                clusters[-1].append(s)
                mean_price = np.mean([x[0] for x in clusters[-1]])
            else:
                clusters.append([s])
                mean_price = s[0]

        for cluster in clusters:
            price = float(np.mean([s[0] for s in cluster]))
            cluster_touches = len(cluster)
            last_ts = max(s[1] for s in cluster)

            # Count additional touches
            tolerance = price * CLUSTER_PCT
            extra_touches = 0
            for i in range(1, n - 1):
                if ltype == "support":
                    if abs(lows[i] - price) <= tolerance and closes[i] > price:
                        extra_touches += 1
                elif ltype == "resistance":
                    if abs(highs[i] - price) <= tolerance and closes[i] < price:
                        extra_touches += 1

            total_touches = cluster_touches + extra_touches
            strength = min(1.0, total_touches / 6.0)

            levels.append(Level(
                price=round(price, 2), type=ltype, source="swing",
                strength=strength, touches=total_touches,
                timeframe=timeframe, asset=asset, timestamp=last_ts,
            ))

    return levels


def _detect_round_numbers(spot_price: float, asset: str) -> List[Level]:
    """Generate round number S/R near current price."""
    increment = ROUND_INCREMENTS.get(asset, 500)
    levels = []
    base = int(spot_price / increment) * increment
    for offset in range(-5, 6):
        price = base + offset * increment
        if price <= 0:
            continue
        if abs(price - spot_price) / spot_price <= 0.02:
            lvl_type = "support" if price < spot_price else "resistance"
            levels.append(Level(
                price=float(price), type=lvl_type, source="round",
                strength=0.3, touches=0, timeframe="static", asset=asset,
            ))
    return levels


def _detect_session_levels(candles_1h: List[Candle], asset: str) -> List[Level]:
    """Prior session (4h, 24h) high/low as S/R."""
    levels = []
    if not candles_1h:
        return levels

    now = time.time()

    recent_4h = [c for c in candles_1h if c.timestamp > now - 4 * 3600]
    if recent_4h:
        levels.append(Level(
            price=max(c.high for c in recent_4h), type="resistance",
            source="session", strength=0.5, touches=1,
            timeframe="4h_session", asset=asset,
        ))
        levels.append(Level(
            price=min(c.low for c in recent_4h), type="support",
            source="session", strength=0.5, touches=1,
            timeframe="4h_session", asset=asset,
        ))

    recent_24h = [c for c in candles_1h if c.timestamp > now - 24 * 3600]
    if recent_24h:
        levels.append(Level(
            price=max(c.high for c in recent_24h), type="resistance",
            source="session", strength=0.7, touches=1,
            timeframe="24h_session", asset=asset,
        ))
        levels.append(Level(
            price=min(c.low for c in recent_24h), type="support",
            source="session", strength=0.7, touches=1,
            timeframe="24h_session", asset=asset,
        ))

    return levels


def _merge_and_cluster(all_levels: List[Level]) -> List[Level]:
    """
    Merge nearby levels with multi-TF confluence scoring.
    Levels found on both 1h and 4h get a strength bonus.
    """
    if not all_levels:
        return []

    sorted_levels = sorted(all_levels, key=lambda l: l.price)
    merged = []
    i = 0

    while i < len(sorted_levels):
        cluster = [sorted_levels[i]]
        ref_price = sorted_levels[i].price
        j = i + 1

        while j < len(sorted_levels) and ref_price > 0 and \
              (sorted_levels[j].price - ref_price) / ref_price <= CLUSTER_PCT:
            cluster.append(sorted_levels[j])
            j += 1

        price = float(np.mean([l.price for l in cluster]))
        total_touches = sum(l.touches for l in cluster)

        timeframes = set()
        sources = set()
        for l in cluster:
            for tf in l.timeframe.split("+"):
                if tf:
                    timeframes.add(tf)
            for src in l.source.split("+"):
                if src:
                    sources.add(src)

        tf_str = "+".join(sorted(timeframes))
        src_str = "+".join(sorted(sources))

        # Confluence bonuses
        real_tfs = timeframes - {"static", "4h_session", "24h_session"}
        mtf_bonus = 0.2 if len(real_tfs) > 1 else 0
        src_bonus = 0.1 if len(sources) > 1 else 0
        base_strength = max(l.strength for l in cluster)
        strength = min(1.0, base_strength + mtf_bonus + src_bonus)

        types = set(l.type for l in cluster)
        if "support" in types and "resistance" in types:
            ltype = "both"
        elif "support" in types:
            ltype = "support"
        else:
            ltype = "resistance"

        last_ts = max(l.timestamp for l in cluster)

        merged.append(Level(
            price=round(price, 2), type=ltype, source=src_str,
            strength=strength, touches=total_touches, timeframe=tf_str,
            asset=cluster[0].asset, timestamp=last_ts,
        ))
        i = j

    return merged


# ---------------------------------------------------------------------------
# Confirmation Signals
# ---------------------------------------------------------------------------

def compute_rsi(candles: List[Candle], period: int = 14) -> float:
    """Compute RSI from recent candles."""
    if len(candles) < period + 1:
        return 50.0
    closes = [c.close for c in candles[-(period + 1):]]
    deltas = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_bb_zscore(candles: List[Candle], period: int = 20) -> float:
    """Bollinger Band z-score — how many std devs from the mean."""
    if len(candles) < period:
        return 0.0
    closes = [c.close for c in candles[-period:]]
    mean = sum(closes) / len(closes)
    std = (sum((c - mean)**2 for c in closes) / len(closes)) ** 0.5
    if std == 0:
        return 0.0
    return (candles[-1].close - mean) / std


def detect_rejection_wick(candles: List[Candle], direction: str,
                          n_candles: int = 3) -> bool:
    """
    Check last n candles for rejection wick.
    direction: "up" = long lower wick, "down" = long upper wick
    """
    for candle in candles[-n_candles:]:
        body = candle.body_size
        full_range = candle.range_size
        if full_range == 0:
            continue

        if direction == "up":
            lower_wick = min(candle.open, candle.close) - candle.low
            if lower_wick > body and lower_wick > full_range * 0.5:
                return True
        elif direction == "down":
            upper_wick = candle.high - max(candle.open, candle.close)
            if upper_wick > body and upper_wick > full_range * 0.5:
                return True

    return False


def check_confirmations(candles_1m: List[Candle], direction: str) -> ConfirmationResult:
    """
    Check all confirmation signals for a bounce trade.
    direction: "up" (at support) or "down" (at resistance)
    """
    result = ConfirmationResult()

    if not candles_1m or len(candles_1m) < 15:
        return result

    result.rsi = compute_rsi(candles_1m, period=14)
    if direction == "up" and result.rsi < RSI_OVERSOLD:
        result.rsi_confirms = True
    elif direction == "down" and result.rsi > RSI_OVERBOUGHT:
        result.rsi_confirms = True

    result.bb_zscore = compute_bb_zscore(candles_1m, period=20)
    if direction == "up" and result.bb_zscore < -BB_ZSCORE_EXTREME:
        result.bb_confirms = True
    elif direction == "down" and result.bb_zscore > BB_ZSCORE_EXTREME:
        result.bb_confirms = True

    result.rejection_wick = detect_rejection_wick(candles_1m, direction, n_candles=3)

    result.n_confirmations = sum([
        result.rsi_confirms, result.bb_confirms, result.rejection_wick,
    ])

    return result


# ---------------------------------------------------------------------------
# Main S/R computation
# ---------------------------------------------------------------------------

def compute_levels(asset: str) -> List[Level]:
    """Compute all S/R levels using multi-timeframe analysis."""
    all_levels = []

    candles_1h = fetch_klines(asset, "1h", SR_LOOKBACK_1H)
    candles_4h = fetch_klines(asset, "4h", SR_LOOKBACK_4H)
    spot = fetch_spot_price(asset)

    logger.debug("%s: fetched %d 1h, %d 4h candles",
                 asset.upper(), len(candles_1h), len(candles_4h))

    # 1h swing levels
    if len(candles_1h) >= 7:
        all_levels.extend(
            _detect_swing_levels(candles_1h, asset, "1h", pivot_len=SWING_WINDOW))

    # 4h swing levels (30% strength boost — more significant timeframe)
    if len(candles_4h) >= 7:
        levels_4h = _detect_swing_levels(candles_4h, asset, "4h", pivot_len=SWING_WINDOW)
        for l in levels_4h:
            l.strength = min(1.0, l.strength * 1.3)
        all_levels.extend(levels_4h)

    # Round numbers
    if spot:
        all_levels.extend(_detect_round_numbers(spot, asset))

    # Session highs/lows
    if candles_1h:
        all_levels.extend(_detect_session_levels(candles_1h, asset))

    # Merge, cluster, filter
    merged = _merge_and_cluster(all_levels)
    qualified = [l for l in merged if l.touches >= MIN_TOUCHES or "swing" not in l.source]

    if spot:
        qualified.sort(key=lambda l: abs(l.price - spot))

    logger.info("%s: %d qualified S/R levels (from %d raw) spot=$%.2f",
                asset.upper(), len(qualified), len(all_levels), spot or 0)

    return qualified


# ---------------------------------------------------------------------------
# SRTracker — interface used by bounce_bot.py
# ---------------------------------------------------------------------------

class SRTracker:
    """Manages S/R levels + confirmation signals for all assets."""

    def __init__(self, assets: List[str], save_path: str = SR_FILE):
        self.assets = assets
        self.save_path = save_path
        self._levels: dict[str, List[Level]] = {}
        self._spot_cache: dict[str, float] = {}
        self._candles_1m_cache: dict[str, List[Candle]] = {}
        self._last_refresh: float = 0
        self._last_1m_fetch: dict[str, float] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
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

    def _refresh_1m_candles(self, asset: str):
        """Fetch recent 1m candles for confirmation signals."""
        now = time.time()
        if now - self._last_1m_fetch.get(asset, 0) < 30:
            return
        candles = fetch_klines(asset, "1m", 30)
        if candles:
            self._candles_1m_cache[asset] = candles
            self._last_1m_fetch[asset] = now

    def get_spot(self, asset: str) -> Optional[float]:
        return self._spot_cache.get(asset)

    def get_levels(self, asset: str) -> List[Level]:
        with self._lock:
            return list(self._levels.get(asset, []))

    def nearest_support(self, asset: str, price: Optional[float] = None) -> Optional[Level]:
        if price is None:
            price = self._spot_cache.get(asset)
        if price is None:
            return None
        with self._lock:
            supports = [l for l in self._levels.get(asset, [])
                        if l.type in ("support", "both") and l.price <= price]
        return max(supports, key=lambda l: l.price) if supports else None

    def nearest_resistance(self, asset: str, price: Optional[float] = None) -> Optional[Level]:
        if price is None:
            price = self._spot_cache.get(asset)
        if price is None:
            return None
        with self._lock:
            resistances = [l for l in self._levels.get(asset, [])
                           if l.type in ("resistance", "both") and l.price >= price]
        return min(resistances, key=lambda l: l.price) if resistances else None

    def is_near_support(self, asset: str, price: Optional[float] = None) -> Tuple[bool, Optional[Level]]:
        if price is None:
            price = self._spot_cache.get(asset)
        if price is None:
            return False, None
        with self._lock:
            supports = [l for l in self._levels.get(asset, [])
                        if l.type in ("support", "both")]
        for level in supports:
            if abs(price - level.price) / price <= PROXIMITY_PCT:
                return True, level
        return False, None

    def is_near_resistance(self, asset: str, price: Optional[float] = None) -> Tuple[bool, Optional[Level]]:
        if price is None:
            price = self._spot_cache.get(asset)
        if price is None:
            return False, None
        with self._lock:
            resistances = [l for l in self._levels.get(asset, [])
                           if l.type in ("resistance", "both")]
        for level in resistances:
            if abs(price - level.price) / price <= PROXIMITY_PCT:
                return True, level
        return False, None

    def check_bounce_signal(self, asset: str, entry_side: str
                            ) -> Tuple[bool, Optional[Level], Optional[float]]:
        """
        Check if spot is at an S/R level supporting a bounce.
        YES buys → check near support. NO buys → check near resistance.
        Returns (should_trade, level, spot_price)
        """
        spot = fetch_spot_price(asset)
        if spot is None:
            return True, None, None

        self._spot_cache[asset] = spot

        if entry_side == "yes":
            at_level, level = self.is_near_support(asset, spot)
        else:
            at_level, level = self.is_near_resistance(asset, spot)

        return at_level, level, spot

    def check_confirmations(self, asset: str, direction: str) -> ConfirmationResult:
        """
        Check RSI, BB z-score, and rejection wick signals.
        direction: "up" (at support) or "down" (at resistance)
        """
        self._refresh_1m_candles(asset)
        candles = self._candles_1m_cache.get(asset, [])
        return check_confirmations(candles, direction)

    def get_level_summary(self, asset: str) -> dict:
        """Summary for dashboard display."""
        with self._lock:
            levels = self._levels.get(asset, [])

        spot = self._spot_cache.get(asset)
        support_levels = [l for l in levels if l.type in ("support", "both")]
        resistance_levels = [l for l in levels if l.type in ("resistance", "both")]

        nearest_sup = None
        nearest_res = None
        if spot:
            below = [l for l in support_levels if l.price <= spot]
            above = [l for l in resistance_levels if l.price >= spot]
            if below:
                nearest_sup = max(below, key=lambda l: l.price)
            if above:
                nearest_res = min(above, key=lambda l: l.price)

        return {
            "total_levels": len(levels),
            "support_count": len(support_levels),
            "resistance_count": len(resistance_levels),
            "spot": spot,
            "nearest_support": {
                "price": nearest_sup.price,
                "strength": nearest_sup.strength,
                "touches": nearest_sup.touches,
                "timeframe": nearest_sup.timeframe,
                "source": nearest_sup.source,
            } if nearest_sup else None,
            "nearest_resistance": {
                "price": nearest_res.price,
                "strength": nearest_res.strength,
                "touches": nearest_res.touches,
                "timeframe": nearest_res.timeframe,
                "source": nearest_res.source,
            } if nearest_res else None,
            "last_refresh": datetime.fromtimestamp(
                self._last_refresh, tz=timezone.utc).isoformat()
            if self._last_refresh else None,
        }
