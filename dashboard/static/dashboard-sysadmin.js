import { fmtBytes, fmtDate, parseFn, _modalOpen, _modalClose, escHtml, state } from './dashboard-common.js';
/*
 * dashboard-sysadmin.js
 * ─────────────────────
 * System Administration tab — system graphs (CPU/RAM/temp/disk), cron list, RMS Process Status table, storagewatch/janitor panel, services-restart helper, log viewer. Loaded by dashboard.html before dashboard-station.js.
 *
 * Depends on dashboard-common.js (escHtml, fmtBytes, fmtDate, parseFn, state.STATIONS_META,
 * state.VDB_CAMERAS, state.statusData, state.vitalsData, state.tlData, state.activeStation, state.activeTab, state.IS_ADMIN,
 * state.AUTH_USER, state.USER_ROLE, state.USER_STATIONS, _modalOpen, _modalClose) and lexical bindings
 * declared at the top level of dashboard-station.js (classic non-module scripts share
 * the global lexical environment, so top-level `let`/`const` are visible across bundles).
 */

/* ─────────────────────────────────────────
   System Admin — graph drawing
───────────────────────────────────────── */
function drawGraph(canvasId, history, field, maxVal, lineColor, unit) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth || canvas.width;
  const H = canvas.height;
  canvas.width = W;
  ctx.clearRect(0, 0, W, H);

  const WINDOW = 150; // fixed window size matches ring buffer max (5 min @ 2s)
  const raw  = (history || []).map(p => p[field]);
  // Pad left with nulls so data always occupies the right portion of the window
  const data = raw.length < WINDOW
    ? Array(WINDOW - raw.length).fill(null).concat(raw)
    : raw;

  // Background
  ctx.fillStyle = 'rgba(13,17,23,.5)';
  ctx.fillRect(0, 0, W, H);

  if (raw.filter(v => v !== null && v !== undefined).length < 2) {
    ctx.fillStyle = 'rgba(255,255,255,.2)';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Collecting data…', W / 2, H / 2 + 4);
    return;
  }

  // Grid lines at 25 / 50 / 75 %
  ctx.strokeStyle = 'rgba(255,255,255,.06)';
  ctx.lineWidth = 1;
  for (const f of [0.25, 0.5, 0.75]) {
    const y = H - f * H;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }

  const step = W / (WINDOW - 1);
  const yOf  = v => H - Math.max(0, Math.min(v, maxVal)) / maxVal * H;

  // Filled area + line — skip null (empty) slots on the left
  let firstX = null;
  ctx.beginPath();
  data.forEach((v, i) => {
    if (v === null || v === undefined) return;
    const x = i * step;
    if (firstX === null) { ctx.moveTo(x, yOf(v)); firstX = x; }
    else ctx.lineTo(x, yOf(v));
  });
  if (firstX !== null) {
    ctx.lineTo((WINDOW - 1) * step, H);
    ctx.lineTo(firstX, H);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0, lineColor + '44');
    grad.addColorStop(1, lineColor + '06');
    ctx.fillStyle = grad;
    ctx.fill();
  }

  ctx.beginPath();
  firstX = null;
  data.forEach((v, i) => {
    if (v === null || v === undefined) return;
    const x = i * step;
    firstX === null ? ctx.moveTo(x, yOf(v)) : ctx.lineTo(x, yOf(v));
    if (firstX === null) firstX = x;
  });
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // Latest value label (top-right)
  const last = raw.filter(v => v !== null && v !== undefined).at(-1);
  ctx.fillStyle = '#fff';
  ctx.font = 'bold 11px sans-serif';
  ctx.textAlign = 'right';
  ctx.fillText(last.toFixed(last < 10 ? 1 : 0) + unit, W - 4, 14);
}

