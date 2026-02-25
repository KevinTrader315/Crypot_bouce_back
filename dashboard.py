#!/usr/bin/env python3
"""
Momentum Bot Dashboard — Flask web UI on port 5052
"""
import argparse
import json
import logging
import os
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string, request

from momentum_bot import MomentumBot, MomentumConfig, KalshiTrader, ASSETS, BOT_VERSION

logger = logging.getLogger("dashboard")

app = Flask(__name__)
bot: MomentumBot = None

# ---------------------------------------------------------------------------
# HTML Dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Momentum Bot v{{ version }}</title>
<script>
async function toggleMode() {
  const btn = document.getElementById('modeBtn');
  btn.disabled = true;
  try {
    const r = await fetch('/api/mode/toggle', {method:'POST'});
    const d = await r.json();
    if (d.error) { alert(d.error); updateModeBtn(d.mode || 'paper'); }
    else updateModeBtn(d.mode);
  } catch(e) { alert('Error: ' + e); }
  btn.disabled = false;
}
function updateModeBtn(mode) {
  const btn = document.getElementById('modeBtn');
  if (!btn) return;
  const isLive = mode === 'live';
  btn.textContent = isLive ? '\\u25cf LIVE \\u2014 Switch to Paper' : '\\u25cb PAPER \\u2014 Switch to Live';
  btn.style.background = isLive ? 'rgba(63,185,80,0.15)' : 'rgba(227,179,65,0.12)';
  btn.style.color = isLive ? '#3fb950' : '#e3b341';
  btn.style.borderColor = isLive ? 'rgba(63,185,80,0.3)' : 'rgba(227,179,65,0.25)';
}
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
    btn.textContent = '\\u23f8 Pause Trading';
    btn.style.background = 'rgba(248,81,73,0.15)';
    btn.style.color = '#f85149';
    btn.style.borderColor = 'rgba(248,81,73,0.3)';
  } else {
    btn.textContent = '\\u25b6 Resume Trading';
    btn.style.background = 'rgba(63,185,80,0.15)';
    btn.style.color = '#3fb950';
    btn.style.borderColor = 'rgba(63,185,80,0.3)';
  }
}
async function syncStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    updateBtn(d.trading_enabled !== false);
    updateModeBtn(d.mode || 'paper');
    renderConfig(d);
  } catch(e) {}
}
function renderConfig(d) {
  const el = document.getElementById('config-card');
  if (!el) return;
  const assets = d.enabled_assets
    ? Object.entries(d.enabled_assets).filter(([,v])=>v).map(([k])=>k.toUpperCase()).join(', ')
    : '—';
  const killHours = (d.kill_hours && d.kill_hours.length)
    ? d.kill_hours.map(h=>h+'h UTC').join(', ')
    : 'none';
  const earlyMins = d.early_window_secs ? (15 - d.early_window_secs/60).toFixed(0) : '3';
  el.innerHTML =
    '<div class="stat"><span class="stat-l">Trade Size</span><span class="stat-v" style="color:var(--accent)">$'+(d.trade_dollars||'—')+' / trade</span></div>'+
    '<div class="stat"><span class="stat-l">Assets</span><span class="stat-v">'+assets+'</span></div>'+
    '<div class="stat"><span class="stat-l">Min Conviction</span><span class="stat-v" style="color:var(--green)">'+(d.min_conviction||4)+'/4</span></div>'+
    '<div class="stat"><span class="stat-l">Entry Range</span><span class="stat-v">'+Math.round((d.min_entry_price||0.4)*100)+'-'+Math.round((d.max_entry_price||0.85)*100)+'c</span></div>'+
    '<div class="stat"><span class="stat-l">Price Confirm</span><span class="stat-v">&gt;'+Math.round((d.min_price_confirm||0.5)*100)+'c</span></div>'+
    '<div class="stat"><span class="stat-l">OFI 5m / 3m</span><span class="stat-v">&gt;'+(d.ofi_threshold||0.3)+' / &gt;'+(d.early_ofi_threshold||0.5)+'</span></div>'+
    '<div class="stat"><span class="stat-l">Early Entry</span><span class="stat-v" style="color:var(--accent)">min '+earlyMins+' ('+(d.early_window_secs||720)+'s left)</span></div>'+
    '<div class="stat"><span class="stat-l">Move Bypass</span><span class="stat-v" style="color:var(--green)">&gt;'+(d.spot_move_bypass_pct||0.15)+'% spot → skip confirm</span></div>'+
    '<div class="stat"><span class="stat-l">Kill Hours</span><span class="stat-v" style="color:var(--red)">'+killHours+'</span></div>'+
    '<div class="stat"><span class="stat-l">Stop-Loss</span><span class="stat-v" style="color:'+(d.stop_loss_enabled?'var(--green)':'var(--red)')+'">'+(d.stop_loss_enabled?'ON (conv&lt;4 only)':'OFF')+'</span></div>'+
    '<div class="stat"><span class="stat-l">Max Open</span><span class="stat-v">'+(d.max_open_positions||3)+'</span></div>';
}
window.addEventListener('DOMContentLoaded', () => { syncStatus(); startAutoRefresh(); });

