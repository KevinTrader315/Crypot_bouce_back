#!/usr/bin/env python3
"""
Momentum Continuation Bot v3.0

Strategy (discovered via signal analysis on 16,639 windows, Feb 2026):
  At minute 7-10 of a 15-min window, compute a 4-signal conviction score.
  If 3+ signals agree on a direction, buy that side's contract (45-78c range).
  Hold to settlement. Stop-loss if conviction collapses AND price drops 15c.

Signal conviction (4 signals):
  1. first_5m_dir — spot direction in first 5 minutes
  2. first_5m_ofi — order flow imbalance in first 5 minutes
  3. mid_dir — spot direction at mid-window (5-10 min)
  4. taker_buy_ratio — ratio of aggressive buyers vs sellers

Win rates from signal discovery:
  - 4/4 conviction: 77.8% WR, +27.8% edge (N=1292)
  - 3/4 conviction: ~65% WR, ~+15% edge
"""

import argparse
import json
import logging
import os
import time
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import requests

from kalshi_auth import load_private_key, sign_request
from kalshi_fetcher import (
    KALSHI_API,
    ASSET_SERIES,
    fetch_all_markets_for_series,
    group_markets_by_event,
    parse_kalshi_bracket,
)
from capital_guard import CapitalGuard
from momentum_signals import MomentumTracker, MomentumSignal, fetch_spot_price

logger = logging.getLogger("MomentumBot")

BOT_VERSION = "3.0.0"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ASSETS = ["btc", "eth", "sol"]


@dataclass
class MomentumConfig:
    """Strategy parameters."""
    eval_window_min: int = 300     # Start checking at 300s remaining (10 min in)
    eval_window_max: int = 480     # Last check at 480s remaining (7 min in)
    min_conviction: int = 3        # Minimum conviction to trade (3 or 4 out of 4)
    min_entry_price: float = 0.45  # Don't buy below 45c (too cheap = low conviction)
    max_entry_price: float = 0.78  # Don't buy above 78c (too expensive, bad risk/reward)
    base_contracts: int = 5
    max_open_positions: int = 3
    stop_loss_enabled: bool = True
    stop_loss_threshold: float = 0.15  # Exit if price drops 15c AND conviction <=1
    time_exit_secs: int = 120      # Exit at 2 min remaining if losing
    time_exit_loss: float = 0.10   # Only time-exit if down >=10c
    mode: str = "paper"
    poll_interval: int = 15
    log_file: str = "data/momentum_trades.jsonl"
    enabled_assets: dict = field(default_factory=lambda: {
        "btc": True, "eth": True, "sol": False
    })


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class MomentumTrade:
    """Single trade record."""
    trade_id: str
    asset: str
    event_ticker: str
    ticker: str
    entry_side: str           # "yes" or "no"
    entry_price: float        # 0.45-0.78 range (expensive side)
    contracts: int
    conviction: int           # 3 or 4
    f5m_dir: str
    f5m_ofi: float
    mid_dir: str
    taker_buy_ratio: float
    bb_z: float
    rsi: float
    entry_time: str
    close_time: str
    status: str = "open"      # open | exited | settled
    exit_price: Optional[float] = None
    exit_type: Optional[str] = None  # "settlement" | "stop_loss" | "time_exit"
    result: Optional[str] = None
    pnl_net: float = 0.0
    notes: str = ""


