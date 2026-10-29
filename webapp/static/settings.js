/* ==========================================================================
   settings.js — credentials form for the Deriv Volatility Bot panel.
   No framework, no build step. The token is only ever held in the input
   element; it is never written to localStorage, a URL, or the console.
   ========================================================================== */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  /* ---------------------------------------------------------------- CSRF */
  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)dv_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  /* ------------------------------------------------------------- helpers */
  function setText(id, text) {
    var el = $(id);
    if (el) { el.textContent = text; }
  }

  function showResult(kind, title, lines) {
    var box = $('result');
    box.hidden = false;
    box.className = 'result result-' + kind;
    var html = '<div class="result-title">' + escapeHtml(title) + '</div>';
    if (lines && lines.length) {
      html += '<ul class="bullets">';
      for (var i = 0; i < lines.length; i++) {
        html += '<li>' + escapeHtml(lines[i]) + '</li>';
      }
      html += '</ul>';
    }
    box.innerHTML = html;
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* --------------------------------------------------------- load state */
  function loadState() {
    fetch('/api/settings', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        if (!body.ok) { throw new Error(body.error || 'Could not load settings'); }
        var d = body.data;

        var badge = $('cfg-badge');
        badge.textContent = d.configured ? 'CONFIGURED' : 'NOT CONFIGURED';
        badge.className = 'badge ' + (d.configured ? 'badge-live' : 'badge-warn');

        setText('st-appid', d.app_id_present ? d.app_id : 'not set');
        setText('st-token', d.token_present ? '•••••••• (stored)' : 'not set');
        setText('st-env', d.env_exists ? d.env_path : 'no .env file yet');
        setText('st-bot', d.configured ? 'ready to start' : 'needs credentials');

        // Prefill only the non-secret fields.
        if (d.app_id) { $('f-appid').value = d.app_id; }
        $('f-stake').value = d.stake || '';
        $('f-max-open').value = d.max_open_contracts || '3';
        $('f-take-profit').value = d.daily_profit_target || '0';
        $('f-loss').value = d.daily_loss_limit || '';
        $('f-trades').value = d.max_trades_per_day || '';
        $('f-streak').value = d.max_consecutive_losses || '';

        var conn = $('conn-label');
        conn.textContent = d.configured ? 'Configured' : 'Not configured';
        $('conn-dot').className = 'conn-dot ' + (d.configured ? 'ok' : 'warn');
      })
      .catch(function (err) {
        setText('conn-label', 'Server unreachable');
        showResult('error', 'Could not load settings', [err.message]);
      });
  }

  /* -------------------------------------------------------------- submit */
  function submit(ev) {
    ev.preventDefault();

    var token = $('f-token').value.trim();
    var appId = $('f-appid').value.trim();

    if (!appId) {
      showResult('error', 'App ID required',
        ['Enter the numeric App ID from developers.deriv.com.']);
      $('f-appid').focus();
      return;
    }
    if (!token) {
      showResult('error', 'Token required',
        ['Paste the token created from your DEMO account.']);
      $('f-token').focus();
      return;
    }

    var payload = {
      app_id: appId,
      token: token,
      verify_live: $('f-verify').checked
    };
    var optional = {
      stake: $('f-stake').value,
      max_open_contracts: $('f-max-open').value,
      daily_profit_target: $('f-take-profit').value,
      daily_loss_limit: $('f-loss').value,
      max_trades_per_day: $('f-trades').value,
      max_consecutive_losses: $('f-streak').value
    };
    for (var k in optional) {
      if (Object.prototype.hasOwnProperty.call(optional, k) && optional[k] !== '') {
        payload[k] = optional[k];
      }
    }

    var btn = $('btn-save');
    btn.disabled = true;
    btn.textContent = $('f-verify').checked ? 'Verifying with Deriv…' : 'Saving…';
    setText('save-status', '');
    $('result').hidden = true;

    fetch('/api/settings', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-CSRF-Token': csrfToken()
      },
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json().then(function (body) { return { status: r.status, body: body }; });
      })
      .then(function (res) {
        var body = res.body;
        if (!body.ok) {
          var lines = [body.error || 'Save failed.'];
          if (body.code === 'csrf_failed') {
            lines.push('Reload this page and try again.');
          }
          if (body.code === 'real_account_rejected') {
            lines.push('The bot is demo-only by design — this cannot be overridden.');
          }
          showResult('error', 'Not saved (HTTP ' + res.status + ')', lines);
          return;
        }

        var d = body.data;
        var lines = [
          'App ID: ' + d.app_id,
          'Token: stored (never displayed)',
          'File mode: ' + d.file_mode,
          'Keys written: ' + d.keys_written.join(', ')
        ];
        if (d.preserved_keys && d.preserved_keys.length) {
          lines.push('Preserved: ' + d.preserved_keys.join(', '));
        }
        if (d.live_check && d.live_check.checked) {
          lines.push('Deriv check: token accepted, demo account found');
        }
        lines.push(d.message);

        showResult('ok', 'Credentials saved', lines);
        setText('save-status', 'Saved');
        $('f-token').value = '';   // never keep the secret in the DOM
        loadState();
      })
      .catch(function (err) {
        showResult('error', 'Request failed', [err.message]);
      })
      .finally(function () {
        btn.disabled = false;
        btn.textContent = 'Save credentials';
      });
  }

  function saveTradingSettings() {
    var btn = $('btn-save-trading');
    var payload = {
      stake: $('f-stake').value,
      max_open_contracts: $('f-max-open').value,
      daily_profit_target: $('f-take-profit').value,
      daily_loss_limit: $('f-loss').value,
      max_trades_per_day: $('f-trades').value,
      max_consecutive_losses: $('f-streak').value
    };
    btn.disabled = true;
    btn.textContent = 'Saving…';
    setText('save-status', '');
    $('result').hidden = true;

    fetch('/api/settings/trading', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-CSRF-Token': csrfToken()
      },
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json().then(function (body) { return { status: r.status, body: body }; });
      })
      .then(function (res) {
        if (!res.body.ok) {
          showResult('error', 'Settings not saved (HTTP ' + res.status + ')',
            [res.body.error || 'Save failed.']);
          return;
        }
        showResult('ok', 'Trading settings saved', [res.body.data.message]);
        setText('save-status', 'Saved');
        loadState();
      })
      .catch(function (err) {
        showResult('error', 'Request failed', [err.message]);
      })
      .finally(function () {
        btn.disabled = false;
        btn.textContent = 'Save trading settings';
      });
  }

  /* --------------------------------------------------------------- init */
  document.addEventListener('DOMContentLoaded', function () {
    loadState();

    $('settings-form').addEventListener('submit', submit);
    $('btn-save-trading').addEventListener('click', saveTradingSettings);

    $('btn-reveal').addEventListener('click', function () {
      var f = $('f-token');
      var showing = f.type === 'text';
      f.type = showing ? 'password' : 'text';
      this.textContent = showing ? 'Show' : 'Hide';
    });

    $('btn-clear').addEventListener('click', function () {
      $('f-token').value = '';
      $('result').hidden = true;
      setText('save-status', '');
      $('f-token').focus();
    });
  });
})();
