import { state, fmtBytes, openAppPanel, appPanelClose, escHtml } from './dashboard-common.js';

// ── Auth / admin link ─────────────────────────────────────────────────────────
(async function() {
  try {
    const auth = await (await fetch('/api/auth/status')).json();
    const el = document.getElementById('nw-auth');
    if (auth.admin) {
      document.getElementById('logo-dd-admin').style.display = '';
      const socialEl = document.getElementById('logo-dd-social');
      if (socialEl) socialEl.style.display = '';
      el.innerHTML = `<span style="color:var(--green)">${escHtml(auth.user)}</span>
        <a href="/logout" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Logout</a>`;
    } else {
      el.innerHTML = `<a href="/login" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Login</a>`;
    }
  } catch(e) {}
})();

// ── Escape key: close app panel ────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const panel = document.getElementById('app-panel');
    if (panel && panel.classList.contains('open')) {
      appPanelClose();
    }
  }
});

// ── State ─────────────────────────────────────────────────────────────────────
let _nwRows = [];           // array of row data objects
let _nwSortCol = 'online';  // default sort: online stations first
let _nwSortDir = -1;        // -1 = desc (online first)

// ── Helpers ───────────────────────────────────────────────────────────────────

function _fmtAge(isoStr) {
  if (!isoStr) return { text: '—', cls: 'na' };
  const dt = new Date(isoStr);
  if (isNaN(dt)) return { text: '—', cls: 'na' };
  const secs = Math.floor((Date.now() - dt) / 1000);
  if (secs < 0) return { text: 'just now', cls: 'fresh' };
  if (secs < 90)  return { text: secs + 's ago',                    cls: 'fresh' };
  if (secs < 3600) return { text: Math.floor(secs / 60) + 'm ago',  cls: 'recent' };
  if (secs < 86400) return { text: Math.floor(secs / 3600) + 'h ago', cls: 'stale' };
  return { text: Math.floor(secs / 86400) + 'd ago',               cls: 'old' };
}

function _metricClass(val, warnThreshold, critThreshold) {
  if (val == null) return 'na';
  if (val >= critThreshold) return 'crit';
  if (val >= warnThreshold) return 'warn';
  return 'ok';
}

function _fmtPct(val) {
  if (val == null) return { text: '—', cls: 'na' };
  const n = Math.round(val);
  return { text: n + '%', cls: _metricClass(n, 75, 90) };
}

function _fmtTemp(val) {
  if (val == null) return { text: '—', cls: 'na' };
  const n = Math.round(val);
  return { text: n + '°', cls: _metricClass(n, 65, 80) };
}

function _fmtDisk(pct) {
  // Caller passes the already-extracted percentage (see _buildRows: diskPct),
  // which handles {pct, total_mb/used_mb, total_gb/used_gb, bare number} shapes.
  // The previous implementation only knew used_gb/total_gb and produced "NaN%"
  // on every station that reports MB-keyed disk objects (gmn0007, etc).
  if (pct == null || Number.isNaN(pct)) return { text: '—', cls: 'na' };
  const n = Math.round(pct);
  return { text: n + '%', cls: _metricClass(n, 80, 93) };
}

// Derive camera service status from the station's services map.
// Services are keyed like "rms-cam1", "gmn-capture-RO000A", etc.
// We match each camera code against any service key that contains it,
// and fall back to "unknown" if nothing matches.
function _camServiceStatus(camCode, services) {
  if (!services || !camCode) return 'svc-unknown';
  // Look for a service key that contains the camera code OR "cam" (generic).
  // Try exact code match first, then any key containing the cam code.
  for (const [svcKey, svcVal] of Object.entries(services)) {
    if (svcKey.includes(camCode)) {
      const active = (svcVal.active === true || svcVal.state === 'active' ||
                      svcVal.running === true);
      return active ? 'svc-ok' : 'svc-dead';
    }
  }
  // No explicit match — unknown
  return 'svc-unknown';
}

// ── Build row data from /api/overview + /api/status/all ───────────────────────