function drawAllGraphs(host) {
  const h = window.vitalsHistory[host] || [];
  // Dynamic ceiling for disk write: at least 10 MB/s, or 1.5× the observed max
  const maxWrite = Math.max(10, ...h.map(p => p.disk_write_mbps ?? 0)) * 1.5;
  drawGraph(`graph-cpu-${host}`,  h, 'cpu_pct',         100,      '#58a6ff', '%');
  drawGraph(`graph-temp-${host}`, h, 'temp_c',            90,      '#e3b341', '°');
  drawGraph(`graph-ram-${host}`,  h, 'ram_pct',          100,      '#bc8cff', '%');
  drawGraph(`graph-disk-${host}`, h, 'disk_write_mbps', maxWrite,  '#3fb950', ' MB/s');
  renderCoreGraph(host);
  renderTopProcs(host);
}

function renderCoreGraph(host) {
  const el = document.getElementById(`graph-cores-${host}`);
  if (!el) return;
  const cores = window.coreData[host]?.cores;
  if (!cores?.length) {
    el.innerHTML = '<span style="color:var(--muted);font-size:11px">Waiting for data…</span>';
    return;
  }
  el.innerHTML = cores.map((c, i) => {
    const pct = c.load_pct ?? 0;
    const ghz = c.freq_ghz != null ? c.freq_ghz.toFixed(2) + ' GHz' : '';
    const col = pct > 80 ? '#f85149' : pct > 50 ? '#e3b341' : '#3fb950';
    return `<div class="core-row">
      <span class="core-label">C${i}</span>
      <div class="core-bar-track"><div class="core-bar-fill" style="width:${pct}%;background:${col}"></div></div>
      <span class="core-pct">${pct}%</span>
      <span class="core-ghz">${ghz}</span>
    </div>`;
  }).join('');
}

function renderTopProcs(host) {
  const el = document.getElementById(`graph-top-procs-${host}`);
  if (!el) return;
  const data = window.coreData[host];
  const procs = data?.top_procs;
  const total = data?.total_cores;
  if (!procs?.length) return;
  const totalStr = total != null ? total.toFixed(1) : '?';
  el.innerHTML = procs.map(p => {
    const pct = total ? p.cores / total : 0;
    const col = pct > 0.6 ? '#f85149' : pct > 0.3 ? '#e3b341' : '#58a6ff';
    return `<div class="top-proc-row">
      <span class="top-proc-name" title="${escHtml(p.name)}">${escHtml(p.name)}</span>
      <div class="core-bar-track"><div class="core-bar-fill" style="width:${Math.min(pct*100,100)}%;background:${col}"></div></div>
      <span class="top-proc-cores" style="color:${col}">${p.cores.toFixed(1)} / ${totalStr}</span>
    </div>`;
  }).join('');
}

