#!/usr/bin/env python3
"""
Intra-Window Contract Bounce-Back Bot

Strategy (backtested on 933 windows, Feb 2026):
  When a contract price drops/rises significantly mid-window (between minute 5
  and minute 10), buy the cheap side and sell at a fixed 50c exit target before
  settlement. This is a SCALP strategy — we exit on reversion, not hold to expiry.

Signal:
  contract_yes_5m → contract_yes_10m
  If delta > +THRESHOLD (contract rallied in last 5 min) → buy NO (bet it reverses)
  If delta < -THRESHOLD (contract fell in last 5 min)   → buy YES (bet it reverses)

Backtest results (best configs):
  NO side, 8c+ drop, c10<=30c, exit @50c: 61% hit, $0.17/trade
  YES side, 20c+ drop, c10<=15c, exit @50c: 63% hit, $0.23/trade
  Contracts 0-10c rarely bounce (14%); 10-30c is the sweet spot

Entry window: 4-10 min into window (240-600s remaining)
Exit: sell at fixed 50c target, or hold to settlement if not hit
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
from support_resistance import SRTracker, ConfirmationResult, fetch_spot_price

logger = logging.getLogger("BounceBot")

BOT_VERSION = "2.1.0"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ASSETS = ["btc", "eth", "sol"]

@dataclass
class BounceConfig:
    """Strategy parameters."""
    # Signal — entry is triggered by contract price range, NOT a move threshold
    # The contract being at 10-30c already implies a big move happened
    entry_window_min: int = 240        # Min seconds remaining when we check (4 min)
    entry_window_max: int = 600        # Max seconds remaining when we check (10 min = 5 min into window)

    # Entry
    max_entry_price: float = 0.30      # Don't buy contracts >30c (backtest: 10-30c is sweet spot)
    min_entry_price: float = 0.10      # Don't buy <10c (only 14% bounce rate)
    base_contracts: int = 5            # Contracts per trade
    max_open_positions: int = 3        # Max simultaneous positions

    # Support/Resistance filter — only trade when spot is at a key level
    sr_enabled: bool = True            # Require S/R confirmation for entry

    # Confirmation signals — from V2 backtest (RSI, BB, rejection wick)
    require_confirmation: bool = False  # If True, need at least 1 confirmation to trade
    log_confirmations: bool = True      # Log confirmation details for analysis

    # Exit — scalp at fixed price target, don't hold to settlement
    exit_target: float = 0.50          # Sell when contract reaches 50c (backtest best)
    exit_poll_interval: int = 5        # Check exit price every 5 seconds

    # Mode
    mode: str = "paper"
    poll_interval: int = 15
    log_file: str = "data/bounce_trades.jsonl"

    # Assets — all enabled (V2 backtest: ETH best, BTC solid, SOL marginal)
    enabled_assets: dict = field(default_factory=lambda: {
        "btc": True, "eth": True, "sol": False
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
    status: str = "open"      # open | exited | settled | cancelled
    exit_price: Optional[float] = None
    exit_type: Optional[str] = None  # "scalp" (hit target) | "settlement" (held to end)
    result: Optional[str] = None
    pnl_net: float = 0.0
    max_price_after_entry: Optional[float] = None  # highest contract price seen post-entry
    # Confirmation signals at entry
    rsi: Optional[float] = None
    rsi_confirms: bool = False
    bb_zscore: Optional[float] = None
    bb_confirms: bool = False
    rejection_wick: bool = False
    n_confirmations: int = 0
    # S/R level details
    sr_price: Optional[float] = None
    sr_strength: Optional[float] = None
    sr_touches: Optional[int] = None
    sr_timeframe: Optional[str] = None
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
                    # Handle records from before new fields existed
                    defaults = {
                        'exit_type': None, 'max_price_after_entry': None,
                        'rsi': None, 'rsi_confirms': False,
                        'bb_zscore': None, 'bb_confirms': False,
                        'rejection_wick': False, 'n_confirmations': 0,
                        'sr_price': None, 'sr_strength': None,
                        'sr_touches': None, 'sr_timeframe': None,
                    }
                    for key, default in defaults.items():
                        if key not in t:
                            t[key] = default
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
        closed = [t for t in self._trades.values() if t.status in ('settled', 'exited')]
        scalps = [t for t in closed if t.exit_type == 'scalp']
        settlements = [t for t in closed if t.exit_type != 'scalp']
        wins = [t for t in closed if t.pnl_net > 0]
        return {
            'total': len(self._trades),
            'open': len(self.get_open()),
            'closed': len(closed),
            'scalp_exits': len(scalps),
            'held_to_settlement': len(settlements),
            'wins': len(wins),
            'win_rate': len(wins) / len(closed) if closed else 0,
            'total_pnl': sum(t.pnl_net for t in closed),
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
        self.sr_tracker = SRTracker(
            assets=[a for a, e in config.enabled_assets.items() if e])
        self.running = False
        self.trading_enabled = True          # toggle via dashboard to pause entries
        self._entered_windows: set = set()  # prevent double-entry per window
        self._stats = {'signals': 0, 'trades': 0, 'skipped': 0,
                       'sr_blocked': 0, 'sr_passed': 0}
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
        """Check if the bounce-back signal is present.

        Signal logic:
          1. Contract YES price is in the 10-30c range → cheap side is YES
             OR contract YES price is in the 70-90c range → cheap side is NO
          2. Spot price is near a support (for YES) or resistance (for NO)
          3. (Optional) Confirmation signals: RSI extreme, BB z-score, rejection wick
        """
        event_ticker = event['event_ticker']

        # Current contract price
        now_price = self.price_cache.latest(event_ticker)
        if now_price is None:
            return None

        min_cents = self.config.min_entry_price * 100
        max_cents = self.config.max_entry_price * 100

        # Determine which side is cheap
        if min_cents <= now_price <= max_cents:
            entry_side = 'yes'
            entry_price = now_price / 100
            direction = 'up'       # YES cheap = price dropped, expect bounce up
        elif (100 - max_cents) <= now_price <= (100 - min_cents):
            entry_side = 'no'
            entry_price = (100 - now_price) / 100
            direction = 'down'     # NO cheap = price rallied, expect drop
        else:
            return None

        # S/R filter
        sr_level = None
        spot_price = None
        if self.config.sr_enabled:
            at_level, sr_level, spot_price = self.sr_tracker.check_bounce_signal(
                asset, entry_side)
            if not at_level:
                logger.debug("%s contract at %.0fc (%s) but spot not at S/R, skip",
                             asset.upper(), now_price, entry_side.upper())
                self._stats['sr_blocked'] += 1
                return None
            self._stats['sr_passed'] += 1

        # Confirmation signals (RSI, BB, rejection wick)
        confirmations = self.sr_tracker.check_confirmations(asset, direction)

        if self.config.log_confirmations:
            logger.debug(
                "%s Confirmations: RSI=%.1f(%s) BB=%.2f(%s) Wick=%s → %d/3",
                asset.upper(), confirmations.rsi,
                "✓" if confirmations.rsi_confirms else "✗",
                confirmations.bb_zscore,
                "✓" if confirmations.bb_confirms else "✗",
                "✓" if confirmations.rejection_wick else "✗",
                confirmations.n_confirmations,
            )

        # Optional: require at least 1 confirmation
        if self.config.require_confirmation and confirmations.n_confirmations < 1:
            logger.debug("%s No confirmations, skip (require_confirmation=True)",
                         asset.upper())
            self._stats['sr_blocked'] += 1
            return None

        return {
            'asset': asset,
            'event_ticker': event_ticker,
            'entry_side': entry_side,
            'entry_price': entry_price,
            'direction': direction,
            'signal_move': now_price if entry_side == 'yes' else -(100 - now_price),
            'c5_price': 0,
            'c10_price': now_price,
            'secs_left': event['secs_left'],
            'sr_level': sr_level,
            'spot_price': spot_price,
            'confirmations': confirmations,
        }

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    def _execute(self, signal: dict, event: dict):
        asset = signal['asset']
        entry_side = signal['entry_side']
        entry_price = signal['entry_price']

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
            logger.info("%s LIVE BUY %s %dx @%.0fc (move=%.1fc, %ds left) → exit target %.0fc",
                        asset.upper(), entry_side.upper(), contracts,
                        entry_price*100, signal['signal_move'], int(signal['secs_left']),
                        self.config.exit_target*100)
        else:
            logger.info("%s PAPER BUY %s %dx @%.0fc (move=%.1fc, %ds left) → exit target %.0fc",
                        asset.upper(), entry_side.upper(), contracts,
                        entry_price*100, signal['signal_move'], int(signal['secs_left']),
                        self.config.exit_target*100)

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
            rsi=signal['confirmations'].rsi if signal.get('confirmations') else None,
            rsi_confirms=signal['confirmations'].rsi_confirms if signal.get('confirmations') else False,
            bb_zscore=signal['confirmations'].bb_zscore if signal.get('confirmations') else None,
            bb_confirms=signal['confirmations'].bb_confirms if signal.get('confirmations') else False,
            rejection_wick=signal['confirmations'].rejection_wick if signal.get('confirmations') else False,
            n_confirmations=signal['confirmations'].n_confirmations if signal.get('confirmations') else 0,
            sr_price=signal['sr_level'].price if signal.get('sr_level') else None,
            sr_strength=signal['sr_level'].strength if signal.get('sr_level') else None,
            sr_touches=signal['sr_level'].touches if signal.get('sr_level') else None,
            sr_timeframe=signal['sr_level'].timeframe if signal.get('sr_level') else None,
        )
        self.trade_log.save(trade)
        self._entered_windows.add(signal['event_ticker'])
        self._stats['trades'] += 1
        self._stats['signals'] += 1

        # Spawn exit monitor thread to watch for scalp exit
        t = threading.Thread(
            target=self._monitor_exit, args=(trade,),
            daemon=True, name=f"exit-{trade_id}"
        )
        t.start()

    # ------------------------------------------------------------------
    # Exit monitoring — scalp at target price
    # ------------------------------------------------------------------

    def _monitor_exit(self, trade: BounceTraide):
        """Monitor an open trade and exit when price hits the target.

        Runs in its own thread. Polls the contract price every exit_poll_interval
        seconds until either:
          1. Price hits exit_target → sell (scalp exit)
          2. Window closes → hold to settlement (fallback)
        """
        try:
            close_ts = datetime.fromisoformat(
                trade.close_time.replace('Z', '+00:00')).timestamp()
        except Exception:
            return

        exit_target_cents = self.config.exit_target * 100  # e.g. 50c
        max_price = trade.entry_price * 100  # track in cents

        while time.time() < close_ts - 5:  # stop 5s before close
            time.sleep(self.config.exit_poll_interval)

            # Get current contract price
            current_price = self.price_cache.latest(trade.event_ticker)
            if current_price is None:
                continue

            # For NO-side trades, our profit is when YES drops (NO price = 100 - YES)
            if trade.entry_side == 'no':
                our_price = 100 - current_price  # NO mid price in cents
            else:
                our_price = current_price  # YES mid price in cents

            max_price = max(max_price, our_price)

            # Check if exit target hit
            if our_price >= exit_target_cents:
                exit_price_frac = our_price / 100
                fee = kalshi_fee(trade.entry_price)
                trade.pnl_net = (exit_price_frac - trade.entry_price - fee) * trade.contracts
                trade.exit_price = exit_price_frac
                trade.exit_type = 'scalp'
                trade.status = 'exited'
                trade.max_price_after_entry = max_price
                trade.notes = f'scalp_exit@{our_price:.0f}c'

                if self.config.mode == 'live' and self.trader:
                    # Place sell order
                    sell_side = trade.entry_side
                    sell_price = int(our_price)
                    order_id = self.trader.place_order(
                        trade.ticker, sell_side, trade.contracts,
                        exit_price_frac)
                    if order_id:
                        trade.notes += f' order={order_id}'
                    else:
                        trade.notes += ' sell_order_failed'
                        logger.error("%s SELL ORDER FAILED @%.0fc",
                                     trade.asset.upper(), our_price)

                self.trade_log.save(trade)
                logger.info(
                    "%s %s EXIT @%.0fc (entry=%.0fc, +%.0fc) P&L=$%.3f  max=%.0fc",
                    trade.asset.upper(),
                    "LIVE" if self.config.mode == 'live' else "PAPER",
                    our_price, trade.entry_price * 100,
                    our_price - trade.entry_price * 100,
                    trade.pnl_net, max_price
                )
                return

        # Window closing without hitting target — record max price seen
        trade.max_price_after_entry = max_price
        trade.notes = f'no_exit_hit max={max_price:.0f}c target={exit_target_cents:.0f}c'
        self.trade_log.save(trade)
        logger.info(
            "%s EXIT TARGET NOT HIT (target=%.0fc, max=%.0fc, entry=%.0fc) → holding to settlement",
            trade.asset.upper(), exit_target_cents, max_price,
            trade.entry_price * 100
        )

    # ------------------------------------------------------------------
    # Price analysis helpers
    # ------------------------------------------------------------------

    def _get_price_high(self, event_ticker: str, entry_time_str: str) -> Optional[float]:
        """Get the highest YES price after entry time (for scalp analysis)."""
        try:
            entry_ts = datetime.fromisoformat(
                entry_time_str.replace('Z', '+00:00')).timestamp()
        except Exception:
            return None
        with self.price_cache._lock:
            entries = list(self.price_cache._data.get(event_ticker, []))
        if not entries:
            return None
        post_entry = [price for ts, price in entries if ts >= entry_ts]
        return max(post_entry) if post_entry else None

    # ------------------------------------------------------------------
    # Settlement checking
    # ------------------------------------------------------------------

    def _check_settlements(self):
        """Check open trades and settle any that have closed.

        Trades that already exited via scalp (status='exited') are skipped.
        Only 'open' trades that missed the exit target get settled here.
        """
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
                        trade.notes = f'inferred_from_price={final_price:.1f}c'
                        # Also record the max price seen during the window for scalp analysis
                        price_high = self._get_price_high(trade.event_ticker, trade.entry_time)
                        if price_high is not None:
                            trade.notes += f' high={price_high:.1f}c'
                        self.trade_log.save(trade)
                        logger.info(
                            "%s PAPER SETTLED (inferred %s, last=%.0fc, high=%s) P&L=$%.3f",
                            trade.asset.upper(), inferred.upper(), final_price,
                            f"{price_high:.0f}c" if price_high else "?",
                            trade.pnl_net
                        )
                    else:
                        # No cached price — mark as expired with unknown result
                        trade.status = 'settled'
                        trade.result = 'unknown'
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

        # Refresh S/R levels periodically (every 15 min)
        if self.config.sr_enabled:
            self.sr_tracker.refresh()

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

            sr_info = ""
            if signal.get('sr_level'):
                lvl = signal['sr_level']
                sr_info = f" S/R={lvl.type}@${lvl.price:.0f}({lvl.source},str={lvl.strength:.1f},t={lvl.touches},{lvl.timeframe})"
            spot_info = f" spot=${signal['spot_price']:.2f}" if signal.get('spot_price') else ""
            conf = signal.get('confirmations')
            conf_info = ""
            if conf:
                flags = []
                if conf.rsi_confirms:
                    flags.append(f"RSI={conf.rsi:.0f}")
                if conf.bb_confirms:
                    flags.append(f"BB={conf.bb_zscore:.1f}")
                if conf.rejection_wick:
                    flags.append("WICK")
                conf_info = f" conf=[{','.join(flags)}]({conf.n_confirmations}/3)" if flags else f" conf=none"
            logger.info(
                "%s SIGNAL: contract=%.0fc → BUY %s  %ds left%s%s%s",
                asset.upper(), signal['c10_price'],
                signal['entry_side'].upper(),
                int(signal['secs_left']),
                spot_info, sr_info, conf_info
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
                entry_start_s = 900 - self.config.entry_window_max  # 300 (5 min in)
                entry_end_s   = 900 - self.config.entry_window_min  # 660 (11 min in)
                c5_ref_s      = 300  # 5 min into window

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
        logger.info("Bounce-Back Bot v%s started (%s mode)", BOT_VERSION, self.config.mode)
        logger.info("Assets: %s  Entry: %.0f-%.0fc  Exit target: %.0fc  S/R: %s  Confirm: %s  Window: %d-%ds",
                    [a for a, e in self.config.enabled_assets.items() if e],
                    self.config.min_entry_price * 100,
                    self.config.max_entry_price * 100,
                    self.config.exit_target * 100,
                    "ON" if self.config.sr_enabled else "OFF",
                    "REQUIRED" if self.config.require_confirmation else "logged",
                    self.config.entry_window_min,
                    self.config.entry_window_max)
        # Initial S/R level load
        if self.config.sr_enabled:
            logger.info("Loading S/R levels...")
            self.sr_tracker.refresh(force=True)
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
    parser.add_argument("--exit-target", type=float, default=50.0,
                        help="Exit target price in cents (sell when contract reaches this)")
    parser.add_argument("--no-sr", action="store_true",
                        help="Disable S/R filter (trade on price range only)")
    parser.add_argument("--require-confirmation", action="store_true",
                        help="Require at least 1 confirmation signal (RSI/BB/wick)")
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
        exit_target=args.exit_target / 100,  # CLI takes cents, config stores fraction
        sr_enabled=not args.no_sr,
        require_confirmation=args.require_confirmation,
        poll_interval=args.poll,
    )

    bot = BounceBackBot(config, trader)
    bot.run()