class TradeLog:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self._trades: dict[str, MomentumTrade] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            with open(self.filepath) as f:
                for line in f:
                    t = json.loads(line)
                    defaults = {
                        'exit_type': None, 'exit_price': None,
                        'result': None, 'pnl_net': 0.0, 'notes': '',
                        'conviction': 0, 'f5m_dir': '', 'f5m_ofi': 0.0,
                        'mid_dir': '', 'taker_buy_ratio': 0.5,
                        'bb_z': 0.0, 'rsi': 50.0,
                    }
                    for key, default in defaults.items():
                        if key not in t:
                            t[key] = default
                    self._trades[t['trade_id']] = MomentumTrade(**t)
        except FileNotFoundError:
            pass

    def save(self, trade: MomentumTrade):
        with self._lock:
            self._trades[trade.trade_id] = trade
            with open(self.filepath, 'w') as f:
                for t in self._trades.values():
                    f.write(json.dumps(asdict(t)) + '\n')

    def get_open(self) -> list[MomentumTrade]:
        return [t for t in self._trades.values() if t.status == 'open']

    def all(self) -> list[MomentumTrade]:
        return list(self._trades.values())

    def summary(self) -> dict:
        closed = [t for t in self._trades.values() if t.status in ('settled', 'exited')]
        wins = [t for t in closed if t.pnl_net > 0]
        conv4 = [t for t in closed if t.conviction == 4]
        conv3 = [t for t in closed if t.conviction == 3]
        stop_losses = [t for t in closed if t.exit_type == 'stop_loss']
        time_exits = [t for t in closed if t.exit_type == 'time_exit']
        settlements = [t for t in closed if t.exit_type in ('settlement', None)]
        return {
            'total': len(self._trades),
            'open': len(self.get_open()),
            'closed': len(closed),
            'wins': len(wins),
            'win_rate': len(wins) / len(closed) if closed else 0,
            'total_pnl': sum(t.pnl_net for t in closed),
            'conv_4': len(conv4),
            'conv_3': len(conv3),
            'conv_4_wr': len([t for t in conv4 if t.pnl_net > 0]) / len(conv4) if conv4 else 0,
            'conv_3_wr': len([t for t in conv3 if t.pnl_net > 0]) / len(conv3) if conv3 else 0,
            'stop_losses': len(stop_losses),
            'time_exits': len(time_exits),
            'settlements': len(settlements),
        }


# ---------------------------------------------------------------------------
# Kalshi trader
# ---------------------------------------------------------------------------