/* ─────────────────────────────────────────
   System Admin — cron jobs
───────────────────────────────────────── */
async function fetchCrons(host) {
  try {
    const r = await window.fetchOnce(`tab:crons:${host}`, `/api/crons/${host}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    window.cronsData[host] = await r.json();
  } catch(e) {
    if (window._isAbort?.(e)) return;
    window.cronsData[host] = [];
  }
  renderCronsTable(host);
}

export function _parseCronSchedule(sched) {
  const s = sched.trim();
  if (s === '@reboot')              return { label: 'At reboot',    detail: '' };
  if (s === '@hourly')              return { label: 'Every hour',   detail: 'at :00' };
  if (s === '@daily' || s === '@midnight') return { label: 'Daily', detail: 'at midnight UTC' };
  if (s === '@weekly')              return { label: 'Weekly',       detail: 'Sunday midnight' };
  if (s === '@monthly')             return { label: 'Monthly',      detail: '1st at midnight' };

  const parts = s.split(/\s+/);
  if (parts.length !== 5) return { label: s, detail: '' };
  const [min, hour, dom, month, dow] = parts;

  const dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const isNum = v => /^\d+$/.test(v);

  // Every N minutes, optionally restricted to a hour range: */N <*|range> * * *
  if (min.startsWith('*/') && dom === '*' && month === '*' && dow === '*') {
    const n = min.slice(2);
    if (hour === '*') return { label: `Every ${n} min`, detail: '' };
    // Hour range like 4-10
    const hm = hour.match(/^(\d+)-(\d+)$/);
    if (hm) return { label: `Every ${n} min`, detail: `${hm[1].padStart(2,'0')}:00–${hm[2].padStart(2,'0')}:59 UTC` };
    return { label: `Every ${n} min`, detail: `hour ${hour} UTC` };
  }
  // Stepped minute range: M-N/S * * * *  (e.g. 5-55/10)
  if (/^\d+-\d+\/\d+$/.test(min) && hour === '*' && dom === '*' && month === '*' && dow === '*') {
    const step = min.split('/')[1];
    return { label: `Every ${step} min`, detail: '' };
  }
  // Every N hours: M */N * * *
  if (hour.startsWith('*/') && dom === '*' && month === '*' && dow === '*') {
    const n = hour.slice(2);
    return { label: `Every ${n}h`, detail: isNum(min) ? `at :${min.padStart(2,'0')}` : '' };
  }
  // Every minute: * * * * *
  if (min === '*' && hour === '*' && dom === '*' && month === '*' && dow === '*') {
    return { label: 'Every minute', detail: '' };
  }
  // Hourly at :MM —  M * * * *  (P1-44, very common idiom that used to fall
  // through to raw display).
  if (isNum(min) && hour === '*' && dom === '*' && month === '*' && dow === '*') {
    return { label: 'Hourly', detail: `at :${min.padStart(2,'0')}` };
  }
  // Weekly on a specific day: M H * * D
  if (isNum(min) && isNum(hour) && dom === '*' && month === '*' && isNum(dow)) {
    const h = hour.padStart(2,'0'), m = min.padStart(2,'0');
    const d = dayNames[parseInt(dow)] || dow;
    return { label: `Weekly (${d})`, detail: `at ${h}:${m} UTC` };
  }
  // Daily at fixed time: M H * * *
  if (isNum(min) && isNum(hour) && dom === '*' && month === '*' && dow === '*') {
    const h = hour.padStart(2,'0'), m = min.padStart(2,'0');
    return { label: 'Daily', detail: `at ${h}:${m} UTC` };
  }
  // Fallback: show raw schedule in a slightly abbreviated form
  return { label: s, detail: '' };
}

function renderCronsTable(host) {
  const el = document.getElementById(`crons-table-${host}`);
  if (!el) return;
  const crons = window.cronsData[host] || [];
  if (!crons.length) {
    el.innerHTML = '<span style="color:var(--muted);font-size:12px">No cron entries found</span>';
    return;
  }
  // Group by source
  const bySource = {};
  for (const c of crons) {
    (bySource[c.source] = bySource[c.source] || []).push(c);
  }
  const srcBadge = src => {
    const isUser = src === 'crontab';
    const label  = isUser ? 'user' : src.replace('/etc/cron.d/', '');
    return `<span class="cron-src-badge${isUser ? ' cron-src-user' : ''}">${escHtml(label)}</span>`;
  };
  const rows = crons.map(c => {
    const { label, detail } = _parseCronSchedule(c.schedule);
    const cmd = c.command.length > 90 ? c.command.slice(0, 87) + '…' : c.command;
    return `<div class="cron-row">
      <div class="cron-schedule-col">
        <span class="cron-schedule-badge">${escHtml(label)}</span>
        ${detail ? `<span class="cron-detail">${escHtml(detail)}</span>` : ''}
      </div>
      <div class="cron-cmd-col">
        <span class="cron-cmd-text" title="${escHtml(c.command)}">${escHtml(cmd)}</span>
        ${srcBadge(c.source)}
      </div>
    </div>`;
  }).join('');
  el.innerHTML = `<div class="cron-list">${rows}</div>`;
}

/* ─────────────────────────────────────────
   System Admin — RMS process status
───────────────────────────────────────── */
async function fetchRMSStatus(host) {
  if (!window.rmsStatusDate[host]) window.rmsStatusDate[host] = window._lastNight();
  const date = window.rmsStatusDate[host];
  const lbl = document.getElementById(`rms-status-date-lbl-${host}`);
  if (lbl) lbl.textContent = `${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6,8)}`;
  try {
    const r = await fetch(`/api/rms/status/${host}?date=${date}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    window.rmsStatusData[host] = await r.json();
  } catch(e) {
    window.rmsStatusData[host] = {date, cameras: {}};
  }
  renderRMSStatusTable(host);
}

