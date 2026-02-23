"""
Momentum Continuation Signals — V3

Computes 4-signal conviction score for 15-min crypto windows:
  1. first_5m_dir — which direction did spot move in first 5 min?
  2. first_5m_ofi — order flow imbalance in first 5 min
  3. mid_dir — which direction at mid-window (5-10 min)?
  4. taker_buy_ratio — ratio of aggressive buyers vs sellers

Signal discovery analysis on 16,639 windows proved:
  - YES conviction 4/4: 77.8% WR, +27.8% edge (N=1292)
  - Sweep dir NO: 83.1% WR, +33.1% edge (N=1251)
  - BB z > 2: 77.2% WR, +27.2% edge (N=838)
  - First 5m YES strong: 72.5% WR, +22.5% edge (N=3373)

Data sources: OKX primary, Binance.US fallback (no auth needed).
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional, List

import requests

logger = logging.getLogger("MomentumSignals")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OKX_BASE = "https://www.okx.com/api/v5/market"
BINANCE_BASE = "https://api.binance.us/api/v3"

OKX_SYMBOLS = {"btc": "BTC-USDT", "eth": "ETH-USDT", "sol": "SOL-USDT"}
BINANCE_SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}


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
class MomentumSignal:
    """Result of momentum analysis for a 15-min window."""
    # 4 conviction signals
    f5m_dir: str           # "yes" or "no" — first 5m spot direction
    f5m_ofi: float         # first 5m OFI (-1 to 1)
    mid_dir: str           # "yes" or "no" — mid-window spot direction
    taker_buy_ratio: float # 0-1, >0.55 = buyers dominate

    # Conviction score
    yes_conviction: int    # 0-4 (how many signals say YES)
    no_conviction: int     # 0-4 (how many signals say NO)

    # Bonus signals (not in core conviction, but logged/boosted)
    bb_z: float            # Bollinger Band z-score
    rsi: float             # RSI value
    spot_return_pct: float # Spot return in the window so far

    @property
    def direction(self) -> str:
        """Which side has conviction."""
        if self.yes_conviction >= 3:
            return "yes"
        elif self.no_conviction >= 3:
            return "no"
        return "none"

    @property
    def conviction(self) -> int:
        return max(self.yes_conviction, self.no_conviction)


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
# Technical indicators
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


# ---------------------------------------------------------------------------
# MomentumTracker
# ---------------------------------------------------------------------------

class MomentumTracker:
    """Fetches spot data and computes momentum signals."""

    def __init__(self, assets: list[str]):
        self.assets = assets
        # Stores rolling 1m candles for each asset (last 30)
        self._candle_cache: dict[str, List[Candle]] = {}
        self._last_fetch: dict[str, float] = {}
        self._spot_cache: dict[str, float] = {}
        # Window tracking: stores spot prices within the current 15-min window
        self._window_spots: dict[str, list[tuple[float, float]]] = {}  # asset -> [(ts, price)]

    def refresh(self, asset: str):
        """Fetch latest 1m candles and spot price."""
        now = time.time()
        # Throttle to every 30s
        if now - self._last_fetch.get(asset, 0) < 30:
            return

        candles = fetch_klines(asset, "1m", 30)
        if candles:
            self._candle_cache[asset] = candles
            self._last_fetch[asset] = now

        spot = fetch_spot_price(asset)
        if spot is not None:
            self._spot_cache[asset] = spot

    def record_spot(self, asset: str, price: float, ts: float):
        """Record a spot price tick for the current window."""
        if asset not in self._window_spots:
            self._window_spots[asset] = []
        self._window_spots[asset].append((ts, price))
        # Keep only last 16 min worth (buffer beyond 15-min window)
        cutoff = ts - 960
        self._window_spots[asset] = [
            (t, p) for t, p in self._window_spots[asset] if t >= cutoff
        ]

    def reset_window(self, asset: str):
        """Clear window spot data for a new window."""
        self._window_spots[asset] = []

    def get_spot(self, asset: str) -> Optional[float]:
        return self._spot_cache.get(asset)

    def compute_signal(self, asset: str, window_open_ts: float,
                       now_ts: float) -> MomentumSignal:
        """Compute the 4-signal conviction score for the current window.

        Uses spot ticks within the window to compute:
        - first_5m_dir: compare spot at 0m vs 5m
        - first_5m_ofi: use 1m candle taker volumes in first 5m
        - mid_dir: compare spot at 5m vs now (7-10m)
        - taker_buy_ratio: aggregate taker buy / total volume

        Also computes BB z-score and RSI from 1m candles.
        """
        candles = self._candle_cache.get(asset, [])
        spots = self._window_spots.get(asset, [])

        # Filter spots to this window
        window_spots = [(ts, p) for ts, p in spots
                        if window_open_ts <= ts <= now_ts]

        # Helper: find spot price closest to a target timestamp
        def spot_at(target_ts: float, tol: float = 90) -> Optional[float]:
            if not window_spots:
                return None
            best_ts, best_p = min(window_spots, key=lambda x: abs(x[0] - target_ts))
            if abs(best_ts - target_ts) <= tol:
                return best_p
            return None

        # --- Signal 1: first_5m_dir ---
        spot_open = spot_at(window_open_ts)
        spot_5m = spot_at(window_open_ts + 300)
        if spot_open and spot_5m:
            f5m_dir = "yes" if spot_5m > spot_open else "no"
        else:
            f5m_dir = "yes"  # neutral default

        # --- Signal 2: first_5m_ofi ---
        # Use 1m candles within the first 5 min of the window
        f5m_start = window_open_ts
        f5m_end = window_open_ts + 300
        f5m_candles = [c for c in candles
                       if f5m_start <= c.timestamp <= f5m_end]
        buy_vol = sum(c.volume for c in f5m_candles if c.is_bullish)
        sell_vol = sum(c.volume for c in f5m_candles if not c.is_bullish)
        total_vol = buy_vol + sell_vol
        f5m_ofi = (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0.0

        # --- Signal 3: mid_dir ---
        spot_now = spot_at(now_ts)
        if spot_5m and spot_now:
            mid_dir = "yes" if spot_now > spot_5m else "no"
        elif spot_open and spot_now:
            mid_dir = "yes" if spot_now > spot_open else "no"
        else:
            mid_dir = "yes"  # neutral default

        # --- Signal 4: taker_buy_ratio ---
        # Use all 1m candles within the window
        window_candles = [c for c in candles
                          if window_open_ts <= c.timestamp <= now_ts]
        w_buy = sum(c.volume for c in window_candles if c.is_bullish)
        w_total = sum(c.volume for c in window_candles)
        taker_buy_ratio = w_buy / w_total if w_total > 0 else 0.5

        # --- Compute conviction ---
        yes_conv = 0
        no_conv = 0

        # Signal 1: first_5m_dir
        if f5m_dir == "yes":
            yes_conv += 1
        else:
            no_conv += 1

        # Signal 2: first_5m_ofi (neutral zone = no point)
        if f5m_ofi > 0.3:
            yes_conv += 1
        elif f5m_ofi < -0.3:
            no_conv += 1

        # Signal 3: mid_dir
        if mid_dir == "yes":
            yes_conv += 1
        else:
            no_conv += 1

        # Signal 4: taker_buy_ratio (neutral zone = no point)
        if taker_buy_ratio > 0.55:
            yes_conv += 1
        elif taker_buy_ratio < 0.45:
            no_conv += 1

        # --- Bonus indicators ---
        bb_z = compute_bb_zscore(candles) if len(candles) >= 20 else 0.0
        rsi = compute_rsi(candles) if len(candles) >= 15 else 50.0

        # Spot return
        if spot_open and spot_now and spot_open > 0:
            spot_return_pct = (spot_now - spot_open) / spot_open * 100
        else:
            spot_return_pct = 0.0

        return MomentumSignal(
            f5m_dir=f5m_dir,
            f5m_ofi=round(f5m_ofi, 3),
            mid_dir=mid_dir,
            taker_buy_ratio=round(taker_buy_ratio, 3),
            yes_conviction=yes_conv,
            no_conviction=no_conv,
            bb_z=round(bb_z, 3),
            rsi=round(rsi, 1),
            spot_return_pct=round(spot_return_pct, 4),
        )