function _buildRows(overviewData, statusAllData) {
  const rows = [];
  for (const [key, info] of Object.entries(overviewData)) {
    const status = statusAllData[key] || {};
    const services = status.services || {};
    const cameras = (info.cameras || []).map(c => ({
      code: c.code,
      svcCls: _camServiceStatus(c.code, services),
    }));
    // Numeric sort values (null sorts last)
    const cpu  = info.cpu_pct  != null ? info.cpu_pct  : null;
    const ram  = info.ram_pct  != null ? info.ram_pct  : null;
    const temp = info.temp_c   != null ? info.temp_c   : null;
    // /api/overview returns disk as {device, used_mb, total_mb, pct, other_mb}
    // — pct is authoritative; fall back to used/total if missing; tolerate
    // a bare scalar in case the shape ever drifts.
    let diskPct = null;
    if (info.disk != null) {
      if (typeof info.disk === 'object') {
        if (info.disk.pct != null) {
          diskPct = info.disk.pct;
        } else if (info.disk.total_mb) {
          diskPct = (info.disk.used_mb / info.disk.total_mb) * 100;
        } else if (info.disk.total_gb) {
          diskPct = (info.disk.used_gb / info.disk.total_gb) * 100;
        }
      } else if (typeof info.disk === 'number') {
        diskPct = info.disk;
      }
    }
    rows.push({
      key,
      label:    info.label || '',
      online:   info.online ? 1 : 0,
      last_seen: info.last_updated || null,
      cpu, ram, temp,
      diskPct,
      diskRaw:  info.disk,
      cameras,
    });
  }
  return rows;
}

// ── Sort ──────────────────────────────────────────────────────────────────────

function nwSort(col) {
  if (_nwSortCol === col) {
    _nwSortDir *= -1;
  } else {
    _nwSortCol = col;
    _nwSortDir = col === 'key' ? 1 : -1;
  }
  // Update header arrows
  document.querySelectorAll('.nw-table th').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.col === col) {
      th.classList.add(_nwSortDir === 1 ? 'sort-asc' : 'sort-desc');
    }
  });
  _renderTable();
}

function _sortRows(rows) {
  const col = _nwSortCol;
  const dir = _nwSortDir;
  return [...rows].sort((a, b) => {
    let va, vb;
    switch (col) {
      case 'key':      va = a.key;       vb = b.key;       break;
      case 'online':   va = a.online;    vb = b.online;    break;
      case 'last_seen':
        va = a.last_seen ? new Date(a.last_seen).getTime() : 0;
        vb = b.last_seen ? new Date(b.last_seen).getTime() : 0;
        break;
      case 'cpu':   va = a.cpu  ?? -1;  vb = b.cpu  ?? -1; break;
      case 'ram':   va = a.ram  ?? -1;  vb = b.ram  ?? -1; break;
      case 'temp':  va = a.temp ?? -1;  vb = b.temp ?? -1; break;
      case 'disk':  va = a.diskPct ?? -1; vb = b.diskPct ?? -1; break;
      default:      return 0;
    }
    if (va < vb) return -dir;
    if (va > vb) return  dir;
    return a.key.localeCompare(b.key);
  });
}

// ── Render table ─────────────────────────────────────────────────────────────