function rmsStatusNavDate(host, delta) {
  if (!window.rmsStatusDate[host]) window.rmsStatusDate[host] = window._lastNight();
  const d  = window.rmsStatusDate[host];
  const dt = new Date(`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}T12:00:00Z`);
  dt.setUTCDate(dt.getUTCDate() + delta);
  const next = dt.toISOString().slice(0, 10).replace(/-/g, '');
  // Never allow navigating to a future date — captured nights can't exist tomorrow.
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  if (next > today) return;
  window.rmsStatusDate[host] = next;
  fetchRMSStatus(host);
}

function renderRMSStatusTable(host) {
  const el = document.getElementById(`rms-status-table-${host}`);
  if (!el) return;
  const d = window.rmsStatusData[host];
  if (!d || !Object.keys(d.cameras || {}).length) {
    el.innerHTML = '<span style="color:var(--muted);font-size:12px">No RMS camera data</span>';
    return;
  }
  const ck  = v => v
    ? '<span style="color:var(--green)">✓</span>'
    : '<span style="color:var(--muted)">—</span>';
  const liveBadge = s => {
    if (s.capturing)  return `<span style="color:var(--green);font-size:10px;font-weight:600;white-space:nowrap">&#9679; RMS Capturing…</span>`;
    if (s.processing) return `<span style="color:var(--yellow);font-size:10px;font-weight:600;white-space:nowrap">&#9679; RMS Processing…</span>`;
    return '';
  };
  const rows = Object.entries(d.cameras).map(([cam, s]) => `
    <tr>
      <td style="font-family:monospace;padding:4px 10px 4px 0">
        <div>${cam}</div>
        ${liveBadge(s) ? `<div style="margin-top:2px">${liveBadge(s)}</div>` : ''}
      </td>
      <td style="padding:4px 8px;text-align:center">${ck(s.processed)}</td>
      <td style="padding:4px 8px;text-align:center">${ck(s.uploaded)}</td>
      <td style="padding:4px 8px">
        ${state.IS_ADMIN ? `<button class="settings-save-btn secondary" style="padding:2px 10px;font-size:11px"
                onclick="reprocessNight('${host}','${cam}','${d.date}',this)">Reprocess</button>` : ''}
      </td>
    </tr>`).join('');
  el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead><tr style="color:var(--muted);border-bottom:1px solid var(--border);text-align:left">
      <th style="padding:4px 10px 4px 0">Camera</th>
      <th style="padding:4px 8px;text-align:center">Processed</th>
      <th style="padding:4px 8px;text-align:center">Uploaded</th>
      <th style="padding:4px 8px"></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function reprocessNight(host, cam, date, btn) {
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = 'Starting…';
  try {
    const r = await fetch(`/api/dawn/run/${host}`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({date}),
    });
    const data = await r.json();
    btn.textContent = data.ok ? '✓ Started' : ('✗ ' + (data.error || r.status));
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 4000);
  } catch(e) {
    btn.textContent = '✗ ' + e.message;
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 4000);
  }
}

