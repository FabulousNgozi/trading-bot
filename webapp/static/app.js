/* ==========================================================================
   Deriv Volatility Bot — control panel front-end
   Plain JS. No build step. SSE for live state, polling fallback if SSE fails.
   ========================================================================== */
'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  sse: null,
  sseFailures: 0,
  pollTimer: null,
  logs: [],
  lastLogTs: 0,
  snapshot: null,
  countdownTimer: null,
};

/* ------------------------------------------------------------------ utils */
function fmt(n, d = 2) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  return Number(n).toLocaleString('en-US', {
    minimumFractionDigits: d, maximumFractionDigits: d,
  });
}
function fmtPrice(n) {
  if (n === null || n === undefined) return '—';
  const v = Number(n);
  return v >= 1000 ? v.toFixed(2) : v.toFixed(v >= 10 ? 3 : 5);
}
function fmtMoney(n, cur = '') {
  if (n === null || n === undefined) return '—';
  const v = Number(n);
  const s = (v > 0 ? '+' : '') + v.toFixed(2);
  return cur ? `${s} ${cur}` : s;
}
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('en-GB', { hour12: false });
}
function fmtDateTime(str) {
  if (!str) return '—';
  // Stored as local ISO from Python; show date + time compactly.
  const d = new Date(str);
  if (Number.isNaN(d.getTime())) return str;
  return d.toLocaleString('en-GB', {
    month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  });
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)dv_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : '';
}

/* --------------------------------------------------------------- api calls */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch (_) { /* empty body ok */ }
  if (!res.ok || (body && body.ok === false)) {
    const msg = (body && body.error) || `HTTP ${res.status}`;
    const err = new Error(msg);
    err.payload = body;
    throw err;
  }
  return body ? body.data : null;
}

async function loadTradeSettings() {
  const status = $('trade-settings-status');
  try {
    const d = await api('/api/settings');
    $('dash-stake').value = d.stake || '1';
    $('dash-max-open').value = d.max_open_contracts || '3';
    $('dash-stop-loss').value = d.daily_loss_limit || '5';
    $('dash-take-profit').value = d.daily_profit_target ?? '0';
    status.textContent = 'Loaded';
  } catch (e) {
    status.textContent = 'Could not load settings';
    console.error(e);
  }
}

