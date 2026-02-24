"""
Momentum Continuation Signals — V3.1

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

V3.1 fix: Switched to Binance primary with real taker_buy_vol (kline[9])
instead of OKX candle-direction approximation. Aligns with window_logger.py
ground truth that produced the signal discovery stats above.

Data sources: Binance.US primary, OKX fallback (no auth needed).
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
    taker_buy_vol: float = 0.0  # Real taker buy volume (Binance kline[9])

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
# Data fetching — Binance.US primary (has taker_buy_vol), OKX fallback
# ---------------------------------------------------------------------------

def _fetch_klines_binance(asset: str, interval: str, limit: int = 200) -> List[Candle]:
    """Primary: fetch klines from Binance.US with real taker_buy_vol."""
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
            taker_buy_vol=float(c[9]),  # Real taker buy volume
        ) for c in resp.json() if float(c[2]) > 0]
    except Exception as e:
        logger.warning("Binance fetch %s %s: %s", asset, interval, e)
        return []


def _fetch_klines_okx(asset: str, interval: str, limit: int = 200) -> List[Candle]:
    """Fallback: fetch klines from OKX (no taker_buy_vol available)."""
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
                # OKX doesn't provide taker_buy_vol — approximate from candle direction
                vol = float(c[5])
                all_candles.append(Candle(
                    timestamp=int(c[0]) // 1000,
                    open=float(c[1]), high=float(c[2]),
                    low=float(c[3]), close=float(c[4]),
                    volume=vol,
                    taker_buy_vol=vol if float(c[4]) >= float(c[1]) else 0.0,
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


def fetch_klines(asset: str, interval: str, limit: int = 200) -> List[Candle]:
    """Fetch candles — Binance.US first (real taker_buy_vol), OKX fallback."""
    candles = _fetch_klines_binance(asset, interval, limit)
    if candles:
        return candles
    logger.info("Binance unavailable for %s %s, trying OKX", asset, interval)
    return _fetch_klines_okx(asset, interval, limit)


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

        V3.1: Uses 1m candle open/close and real taker_buy_vol (Binance kline[9])
        to match window_logger.py ground truth computation:
        - f5m_dir: candles[0].open vs candles[4].close (first 5 1m candles)
        - f5m_ofi: 2*(sum(taker_buy_vol)/sum(volume))-1 for first 5 candles
        - mid_dir: candles[5].open vs candles[9].close (mid 5 1m candles)
        - taker_buy_ratio: sum(taker_buy_vol)/sum(volume) for all window candles

        Also computes BB z-score and RSI from 1m candles.
        """
        candles = self._candle_cache.get(asset, [])

        # Filter 1m candles to this window — match by timestamp
        # Candle timestamp is the open time; window candles start at window_open_ts
        window_candles = [c for c in candles
                          if window_open_ts <= c.timestamp < window_open_ts + 900]
        # Sort by timestamp to ensure correct indexing
        window_candles.sort(key=lambda c: c.timestamp)

        # --- Signal 1: f5m_dir (first 5 minutes) ---
        # Match window_logger: candles[0].open vs candles[4].close
        first5 = window_candles[:5]
        if len(first5) >= 5:
            f5m_dir = "yes" if first5[4].close >= first5[0].open else "no"
        elif len(first5) >= 2:
            f5m_dir = "yes" if first5[-1].close >= first5[0].open else "no"
        else:
            f5m_dir = "yes"  # neutral default if insufficient data

        # --- Signal 2: f5m_ofi (first 5 min order flow imbalance) ---
        # Match window_logger: 2 * (sum(taker_buy_vol) / sum(volume)) - 1
        if first5:
            f5m_total = sum(c.volume for c in first5)
            f5m_buy = sum(c.taker_buy_vol for c in first5)
            f5m_ofi = 2 * (f5m_buy / f5m_total) - 1 if f5m_total > 0 else 0.0
        else:
            f5m_ofi = 0.0

        # --- Signal 3: mid_dir (mid 5 minutes, candles 5-9) ---
        # Match window_logger: candles[5].open vs candles[9].close
        mid5 = window_candles[5:10]
        if len(mid5) >= 5:
            mid_dir = "yes" if mid5[4].close >= mid5[0].open else "no"
        elif len(mid5) >= 2:
            mid_dir = "yes" if mid5[-1].close >= mid5[0].open else "no"
        elif len(first5) >= 5 and len(window_candles) > 5:
            # Have some mid candles but less than 2 — compare last available to first5 close
            mid_dir = "yes" if window_candles[-1].close >= first5[4].close else "no"
        else:
            mid_dir = "yes"  # neutral default

        # --- Signal 4: taker_buy_ratio (all window candles) ---
        # Match window_logger: taker_buy_vol / volume
        if window_candles:
            w_total = sum(c.volume for c in window_candles)
            w_buy = sum(c.taker_buy_vol for c in window_candles)
            taker_buy_ratio = w_buy / w_total if w_total > 0 else 0.5
        else:
            taker_buy_ratio = 0.5

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

        # Spot return — use first candle open vs last candle close
        if window_candles and window_candles[0].open > 0:
            spot_return_pct = ((window_candles[-1].close - window_candles[0].open)
                               / window_candles[0].open * 100)
        else:
            spot_return_pct = 0.0

        return MomentumSignal(
            f5m_dir=f5m_dir,
            f5m_ofi=round(f5m_ofi, 4),
            mid_dir=mid_dir,
            taker_buy_ratio=round(taker_buy_ratio, 4),
            yes_conviction=yes_conv,
            no_conviction=no_conv,
            bb_z=round(bb_z, 3),
            rsi=round(rsi, 1),
            spot_return_pct=round(spot_return_pct, 4),
        )