// -- Timeline chart --
const ASSET_COLORS = {btc:'#f7931a', eth:'#627eea', sol:'#9945ff'};
const WINDOW_SECS = 900;

function drawChart(canvas, window_data, compact) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const PAD = compact ? {t:8,r:8,b:18,l:30} : {t:12,r:12,b:28,l:38};
  const cW = W - PAD.l - PAD.r, cH = H - PAD.t - PAD.b;

  ctx.clearRect(0, 0, W, H);

  if (!window_data || !window_data.prices || window_data.prices.length < 2) {
    ctx.fillStyle = '#555';
    ctx.font = (compact?9:11)+'px monospace';
    ctx.textAlign = 'center';
    ctx.fillText('No data yet', W/2, H/2);
    return;
  }

  const prices = window_data.prices;
  const entry_start = window_data.entry_start_s || 420;
  const entry_end   = window_data.entry_end_s   || 600;
  const trade       = window_data.trade;

  // Y range: tight fit to data
  const vals = prices.map(p => p[1]);
  const dataMin = Math.min(...vals);
  const dataMax = Math.max(...vals);
  const pad = Math.max((dataMax - dataMin) * 0.2, 4);
  let yMin = Math.max(0,   Math.floor(dataMin - pad));
  let yMax = Math.min(100, Math.ceil(dataMax  + pad));
  if (yMax - yMin < 12) { const mid = (yMin+yMax)/2; yMin = Math.max(0, Math.floor(mid-6)); yMax = Math.min(100, Math.ceil(mid+6)); }

  function xPx(s)   { return PAD.l + (s / WINDOW_SECS) * cW; }
  function yPx(c)   { return PAD.t + cH - ((c - yMin) / (yMax - yMin)) * cH; }

  // Background
  ctx.fillStyle = '#0d1117';
  ctx.fillRect(0, 0, W, H);

  // Grid
  ctx.strokeStyle = '#1e2a3a';
  ctx.lineWidth = 1;
  for (let v = Math.ceil(yMin/10)*10; v <= yMax; v += 10) {
    const y = yPx(v);
    ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(PAD.l + cW, y); ctx.stroke();
    ctx.fillStyle = '#444'; ctx.font = (compact?7:9)+'px monospace'; ctx.textAlign = 'right';
    ctx.fillText(v + 'c', PAD.l - 3, y + 3);
  }
  if (!compact) {
    for (let s = 0; s <= WINDOW_SECS; s += 180) {
      const x = xPx(s);
      ctx.beginPath(); ctx.moveTo(x, PAD.t); ctx.lineTo(x, PAD.t + cH); ctx.stroke();
      ctx.fillStyle = '#444'; ctx.font = '8px monospace'; ctx.textAlign = 'center';
      ctx.fillText((s/60).toFixed(0)+'m', x, PAD.t + cH + 12);
    }
  }

  // Eval window shading
  ctx.fillStyle = 'rgba(88,166,255,0.07)';
  ctx.fillRect(xPx(entry_start), PAD.t, xPx(entry_end) - xPx(entry_start), cH);
  ctx.strokeStyle = 'rgba(88,166,255,0.25)';
  ctx.lineWidth = 1;
  ctx.setLineDash([3,3]);
  ctx.beginPath(); ctx.moveTo(xPx(entry_start), PAD.t); ctx.lineTo(xPx(entry_start), PAD.t+cH); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(xPx(entry_end),   PAD.t); ctx.lineTo(xPx(entry_end),   PAD.t+cH); ctx.stroke();
  ctx.setLineDash([]);

  // Entry price zone bands (45-78c)
  const zoneMin = window_data.entry_zone_min || 45;
  const zoneMax = window_data.entry_zone_max || 78;
  if (zoneMin >= yMin && zoneMax <= yMax) {
    ctx.fillStyle = 'rgba(63,185,80,0.04)';
    ctx.fillRect(PAD.l, yPx(zoneMax), cW, yPx(zoneMin) - yPx(zoneMax));
    ctx.strokeStyle = 'rgba(63,185,80,0.2)';
    ctx.lineWidth = 1;
    ctx.setLineDash([2,4]);
    ctx.beginPath(); ctx.moveTo(PAD.l, yPx(zoneMin)); ctx.lineTo(PAD.l+cW, yPx(zoneMin)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(PAD.l, yPx(zoneMax)); ctx.lineTo(PAD.l+cW, yPx(zoneMax)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // Price line
  const asset = window_data.event_ticker ? (
    window_data.event_ticker.includes('BTC') ? 'btc' :
    window_data.event_ticker.includes('ETH') ? 'eth' : 'sol'
  ) : 'btc';
  ctx.strokeStyle = ASSET_COLORS[asset] || '#f7931a';
  ctx.lineWidth = compact ? 1.5 : 2;
  ctx.beginPath();
  prices.forEach(([s, p], i) => {
    const x = xPx(s), y = yPx(p);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Trade entry marker
  if (trade) {
    const entryMid = (entry_start + entry_end) / 2;
    const entryPx  = yPx(trade.price);
    const won = trade.won;
    const markerColor = won === true ? '#3fb950' : won === false ? '#f85149' : '#e3b341';
    ctx.fillStyle = markerColor;
    // Triangle marker
    ctx.beginPath();
    if (trade.side === 'yes') {
      ctx.moveTo(xPx(entryMid), entryPx - (compact?6:9));
      ctx.lineTo(xPx(entryMid) - (compact?4:6), entryPx + (compact?2:3));
      ctx.lineTo(xPx(entryMid) + (compact?4:6), entryPx + (compact?2:3));
    } else {
      ctx.moveTo(xPx(entryMid), entryPx + (compact?6:9));
      ctx.lineTo(xPx(entryMid) - (compact?4:6), entryPx - (compact?2:3));
      ctx.lineTo(xPx(entryMid) + (compact?4:6), entryPx - (compact?2:3));
    }
    ctx.closePath(); ctx.fill();
    if (!compact) {
      ctx.fillStyle = markerColor; ctx.font = 'bold 8px monospace'; ctx.textAlign = 'center';
      const convLabel = trade.conviction ? ' ['+trade.conviction+'/4]' : '';
      ctx.fillText(trade.side.toUpperCase() + ' ' + trade.price + 'c' + convLabel,
        xPx(entryMid), entryPx + (trade.side==='yes'?-13:16));
    }
  }

  // "Now" cursor for current window
  if (window_data.elapsed_s != null) {
    const nowX = xPx(Math.min(window_data.elapsed_s, WINDOW_SECS));
    ctx.strokeStyle = 'rgba(255,255,255,0.3)';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(nowX, PAD.t); ctx.lineTo(nowX, PAD.t+cH); ctx.stroke();
    if (!compact) {
      ctx.fillStyle = '#aaa'; ctx.font = '8px monospace'; ctx.textAlign = 'center';
      const secsLeft = window_data.seconds_remaining;
      ctx.fillText(secsLeft > 0 ? Math.round(secsLeft)+'s left' : 'closed', nowX, PAD.t - 2);
    }
  }
}

function renderTimeline(data) {
  const container = document.getElementById('timeline-container');
  if (!container) return;

  let html = '';
  for (const asset of ['btc','eth','sol']) {
    const d = data[asset] || {};
    const cw = d.current_window;
    const rw = d.recent_windows || [];
    const color = ASSET_COLORS[asset];

    html += '<div class="tl-asset">' +
      '<div class="tl-asset-label" style="color:'+color+'">'+asset.toUpperCase()+'</div>' +
      '<div class="tl-body">';

    // Current window
    html += '<div class="tl-current">' +
      '<div class="tl-section-label">Current Window' +
        (cw ? '<span style="color:#555;font-size:.7rem;margin-left:.5rem">'+((cw.event_ticker)||'')+'</span>' : '') +
        (cw && cw.seconds_remaining > 0 ? '<span class="tl-countdown" id="cd-'+asset+'">'+Math.round(cw.seconds_remaining)+'s</span>' : '') +
      '</div>' +
      '<canvas id="canvas-'+asset+'-current" width="820" height="130" style="display:block;max-width:100%"></canvas>' +
    '</div>';

    // Recent windows
    if (rw.length > 0) {
      html += '<div class="tl-section-label" style="margin-top:.75rem">Recent Windows</div>';
      html += '<div class="tl-recent-grid">';
      for (let i = 0; i < rw.length; i++) {
        const w = rw[i];
        const trade = w.trade;
        const label = w.close_time ? w.close_time.substring(11,16)+'Z' : '';
        const tradeLabel = trade ?
          '<span class="tl-trade-badge" style="color:'+(trade.won===true?'#3fb950':trade.won===false?'#f85149':'#e3b341')+'">'+
            trade.side.toUpperCase()+' '+trade.price+'c ['+trade.conviction+'/4] '+
            (trade.won===true?'W':trade.won===false?'L':'...')+
          '</span>' : '';
        html += '<div class="tl-recent-card">' +
          '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">' +
            '<span style="font-size:.65rem;color:#555">'+label+'</span>'+tradeLabel +
          '</div>' +
          '<canvas id="canvas-'+asset+'-recent-'+i+'" width="160" height="65" style="display:block;width:100%"></canvas>' +
        '</div>';
      }
      html += '</div>';
    }

    html += '</div></div>';
  }
  container.innerHTML = html;

  // Draw all canvases
  for (const asset of ['btc','eth','sol']) {
    const d = data[asset] || {};
    const cw = d.current_window;
    const rw = d.recent_windows || [];
    const cvs = document.getElementById('canvas-'+asset+'-current');
    if (cvs) { cvs.width = cvs.offsetWidth || 820; drawChart(cvs, cw, false); }
    for (let i = 0; i < rw.length; i++) {
      const rc = document.getElementById('canvas-'+asset+'-recent-'+i);
      if (rc) { rc.width = rc.offsetWidth || 160; drawChart(rc, rw[i], true); }
    }
  }

  // Store countdowns
  window._tlCountdowns = {};
  for (const asset of ['btc','eth','sol']) {
    const cw = (data[asset]||{}).current_window;
    if (cw && cw.seconds_remaining > 0) {
      window._tlCountdowns[asset] = {ends: Date.now() + cw.seconds_remaining * 1000};
    }
  }
}

function tickCountdowns() {
  if (!window._tlCountdowns) return;
  for (const [asset, cd] of Object.entries(window._tlCountdowns)) {
    const el = document.getElementById('cd-' + asset);
    if (!el) continue;
    const left = Math.round((cd.ends - Date.now()) / 1000);
    el.textContent = left > 0 ? left + 's' : 'settling...';
    el.style.color = left < 60 ? '#f85149' : left < 180 ? '#e3b341' : '#3fb950';
  }
}

async function fetchTimeline() {
  try {
    const r = await fetch('/api/timeline');
    const d = await r.json();
    renderTimeline(d);
  } catch(e) {}
}

async function fetchStats() {
  try {
    const r = await fetch('/api/trades');
    const d = await r.json();
    const s = d.summary || {};
    const el = document.getElementById('stats-card');
    if (!el) return;
    const closed = (s.closed||0);
    const wr = closed ? (s.win_rate*100).toFixed(1)+'% ('+s.wins+'/'+closed+')' : '\\u2014';
    const pnl = s.total_pnl ? '$'+(s.total_pnl>=0?'+':'')+s.total_pnl.toFixed(3) : '$0.000';
    const c4wr = s.conv_4 ? (s.conv_4_wr*100).toFixed(0)+'%' : '\\u2014';
    const c3wr = s.conv_3 ? (s.conv_3_wr*100).toFixed(0)+'%' : '\\u2014';
    el.innerHTML =
      '<div class="stat"><span class="stat-l">Total Trades</span><span class="stat-v">'+((s.total)||0)+'</span></div>' +
      '<div class="stat"><span class="stat-l">Open</span><span class="stat-v" style="color:var(--accent)">'+((s.open)||0)+'</span></div>' +
      '<div class="stat"><span class="stat-l">4/4 Conv</span><span class="stat-v" style="color:var(--green)">'+((s.conv_4)||0)+' ('+c4wr+')</span></div>' +
      '<div class="stat"><span class="stat-l">3/4 Conv</span><span class="stat-v" style="color:var(--yellow)">'+((s.conv_3)||0)+' ('+c3wr+')</span></div>' +
      '<div class="stat"><span class="stat-l">Settlements</span><span class="stat-v">'+((s.settlements)||0)+'</span></div>' +
      '<div class="stat"><span class="stat-l">Stop-Losses</span><span class="stat-v" style="color:var(--red)">'+((s.stop_losses)||0)+'</span></div>' +
      '<div class="stat"><span class="stat-l">Time Exits</span><span class="stat-v">'+((s.time_exits)||0)+'</span></div>' +
      '<div class="stat"><span class="stat-l">Win Rate</span><span class="stat-v '+(s.win_rate>=0.6?'pos':s.win_rate>0&&s.win_rate<0.4?'neg':'')+'">'+wr+'</span></div>' +
      '<div class="stat"><span class="stat-l">Total P&amp;L</span><span class="stat-v '+(s.total_pnl>=0?'pos':'neg')+'">'+pnl+'</span></div>';

    // Recent trades table
    const trades = (d.trades || []).slice(-20).reverse();
    const tbl = document.getElementById('trades-table');
    if (!tbl) return;
    if (!trades.length) { tbl.innerHTML = '<tr><td colspan="9" style="color:var(--dim);text-align:center;padding:1rem">No trades yet</td></tr>'; return; }
    tbl.innerHTML = trades.map(function(t) {
      const exitLabel = t.exit_type === 'stop_loss' ? 'STOP @'+Math.round((t.exit_price||0)*100)+'c'
        : t.exit_type === 'time_exit' ? 'TIME @'+Math.round((t.exit_price||0)*100)+'c'
        : t.status === 'settled' ? 'SETTLE '+(t.result||'?').toUpperCase()
        : t.status === 'open' ? 'HOLDING' : '\\u2014';
      const exitColor = t.exit_type === 'stop_loss' ? 'var(--red)' : t.exit_type === 'time_exit' ? 'var(--yellow)' : t.status === 'settled' ? 'var(--dim)' : 'var(--accent)';
      const convColor = t.conviction === 4 ? 'var(--green)' : 'var(--yellow)';
      const signals = t.f5m_dir.substring(0,1).toUpperCase() + '/' + t.mid_dir.substring(0,1).toUpperCase() + '/' + (t.taker_buy_ratio > 0.55 ? 'B' : t.taker_buy_ratio < 0.45 ? 'S' : '-');
      return '<tr>' +
      '<td>'+t.entry_time.substring(11,19)+'</td>' +
      '<td style="font-weight:700;color:var(--orange)">'+t.asset.toUpperCase()+'</td>' +
      '<td><span class="badge badge-'+t.entry_side+'">'+t.entry_side.toUpperCase()+'</span></td>' +
      '<td>'+Math.round(t.entry_price*100)+'c</td>' +
      '<td>'+t.contracts+'</td>' +
      '<td style="color:'+convColor+';font-weight:700">'+t.conviction+'/4</td>' +
      '<td style="font-size:.7rem;color:var(--dim)">'+signals+'</td>' +
      '<td style="font-size:.7rem;color:'+exitColor+'">'+exitLabel+'</td>' +
      '<td class="'+(t.pnl_net>0?'pos':t.pnl_net<0?'neg':'')+'">'+
        (t.pnl_net?'$'+t.pnl_net.toFixed(3):'\\u2014')+'</td>' +
    '</tr>';}).join('');
  } catch(e) {}
}

async function saveContracts() {
  const val = parseInt(document.getElementById('contractsInput').value);
  const fb = document.getElementById('contractsFb');
  if (!val || val < 1 || val > 50) { fb.textContent = 'invalid'; fb.style.color='var(--red)'; return; }
  try {
    const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({base_contracts: val})});
    const d = await r.json();
    if (d.ok) { fb.textContent = 'saved'; fb.style.color='var(--green)'; setTimeout(function(){fb.textContent='';}, 2000); }
    else { fb.textContent = d.error || 'error'; fb.style.color='var(--red)'; }
  } catch(e) { fb.textContent = 'error'; fb.style.color='var(--red)'; }
}

function startAutoRefresh() {
  fetchTimeline();
  fetchStats();
  setInterval(fetchTimeline, 5000);
  setInterval(fetchStats, 5000);
  setInterval(syncStatus, 5000);
  setInterval(tickCountdowns, 1000);
}
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
  /* Timeline */
  .tl-asset{margin-bottom:1.25rem;}
  .tl-asset-label{font-size:.8rem;font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:.4rem;}
  .tl-body{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:.75rem;}
  .tl-section-label{font-size:.65rem;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:.4rem;display:flex;align-items:center;gap:.5rem;}
  .tl-countdown{font-size:.7rem;font-weight:700;color:var(--green);background:rgba(63,185,80,.1);padding:1px 6px;border-radius:3px;}
  .tl-current canvas{border-radius:4px;background:#0d1117;}
  .tl-recent-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:.5rem;}
  .tl-recent-card{background:#0d1520;border:1px solid #1e2a3a;border-radius:6px;padding:.4rem;}
  .tl-trade-badge{font-size:.65rem;font-weight:700;}
  .tl-legend{display:flex;gap:1rem;font-size:.65rem;color:var(--dim);margin-top:.35rem;flex-wrap:wrap;}
  .tl-legend span{display:flex;align-items:center;gap:.3rem;}
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;">
  <h1 style="margin:0">Momentum Bot v{{ version }} &mdash; <span class="mode-{{ mode }}">{{ mode.upper() }}</span></h1>
  <div style="display:flex;gap:0.5rem;">
    <button id="modeBtn" onclick="toggleMode()" style="
      padding:0.4rem 1rem;border-radius:6px;cursor:pointer;font-family:monospace;
      font-size:0.8rem;font-weight:700;border:1px solid;transition:all 0.2s;
      background:rgba(227,179,65,0.12);color:#e3b341;border-color:rgba(227,179,65,0.25);">
      &#9675; PAPER &mdash; Switch to Live
    </button>
    <button id="tradeBtn" onclick="toggleTrading()" style="
      padding:0.4rem 1rem;border-radius:6px;cursor:pointer;font-family:monospace;
      font-size:0.8rem;font-weight:700;border:1px solid;transition:all 0.2s;
      background:rgba(248,81,73,0.15);color:#f85149;border-color:rgba(248,81,73,0.3);">
      &#9208; Pause Trading
    </button>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h3>Performance</h3>
    <div id="stats-card">
      <div class="stat"><span class="stat-l">Total Trades</span><span class="stat-v">{{ summary.total }}</span></div>
      <div class="stat"><span class="stat-l">Open</span><span class="stat-v" style="color:var(--accent)">{{ summary.open }}</span></div>
      <div class="stat"><span class="stat-l">4/4 Conv</span><span class="stat-v" style="color:var(--green)">{{ summary.conv_4 }}</span></div>
      <div class="stat"><span class="stat-l">3/4 Conv</span><span class="stat-v" style="color:var(--yellow)">{{ summary.conv_3 }}</span></div>
      <div class="stat"><span class="stat-l">Win Rate</span><span class="stat-v">&mdash;</span></div>
      <div class="stat"><span class="stat-l">Total P&amp;L</span><span class="stat-v">$0.000</span></div>
    </div>
  </div>
  <div class="card">
    <h3>Config</h3>
    <div id="config-card"><div style="color:var(--dim);font-size:.75rem">Loading...</div></div>
  </div>
  <div class="card" style="grid-column:1/-1">
    <div class="tl-legend">
      <span><svg width="20" height="8"><rect x="0" y="0" width="20" height="8" fill="rgba(88,166,255,0.15)" stroke="rgba(88,166,255,0.4)" stroke-width="1"/></svg> Eval window (7-10m into window)</span>
      <span><svg width="20" height="8"><line x1="0" y1="4" x2="20" y2="4" stroke="rgba(63,185,80,0.4)" stroke-width="1" stroke-dasharray="2,4"/></svg> 40-85c entry zone</span>
      <span><svg width="8" height="8"><polygon points="4,0 8,8 0,8" fill="#3fb950"/></svg> Trade entry (conv 3-4/4)</span>
    </div>
  </div>
</div>

<!-- Timeline -->
<div id="timeline-container" style="margin-bottom:1rem">
  <div style="color:var(--dim);text-align:center;padding:2rem">Loading timeline...</div>
</div>

<!-- Trade log -->
<div class="card" style="margin-bottom:1rem">
  <h3>Recent Trades (last 20)</h3>
  <table>
    <thead><tr><th>Time</th><th>Asset</th><th>Side</th><th>Entry</th><th>Qty</th><th>Conv</th><th>Signals</th><th>Exit</th><th>P&amp;L</th></tr></thead>
    <tbody id="trades-table"><tr><td colspan="9" style="color:var(--dim);text-align:center;padding:1rem">Loading...</td></tr></tbody>
  </table>
</div>

<div style="color:var(--dim);font-size:.7rem">Auto-refresh 5s &middot; Momentum v{{ version }}</div>
</body>
</html>"""


@app.route("/")
def index():
    global bot
    summary = bot.trade_log.summary() if bot else {}
    cfg = bot.config if bot else MomentumConfig()
    return render_template_string(
        DASHBOARD_HTML,
        version=BOT_VERSION,
        mode=cfg.mode,
        summary=summary,
        eval_min=cfg.eval_window_min,
        eval_max=cfg.eval_window_max,
        min_conv=cfg.min_conviction,
        ofi_threshold=cfg.ofi_threshold,
        price_confirm=int(cfg.min_price_confirm * 100),
        stop_loss=cfg.stop_loss_enabled,
        contracts=cfg.base_contracts,
        min_entry=int(cfg.min_entry_price * 100),
        max_entry=int(cfg.max_entry_price * 100),
        assets=', '.join(a.upper() for a, e in cfg.enabled_assets.items() if e),
        max_open=cfg.max_open_positions,
        poll=cfg.poll_interval,
    )


@app.route("/api/mode/toggle", methods=["POST"])
def api_toggle_mode():
    global bot
    if not bot:
        return jsonify({"error": "Bot not running"}), 503
    if bot.config.mode == "live":
        bot.config.mode = "paper"
    else:
        bot.config.mode = "live"
        # Require Kalshi auth to go live
        if bot.trader is None:
            bot.config.mode = "paper"
            return jsonify({"error": "No Kalshi credentials — cannot switch to live", "mode": "paper"}), 400
    logger.info("Mode switched to %s via dashboard", bot.config.mode.upper())
    return jsonify({"mode": bot.config.mode})


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
        # Live config
        "trade_dollars": bot.config.trade_dollars,
        "base_contracts": bot.config.base_contracts,
        "min_entry_price": bot.config.min_entry_price,
        "max_entry_price": bot.config.max_entry_price,
        "min_conviction": bot.config.min_conviction,
        "ofi_threshold": bot.config.ofi_threshold,
        "min_price_confirm": bot.config.min_price_confirm,
        "stop_loss_enabled": bot.config.stop_loss_enabled,
        "kill_hours": bot.config.kill_hours,
        "eval_window_min": bot.config.eval_window_min,
        "eval_window_max": bot.config.eval_window_max,
        "max_open_positions": bot.config.max_open_positions,
        "enabled_assets": bot.config.enabled_assets,
        "early_window_secs": bot.config.early_window_secs,
        "early_ofi_threshold": bot.config.early_ofi_threshold,
        "spot_move_bypass_pct": bot.config.spot_move_bypass_pct,
    })


@app.route("/api/trades")
def api_trades():
    global bot
    if not bot:
        return jsonify({"trades": []})
    trades = [t.__dict__ for t in bot.trade_log.all()]
    return jsonify({"trades": trades[-50:], "summary": bot.trade_log.summary()})


@app.route("/api/config", methods=["POST"])
def api_config():
    global bot
    if not bot:
        return jsonify({"error": "Bot not running"}), 503
    data = request.get_json() or {}
    changed = []
    if "base_contracts" in data:
        val = int(data["base_contracts"])
        if 1 <= val <= 50:
            bot.config.base_contracts = val
            changed.append(f"base_contracts={val}")
        else:
            return jsonify({"error": "base_contracts must be 1-50"}), 400
    if not changed:
        return jsonify({"error": "No valid fields provided"}), 400
    logger.info("Config updated via dashboard: %s", ", ".join(changed))
    return jsonify({"ok": True, "changed": changed})


@app.route("/api/timeline")
def api_timeline():
    global bot
    if not bot:
        return jsonify({})
    return jsonify(bot.get_timeline_data())


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
    parser.add_argument("--trade-dollars", type=float, default=15.0,
                        help="Target spend per trade in dollars")
    parser.add_argument("--min-conviction", type=int, default=4,
                        help="Minimum conviction score (default 4 = all signals agree)")
    parser.add_argument("--kill-hours", type=str, default="",
                        help="Comma-separated UTC hours to skip, e.g. '20,21'")
    parser.add_argument("--no-stop-loss", action="store_true",
                        help="Disable stop-loss exits")
    parser.add_argument("--port", type=int, default=5052)
    parser.add_argument("--poll", type=int, default=15)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")

    api_key = os.environ.get("KALSHI_API_KEY", "")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

    # Always init trader if credentials present — needed for live mode toggle at runtime
    trader = None
    if api_key and key_path:
        try:
            trader = KalshiTrader(api_key, key_path)
            logger.info("Kalshi authenticated (ready for live mode)")
        except Exception as e:
            logger.warning("Auth failed: %s — live mode toggle will be unavailable", e)

    kill_hours = [int(h.strip()) for h in args.kill_hours.split(",") if h.strip()] if args.kill_hours else [20, 21]

    config = MomentumConfig(
        mode=args.mode,
        trade_dollars=args.trade_dollars,
        base_contracts=args.contracts,
        min_conviction=args.min_conviction,
        kill_hours=kill_hours,
        stop_loss_enabled=not args.no_stop_loss,
        poll_interval=args.poll,
    )

    bot = MomentumBot(config, trader)
    threading.Thread(target=bot.run, daemon=True).start()

    logger.info("Dashboard: http://localhost:%d", args.port)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
