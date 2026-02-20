#!/usr/bin/env python3
"""
Bounce-Back Bot Dashboard — Flask web UI on port 5052
"""
import argparse
import json
import logging
import os
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string, request

from bounce_bot import BounceBackBot, BounceConfig, KalshiTrader, ASSETS

logger = logging.getLogger("dashboard")

app = Flask(__name__)
bot: BounceBackBot = None

# ---------------------------------------------------------------------------
# HTML Dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Bounce-Back Bot</title>
<meta http-equiv="refresh" content="15">
<script>
async function toggleTrading() {
  const btn = document.getElementById('tradeBtn');
  btn.disabled = true;
  try {
    const r = await fetch('/api/trading/toggle', {method:'POST'});
    const d = await r.json();
    updateBtn(d.trading_enabled);
  } catch(e) { alert('Error: ' + e); }
  btn.disabled = false;
}
function updateBtn(enabled) {
  const btn = document.getElementById('tradeBtn');
  if (!btn) return;
  if (enabled) {
    btn.textContent = '⏸ Pause Trading';
    btn.style.background = 'rgba(248,81,73,0.15)';
    btn.style.color = '#f85149';
    btn.style.borderColor = 'rgba(248,81,73,0.3)';
  } else {
    btn.textContent = '▶ Resume Trading';
    btn.style.background = 'rgba(63,185,80,0.15)';
    btn.style.color = '#3fb950';
    btn.style.borderColor = 'rgba(63,185,80,0.3)';
  }
}
// Sync button state from API on load
window.addEventListener('DOMContentLoaded', async () => {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    updateBtn(d.trading_enabled !== false);
  } catch(e) {}
});
</script>
<style>
  :root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;
        --green:#3fb950;--red:#f85149;--yellow:#e3b341;--accent:#58a6ff;--orange:#f0883e;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:monospace;font-size:13px;padding:1rem;}
  h1{font-size:1rem;font-weight:700;color:var(--accent);margin-bottom:1rem;}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1rem;margin-bottom:1rem;}
  .card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:1rem;}
  .card h3{font-size:.75rem;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:.75rem;}
  .stat{display:flex;justify-content:space-between;padding:.25rem 0;border-bottom:1px solid rgba(255,255,255,.04);}
  .stat:last-child{border:none;}
  .stat-l{color:var(--dim);font-size:.75rem;}
  .stat-v{font-weight:600;}
  .pos{color:var(--green);}  .neg{color:var(--red);}
  table{width:100%;border-collapse:collapse;font-size:.78rem;}
  th{color:var(--dim);font-weight:600;text-transform:uppercase;font-size:.65rem;padding:.4rem .6rem;text-align:left;border-bottom:1px solid var(--border);}
  td{padding:.4rem .6rem;border-bottom:1px solid rgba(255,255,255,.04);}
  .badge{padding:1px 6px;border-radius:3px;font-size:.65rem;font-weight:700;}
  .badge-yes{background:rgba(63,185,80,.15);color:var(--green);}
  .badge-no{background:rgba(248,81,73,.15);color:var(--red);}
  .badge-open{background:rgba(88,166,255,.15);color:var(--accent);}
  .badge-settled{background:rgba(139,148,158,.15);color:var(--dim);}
  .mode-paper{color:var(--yellow);}  .mode-live{color:var(--green);}
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;">
  <h1 style="margin:0">Bounce-Back Bot v{{ version }} &mdash; <span class="mode-{{ mode }}">{{ mode.upper() }}</span></h1>
  <button id="tradeBtn" onclick="toggleTrading()" style="
    padding:0.4rem 1rem;border-radius:6px;cursor:pointer;font-family:monospace;
    font-size:0.8rem;font-weight:700;border:1px solid;transition:all 0.2s;
    background:rgba(248,81,73,0.15);color:#f85149;border-color:rgba(248,81,73,0.3);">
    ⏸ Pause Trading
  </button>
</div>
<div class="grid">
  <div class="card">
    <h3>Performance</h3>
    {% set s = summary %}
    <div class="stat"><span class="stat-l">Total Trades</span><span class="stat-v">{{ s.total }}</span></div>
    <div class="stat"><span class="stat-l">Open</span><span class="stat-v" style="color:var(--accent)">{{ s.open }}</span></div>
    <div class="stat"><span class="stat-l">Settled</span><span class="stat-v">{{ s.settled }}</span></div>
    <div class="stat"><span class="stat-l">Win Rate</span>
      <span class="stat-v {% if s.win_rate >= 0.6 %}pos{% elif s.win_rate >= 0.4 %}{% else %}neg{% endif %}">
        {% if s.settled %}{{ "%.1f"|format(s.win_rate*100) }}% ({{ s.wins }}/{{ s.settled }}){% else %}—{% endif %}
      </span></div>
    <div class="stat"><span class="stat-l">Total P&L</span>
      <span class="stat-v {% if s.total_pnl >= 0 %}pos{% else %}neg{% endif %}">
        {% if s.total_pnl %}${{ "%.3f"|format(s.total_pnl) }}{% else %}$0.000{% endif %}
      </span></div>
  </div>
  <div class="card">
    <h3>Config</h3>
    <div class="stat"><span class="stat-l">Signal Threshold</span><span class="stat-v">{{ threshold }}¢ move</span></div>
    <div class="stat"><span class="stat-l">Entry Window</span><span class="stat-v">{{ entry_min }}-{{ entry_max }}s to close</span></div>
    <div class="stat"><span class="stat-l">Base Contracts</span><span class="stat-v">{{ contracts }}</span></div>
    <div class="stat"><span class="stat-l">Assets</span><span class="stat-v">{{ assets }}</span></div>
    <div class="stat"><span class="stat-l">Max Open</span><span class="stat-v">{{ max_open }}</span></div>
    <div class="stat"><span class="stat-l">Poll</span><span class="stat-v">{{ poll }}s</span></div>
  </div>