function _renderTable() {
  const tbody = document.getElementById('nw-tbody');
  const cardsEl = document.getElementById('nw-cards');
  if (!tbody || !_nwRows.length) return;

  const sorted = _sortRows(_nwRows);
  let html = '';
  let cardHtml = '';

  for (const r of sorted) {
    const age   = _fmtAge(r.last_seen);
    const cpu   = _fmtPct(r.cpu);
    const ram   = _fmtPct(r.ram);
    const temp  = _fmtTemp(r.temp);
    const disk  = _fmtDisk(r.diskPct);
    const onlineCls  = r.online ? 'up' : 'down';
    const onlineText = r.online ? 'Online' : 'Offline';

    // Camera dots — c.code is API-supplied; svcCls is a fixed class slug.
    const camDots = r.cameras.map(c =>
      `<div class="nw-cam-dot" title="${escHtml(c.code)}">
         <div class="nw-cam-dot-circle ${c.svcCls}"></div>
         <span class="nw-cam-dot-label">${escHtml(c.code)}</span>
       </div>`
    ).join('');

    // Table row — r.key/r.label come from dashboard_config.yaml (admin-managed)
    // and the upstream /api/overview payload, so escape them.
    html += `<tr class="${r.online ? '' : 'offline'}">
      <td>
        <a class="nw-station-key" href="/station/${encodeURIComponent(r.key)}">${escHtml(r.key)}</a>
        <div class="nw-station-label">${escHtml(r.label)}</div>
      </td>
      <td>
        <div class="nw-online-cell">
          <span class="nw-dot ${onlineCls}"></span>
          <span class="nw-online-text ${onlineCls}">${onlineText}</span>
        </div>
      </td>
      <td><span class="nw-age ${age.cls}">${age.text}</span></td>
      <td><span class="nw-metric ${cpu.cls}">${cpu.text}</span></td>
      <td><span class="nw-metric ${ram.cls}">${ram.text}</span></td>
      <td><span class="nw-metric ${temp.cls}">${temp.text}</span></td>
      <td><span class="nw-metric ${disk.cls}">${disk.text}</span></td>
      <td><div class="nw-cam-dots">${camDots || '<span style="color:var(--muted)">—</span>'}</div></td>
    </tr>`;

    // Mobile card
    cardHtml += `<div class="nw-card ${r.online ? '' : 'offline'}">
      <div class="nw-card-top">
        <span class="nw-dot ${onlineCls}"></span>
        <a class="nw-card-key" href="/station/${encodeURIComponent(r.key)}">${escHtml(r.key)}</a>
        <span class="nw-card-label">${escHtml(r.label)}</span>
      </div>
      <div class="nw-card-row">
        <span class="nw-card-field">Status: <b class="${onlineCls === 'up' ? '' : ''}" style="color:${r.online ? 'var(--green)' : 'var(--red)'}">${onlineText}</b></span>
        <span class="nw-card-field" title="Time since the dashboard's last successful poll, not a station heartbeat — polls run on a 30–60 s parallel schedule.">Last polled: <b class="nw-age ${age.cls}">${age.text}</b></span>
      </div>
      <div class="nw-card-row">
        <span class="nw-card-field">CPU: <b class="nw-metric ${cpu.cls}">${cpu.text}</b></span>
        <span class="nw-card-field">RAM: <b class="nw-metric ${ram.cls}">${ram.text}</b></span>
        <span class="nw-card-field">Temp: <b class="nw-metric ${temp.cls}">${temp.text}</b></span>
        <span class="nw-card-field">Disk: <b class="nw-metric ${disk.cls}">${disk.text}</b></span>
      </div>
      <div class="nw-cam-dots" style="margin-top:4px">${camDots || '<span style="color:var(--muted)">No cameras</span>'}</div>
    </div>`;
  }

  tbody.innerHTML = html;
  if (cardsEl) cardsEl.innerHTML = cardHtml;
}

// ── Summary bar ───────────────────────────────────────────────────────────────

function _renderSummary(rows) {
  const total   = rows.length;
  const online  = rows.filter(r => r.online).length;
  const offline = total - online;
  const totalCams = rows.reduce((s, r) => s + r.cameras.length, 0);
  const el = document.getElementById('nw-summary');
  if (!el) return;
  el.innerHTML = `
    <div class="nw-summary-item green"><b>${online}</b>Online</div>
    <div class="nw-summary-item red"><b>${offline}</b>Offline</div>
    <div class="nw-summary-item blue"><b>${totalCams}</b>Cameras</div>`;
}

// ── Data fetch ────────────────────────────────────────────────────────────────

async function nwFetch() {
  try {
    const [overviewResp, statusResp] = await Promise.all([
      fetch('/api/overview'),
      fetch('/api/status/all'),
    ]);
    const [overview, statusAll] = await Promise.all([
      overviewResp.json(),
      statusResp.json(),
    ]);

    _nwRows = _buildRows(overview, statusAll);
    _renderSummary(_nwRows);
    _renderTable();

    // Update default sort header indicator on first load
    document.querySelectorAll('.nw-table th').forEach(th => {
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.dataset.col === _nwSortCol) {
        th.classList.add(_nwSortDir === 1 ? 'sort-asc' : 'sort-desc');
      }
    });

    const el = document.getElementById('nw-last-fetch');
    if (el) {
      const now = new Date();
      el.textContent =
        String(now.getUTCHours()).padStart(2, '0') + ':' +
        String(now.getUTCMinutes()).padStart(2, '0') + ':' +
        String(now.getUTCSeconds()).padStart(2, '0') + ' UTC';
    }
  } catch(e) {
    const tbody = document.getElementById('nw-tbody');
    if (tbody && !_nwRows.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="nw-error">Failed to load station data</td></tr>`;
    }
  }
}

// Initial load + 30 s auto-refresh (paused when tab hidden)
nwFetch();
setInterval(() => { if (!document.hidden) nwFetch(); }, 30000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) nwFetch(); });

// ── Window exposure for onclick handlers ──────────────────────────────────────
window.nwSort = nwSort;
window.openAppPanel = openAppPanel;
window.appPanelClose = appPanelClose;