async function saveTradeSettings() {
  const btn = $('btn-save-trade-settings');
  const status = $('trade-settings-status');
  const payload = {
    stake: $('dash-stake').value,
    max_open_contracts: $('dash-max-open').value,
    daily_loss_limit: $('dash-stop-loss').value,
    daily_profit_target: $('dash-take-profit').value,
  };
  btn.disabled = true;
  status.textContent = 'Saving…';
  try {
    const data = await api('/api/settings/trading', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken(),
      },
      body: JSON.stringify(payload),
    });
    status.textContent = data.message || 'Saved';
  } catch (e) {
    status.textContent = `Save failed: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------ render: head */
const STATE_LABEL = {
  not_configured: 'NOT CONFIGURED',
  stopped: 'STOPPED',
  starting: 'STARTING',
  running: 'RUNNING',
  paused: 'PAUSED',
  stopping: 'STOPPING',
  error: 'ERROR',
};

function renderStatus(snap) {
  const status = snap.status || 'stopped';

  const badge = $('state-badge');
  badge.textContent = STATE_LABEL[status] || status.toUpperCase();
  badge.className = 'badge ' + status;

  $('state-detail').textContent = snap.status_detail || '';

  // Connection indicator
  const dot = $('conn-dot');
  const label = $('conn-label');
  dot.className = 'conn-dot';
  if (status === 'running') { dot.classList.add('live'); label.textContent = 'Connected · live'; }
  else if (status === 'paused') { dot.classList.add('paused'); label.textContent = 'Connected · paused'; }
  else if (status === 'error') { dot.classList.add('err'); label.textContent = 'Error'; }
  else if (status === 'starting' || status === 'stopping') { label.textContent = status === 'starting' ? 'Connecting…' : 'Stopping…'; }
  else if (status === 'not_configured') { label.textContent = 'Not configured'; }
  else { label.textContent = 'Disconnected'; }

  // Buttons
  const running = status === 'running';
  const paused = status === 'paused';
  const active = running || paused;
  $('btn-start').disabled = active || status === 'starting' || status === 'stopping';
  $('btn-stop').disabled = !active && status !== 'starting';
  $('btn-pause').disabled = !running;
  $('btn-resume').disabled = !paused;

  // Account box
  const acct = snap.account;
  if (acct && acct.account_id) {
    $('account-box').hidden = false;
    $('acct-id').textContent = acct.account_id;
    $('acct-bal').textContent = `${fmt(acct.balance)} ${acct.currency || ''}`.trim();
  } else {
    $('account-box').hidden = true;
  }

  // Danger banner: only for a genuine demo violation, not a generic error.
  const danger = $('danger-banner');
  const errText = (snap.error || '') + ' ' + (snap.status_detail || '');
  const isDemoViolation = /demo-only|not a demo|real-money|real account/i.test(errText);
  if (isDemoViolation) {
    danger.hidden = false;
    $('danger-title').textContent = 'NOT A DEMO ACCOUNT';
    $('danger-text').textContent =
      'The bot refused to connect. Use an API token created from your DERIV DEMO account.';
  } else {
    danger.hidden = true;
  }

  // Setup card
  const cfg = snap.config || {};
  const showSetup = !cfg.configured && !active;
  $('setup-card').hidden = !showSetup;
  if (showSetup) {
    const missing = [];
    if (!cfg.app_id_present) missing.push('DERIV_APP_ID');
    if (!cfg.token_present) missing.push('DERIV_API_TOKEN');
    $('setup-text').textContent = missing.length
      ? `Missing from the environment: ${missing.join(', ')}.`
      : 'Credentials look present but the bot has not started yet.';
  }

  // Error card
  const showErr = snap.error && !isDemoViolation;
  $('error-card').hidden = !showErr;
  if (showErr) {
    $('error-title').textContent =
      status === 'not_configured' ? 'Not configured' : 'Bot error';
    $('error-text').textContent = snap.error;
  }

  // Uptime / scans
  const up = snap.uptime_seconds || 0;
  $('uptime').textContent = up
    ? `Uptime ${Math.floor(up / 3600)}h ${Math.floor((up % 3600) / 60)}m ${up % 60}s`
    : '—';
  $('scan-count').textContent = snap.scans
    ? `${snap.scans} scan${snap.scans === 1 ? '' : 's'} · ${snap.signals_seen} signal(s)`
    : '—';
}

/* ------------------------------------------------------------ render: risk */
function barClass(pct, invert = false) {
  if (invert) {
    if (pct >= 90) return 'bad';
    if (pct >= 60) return 'warn';
    return 'good';
  }
  if (pct >= 100) return 'bad';
  if (pct >= 70) return 'warn';
  return 'good';
}

function renderRisk(snap) {
  const r = snap.risk || {};
  const cur = (snap.account && snap.account.currency) || '';

  $('risk-day').textContent = r.trading_day ? `Trading day ${r.trading_day}` : '—';

  // Daily P/L
  const pnl = Number(r.realised_pnl || 0);
  const pnlEl = $('m-pnl');
  pnlEl.textContent = fmtMoney(pnl, cur);
  pnlEl.className = 'metric-value ' + (pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '');
  const limit = Number(r.daily_loss_limit || 0);
  const target = Number(r.daily_profit_target || 0);
  const pnlLimits = [];
  if (limit) pnlLimits.push(`stop loss −${fmt(limit)} ${cur}`.trim());
  if (target) pnlLimits.push(`take profit +${fmt(target)} ${cur}`.trim());
  $('m-pnl-sub').textContent = pnlLimits.join(' · ') || 'no daily P/L limits';

  // Loss limit remaining
  const left = Number(r.daily_loss_remaining ?? limit);
  const pctUsed = Number(r.daily_loss_pct || 0);
  $('m-lossleft').textContent = `${fmt(left)} ${cur}`.trim();
  $('m-lossleft').className = 'metric-value ' + (pctUsed >= 90 ? 'neg' : pctUsed >= 60 ? 'warn' : 'pos');
  const lossBar = $('bar-loss');
  lossBar.style.width = Math.min(100, pctUsed) + '%';
  lossBar.className = 'bar-fill ' + barClass(pctUsed, true);
  $('m-loss-sub').textContent = limit
    ? `${fmt(r.daily_loss_used || 0)} of ${fmt(limit)} used (${pctUsed}%)`
    : '—';

  const profitLeft = Number(r.daily_profit_remaining ?? target);
  const profitPct = Number(r.daily_profit_pct || 0);
  $('m-profitleft').textContent = target
    ? `${fmt(profitLeft)} ${cur}`.trim()
    : 'off';
  $('m-profitleft').className = 'metric-value ' + (profitPct >= 90 ? 'pos' : '');
  const profitBar = $('bar-profit');
  profitBar.style.width = Math.min(100, profitPct) + '%';
  profitBar.className = 'bar-fill good';
  $('m-profit-sub').textContent = target
    ? `${fmt(Math.max(0, pnl))} of ${fmt(target)} reached (${profitPct}%)`
    : 'disabled';

  // Consecutive losses
  const streak = Number(r.consecutive_losses || 0);
  const maxStreak = Number(r.max_consecutive_losses || 3);
  $('m-streak').textContent = `${streak} / ${maxStreak}`;
  $('m-streak').className = 'metric-value ' + (streak >= maxStreak ? 'neg' : streak > 0 ? 'warn' : '');
  const sPct = maxStreak ? Math.min(100, (streak / maxStreak) * 100) : 0;
  const sBar = $('bar-streak');
  sBar.style.width = sPct + '%';
  sBar.className = 'bar-fill ' + (streak >= maxStreak ? 'bad' : streak > 0 ? 'warn' : 'good');
  $('m-streak-sub').textContent = streak >= maxStreak
    ? 'Paused — reset required'
    : `${maxStreak - streak} more before pause`;

  // Trades today
  const used = Number(r.trades_today || 0);
  const maxT = Number(r.max_trades_per_day || 0);
  $('m-trades').textContent = maxT ? `${used} / ${maxT}` : `${used}`;
  $('m-trades').className = 'metric-value ' + (maxT && used >= maxT ? 'neg' : '');
  const tPct = Number(r.trades_pct || 0);
  const tBar = $('bar-trades');
  tBar.style.width = Math.min(100, tPct) + '%';
  tBar.className = 'bar-fill ' + barClass(tPct, true);
  $('m-trades-sub').textContent = maxT
    ? `${Math.max(0, maxT - used)} remaining today`
    : '—';

  // Footer
  $('m-cooldown').textContent = r.can_trade ? 'clear' : (r.blocked_by === 'cooldown' ? 'active' : '—');
  $('m-open').textContent = `${r.open_contracts ?? 0} / ${r.max_open_contracts ?? 1}`;

  const blocked = r.blocked_by;
  $('m-blocked').textContent = (!blocked || blocked === 'ok') ? 'nothing' : blocked.replaceAll('_', ' ');
  $('m-blocked').className = (!blocked || blocked === 'ok') ? 'pos' : 'neg';
}

/* -------------------------------------------------------- render: contract */
function renderContract(snap) {
  const contracts = Array.isArray(snap.open_contracts)
    ? snap.open_contracts
    : (snap.open_contract ? [snap.open_contract] : []);
  const card = $('contract-card');
  if (!contracts.length) { card.hidden = true; return; }
  card.hidden = false;
  $('ct-count').textContent = `${contracts.length} open`;
  $('open-contracts-body').innerHTML = contracts.map((c) => {
    const dir = c.direction || '—';
    const dirCls = dir === 'CALL' ? 'dir-call' : dir === 'PUT' ? 'dir-put' : '';
    return `<tr>
      <td class="sym">${esc(c.symbol || '—')}</td>
      <td class="${dirCls}">${esc(dir)}</td>
      <td class="num">${fmt(c.stake, 2)}</td>
      <td class="num">${fmt(c.payout, 2)}</td>
      <td>${esc(c.contract_id ?? '—')}</td>
      <td>${esc(c.entry_time ? fmtDateTime(c.entry_time) : '—')}</td>
      <td class="num contract-countdown" data-expires="${Number(c.expires_ts) || 0}">—</td>
      <td class="num">${esc(c.signal_score ?? '—')}</td>
    </tr>`;
  }).join('');
  tickCountdown();
}

function tickCountdown() {
  const now = Date.now() / 1000;
  document.querySelectorAll('.contract-countdown').forEach((el) => {
    const expiry = Number(el.dataset.expires || 0);
    if (!expiry) { el.textContent = '—'; return; }
    const left = Math.max(0, expiry - now);
    const m = Math.floor(left / 60);
    const s = Math.floor(left % 60);
    el.textContent = left > 0
      ? `${m}:${String(s).padStart(2, '0')}`
      : 'settling…';
  });
}

/* --------------------------------------------------------- render: scanner */
function statusClass(row) {
  if (row.actionable) return 'sig';
  const s = (row.status || '').toLowerCase();
  if (s.includes('risk') || s.includes('warming') || s.includes('error') || s.includes('not enough')) return 'err';
  if (s.includes('blocked') || s.includes('score')) return 'blk';
  return '';
}

function renderScanner(snap) {
  const body = $('scanner-body');
  const rows = snap.scanner || [];
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="12" class="empty">Waiting for the first scan…</td></tr>';
    return;
  }

  body.innerHTML = rows.map((r) => {
    const dir = r.direction || 'NONE';
    const dirCls = dir === 'CALL' ? 'dir-call' : dir === 'PUT' ? 'dir-put' : 'dir-none';
    const score = Number(r.score || 0);
    const scoreCls = score >= 80 ? 'hi' : score < 60 ? 'lo' : '';
    const reasons = (r.reasons || []).join('\n');
    return `<tr title="${esc(reasons)}">
      <td class="sym">${esc(r.symbol || `Market ${r.index_value ?? '?'}`)}</td>
      <td class="num">${fmtPrice(r.price)}</td>
      <td class="num">${fmtPrice(r.ema_fast)}</td>
      <td class="num">${fmtPrice(r.ema_mid)}</td>
      <td class="num">${fmtPrice(r.ema_slow)}</td>
      <td class="num">${r.rsi === null || r.rsi === undefined ? '—' : Number(r.rsi).toFixed(1)}</td>
      <td class="num">${fmtPrice(r.support)}</td>
      <td class="num">${fmtPrice(r.resistance)}</td>
      <td class="num"><div class="score-cell">${score}<span class="score-mini"><i class="${scoreCls}" style="width:${Math.min(100, score)}%"></i></span></div></td>
      <td class="${dirCls}">${esc(dir)}</td>
      <td class="dim">${esc(r.trend_1m || '—')} / ${esc(r.trend_1h || '—')}</td>
      <td><span class="status-pill ${statusClass(r)}" title="${esc(reasons)}">${esc(r.status || '—')}</span></td>
    </tr>`;
  }).join('');

  $('scan-time').textContent = snap.last_scan_at
    ? `Last scan ${fmtTime(snap.last_scan_at)}`
    : '—';
}

/* ---------------------------------------------------------- render: trades */
function renderTrades(snap) {
  const body = $('trades-body');
  const trades = (snap.trades || []).slice(0, 100);

  // Prefer the /api/trades call; fall back to scanning snapshot.
  if (!trades.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No trades yet.</td></tr>';
    return;
  }

  body.innerHTML = trades.map((t) => {
    const dir = t.direction || '';
    const dirCls = dir === 'CALL' ? 'dir-call' : 'dir-put';
    const profit = t.profit === null || t.profit === undefined ? null : Number(t.profit);
    const pnlCls = profit === null ? '' : profit > 0 ? 'pos' : profit < 0 ? 'neg' : '';
    const pnlTxt = profit === null ? '—' : fmtMoney(profit);
    const status = (t.status || '—');
    const stCls = status === 'won' ? 'pos' : status === 'lost' ? 'neg' : status === 'unresolved' ? 'warn' : '';
    return `<tr>
      <td class="dim">${esc(fmtDateTime(t.opened_at))}</td>
      <td class="sym">${esc(t.symbol || '—')}</td>
      <td class="${dirCls}">${esc(dir)}</td>
      <td class="num">${fmt(t.stake, 2)}</td>
      <td class="num">${fmt(t.payout, 2)}</td>
      <td class="num ${pnlCls}">${pnlTxt}</td>
      <td class="${stCls}">${esc(status.toUpperCase())}</td>
      <td class="num dim">${t.signal_score ?? '—'}</td>
    </tr>`;
  }).join('');
}

/* -------------------------------------------------------- render: backtest */
function renderBacktest(snap) {
  const running = snap.backtest_running;
  $('bt-running').hidden = !running;
  $('btn-backtest').disabled = running;

  const bt = snap.backtest;
  if (!bt) {
    $('bt-empty').hidden = running;
    $('bt-result').hidden = true;
    return;
  }
  $('bt-empty').hidden = true;

  if (bt.error) {
    $('bt-result').hidden = false;
    $('bt-summary').innerHTML =
      `<p class="neg">Backtest failed: ${esc(bt.error)}</p>`;
    $('bt-body').innerHTML = '';
    $('bt-assumptions').innerHTML = '';
    $('bt-limitations').innerHTML = '';
    $('bt-warn').textContent = '';
    return;
  }

  $('bt-result').hidden = false;
  const wr = bt.overall_win_rate;
  const be = bt.breakeven_win_rate;
  const edge = (wr !== null && be !== null) ? (wr - be) : null;

  $('bt-summary').innerHTML = `
    <div class="bt-stat"><span>Trades</span><b>${bt.total_trades}</b></div>
    <div class="bt-stat"><span>Win rate</span><b>${wr === null ? '—' : wr + '%'}</b></div>
    <div class="bt-stat"><span>Breakeven</span><b>${be === null ? '—' : be + '%'}</b></div>
    <div class="bt-stat"><span>Edge</span><b class="${edge === null ? '' : edge > 0 ? 'pos' : 'neg'}">${edge === null ? '—' : (edge > 0 ? '+' : '') + edge.toFixed(2) + 'pt'}</b></div>
    <div class="bt-stat"><span>Net</span><b class="${bt.net_profit > 0 ? 'pos' : bt.net_profit < 0 ? 'neg' : ''}">${fmtMoney(bt.net_profit)}</b></div>
    <div class="bt-stat"><span>Period</span><b style="font-size:12px">${esc(bt.period_start)} → ${esc(bt.period_end)}</b></div>
  `;

  $('bt-body').innerHTML = (bt.markets || []).map((m) => {
    if (m.error) {
      return `<tr><td class="sym">${esc(m.symbol)}</td><td colspan="7" class="dim">${esc(m.error)}</td></tr>`;
    }
    return `<tr>
      <td class="sym">${esc(m.symbol)}</td>
      <td class="num">${m.trades}</td>
      <td class="num">${m.wins}/${m.losses}</td>
      <td class="num">${m.win_rate === null ? '—' : m.win_rate + '%'}</td>
      <td class="num ${m.net_profit > 0 ? 'pos' : m.net_profit < 0 ? 'neg' : ''}">${fmtMoney(m.net_profit)}</td>
      <td class="num">${m.return_pct === null ? '—' : m.return_pct + '%'}</td>
      <td class="num">${fmt(m.max_drawdown)}</td>
      <td class="num dim">${m.longest_losing_streak}</td>
    </tr>`;
  }).join('');

  $('bt-assumptions').innerHTML = (bt.assumptions || [])
    .map((a) => `<li>${esc(a)}</li>`).join('');
  $('bt-limitations').innerHTML = (bt.limitations || [])
    .map((a) => `<li>${esc(a)}</li>`).join('');

  $('bt-warn').textContent = edge !== null && edge <= 1
    ? `Win rate is within ${Math.abs(edge).toFixed(2)} points of breakeven on ${bt.total_trades} trades — this is noise, not an edge.`
    : '';
}

/* ------------------------------------------------------------ render: logs */
async function pollLogs() {
  try {
    const data = await api(`/api/logs?since=${state.lastLogTs}`);
    const fresh = (data.logs || []);
    if (!fresh.length) return;
    for (const l of fresh) {
      state.logs.push(l);
      if (l.ts > state.lastLogTs) state.lastLogTs = l.ts;
    }
    if (state.logs.length > 400) state.logs = state.logs.slice(-400);
    renderLogs();
  } catch (_) { /* logs are non-critical */ }
}

function renderLogs() {
  const box = $('logbox');
  if (!state.logs.length) {
    box.innerHTML = '<div class="muted small">No log output yet.</div>';
    return;
  }
  const follow = $('log-follow').checked;
  box.innerHTML = state.logs.map((l) => `
    <div class="log-line lvl-${esc(l.level)}">
      <span class="log-ts">${fmtTime(l.ts)}</span>
      <span class="log-lvl">${esc(l.level)}</span>
      <span class="log-msg">${esc(l.message)}</span>
    </div>`).join('');
  if (follow) box.scrollTop = box.scrollHeight;
}

/* ------------------------------------------------------------- apply state */
function apply(snap) {
  state.snapshot = snap;
  renderStatus(snap);
  renderRisk(snap);
  renderScanner(snap);
  renderContract(snap);
  renderBacktest(snap);
  if (snap.trades && snap.trades.length) renderTrades(snap);
}

/* -------------------------------------------------------------- SSE / poll */
function startSSE() {
  if (state.sse) { try { state.sse.close(); } catch (_) {} }
  const es = new EventSource('/api/events');
  state.sse = es;

  es.addEventListener('state', (ev) => {
    state.sseFailures = 0;
    stopPolling();
    try { apply(JSON.parse(ev.data)); } catch (e) { console.error(e); }
  });

  es.onerror = () => {
    state.sseFailures += 1;
    if (state.sseFailures >= 2) {
      try { es.close(); } catch (_) {}
      state.sse = null;
      startPolling();
      setTimeout(() => { stopPolling(); startSSE(); }, 15000);
    }
  };
}

function startPolling() {
  if (state.pollTimer) return;
  const tick = async () => {
    try {
      const snap = await api('/api/status');
      snap.trades = (state.snapshot && state.snapshot.trades) || [];
      try {
        const t = await api('/api/trades');
        snap.trades = t.trades || [];
      } catch (_) {}
      apply(snap);
    } catch (e) { console.error(e); }
  };
  tick();
  state.pollTimer = setInterval(tick, 2000);
}
function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

/* ---------------------------------------------------------------- actions */
async function act(btnId, path, confirmMsg) {
  if (confirmMsg && !window.confirm(confirmMsg)) return;
  const btn = $(btnId);
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '…';
  try {
    await api(path, { method: 'POST' });
  } catch (e) {
    window.alert(e.message);
  } finally {
    btn.textContent = label;
    // The stream will refresh button states within ~0.5s.
    setTimeout(() => refreshOnce(), 400);
  }
}

async function refreshOnce() {
  try {
    const snap = await api('/api/status');
    let trades = [];
    try { trades = (await api('/api/trades')).trades || []; } catch (_) {}
    snap.trades = trades;
    apply(snap);
  } catch (e) { console.error(e); }
}

/* ------------------------------------------------------------------- boot */
document.addEventListener('DOMContentLoaded', () => {
  $('btn-save-trade-settings').addEventListener('click', saveTradeSettings);
  $('btn-start').addEventListener('click', () => act('btn-start', '/api/bot/start'));
  $('btn-stop').addEventListener('click', () => act('btn-stop', '/api/bot/stop',
    'Stop the bot? It will finish monitoring any open contract first.'));
  $('btn-pause').addEventListener('click', () => act('btn-pause', '/api/bot/pause'));
  $('btn-resume').addEventListener('click', () => act('btn-resume', '/api/bot/resume'));

  $('btn-reset').addEventListener('click', async () => {
    if (!window.confirm(
      'Reset the consecutive-loss pause?\n\nThis clears the loss streak and any pause window. '
      + 'It does NOT reset the daily P/L or the daily trade count.'
    )) return;
    try {
      const r = await api('/api/risk/reset', { method: 'POST' });
      window.alert(`Reset done. Consecutive losses: ${r.consecutive_losses_before} → ${r.consecutive_losses_after}.`);
      refreshOnce();
    } catch (e) { window.alert(e.message); }
  });

  $('btn-backtest').addEventListener('click', async () => {
    try {
      await api('/api/backtest/run', {
        method: 'POST',
        body: JSON.stringify({ count: 1000, payout: 1.95 }),
      });
      $('bt-running').hidden = false;
      $('btn-backtest').disabled = true;
    } catch (e) { window.alert(e.message); }
  });

  // Initial paint, then go live.
  loadTradeSettings();
  refreshOnce();
  startSSE();

  // Logs poll independently of state (they change more often).
  pollLogs();
  setInterval(pollLogs, 2000);

  // Countdown ticks locally every second.
  state.countdownTimer = setInterval(tickCountdown, 1000);

  // Refresh trades periodically as a safety net if SSE misses a beat.
  setInterval(() => {
    api('/api/trades').then((t) => {
      if (state.snapshot && t && t.trades) {
        state.snapshot.trades = t.trades;
        renderTrades(state.snapshot);
      }
    }).catch(() => {});
  }, 5000);
});
