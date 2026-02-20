#!/usr/bin/env python3
"""
Intra-Window Contract Bounce-Back Bot

Strategy (validated on 193 BTC windows, Feb 18-20 2026):
  At minute 10 of each 15-min window, if the contract moved >THRESHOLD cents
  between minute 5 and minute 10, the move is WRONG ~69.7% of the time.
  Bet AGAINST the move and hold 5 minutes to settlement.

Signal:
  contract_yes_5m → contract_yes_10m
  If delta > +THRESHOLD (contract rallied in last 5 min) → buy NO (bet it reverses)
  If delta < -THRESHOLD (contract fell in last 5 min)   → buy YES (bet it reverses)

Backtest results:
  WR: 69.7%  (n=33 BTC windows)
  Avg entry: ~30c  EV: +$0.36/contract
  Frequency: ~17% of windows → ~49 signals/day across BTC+ETH+SOL

Entry window: when 270-330s remain (minute 9.5-10.5 of the window)
Hold: to settlement (~5 min)
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

logger = logging.getLogger("BounceBot")

BOT_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ASSETS = ["btc", "eth", "sol"]

@dataclass
class BounceConfig:
    """Strategy parameters."""
    # Signal
    move_threshold: float = 8.0        # Min contract move (cents) to trigger signal
    entry_window_min: int = 240        # Min seconds remaining when we check (4 min)
    entry_window_max: int = 360        # Max seconds remaining when we check (6 min)
    lookback_secs: int = 300           # How far back to look for the "5 min ago" price

    # Entry
    max_entry_price: float = 0.35      # Don't buy contracts already >35c (backtest avg ~30c)
    min_entry_price: float = 0.05      # Don't buy near-zero contracts
    base_contracts: int = 5            # Contracts per trade
    max_open_positions: int = 3        # Max simultaneous positions

    # Mode
    mode: str = "paper"
    poll_interval: int = 15
    log_file: str = "data/bounce_trades.jsonl"

    # Assets — disable BTC until more data (52.9% WR in extreme regime); keep ETH/SOL
    enabled_assets: dict = field(default_factory=lambda: {
        "btc": True, "eth": True, "sol": True
    })


# ---------------------------------------------------------------------------
# Trade log
# ---------------------------------------------------------------------------

@dataclass
class BounceTraide:
    """Single trade record."""
    trade_id: str
    asset: str
    event_ticker: str
    ticker: str
    entry_side: str           # "yes" or "no"
    entry_price: float
    contracts: int
    signal_move: float        # c10 - c5 (cents) that triggered this
    c5_price: float
    c10_price: float
    entry_time: str
    close_time: str
    status: str = "open"      # open | settled | cancelled
    exit_price: Optional[float] = None
    result: Optional[str] = None
    pnl_net: float = 0.0
    notes: str = ""


class TradeLog:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self._trades: dict[str, BounceTraide] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            with open(self.filepath) as f:
                for line in f:
                    t = json.loads(line)
                    self._trades[t['trade_id']] = BounceTraide(**t)
        except FileNotFoundError:
            pass

    def save(self, trade: BounceTraide):
        with self._lock:
            self._trades[trade.trade_id] = trade
            with open(self.filepath, 'w') as f:
                for t in self._trades.values():
                    f.write(json.dumps(asdict(t)) + '\n')

    def get_open(self) -> list[BounceTraide]:
        return [t for t in self._trades.values() if t.status == 'open']

    def all(self) -> list[BounceTraide]:
        return list(self._trades.values())

    def summary(self) -> dict:
        settled = [t for t in self._trades.values() if t.status == 'settled']
        wins = [t for t in settled if t.pnl_net > 0]
        return {
            'total': len(self._trades),
            'open': len(self.get_open()),
            'settled': len(settled),
            'wins': len(wins),
            'win_rate': len(wins) / len(settled) if settled else 0,
            'total_pnl': sum(t.pnl_net for t in settled),
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
        return sign_request(self.api_key, self.private_key, method, path)

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

    def get_fills(self, ticker: str) -> list[dict]:
        try:
            path = f"/trade-api/v2/portfolio/fills"
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


class BounceBackBot:
    def __init__(self, config: BounceConfig, trader: Optional[KalshiTrader] = None):
        self.config = config
        self.trader = trader
        self.trade_log = TradeLog(config.log_file)
        self.price_cache = PriceCache()
        self.capital_guard = CapitalGuard("bounce-back")
        self.running = False
        self.trading_enabled = True          # toggle via dashboard to pause entries
        self._entered_windows: set = set()  # prevent double-entry per window
        self._stats = {'signals': 0, 'trades': 0, 'skipped': 0}
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
            if self.config.entry_window_min <= secs_left <= self.config.entry_window_max:
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
        """Check if the bounce-back signal is present."""
        event_ticker = event['event_ticker']
        close_ts = event['close_ts']

        # Price now
        now_ts = time.time()
        now_price = self.price_cache.latest(event_ticker)
        if now_price is None:
            return None

        # Price ~5 minutes ago (at the start of this check window)
        target_5m_ago = close_ts - self.config.lookback_secs
        price_5m_ago = self.price_cache.price_at(event_ticker, target_5m_ago)
        if price_5m_ago is None:
            return None

        move = now_price - price_5m_ago  # positive = contract moved toward YES

        if abs(move) < self.config.move_threshold:
            return None

        # Signal: bet AGAINST the move
        if move > 0:
            entry_side = 'no'   # contract rallied → bet NO (reversal)
            entry_price = (100 - now_price) / 100  # no_ask ≈ 100 - yes_bid
        else:
            entry_side = 'yes'  # contract fell → bet YES (reversal)
            entry_price = now_price / 100  # yes_ask

        # Price guards
        if entry_price > self.config.max_entry_price:
            logger.debug("%s signal but entry %.0fc > max %.0fc, skip",
                         asset.upper(), entry_price*100, self.config.max_entry_price*100)
            return None
        if entry_price < self.config.min_entry_price:
            logger.debug("%s signal but entry %.0fc < min %.0fc, skip",
                         asset.upper(), entry_price*100, self.config.min_entry_price*100)
            return None

        return {
            'asset': asset,
            'event_ticker': event_ticker,
            'entry_side': entry_side,
            'entry_price': entry_price,
            'signal_move': move,
            'c5_price': price_5m_ago,
            'c10_price': now_price,
            'secs_left': event['secs_left'],
        }

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    def _execute(self, signal: dict, event: dict):
        asset = signal['asset']
        entry_side = signal['entry_side']
        entry_price = signal['entry_price']

        # Capital guard
        exposure = len(self.trade_log.get_open()) * self.config.base_contracts * entry_price
        if not self.capital_guard.check(exposure, self.config.base_contracts * entry_price):
            logger.info("%s Signal blocked by capital guard", asset.upper())
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
            logger.info("%s LIVE BUY %s %dx @%.0fc (move=%.1fc, %ds left)",
                        asset.upper(), entry_side.upper(), contracts,
                        entry_price*100, signal['signal_move'], int(signal['secs_left']))
        else:
            logger.info("%s PAPER BUY %s %dx @%.0fc (move=%.1fc, %ds left)",
                        asset.upper(), entry_side.upper(), contracts,
                        entry_price*100, signal['signal_move'], int(signal['secs_left']))

        trade = BounceTraide(
            trade_id=trade_id,
            asset=asset,
            event_ticker=signal['event_ticker'],
            ticker=ticker,
            entry_side=entry_side,
            entry_price=entry_price,
            contracts=contracts,
            signal_move=signal['signal_move'],
            c5_price=signal['c5_price'],
            c10_price=signal['c10_price'],
            entry_time=datetime.now(timezone.utc).isoformat(),
            close_time=datetime.fromtimestamp(
                event['close_ts'], tz=timezone.utc).isoformat(),
        )
        self.trade_log.save(trade)
        self._entered_windows.add(signal['event_ticker'])
        self._stats['trades'] += 1
        self._stats['signals'] += 1

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
                path = f"/trade-api/v2/markets?series_ticker={series}&status=settled&limit=20"
                if self.trader:
                    resp = self.trader.session.get(
                        f"{KALSHI_API}/markets",
                        headers=self.trader._headers("GET", f"/trade-api/v2/markets"),
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
                                trade.exit_price = payout
                                self.trade_log.save(trade)
                                logger.info(
                                    "%s SETTLED %s → %s  P&L=$%.3f",
                                    trade.asset.upper(), trade.entry_side.upper(),
                                    result.upper(), trade.pnl_net
                                )
                                break
                else:
                    # Paper mode — check if close_time has passed, mark settled
                    # Use price cache to infer result
                    final_price = self.price_cache.latest(trade.event_ticker)
                    if final_price is not None:
                        if final_price > 90:
                            inferred = 'yes'
                        elif final_price < 10:
                            inferred = 'no'
                        else:
                            inferred = None
                        if inferred:
                            won = (inferred == trade.entry_side)
                            payout = 1.0 if won else 0.0
                            fee = kalshi_fee(trade.entry_price)
                            trade.pnl_net = (payout - trade.entry_price - fee) * trade.contracts
                            trade.result = inferred
                            trade.status = 'settled'
                            trade.notes = 'inferred_from_price'
                            self.trade_log.save(trade)
                            logger.info(
                                "%s PAPER SETTLED (inferred %s) P&L=$%.3f",
                                trade.asset.upper(), inferred.upper(), trade.pnl_net
                            )
            except Exception as e:
                logger.debug("Settlement check error %s: %s", trade.trade_id, e)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_once(self):
        """One poll cycle — fetch prices, check signal, settle."""
        self._check_settlements()

        # Clean up entered_windows for events that have closed
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
            # markets are already parsed by parse_kalshi_bracket so yes_bid/ask are 0-1 fractions
            # Store as cents (0-100) for the chart
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

            # Find event in the entry window
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

            logger.info(
                "%s SIGNAL: c5=%.0fc c10=%.0fc move=%.1fc → BUY %s  %ds left",
                asset.upper(), signal['c5_price'], signal['c10_price'],
                signal['signal_move'], signal['entry_side'].upper(),
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
        """Return price history per asset for the timeline chart.

        Returns a dict keyed by asset, each containing:
          - current_window: latest window with full price series
          - recent_windows: last 5 completed windows with summary stats
        """
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

                # Entry window bounds in elapsed seconds
                entry_start_s = 900 - self.config.entry_window_max  # 540
                entry_end_s   = 900 - self.config.entry_window_min  # 660
                c5_ref_s      = 900 - self.config.lookback_secs      # 600

                # Find c5 and c10 prices from the price series
                def price_at_elapsed(target_s, tol=45):
                    best = min(prices, key=lambda p: abs(p[0] - target_s))
                    return best[1] if abs(best[0] - target_s) <= tol else None

                c5_price  = price_at_elapsed(c5_ref_s)
                c10_price = price_at_elapsed((entry_start_s + entry_end_s) / 2)

                # Check if a trade was taken for this window
                trade_info = None
                for t in self.trade_log.all():
                    if t.event_ticker == et:
                        trade_info = {
                            'side': t.entry_side,
                            'price': round(t.entry_price * 100, 1),
                            'contracts': t.contracts,
                            'signal_move': round(t.signal_move, 1),
                            'status': t.status,
                            'result': t.result,
                            'pnl': round(t.pnl_net, 3),
                            'won': t.pnl_net > 0 if t.status == 'settled' else None,
                        }
                        break

                window_data = {
                    'event_ticker': et,
                    'close_time': meta['close_time_str'],
                    'window_start_ts': window_start_ts,
                    'close_ts': close_ts,
                    'prices': prices,
                    'entry_start_s': entry_start_s,
                    'entry_end_s': entry_end_s,
                    'c5_ref_s': c5_ref_s,
                    'c5_price': c5_price,
                    'c10_price': c10_price,
                    'threshold': self.config.move_threshold,
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
        logger.info("Bounce-Back Bot v%s started (%s mode)", BOT_VERSION, self.config.mode)
        logger.info("Assets: %s  Threshold: %.0fc  Entry window: %d-%ds",
                    [a for a, e in self.config.enabled_assets.items() if e],
                    self.config.move_threshold,
                    self.config.entry_window_min,
                    self.config.entry_window_max)
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
    parser = argparse.ArgumentParser(description="Bounce-Back Bot")
    parser.add_argument("--mode", choices=["paper", "live", "monitor"], default="paper")
    parser.add_argument("--contracts", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=8.0,
                        help="Min contract move in cents to trigger signal")
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

    config = BounceConfig(
        mode=args.mode,
        base_contracts=args.contracts,
        move_threshold=args.threshold,
        poll_interval=args.poll,
    )

    bot = BounceBackBot(config, trader)
    bot.run()