/* ─────────────────────────────────────────
   System Admin — janitor / storagewatch
───────────────────────────────────────── */
async function fetchStoragewatchStatus(host) {
  try {
    const r = await window.fetchOnce(`tab:storagewatch:${host}`, `/api/storagewatch/${host}`);
    if (!r.ok) { window.storagewatchData[host] = null; renderJanitorSection(host); return; }
    window.storagewatchData[host] = await r.json();
  } catch(e) {
    if (window._isAbort?.(e)) return;
    window.storagewatchData[host] = null;
  }
  renderJanitorSection(host);
}

function renderJanitorSection(host) {
  const el = document.getElementById(`janitor-section-${host}`);
  if (!el) return;
  const d = window.storagewatchData[host];
  if (!d || d.error) {
    el.innerHTML = `<div style="font-size:11px;color:var(--muted)">Janitor: ${d?.error === 'no_state' ? 'no data yet (runs every 10 min)' : 'unavailable'}</div>`;
    return;
  }
  const relTime = t => {
    const s = Math.floor((Date.now() - new Date(t)) / 1000);
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s/60)}m ago`;
    return `${Math.floor(s/3600)}h ago`;
  };
  const fsRows = Object.entries(d.filesystems || {}).map(([path, fs]) => {
    const barW = fs.pct;
    const barC = fs.pct > (fs.nuclear_pct || 90) ? 'var(--red)'
               : fs.pct > (fs.warn_pct    || 85) ? 'var(--yellow)'
               : 'var(--green)';
    const untilStr = fs.gb_until_warn > 0 ? `${fs.gb_until_warn} GB free before warn` : 'above warn threshold';
    return `<div style="margin-bottom:8px">
      <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:3px">
        <span style="color:var(--muted);font-family:monospace">${escHtml(path)}</span>
        <span>${fs.pct}% · ${fs.free_gb} GB free · <span style="color:var(--muted)">${untilStr}</span></span>
      </div>
      <div class="vitals-bar-track"><div class="vitals-bar-fill" style="width:${barW}%;background:${barC}"></div></div>
    </div>`;
  }).join('');
  el.innerHTML = `<div style="border-top:1px solid var(--border);padding-top:10px;margin-top:4px">
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:8px">Janitor</div>
    <div style="display:flex;gap:20px;font-size:12px;margin-bottom:10px">
      <span><span style="color:var(--muted)">Last run</span> ${relTime(d.last_run)}</span>
      <span><span style="color:var(--muted)">Freed this run</span> ${d.deleted_gb > 0 ? d.deleted_gb + ' GB' : '—'}</span>
    </div>
    ${fsRows}
  </div>`;
}
/* ─────────────────────────────────────────
   Service restart (admin only)
───────────────────────────────────────── */
function restartService(host, service, btn) {
  window.askConfirm(
    `Restart ${service} on ${host}?`,
    async () => {
      const prev = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '&#8987;';
      try {
        const resp = await fetch(`/api/restart/${host}/${service}`, {method: 'POST'});
        const data = await resp.json();
        if (!resp.ok || data.error) throw new Error(data.error || resp.statusText);
        btn.innerHTML = '&#10003;';
        btn.style.color = 'var(--green)';
        setTimeout(() => { btn.innerHTML = prev; btn.style.color = ''; btn.disabled = false; }, 3000);
        // Refresh status after restart
        setTimeout(() => window.fetchStatus(host), 5000);
      } catch(e) {
        btn.innerHTML = '&#10007;';
        btn.style.color = 'var(--red)';
        alert('Restart failed: ' + e.message);
        setTimeout(() => { btn.innerHTML = prev; btn.style.color = ''; btn.disabled = false; }, 3000);
      }
    },
    { confirmLabel: `Restart ${service}`, title: 'Restart service' }
  );
}
/* ─────────────────────────────────────────
   Log Viewer
───────────────────────────────────────── */
const LOGS_SERVICES = [
  'color-capture',
  'rovimen-station-api',
  'dawn',
  'stacker',
  'encoder',
  'timelapse',
  'janitor',
];

const _LOGS_FILE_SERVICES = new Set();  // no date-picker sources currently
window.logsState = { service: LOGS_SERVICES[0], lines: 200, minLevel: 'info', filter: '', date: '', live: false, timer: null };

async function logsInit() {
  // Ensure settings (config) are loaded so RMS camera buttons can be built
  if (!window.settingsData[state.activeStation]) {
    try {
      const r = await fetch(`/api/settings/${state.activeStation}`);
      if (r.ok) window.settingsData[state.activeStation] = await r.json();
    } catch(e) {}
  }
  renderLogsShell();
  fetchLogs();
}

function renderLogsShell() {
  const el = document.getElementById(`logs-section-${state.activeStation}`);
  if (!el) return;
  // Build RMS buttons from station IDs in the config (window.settingsData has the full config)
  const cams = Object.keys((window.settingsData[state.activeStation] || {}).stations || {});
  const rmsSources = cams.map(c => 'rms-' + c);
  const allSources = [...LOGS_SERVICES, ...rmsSources];
  // Ensure selected service is still valid after station switch
  if (!allSources.includes(window.logsState.service)) window.logsState.service = allSources[0];
  const svcOptions = allSources.map(s => {
    const label = s.startsWith('rms-') ? 'RMS ' + s.slice(4) : s;
    return `<option value="${escHtml(s)}"${s === window.logsState.service ? ' selected' : ''}>${escHtml(label)}</option>`;
  }).join('');
  el.innerHTML = `
      <div class="logs-toolbar">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <select id="logs-svc-select" class="log-select" onchange="logsSelectService(this.value)">
            ${svcOptions}
          </select>
          <select id="logs-level" class="log-select" onchange="logsOnLevelChange(this.value)">
            <option value="debug">All levels</option>
            <option value="info"${window.logsState.minLevel === 'info' ? ' selected' : ''}>Info+</option>
            <option value="warning"${window.logsState.minLevel === 'warning' ? ' selected' : ''}>Warning+</option>
            <option value="error"${window.logsState.minLevel === 'error' ? ' selected' : ''}>Error only</option>
          </select>
          <select id="logs-lines" class="log-select" onchange="logsOnLinesChange(this.value)">
            <option value="100"${window.logsState.lines === 100 ? ' selected' : ''}>100 lines</option>
            <option value="200"${window.logsState.lines === 200 ? ' selected' : ''}>200 lines</option>
            <option value="500"${window.logsState.lines === 500 ? ' selected' : ''}>500 lines</option>
            <option value="0"${window.logsState.lines === 0 ? ' selected' : ''}>Full log</option>
          </select>
          <input id="logs-date" type="date" class="log-select" oninput="logsOnDateChange(this.value)"
                 style="display:${_LOGS_FILE_SERVICES.has(window.logsState.service) ? '' : 'none'}">
          <input id="logs-filter" type="search" placeholder="Filter…" class="log-select" style="width:140px"
                 oninput="logsOnFilterChange(this.value)">
          <label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">
            <input type="checkbox" id="logs-live-toggle" onchange="logsToggleLive(this.checked)">
            <span>Live</span>
          </label>
          <span style="font-size:11px;color:var(--muted)" id="logs-ts"></span>
          <button class="hdr-btn" onclick="fetchLogs()" style="font-size:11px">Refresh</button>
        </div>
      </div>
      <pre id="logs-output" class="logs-output">Loading…</pre>`;
}

function logsSelectService(svc) {
  window.logsState.service = svc;
  window.logsState.date = '';
  const datePicker = document.getElementById('logs-date');
  if (datePicker) {
    datePicker.style.display = _LOGS_FILE_SERVICES.has(svc) ? '' : 'none';
    datePicker.value = '';
  }
  fetchLogs();
}

function logsOnDateChange(val) {
  // val is YYYY-MM-DD, API expects YYYYMMDD
  window.logsState.date = val ? val.replace(/-/g, '') : '';
  fetchLogs();
}

function logsOnLinesChange(val) {
  window.logsState.lines = parseInt(val, 10);
  fetchLogs();
}

function logsOnLevelChange(val) {
  window.logsState.minLevel = val;
  logsApplyFilter();
}

function logsOnFilterChange(val) {
  window.logsState.filter = val.toLowerCase();
  logsApplyFilter();
}

function logsToggleLive(on) {
  window.logsState.live = on;
  if (on) {
    fetchLogs();
    window.logsState.timer = setInterval(fetchLogs, 5000);
  } else {
    clearInterval(window.logsState.timer);
    window.logsState.timer = null;
  }
}

let _logsRawLines = [];

async function fetchLogs() {
  if (state.activeTab !== 'settings') return;
  const host = state.activeStation;
  const { service, lines, date } = window.logsState;
  const qs = `lines=${lines}` + (date ? `&date=${date}` : '');
  try {
    const resp = await window.fetchOnce(`tab:logs:${host}:${service}`, `/api/logs/${host}/${service}?${qs}`);
    const data = await resp.json();
    const out = document.getElementById('logs-output');
    const ts  = document.getElementById('logs-ts');
    if (!out) return;
    if (data.error) { out.textContent = 'Error: ' + data.error; return; }
    _logsRawLines = data.lines;
    logsApplyFilter();
    if (ts) ts.textContent = 'Updated ' + new Date().toLocaleTimeString('en-GB', {timeZone: 'UTC'}) + ' UTC';
  } catch(e) {
    if (window._isAbort?.(e)) return;
    const out = document.getElementById('logs-output');
    if (out) out.textContent = 'Failed to load logs: ' + e.message;
  }
}

const _LOG_LEVELS = { debug: 0, info: 1, warning: 2, error: 3 };

export function _logLineLevel(line) {
  if (/\bERROR\b|\bCRITICAL\b/i.test(line)) return 'error';
  if (/\bWARNING\b|\bWARN\b/i.test(line))   return 'warning';
  if (/\bDEBUG\b/i.test(line))               return 'debug';
  return 'info';
}

function logsApplyFilter() {
  const out = document.getElementById('logs-output');
  if (!out) return;
  const needle   = window.logsState.filter;
  const minRank  = _LOG_LEVELS[window.logsState.minLevel] ?? 0;
  let lines = _logsRawLines;
  if (minRank > 0) lines = lines.filter(l => (_LOG_LEVELS[_logLineLevel(l)] ?? 1) >= minRank);
  if (needle)      lines = lines.filter(l => l.toLowerCase().includes(needle));
  const html = lines.map(line => {
    const lvl = _logLineLevel(line);
    const cls = lvl === 'error' ? 'log-error' : lvl === 'warning' ? 'log-warn' : lvl === 'debug' ? 'log-debug' : '';
    return `<span class="${cls}">${escHtml(line)}</span>`;
  }).join('\n');
  out.innerHTML = html || '<span style="color:var(--muted)">No log entries.</span>';
  out.scrollTop = out.scrollHeight;
}

// Expose to global scope for inline onclick handlers
window.fetchLogs = fetchLogs;
window.logsOnDateChange = logsOnDateChange;
window.logsOnFilterChange = logsOnFilterChange;
window.logsToggleLive = logsToggleLive;
window.reprocessNight = reprocessNight;
window.rmsStatusNavDate = rmsStatusNavDate;
window.drawAllGraphs = drawAllGraphs;
window.fetchCrons = fetchCrons;
window.fetchRMSStatus = fetchRMSStatus;
window.fetchStoragewatchStatus = fetchStoragewatchStatus;
window.renderLogsShell = renderLogsShell;
window.restartService = restartService;
window.logsSelectService = logsSelectService;
window.logsOnLevelChange = logsOnLevelChange;
window.logsOnLinesChange = logsOnLinesChange;