</div>

<div class="card" style="margin-bottom:1rem">
  <h3>Recent Trades (last 20)</h3>
  {% if trades %}
  <table>
    <tr><th>Time</th><th>Asset</th><th>Side</th><th>Entry</th><th>Qty</th>
        <th>Move</th><th>C5→C10</th><th>Status</th><th>P&L</th></tr>
    {% for t in trades %}
    <tr>
      <td>{{ t.entry_time[11:19] }}</td>
      <td style="font-weight:700;color:var(--orange)">{{ t.asset.upper() }}</td>
      <td><span class="badge badge-{{ t.entry_side }}">{{ t.entry_side.upper() }}</span></td>
      <td>{{ "%.0f"|format(t.entry_price*100) }}¢</td>
      <td>{{ t.contracts }}</td>
      <td style="color:{% if t.signal_move > 0 %}var(--green){% else %}var(--red){% endif %}">
        {{ "%+.1f"|format(t.signal_move) }}¢</td>
      <td style="font-size:.7rem;color:var(--dim)">{{ "%.0f"|format(t.c5_price) }}→{{ "%.0f"|format(t.c10_price) }}¢</td>
      <td><span class="badge badge-{{ t.status }}">{{ t.status.upper() }}</span></td>
      <td class="{% if t.pnl_net > 0 %}pos{% elif t.pnl_net < 0 %}neg{% endif %}">
        {% if t.pnl_net %}${{ "%.3f"|format(t.pnl_net) }}{% else %}—{% endif %}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <div style="color:var(--dim);text-align:center;padding:1rem">No trades yet</div>
  {% endif %}
</div>
<div style="color:var(--dim);font-size:.7rem">Auto-refresh 15s &middot; Bounce-Back v{{ version }}</div>
</body>
</html>"""


@app.route("/")
def index():
    global bot
    summary = bot.trade_log.summary() if bot else {}
    trades = sorted(bot.trade_log.all(), key=lambda t: t.entry_time, reverse=True)[:20] if bot else []
    cfg = bot.config if bot else BounceConfig()
    return render_template_string(
        DASHBOARD_HTML,
        version="1.0.0",
        mode=cfg.mode,
        summary=summary,
        trades=trades,
        threshold=cfg.move_threshold,
        entry_min=cfg.entry_window_min,
        entry_max=cfg.entry_window_max,
        contracts=cfg.base_contracts,
        assets=', '.join(a.upper() for a, e in cfg.enabled_assets.items() if e),
        max_open=cfg.max_open_positions,
        poll=cfg.poll_interval,
    )


@app.route("/api/trading/toggle", methods=["POST"])
def api_toggle_trading():
    global bot
    if not bot:
        return jsonify({"error": "Bot not running"}), 503
    bot.trading_enabled = not bot.trading_enabled
    state = "enabled" if bot.trading_enabled else "paused"
    logger.info("Trading %s via dashboard", state)
    return jsonify({"trading_enabled": bot.trading_enabled, "state": state})


@app.route("/api/status")
def api_status():
    global bot
    if not bot:
        return jsonify({"running": False})
    return jsonify({
        "running": bot.running,
        "mode": bot.config.mode,
        "trading_enabled": bot.trading_enabled,
        "summary": bot.trade_log.summary(),
        "stats": bot._stats,
        "open_trades": len(bot.trade_log.get_open()),
    })


@app.route("/api/trades")
def api_trades():
    global bot
    if not bot:
        return jsonify({"trades": []})
    trades = [t.__dict__ for t in bot.trade_log.all()]
    return jsonify({"trades": trades[-50:], "summary": bot.trade_log.summary()})


@app.route("/api/health")
def api_health():
    global bot
    running = bot is not None and bot.running
    return jsonify({"status": "healthy" if running else "unhealthy",
                    "running": running}), 200 if running else 503


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["paper", "live", "monitor"], default="paper")
    parser.add_argument("--contracts", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=8.0)
    parser.add_argument("--port", type=int, default=5052)
    parser.add_argument("--poll", type=int, default=15)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")

    api_key = os.environ.get("KALSHI_API_KEY", "")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

    trader = None
    if args.mode == "live" and api_key and key_path:
        try:
            trader = KalshiTrader(api_key, key_path)
            logger.info("Kalshi authenticated")
        except Exception as e:
            logger.warning("Auth failed: %s — paper mode", e)

    config = BounceConfig(
        mode=args.mode,
        base_contracts=args.contracts,
        move_threshold=args.threshold,
        poll_interval=args.poll,
    )

    bot = BounceBackBot(config, trader)
    threading.Thread(target=bot.run, daemon=True).start()

    logger.info("Dashboard: http://localhost:%d", args.port)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