class KalshiTrader:
    def __init__(self, api_key: str, private_key_path: str):
        self.api_key = api_key
        self.private_key = load_private_key(private_key_path)
        self.session = requests.Session()

    def _headers(self, method: str, path: str) -> dict:
        return sign_request(self.private_key, self.api_key, method, path)

    def get_balance(self) -> Optional[int]:
        try:
            path = "/trade-api/v2/portfolio/balance"
            resp = self.session.get(f"{KALSHI_API}/portfolio/balance",
                                    headers=self._headers("GET", path), timeout=10)
            return resp.json().get('balance')
        except Exception:
            return None

    def place_order(self, ticker: str, side: str, contracts: int,
                    price: float) -> Optional[str]:
        """Place a limit order. Returns order_id or None."""
        path = "/trade-api/v2/portfolio/orders"
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": contracts,
            "yes_price": int(price * 100) if side == "yes" else int((1-price) * 100),
        }
        try:
            resp = self.session.post(
                f"{KALSHI_API}/portfolio/orders",
                headers=self._headers("POST", path),
                json=body, timeout=10
            )
            if resp.status_code in (200, 201):
                return resp.json().get('order', {}).get('order_id')
            logger.error("Order failed: %d %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("Order error: %s", e)
        return None

    def sell_position(self, ticker: str, side: str, contracts: int,
                      price: float) -> Optional[str]:
        """Sell (close) a position. Returns order_id or None."""
        path = "/trade-api/v2/portfolio/orders"
        body = {
            "ticker": ticker,
            "action": "sell",
            "side": side,
            "type": "limit",
            "count": contracts,
            "yes_price": int(price * 100) if side == "yes" else int((1-price) * 100),
        }
        try:
            resp = self.session.post(
                f"{KALSHI_API}/portfolio/orders",
                headers=self._headers("POST", path),
                json=body, timeout=10
            )
            if resp.status_code in (200, 201):
                return resp.json().get('order', {}).get('order_id')
            logger.error("Sell order failed: %d %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("Sell error: %s", e)
        return None

    def get_fills(self, ticker: str) -> list[dict]:
        try:
            path = "/trade-api/v2/portfolio/fills"
            resp = self.session.get(
                f"{KALSHI_API}/portfolio/fills",
                headers=self._headers("GET", path),
                params={"ticker": ticker, "limit": 50}, timeout=10
            )
            return resp.json().get('fills', [])
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Price cache — stores rolling contract price history
# ---------------------------------------------------------------------------

class PriceCache:
    """Stores (timestamp, yes_mid) tuples for each event_ticker."""
    def __init__(self, ttl_secs: int = 700):
        self._data: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
        self._ttl = ttl_secs
        self._lock = threading.Lock()

    def record(self, event_ticker: str, yes_mid: float):
        now = time.time()
        with self._lock:
            self._data[event_ticker].append((now, yes_mid))

    def price_at(self, event_ticker: str, target_ts: float,
                 tolerance: float = 30) -> Optional[float]:
        """Get the closest price to target_ts within tolerance seconds."""
        with self._lock:
            entries = list(self._data.get(event_ticker, []))
        if not entries:
            return None
        best = min(entries, key=lambda x: abs(x[0] - target_ts))
        if abs(best[0] - target_ts) <= tolerance:
            return best[1]
        return None

    def latest(self, event_ticker: str) -> Optional[float]:
        with self._lock:
            entries = self._data.get(event_ticker)
            if entries:
                return entries[-1][1]
        return None


# ---------------------------------------------------------------------------
# Main bot
# ---------------------------------------------------------------------------

def kalshi_fee(price: float) -> float:
    return min(0.07 * min(price, 1.0 - price), 0.035)


class MomentumBot:
    def __init__(self, config: MomentumConfig, trader: Optional[KalshiTrader] = None):
        self.config = config
        self.trader = trader
        self.trade_log = TradeLog(config.log_file)
        self.price_cache = PriceCache()
        self.capital_guard = CapitalGuard("bounce-back")
        self.momentum_tracker = MomentumTracker(
            assets=[a for a, e in config.enabled_assets.items() if e])
        self.running = False
        self.trading_enabled = True          # toggle via dashboard to pause entries
        self._entered_windows: set = set()   # prevent double-entry per window
        self._stats = {'signals': 0, 'trades': 0, 'skipped': 0,
                       'conv_3': 0, 'conv_4': 0}
        # event_ticker -> {asset, close_time_ts, close_time_str, window_start_ts}
        self._window_meta: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Market fetching
    # ------------------------------------------------------------------

    def _fetch_markets(self, asset: str) -> list[dict]:
        series_map = ASSET_SERIES.get(asset, {})
        series = series_map.get("15m") if isinstance(series_map, dict) else series_map
        if not series:
            return []
        try:
            raw = fetch_all_markets_for_series(series, status="open")
            return [parse_kalshi_bracket(m) for m in raw]
        except Exception as e:
            logger.warning("Fetch markets %s: %s", asset, e)
            return []

    def _find_active_event(self, markets: list[dict]) -> Optional[dict]:
        """Find the event closest to settlement with enough time remaining."""
        now = time.time()
        events = {}
        for m in markets:
            et = m.get('event_ticker') or ''
            ct = m.get('close_time') or ''
            if not et or not ct:
                continue
            try:
                close_ts = datetime.fromisoformat(
                    ct.replace('Z', '+00:00')).timestamp()
            except Exception:
                continue
            secs_left = close_ts - now
            if self.config.eval_window_min <= secs_left <= self.config.eval_window_max:
                if et not in events:
                    events[et] = {'event_ticker': et, 'close_ts': close_ts,
                                  'secs_left': secs_left, 'markets': []}
                events[et]['markets'].append(m)

        if not events:
            return None
        return min(events.values(), key=lambda e: e['secs_left'])

    def _get_market_for_side(self, event: dict, side: str) -> Optional[dict]:
        """Get the YES or NO market for the given side."""
        for m in event.get('markets', []):
            title = (m.get('title', '') + m.get('subtitle', '') +
                     m.get('ticker', '')).lower()
            if side == 'yes' and 'up' in title:
                return m
            if side == 'no' and 'down' in title:
                return m
        # Fallback: return first market
        return event['markets'][0] if event.get('markets') else None

    # ------------------------------------------------------------------
    # Signal detection
    # ------------------------------------------------------------------

    def _check_signal(self, asset: str, event: dict) -> Optional[dict]:
        """Check if momentum continuation signal is present.

        Signal logic:
          1. Refresh spot data and record spot tick for this window
          2. Compute 4-signal conviction at 7-10 min into window
          3. If conviction >= min_conviction, check contract price is in 45-78c range
        """
        event_ticker = event['event_ticker']
        close_ts = event['close_ts']
        window_open_ts = close_ts - 900  # 15-min window
        now_ts = time.time()

        # Refresh spot data
        self.momentum_tracker.refresh(asset)
        spot = self.momentum_tracker.get_spot(asset)
        if spot:
            self.momentum_tracker.record_spot(asset, spot, now_ts)

        # Compute momentum signal
        signal = self.momentum_tracker.compute_signal(
            asset, window_open_ts, now_ts)

        if signal.direction == "none":
            return None

        if signal.conviction < self.config.min_conviction:
            return None

        self._stats['signals'] += 1

        # Get current contract YES mid price
        now_price = self.price_cache.latest(event_ticker)
        if now_price is None:
            return None

        # Determine entry side and check price range
        entry_side = signal.direction
        min_cents = self.config.min_entry_price * 100
        max_cents = self.config.max_entry_price * 100

        if entry_side == 'yes':
            # Buy YES — check YES price is in 45-78c range
            if not (min_cents <= now_price <= max_cents):
                logger.debug("%s Conv %d/4 YES but price %.0fc outside %.0f-%.0fc",
                             asset.upper(), signal.conviction,
                             now_price, min_cents, max_cents)
                return None
            entry_price = now_price / 100
        else:
            # Buy NO — check NO price (100 - YES) is in 45-78c range
            no_price = 100 - now_price
            if not (min_cents <= no_price <= max_cents):
                logger.debug("%s Conv %d/4 NO but price %.0fc outside %.0f-%.0fc",
                             asset.upper(), signal.conviction,
                             no_price, min_cents, max_cents)
                return None
            entry_price = no_price / 100

        return {
            'asset': asset,
            'event_ticker': event_ticker,
            'entry_side': entry_side,
            'entry_price': entry_price,
            'conviction': signal.conviction,
            'signal': signal,
            'secs_left': event['secs_left'],
            'spot_price': spot,
        }

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    def _execute(self, signal: dict, event: dict):
        asset = signal['asset']
        entry_side = signal['entry_side']
        entry_price = signal['entry_price']
        msig = signal['signal']

        # Capital guard
        investment_cents = int(self.config.base_contracts * entry_price * 100)
        exposure_cents = int(len(self.trade_log.get_open()) * self.config.base_contracts * entry_price * 100)
        allowed, reason = self.capital_guard.check_order(investment_cents, exposure_cents)
        if not allowed:
            logger.info("%s Signal blocked by capital guard: %s", asset.upper(), reason)
            return

        market = self._get_market_for_side(event, entry_side)
        if not market:
            logger.warning("%s No market found for side %s", asset.upper(), entry_side)
            return

        ticker = market.get('ticker', '')
        contracts = self.config.base_contracts
        trade_id = f"{asset}_{signal['event_ticker']}_{int(time.time())}"

        if self.config.mode == 'live' and self.trader:
            order_id = self.trader.place_order(ticker, entry_side, contracts, entry_price)
            if not order_id:
                logger.error("%s Order placement failed", asset.upper())
                return
            logger.info("%s LIVE BUY %s %dx @%.0fc (conv=%d/4, %ds left)",
                        asset.upper(), entry_side.upper(), contracts,
                        entry_price*100, msig.conviction, int(signal['secs_left']))
        else:
            logger.info("%s PAPER BUY %s %dx @%.0fc (conv=%d/4, %ds left)",
                        asset.upper(), entry_side.upper(), contracts,
                        entry_price*100, msig.conviction, int(signal['secs_left']))

        trade = MomentumTrade(
            trade_id=trade_id,
            asset=asset,
            event_ticker=signal['event_ticker'],
            ticker=ticker,
            entry_side=entry_side,
            entry_price=entry_price,
            contracts=contracts,
            conviction=msig.conviction,
            f5m_dir=msig.f5m_dir,
            f5m_ofi=msig.f5m_ofi,
            mid_dir=msig.mid_dir,
            taker_buy_ratio=msig.taker_buy_ratio,
            bb_z=msig.bb_z,
            rsi=msig.rsi,
            entry_time=datetime.now(timezone.utc).isoformat(),
            close_time=datetime.fromtimestamp(
                event['close_ts'], tz=timezone.utc).isoformat(),
        )
        self.trade_log.save(trade)
        self._entered_windows.add(signal['event_ticker'])
        self._stats['trades'] += 1
        if msig.conviction == 4:
            self._stats['conv_4'] += 1
        else:
            self._stats['conv_3'] += 1

        # Spawn exit monitor thread
        t = threading.Thread(
            target=self._monitor_exit, args=(trade,),
            daemon=True, name=f"exit-{trade_id}"
        )
        t.start()

    # ------------------------------------------------------------------
    # Exit monitoring
    # ------------------------------------------------------------------

    def _monitor_exit(self, trade: MomentumTrade):
        """Monitor an open trade for stop-loss and time-exit conditions.

        Primary exit: hold to settlement.
        Stop-loss: if conviction drops to <=1 AND price falls 15c from entry.
        Time exit: at 2 min remaining, if losing >=10c.
        """
        try:
            close_ts = datetime.fromisoformat(
                trade.close_time.replace('Z', '+00:00')).timestamp()
        except Exception:
            return

        window_open_ts = close_ts - 900
        entry_cents = trade.entry_price * 100

        while time.time() < close_ts - 5:
            time.sleep(10)  # Check every 10 seconds

            current_price = self.price_cache.latest(trade.event_ticker)
            if current_price is None:
                continue

            # Calculate our side's price
            if trade.entry_side == 'no':
                our_price_cents = 100 - current_price
            else:
                our_price_cents = current_price

            now_ts = time.time()
            secs_left = close_ts - now_ts
            price_drop = entry_cents - our_price_cents

            # --- Stop-loss check ---
            if (self.config.stop_loss_enabled
                    and price_drop >= self.config.stop_loss_threshold * 100):
                # Recalculate conviction
                try:
                    fresh_signal = self.momentum_tracker.compute_signal(
                        trade.asset, window_open_ts, now_ts)
                    current_conv = fresh_signal.conviction
                except Exception:
                    current_conv = trade.conviction  # keep original if compute fails

                if current_conv <= 1:
                    exit_price = our_price_cents / 100
                    fee = kalshi_fee(trade.entry_price)
                    trade.pnl_net = (exit_price - trade.entry_price - fee) * trade.contracts
                    trade.exit_price = exit_price
                    trade.exit_type = 'stop_loss'
                    trade.status = 'exited'
                    trade.notes = f'stop_loss@{our_price_cents:.0f}c conv={current_conv}'

                    if self.config.mode == 'live' and self.trader:
                        order_id = self.trader.sell_position(
                            trade.ticker, trade.entry_side, trade.contracts,
                            exit_price)
                        if order_id:
                            trade.notes += f' order={order_id}'
                        else:
                            trade.notes += ' sell_failed'

                    self.trade_log.save(trade)
                    logger.info(
                        "%s %s STOP-LOSS @%.0fc (entry=%.0fc, -%.0fc, conv=%d) P&L=$%.3f",
                        trade.asset.upper(),
                        "LIVE" if self.config.mode == 'live' else "PAPER",
                        our_price_cents, entry_cents, price_drop, current_conv,
                        trade.pnl_net
                    )
                    return

            # --- Time exit check ---
            if (secs_left <= self.config.time_exit_secs
                    and price_drop >= self.config.time_exit_loss * 100):
                exit_price = our_price_cents / 100
                fee = kalshi_fee(trade.entry_price)
                trade.pnl_net = (exit_price - trade.entry_price - fee) * trade.contracts
                trade.exit_price = exit_price
                trade.exit_type = 'time_exit'
                trade.status = 'exited'
                trade.notes = f'time_exit@{our_price_cents:.0f}c {secs_left:.0f}s_left'

                if self.config.mode == 'live' and self.trader:
                    order_id = self.trader.sell_position(
                        trade.ticker, trade.entry_side, trade.contracts,
                        exit_price)
                    if order_id:
                        trade.notes += f' order={order_id}'
                    else:
                        trade.notes += ' sell_failed'

                self.trade_log.save(trade)
                logger.info(
                    "%s %s TIME-EXIT @%.0fc (entry=%.0fc, -%.0fc, %ds left) P&L=$%.3f",
                    trade.asset.upper(),
                    "LIVE" if self.config.mode == 'live' else "PAPER",
                    our_price_cents, entry_cents, price_drop,
                    int(secs_left), trade.pnl_net
                )
                return

        # Wait until ~1s before close to capture final contract price
        remaining = close_ts - time.time()
        if remaining > 1:
            time.sleep(remaining - 1)

        final_price = self.price_cache.latest(trade.event_ticker)
        if final_price is not None:
            if trade.entry_side == 'no':
                our_final = 100 - final_price
            else:
                our_final = final_price
            trade.exit_price = our_final / 100
            trade.notes = f'final_contract={our_final:.0f}c'
            self.trade_log.save(trade)
            logger.info(
                "%s Holding to settlement (entry=%.0fc %s, conv=%d, final=%.0fc)",
                trade.asset.upper(), entry_cents, trade.entry_side.upper(),
                trade.conviction, our_final
            )
        else:
            logger.info(
                "%s Holding to settlement (entry=%.0fc %s, conv=%d, no final price)",
                trade.asset.upper(), entry_cents, trade.entry_side.upper(),
                trade.conviction
            )

    # ------------------------------------------------------------------
    # Settlement checking
    # ------------------------------------------------------------------

    def _check_settlements(self):
        """Check open trades and settle any that have closed."""
        open_trades = self.trade_log.get_open()
        if not open_trades:
            return
        now = time.time()
        for trade in open_trades:
            try:
                close_ts = datetime.fromisoformat(
                    trade.close_time.replace('Z', '+00:00')).timestamp()
            except Exception:
                continue
            if now < close_ts + 30:  # wait 30s after close for settlement
                continue

            # Fetch settlement
            try:
                series_map = ASSET_SERIES.get(trade.asset, {})
                series = series_map.get("15m") if isinstance(series_map, dict) else series_map
                if not series:
                    continue
                if self.trader:
                    resp = self.trader.session.get(
                        f"{KALSHI_API}/markets",
                        headers=self.trader._headers("GET", "/trade-api/v2/markets"),
                        params={"series_ticker": series, "status": "settled", "limit": 20},
                        timeout=10
                    )
                    markets = resp.json().get('markets', [])
                    for m in markets:
                        if m.get('ticker') == trade.ticker:
                            result = m.get('result', '')
                            if result:
                                won = (result == 'yes' and trade.entry_side == 'yes') or \
                                      (result == 'no' and trade.entry_side == 'no')
                                payout = 1.0 if won else 0.0
                                fee = kalshi_fee(trade.entry_price)
                                trade.pnl_net = (payout - trade.entry_price - fee) * trade.contracts
                                trade.result = result
                                trade.status = 'settled'
                                trade.exit_type = 'settlement'
                                trade.exit_price = payout
                                self.trade_log.save(trade)
                                logger.info(
                                    "%s SETTLED %s -> %s  conv=%d P&L=$%.3f",
                                    trade.asset.upper(), trade.entry_side.upper(),
                                    result.upper(), trade.conviction, trade.pnl_net
                                )
                                break
                else:
                    # Paper mode — infer result from cached price
                    final_price = self.price_cache.latest(trade.event_ticker)
                    if final_price is not None:
                        if final_price > 50:
                            inferred = 'yes'
                        else:
                            inferred = 'no'
                        won = (inferred == trade.entry_side)
                        payout = 1.0 if won else 0.0
                        fee = kalshi_fee(trade.entry_price)
                        trade.pnl_net = (payout - trade.entry_price - fee) * trade.contracts
                        trade.result = inferred
                        trade.status = 'settled'
                        trade.exit_type = 'settlement'
                        trade.notes = f'inferred_from_price={final_price:.1f}c'
                        self.trade_log.save(trade)
                        logger.info(
                            "%s PAPER SETTLED (inferred %s, last=%.0fc) conv=%d P&L=$%.3f",
                            trade.asset.upper(), inferred.upper(), final_price,
                            trade.conviction, trade.pnl_net
                        )
                    else:
                        # No cached price — mark as expired with unknown result
                        trade.status = 'settled'
                        trade.result = 'unknown'
                        trade.exit_type = 'settlement'
                        trade.pnl_net = -trade.entry_price * trade.contracts  # assume loss
                        trade.notes = 'no_cached_price'
                        self.trade_log.save(trade)
                        logger.info(
                            "%s PAPER SETTLED (no price data, assumed loss) P&L=$%.3f",
                            trade.asset.upper(), trade.pnl_net
                        )
            except Exception as e:
                logger.debug("Settlement check error %s: %s", trade.trade_id, e)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_once(self):
        """One poll cycle — fetch prices, check signal, settle."""
        self._check_settlements()

        now = time.time()

        for asset in ASSETS:
            if not self.config.enabled_assets.get(asset, True):
                continue
            if len(self.trade_log.get_open()) >= self.config.max_open_positions:
                continue

            try:
                markets = self._fetch_markets(asset)
            except Exception as e:
                logger.debug("Fetch error %s: %s", asset, e)
                continue

            # Update price cache for all open events
            for m in markets:
                et = m.get('event_ticker')
                if et:
                    yes_bid = m.get('yes_bid') or 0
                    yes_ask = m.get('yes_ask') or 1
                    yes_mid = (yes_bid + yes_ask) / 2
                    self.price_cache.record(et, yes_mid * 100)  # store as cents
                    # Track window metadata for timeline
                    if et not in self._window_meta:
                        ct = m.get('close_time', '')
                        try:
                            close_ts = datetime.fromisoformat(ct.replace('Z', '+00:00')).timestamp()
                            window_start_ts = close_ts - 900  # 15-min window
                            self._window_meta[et] = {
                                'asset': asset,
                                'close_time_ts': close_ts,
                                'close_time_str': ct,
                                'window_start_ts': window_start_ts,
                            }
                        except Exception:
                            pass

            # Also refresh spot and record for momentum tracker
            self.momentum_tracker.refresh(asset)
            spot = self.momentum_tracker.get_spot(asset)
            if spot:
                self.momentum_tracker.record_spot(asset, spot, now)

            # Find event in the eval window
            event = self._find_active_event(markets)
            if event is None:
                continue

            et = event['event_ticker']
            if et in self._entered_windows:
                continue  # already traded this window

            # Check signal
            signal = self._check_signal(asset, event)
            if signal is None:
                continue

            msig = signal['signal']
            logger.info(
                "%s SIGNAL: conv=%d/4 -> %s @%.0fc  f5m=%s ofi=%.2f mid=%s tbr=%.2f  bb=%.1f rsi=%.0f  %ds left",
                asset.upper(), msig.conviction, signal['entry_side'].upper(),
                signal['entry_price'] * 100,
                msig.f5m_dir, msig.f5m_ofi, msig.mid_dir, msig.taker_buy_ratio,
                msig.bb_z, msig.rsi,
                int(signal['secs_left'])
            )
            if not self.trading_enabled:
                logger.info("%s Trading paused — signal skipped", asset.upper())
                self._stats['skipped'] += 1
                continue
            self._execute(signal, event)

    # ------------------------------------------------------------------
    # Timeline data for dashboard
    # ------------------------------------------------------------------

    def get_timeline_data(self) -> dict:
        """Return price history per asset for the timeline chart."""
        now = time.time()
        result = {}

        for asset in ASSETS:
            # Find all windows for this asset, sorted newest first
            asset_windows = sorted(
                [(et, meta) for et, meta in self._window_meta.items()
                 if meta['asset'] == asset],
                key=lambda x: x[1]['close_time_ts'],
                reverse=True
            )

            current_window = None
            recent_windows = []

            for et, meta in asset_windows:
                close_ts = meta['close_time_ts']
                window_start_ts = meta['window_start_ts']
                is_current = close_ts > now - 30  # still open or just closed

                # Get price series from cache
                with self.price_cache._lock:
                    raw = list(self.price_cache._data.get(et, []))

                if not raw:
                    continue

                # Convert to [elapsed_s, price_cents] relative to window start
                prices = []
                for ts, price in raw:
                    elapsed = round(ts - window_start_ts, 1)
                    if 0 <= elapsed <= 960:  # within window + small buffer
                        prices.append([elapsed, round(price, 1)])

                if not prices:
                    continue

                # Eval window bounds in elapsed seconds
                eval_start_s = 900 - self.config.eval_window_max  # 420 (7 min in)
                eval_end_s = 900 - self.config.eval_window_min    # 600 (10 min in)

                # Check if a trade was taken for this window
                trade_info = None
                for t in self.trade_log.all():
                    if t.event_ticker == et:
                        trade_info = {
                            'side': t.entry_side,
                            'price': round(t.entry_price * 100, 1),
                            'contracts': t.contracts,
                            'conviction': t.conviction,
                            'status': t.status,
                            'result': t.result,
                            'pnl': round(t.pnl_net, 3),
                            'won': t.pnl_net > 0 if t.status in ('settled', 'exited') else None,
                        }
                        break

                window_data = {
                    'event_ticker': et,
                    'close_time': meta['close_time_str'],
                    'window_start_ts': window_start_ts,
                    'close_ts': close_ts,
                    'prices': prices,
                    'entry_start_s': eval_start_s,
                    'entry_end_s': eval_end_s,
                    'entry_zone_min': self.config.min_entry_price * 100,
                    'entry_zone_max': self.config.max_entry_price * 100,
                    'trade': trade_info,
                }

                if is_current and current_window is None:
                    window_data['elapsed_s'] = round(now - window_start_ts, 1)
                    window_data['seconds_remaining'] = round(close_ts - now, 1)
                    current_window = window_data
                elif not is_current and len(recent_windows) < 6:
                    recent_windows.append(window_data)

            result[asset] = {
                'current_window': current_window,
                'recent_windows': recent_windows,
            }

        return result

    def run(self):
        self.running = True
        logger.info("Momentum Bot v%s started (%s mode)", BOT_VERSION, self.config.mode)
        logger.info("Assets: %s  Entry: %.0f-%.0fc  Conviction: %d+/4  Stop-loss: %s  Window: %d-%ds",
                    [a for a, e in self.config.enabled_assets.items() if e],
                    self.config.min_entry_price * 100,
                    self.config.max_entry_price * 100,
                    self.config.min_conviction,
                    "ON" if self.config.stop_loss_enabled else "OFF",
                    self.config.eval_window_min,
                    self.config.eval_window_max)
        while self.running:
            try:
                self.run_once()
            except Exception as e:
                logger.error("Poll error: %s", e)
            time.sleep(self.config.poll_interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Momentum Bot")
    parser.add_argument("--mode", choices=["paper", "live", "monitor"], default="paper")
    parser.add_argument("--contracts", type=int, default=5)
    parser.add_argument("--min-conviction", type=int, default=3,
                        help="Minimum conviction score (3 or 4)")
    parser.add_argument("--no-stop-loss", action="store_true",
                        help="Disable stop-loss exits")
    parser.add_argument("--poll", type=int, default=15)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    api_key = os.environ.get("KALSHI_API_KEY", "")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

    trader = None
    if args.mode == "live" and api_key and key_path:
        try:
            trader = KalshiTrader(api_key, key_path)
            logger.info("Kalshi authenticated")
        except Exception as e:
            logger.error("Auth failed: %s — falling back to paper", e)

    config = MomentumConfig(
        mode=args.mode,
        base_contracts=args.contracts,
        min_conviction=args.min_conviction,
        stop_loss_enabled=not args.no_stop_loss,
        poll_interval=args.poll,
    )

    bot = MomentumBot(config, trader)
    bot.run()
