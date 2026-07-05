// TODO (#346): Replace all window.confirm() calls in this file with the
// non-blocking askConfirm() dialog from dashboard-station.js. askConfirm is
// already exposed as window.askConfirm and styled in dashboard.css, so the
// migration is straightforward but each call site needs to be converted from
// synchronous if(!confirm(...)) return to callback/async style.
import { escHtml, openAppPanel, appPanelClose } from './dashboard-common.js';

// toggleLogoMenu / openAppPanel / appPanelClose / _APP_PANELS now live in
// dashboard-common.js. Inline copies removed to avoid "Identifier
// '_APP_PANELS' has already been declared" SyntaxError.

let _cfg = null;

// ── Tab switching ─────────────────────────────────────────────────────────────

let _configIframeLoaded = false;

function switchAdmTab(name) {
  document.querySelectorAll('.adm-tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.adm-pane').forEach(p => p.classList.remove('active'));
  document.getElementById(`tab-btn-${name}`).classList.add('active');
  document.getElementById(`adm-pane-${name}`).classList.add('active');
  window.location.hash = name === 'network' ? '' : name;
  if (name === 'config' && !_configIframeLoaded) {
    document.getElementById('config-iframe').src = '/config';
    _configIframeLoaded = true;
  }
  if (name === 'users') loadUsers();
  if (name === 'usage') loadUsage();
}

// Restore tab from URL hash on load
(function() {
  const hash = window.location.hash.replace('#', '');
  if (['config','software','users','usage'].includes(hash)) switchAdmTab(hash);
})();

// ── Global settings ────────────────────────────────────────────────────────────

async function saveGlobalSettings() {
  const val = parseInt(document.getElementById('gs-correlation-window').value, 10);
  const statusEl = document.getElementById('gs-status');
  if (!val || val < 1 || val > 3600) {
    statusEl.textContent = 'Invalid value'; statusEl.className = 'nc-status err'; return;
  }
  statusEl.textContent = 'Saving…'; statusEl.className = 'nc-status';
  try {
    const r = await fetch('/api/admin/network-config/global', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({correlation_window_s: val}),
    });
    const d = await r.json();
    if (d.ok) {
      statusEl.textContent = 'Saved'; statusEl.className = 'nc-status ok';
      setTimeout(() => { statusEl.textContent = ''; }, 2000);
    } else {
      statusEl.textContent = 'Error'; statusEl.className = 'nc-status err';
    }
  } catch(e) {
    statusEl.textContent = 'Error'; statusEl.className = 'nc-status err';
  }
}

// ── Sync rotate flags to stations ─────────────────────────────────────────────
// Pushes rotate from dashboard_config.yaml to every station's config.json.
// Heals drift without waiting for a redeploy.

async function syncRotateAll(btn) {
  if (!confirm('Push rotate flags from dashboard_config.yaml to every station?\n\n'
             + 'Each camera will get a PATCH /api/settings call. Offline stations '
             + 'will be reported but not block the others.')) return;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⇅ Syncing…';
  try {
    const r = await fetch('/api/admin/sync-rotate-all', {method: 'POST'});
    const d = await r.json();
    const failures = (d.results || []).filter(x => !x.ok);
    let msg = `Sync complete: ${d.succeeded}/${d.total} cameras updated.`;
    if (failures.length) {
      msg += '\n\nFailures:\n' + failures.map(f =>
        `  ${f.host}/${f.cam}: ${f.error || 'unknown'}`).join('\n');
    }
    alert(msg);
  } catch(e) {
    alert('Sync failed: ' + e);
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

// ── Config / Network ──────────────────────────────────────────────────────────

async function loadConfig() {
  const r = await fetch('/api/admin/network-config');
  if (!r.ok) {
    document.getElementById('nc-area').innerHTML = '<div class="offline">Failed to load config</div>';
    return;
  }
  _cfg = await r.json();
  if (_cfg.correlation_window_s != null) {
    document.getElementById('gs-correlation-window').value = _cfg.correlation_window_s;
  }
  renderOverview();
  renderNetworkConfig();
  loadVersions();
}

function renderOverview() {
  const stations = _cfg.stations || {};
  const cards = Object.entries(stations).map(([host, st]) => {
    const cams = (st.cameras || []).map(c =>
      `<span class="ov-tag cam">${escHtml(c.code)}</span>`).join('');
    const proxy = st.proxy_media ? `<span class="ov-tag proxy">proxy</span>` : '';
    const jumps = (st.jump_hosts || []).map(j =>
      `<span class="ov-tag jump">via ${escHtml(j)}</span>`).join('');
    return `<div class="ov-card">
      <div class="ov-card-host">${escHtml(host)}</div>
      <div class="ov-card-label">${escHtml(st.label || '')}</div>
      <div class="ov-card-row">
        <span class="ov-card-key">IP</span>
        <span class="ov-card-val">${escHtml(st.ip)}</span>
      </div>
      <div class="ov-card-row">
        <span class="ov-card-key">SSH</span>
        <span class="ov-card-val">${escHtml(st.ssh_user)}</span>
      </div>
      <div class="ov-card-tags">${cams}${proxy}${jumps}</div>
    </div>`;
  }).join('');
  const count = Object.keys(stations).length;
  document.getElementById('overview-area').innerHTML = `
    <div style="margin-bottom:8px;font-size:11px;color:var(--muted)">API port: <strong style="color:var(--text)">${escHtml(_cfg.station_api_port)}</strong>
      <span style="margin-left:12px">${count} station${count !== 1 ? 's' : ''}</span></div>
    <div class="ov-cards">${cards}</div>`;
}

function renderNetworkConfig() {
  const stations = _cfg.stations || {};
  const html = Object.entries(stations).map(([host, st]) => {
    const camRows = (st.cameras || []).map(c => `
      <div class="nc-cam-row" data-orig-code="${escHtml(c.code)}">
        <input type="text" id="cam-code-${escHtml(host)}-${escHtml(c.code)}" value="${escHtml(c.code)}" placeholder="Camera code">
        <input type="text" id="cam-ip-${escHtml(host)}-${escHtml(c.code)}" value="${escHtml(c.cam_ip ?? '')}" placeholder="Camera IP">
        <label class="nc-toggle" title="Rotate 180° for livestream display only — does not affect recordings">
          <input type="checkbox" id="cam-rot-${escHtml(host)}-${escHtml(c.code)}" ${c.rotate ? 'checked' : ''}>
          <span style="font-size:11px;color:var(--muted)">Rotate <span style="font-size:10px">(Livestream only)</span></span>
        </label>
        <button class="danger-btn" style="padding:2px 8px;font-size:11px"
                onclick="this.closest('.nc-cam-row').remove()">✕</button>
      </div>`).join('');

    const jumpVal = (st.jump_hosts || []).join(', ');
    return `<div class="nc-station" id="nc-st-${escHtml(host)}">
      <div class="nc-station-hdr" onclick="ncToggle('${escHtml(host)}')">
        <span class="nc-host">${escHtml(host)}</span>
        <span class="nc-label">${escHtml(st.label)}</span>
        <span class="nc-chevron" id="nc-chev-${escHtml(host)}">&#9660;</span>
      </div>
      <div class="nc-station-body" id="nc-body-${escHtml(host)}">
        <div class="nc-grid">
          <div class="nc-field">
            <label>Label</label>
            <input type="text" id="st-label-${escHtml(host)}" value="${escHtml(st.label)}">
          </div>
          <div class="nc-field">
            <label>Tailscale IP</label>
            <input type="text" id="st-ip-${escHtml(host)}" value="${escHtml(st.ip)}">
          </div>
          <div class="nc-field">
            <label>SSH User</label>
            <input type="text" id="st-user-${escHtml(host)}" value="${escHtml(st.ssh_user)}">
          </div>
          <div class="nc-field">
            <label>Jump Hosts (comma-separated)</label>
            <input type="text" id="st-jump-${escHtml(host)}" value="${escHtml(jumpVal)}" placeholder="e.g. gmnro02, gmnro03">
          </div>
          <div class="nc-field">
            <label>Latitude</label>
            <input type="number" step="any" id="st-lat-${escHtml(host)}" value="${escHtml(st.lat ?? '')}" placeholder="e.g. 44.945">
          </div>
          <div class="nc-field">
            <label>Longitude</label>
            <input type="number" step="any" id="st-lon-${escHtml(host)}" value="${escHtml(st.lon ?? '')}" placeholder="e.g. 25.662">
          </div>
          <div class="nc-field">
            <label>&nbsp;</label>
            <button class="settings-save-btn secondary" style="width:100%;font-size:11px;padding:5px 8px"
                    onclick="ncAutoDetectCoords('${escHtml(host)}',this)">⟳ Auto-detect from platepar</button>
          </div>
          <div class="nc-field">
            <label>&nbsp;</label>
            <label class="nc-toggle" style="height:100%;align-items:center">
              <input type="checkbox" id="st-proxy-${escHtml(host)}" ${st.proxy_media ? 'checked' : ''}>
              <span>Proxy all media</span>
            </label>
          </div>
        </div>
        <div class="nc-cams-title" style="display:flex;align-items:center;gap:8px">
          <span>Cameras (${(st.cameras||[]).length})</span>
          <button class="settings-save-btn" style="padding:2px 10px;font-size:10px;margin-left:auto"
                  onclick="ncAutoDetectCams('${escHtml(host)}',this)">⟳ Auto-detect from RMS</button>
          <button class="settings-save-btn secondary" style="padding:2px 10px;font-size:10px"
                  onclick="ncAddCamRow('${escHtml(host)}',this)">＋ Add camera</button>
        </div>
        <div class="nc-cam-hdr">
          <span>Code</span><span>IP</span><span>Rotate (Livestream only)</span>
        </div>
        ${camRows}
        <div class="nc-save-row">
          <button class="settings-save-btn" onclick="ncSave('${escHtml(host)}', this)">&#10003; Save ${escHtml(host)}</button>
          <button class="danger-btn" onclick="ncRemove('${escHtml(host)}')">✕ Remove station</button>
          <span class="nc-status" id="nc-status-${escHtml(host)}"></span>
        </div>
      </div>
    </div>`;
  }).join('');
  document.getElementById('nc-area').innerHTML = html;
}

// escHtml() lives in dashboard-common.js. Inline copy removed (P1-1 XSS sweep)
// so every page shares the same attribute-safe implementation (escapes &<>"',
// returns "" for null/undefined).

function ncToggle(host) {
  const body = document.getElementById(`nc-body-${host}`);
  const chev = document.getElementById(`nc-chev-${host}`);
  const open = body.classList.toggle('open');
  chev.style.transform = open ? 'rotate(180deg)' : '';
}

async function ncSave(host, btn) {
  const orig = btn.textContent;
  const statusEl = document.getElementById(`nc-status-${host}`);
  btn.disabled = true; btn.textContent = 'Saving…';
  statusEl.textContent = ''; statusEl.className = 'nc-status';

  const st = _cfg.stations[host];
  const jumpRaw = document.getElementById(`st-jump-${host}`).value;
  const latVal = parseFloat(document.getElementById(`st-lat-${host}`).value);
  const lonVal = parseFloat(document.getElementById(`st-lon-${host}`).value);
  const stPatch = {
    label:      document.getElementById(`st-label-${host}`).value.trim(),
    ip:         document.getElementById(`st-ip-${host}`).value.trim(),
    ssh_user:   document.getElementById(`st-user-${host}`).value.trim(),
    proxy_media: document.getElementById(`st-proxy-${host}`).checked,
    jump_hosts: jumpRaw.split(',').map(s => s.trim()).filter(Boolean),
    ...(isFinite(latVal) ? { lat: latVal } : {}),
    ...(isFinite(lonVal) ? { lon: lonVal } : {}),
  };

  try {
    const r = await fetch(`/api/admin/network-config/station/${host}`, {
      method: 'PATCH', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(stPatch),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);

    // Delete cameras that were removed from the DOM
    const camRows = document.querySelectorAll(`#nc-body-${host} .nc-cam-row`);
    const remainingCodes = new Set([...camRows].map(r => r.dataset.origCode).filter(c => c && !c.startsWith('new_')));
    for (const cam of (st.cameras || [])) {
      if (!remainingCodes.has(cam.code)) {
        const dr = await fetch(`/api/admin/network-config/station/${host}/camera/${cam.code}`, { method: 'DELETE' });
        if (!dr.ok) throw new Error(`Delete ${cam.code}: HTTP ${dr.status}`);
      }
    }

    // Save cameras — iterate over all cam rows in the DOM (includes auto-detected ones)
    for (const row of camRows) {
      const origCode = row.dataset.origCode;
      if (!origCode) continue;
      const codeEl = document.getElementById(`cam-code-${host}-${origCode}`);
      const ipEl   = document.getElementById(`cam-ip-${host}-${origCode}`);
      const rotEl  = document.getElementById(`cam-rot-${host}-${origCode}`);
      if (!ipEl) continue;
      const isNew = origCode.startsWith('new_');
      const newCode = codeEl?.value.trim() || origCode;
      if (isNew) {
        if (!newCode) continue;
        const cr = await fetch(`/api/admin/network-config/station/${host}/camera`, {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ code: newCode, cam_ip: ipEl.value.trim(), rotate: rotEl.checked }),
        });
        if (!cr.ok) throw new Error(`Camera ${newCode}: HTTP ${cr.status}`);
      } else {
        const camPatch = { cam_ip: ipEl.value.trim(), rotate: rotEl.checked, code: newCode };
        const cr = await fetch(`/api/admin/network-config/station/${host}/camera/${origCode}`, {
          method: 'PATCH', headers: {'Content-Type':'application/json'},
          body: JSON.stringify(camPatch),
        });
        if (!cr.ok) throw new Error(`Camera ${origCode}: HTTP ${cr.status}`);
      }
    }

    statusEl.textContent = '✓ Saved (restart dashboard to apply)';
    statusEl.className = 'nc-status ok';
    await loadConfig();
  } catch(e) {
    statusEl.textContent = '✗ ' + e.message;
    statusEl.className = 'nc-status err';
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
}

async function ncRemove(host) {
  if (!confirm(`Remove ${host} from dashboard config? This does not uninstall anything from the station.`)) return;
  try {
    const r = await fetch(`/api/admin/network-config/station/${host}`, { method: 'DELETE' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await loadConfig();
  } catch(e) {
    alert('Error removing station: ' + e.message);
  }
}

async function ncAutoDetectCoords(host, btn) {
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = 'Detecting…';
  try {
    const r = await fetch(`/api/platepar/${host}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    // Pick first camera's platepar
    const first = Object.values(data)[0];
    if (!first?.lat || !first?.lon) throw new Error('No coordinates in platepar');
    const lat = Math.round(first.lat * 100) / 100;
    const lon = Math.round(first.lon * 100) / 100;
    document.getElementById(`st-lat-${host}`).value = lat;
    document.getElementById(`st-lon-${host}`).value = lon;
    btn.textContent = `✓ ${lat}, ${lon}`;
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2500);
  } catch(e) {
    btn.textContent = '✗ ' + e.message;
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
  }
}

async function ncAutoDetectCams(host, btn) {
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = 'Detecting…';
  try {
    const r = await fetch(`/api/rms_cameras/${host}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const detected = await r.json();
    if (!detected.length) throw new Error('No cameras found in RMS config');
    const container = btn.closest('.nc-station-body');
    const camHdr = container.querySelector('.nc-cam-hdr');
    const detectedCodes = new Set(detected.map(c => c.code));

    // Highlight existing rows not found in the detection result
    container.querySelectorAll('.nc-cam-row').forEach(row => {
      const code = row.dataset.origCode;
      if (code && !detectedCodes.has(code)) {
        row.style.borderColor = 'var(--yellow)';
        row.style.background = 'rgba(210,153,34,.08)';
        if (!row.querySelector('.nc-not-found-badge')) {
          const badge = document.createElement('span');
          badge.className = 'nc-not-found-badge';
          badge.style.cssText = 'font-size:10px;color:var(--yellow);white-space:nowrap;grid-column:1/-1;padding:2px 0 0';
          badge.textContent = '⚠ Not found in RMS config';
          row.appendChild(badge);
        }
      } else {
        row.style.borderColor = '';
        row.style.background = '';
        row.querySelector('.nc-not-found-badge')?.remove();
      }
    });

    detected.forEach(cam => {
      const existing = container.querySelector(`[data-orig-code="${cam.code}"]`);
      if (existing) {
        const ipEl = existing.querySelector(`#cam-ip-${host}-${cam.code}`);
        if (ipEl) ipEl.value = cam.cam_ip;
      } else {
        const row = document.createElement('div');
        row.className = 'nc-cam-row';
        row.dataset.origCode = cam.code;
        row.innerHTML = `
          <input type="text" id="cam-code-${escHtml(host)}-${escHtml(cam.code)}" value="${escHtml(cam.code)}" placeholder="Camera code">
          <input type="text" id="cam-ip-${escHtml(host)}-${escHtml(cam.code)}" value="${escHtml(cam.cam_ip)}" placeholder="Camera IP">
          <label class="nc-toggle" title="Rotate 180° for livestream display only">
            <input type="checkbox" id="cam-rot-${escHtml(host)}-${escHtml(cam.code)}">
            <span style="font-size:11px;color:var(--muted)">Rotate <span style="font-size:10px">(Livestream only)</span></span>
          </label>
          <button class="danger-btn" style="padding:2px 8px;font-size:11px"
                  onclick="this.closest('.nc-cam-row').remove()">✕</button>`;
        camHdr.after(row);
      }
    });
    btn.textContent = `✓ ${detected.length} camera(s)`;
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2500);
  } catch(e) {
    btn.textContent = '✗ ' + e.message;
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
  }
}

function ncAddCamRow(host, btn) {
  const container = btn.closest('.nc-station-body');
  const camHdr = container.querySelector('.nc-cam-hdr');
  const uid = 'new_' + Date.now();
  const row = document.createElement('div');
  row.className = 'nc-cam-row';
  row.dataset.origCode = uid;
  row.innerHTML = `
    <input type="text" id="cam-code-${escHtml(host)}-${escHtml(uid)}" value="" placeholder="e.g. RO000X">
    <input type="text" id="cam-ip-${escHtml(host)}-${escHtml(uid)}" value="" placeholder="Camera IP">
    <label class="nc-toggle" title="Rotate 180° for livestream display only">
      <input type="checkbox" id="cam-rot-${escHtml(host)}-${escHtml(uid)}">
      <span style="font-size:11px;color:var(--muted)">Rotate <span style="font-size:10px">(Livestream only)</span></span>
    </label>
    <button class="danger-btn" style="padding:2px 8px;font-size:11px"
            onclick="this.closest('.nc-cam-row').remove()">✕</button>`;
  // Append after last cam row, or after header if none
  const allRows = container.querySelectorAll('.nc-cam-row');
  const insertAfter = allRows.length ? allRows[allRows.length - 1] : camHdr;
  insertAfter.after(row);
  row.querySelector('input[type=text]').focus();
}

function openAddModal() {
  document.getElementById('add-host').value = '';
  document.getElementById('add-ip').value = '';
  document.getElementById('add-port').value = '7779';
  document.getElementById('add-label').value = '';
  document.getElementById('add-user').value = 'gmn';
  document.getElementById('add-jump').value = '';
  document.getElementById('add-proxy').checked = false;
  document.getElementById('add-show-on-map').checked = true;
  document.getElementById('add-lat').value = '';
  document.getElementById('add-lon').value = '';
  document.getElementById('add-cameras').innerHTML = '';
  document.getElementById('add-detect-status').textContent = '';
  document.getElementById('add-detect-status').className = 'add-detect-status';
  document.getElementById('add-save-status').textContent = '';
  document.getElementById('add-save-status').className = 'nc-status';
  document.getElementById('add-station-modal').classList.remove('hidden');
}

function closeAddModal() {
  document.getElementById('add-station-modal').classList.add('hidden');
}

function addCamRow(code, camIp, rotate, az, alt) {
  const container = document.getElementById('add-cameras');
  const row = document.createElement('div');
  row.className = 'add-cam-row';
  const inp = (cls, val, ph, type='text') =>
    `<input type="${type}" value="${escHtml(String(val??''))}" placeholder="${ph}"
      style="background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:'SF Mono',monospace;font-size:12px;padding:5px 7px;width:100%"
      class="${cls}">`;
  row.innerHTML = `
    ${inp('add-cam-code', code||'', 'RO000X')}
    ${inp('add-cam-ip', camIp||'', '192.168.1.x')}
    ${inp('add-cam-az', az??'', 'Az°', 'number')}
    ${inp('add-cam-alt', alt??'', 'Alt°', 'number')}
    <label class="nc-toggle">
      <input type="checkbox" class="add-cam-rotate" ${rotate ? 'checked' : ''}>
      <span style="font-size:12px">Rotate</span>
    </label>
    <button onclick="this.closest('.add-cam-row').remove()"
      style="background:none;border:none;color:var(--red);font-size:16px;cursor:pointer;padding:0;line-height:1">✕</button>`;
  container.appendChild(row);
}

async function autoDetect() {
  const ip = document.getElementById('add-ip').value.trim();
  const port = parseInt(document.getElementById('add-port').value, 10) || 7779;
  const statusEl = document.getElementById('add-detect-status');
  const btn = document.getElementById('add-detect-btn');
  if (!ip) { statusEl.textContent = 'Enter a Tailscale IP first.'; statusEl.className = 'add-detect-status err'; return; }
  btn.disabled = true;
  statusEl.textContent = 'Detecting…';
  statusEl.className = 'add-detect-status';
  try {
    const r = await fetch('/api/admin/autodetect', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ip, port}),
    });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || 'Unknown error');
    document.getElementById('add-cameras').innerHTML = '';
    for (const cam of (data.cameras || [])) {
      addCamRow(cam.code, cam.cam_ip, cam.rotate, cam.az, cam.alt);
    }
    if (data.ssh_user) document.getElementById('add-user').value = data.ssh_user;
    statusEl.textContent = `✓ Detected ${(data.cameras||[]).length} camera(s)`;
    statusEl.className = 'add-detect-status ok';
  } catch(e) {
    statusEl.textContent = '✗ ' + e.message;
    statusEl.className = 'add-detect-status err';
  } finally {
    btn.disabled = false;
  }
}

async function saveAddStation(btn) {
  const host = document.getElementById('add-host').value.trim();
  const ip = document.getElementById('add-ip').value.trim();
  const label = document.getElementById('add-label').value.trim();
  const ssh_user = document.getElementById('add-user').value.trim();
  const proxy_media = document.getElementById('add-proxy').checked;
  const show_on_map = document.getElementById('add-show-on-map').checked;
  const latVal = parseFloat(document.getElementById('add-lat').value);
  const lonVal = parseFloat(document.getElementById('add-lon').value);
  const jumpRaw = document.getElementById('add-jump').value;
  const jump_hosts = jumpRaw.split(',').map(s => s.trim()).filter(Boolean);
  const statusEl = document.getElementById('add-save-status');

  const cameras = Array.from(document.querySelectorAll('#add-cameras .add-cam-row')).map(row => {
    const azV = parseFloat(row.querySelector('.add-cam-az').value);
    const altV = parseFloat(row.querySelector('.add-cam-alt').value);
    return {
      code: row.querySelector('.add-cam-code').value.trim(),
      cam_ip: row.querySelector('.add-cam-ip').value.trim(),
      rotate: row.querySelector('.add-cam-rotate').checked,
      az: isNaN(azV) ? null : azV,
      alt: isNaN(altV) ? null : altV,
    };
  }).filter(c => c.code);

  if (!host) { statusEl.textContent = '✗ Host key is required'; statusEl.className = 'nc-status err'; return; }
  if (!ip) { statusEl.textContent = '✗ IP is required'; statusEl.className = 'nc-status err'; return; }

  btn.disabled = true;
  statusEl.textContent = 'Saving…';
  statusEl.className = 'nc-status';
  try {
    const r = await fetch('/api/admin/network-config/station', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({host, ip, label: label || host, ssh_user, proxy_media, jump_hosts, cameras,
        lat: isNaN(latVal) ? null : latVal, lon: isNaN(lonVal) ? null : lonVal, show_on_map}),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);
    await loadConfig();
    closeAddModal();
  } catch(e) {
    statusEl.textContent = '✗ ' + e.message;
    statusEl.className = 'nc-status err';
    btn.disabled = false;
  }
}

// ── Software Version Overview ─────────────────────────────────────────────────

let _svHosts = [];
let _svPolling = {};
let _svBatch = { active: false, total: 0, done: 0, label: '' };

function svUpdateProgress() {
  const el = document.getElementById('sv-progress');
  if (!el) return;
  if (!_svBatch.active) { el.textContent = ''; return; }
  el.textContent = `${_svBatch.label} ${_svBatch.done}/${_svBatch.total}`;
}

async function loadVersions() {
  if (!_cfg) return;
  _svHosts = Object.keys(_cfg.stations || {});
  renderVersionTable();
  await Promise.all(_svHosts.map(h => fetchVersionStatus(h)));
}

function renderVersionTable() {
  if (!_svHosts.length) {
    document.getElementById('sv-area').innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px 0">No stations configured.</div>';
    return;
  }
  const cards = _svHosts.map(h => {
    const st = (_cfg.stations || {})[h] || {};
    const hE = escHtml(h);
    return `<div class="sv-card" id="sv-row-${hE}">
      <div class="sv-card-hdr">
        <a class="sv-card-host" href="/station/${encodeURIComponent(h)}" target="_blank">${hE}</a>
        <span class="sv-card-label">${escHtml(st.label||'')}</span>
        <span id="sv-status-${hE}" style="margin-left:auto"><span class="sv-badge dim"><span class="sv-spinner"></span></span></span>
      </div>
      <div class="sv-card-row">
        <span class="sv-card-key">Channel</span>
        <select id="sv-ch-${hE}" class="sv-channel-sel"
          onchange="saveChannel('${hE}',this)"
          style="background:var(--bg);border:1px solid var(--border);border-radius:4px;
            color:var(--text);font-family:'SF Mono',monospace;font-size:12px;
            padding:4px 8px;cursor:pointer;opacity:.5;min-height:28px">
          <option value="main">main</option>
          <option value="dev">dev</option>
        </select>
        <span id="sv-ch-status-${hE}" style="font-size:10px;margin-left:4px"></span>
      </div>
      <div class="sv-card-row">
        <span class="sv-card-key">Local</span>
        <span class="sv-ver none" id="sv-local-${hE}"><span class="sv-spinner"></span></span>
      </div>
      <div class="sv-card-row">
        <span class="sv-card-key">Remote</span>
        <span class="sv-ver none" id="sv-remote-${hE}"><span class="sv-spinner"></span></span>
      </div>
      <div class="sv-card-actions">
        <button class="settings-save-btn secondary"
          id="sv-check-btn-${hE}" onclick="checkOneVersion('${hE}',this)">Check</button>
        <button class="settings-save-btn"
          id="sv-upd-btn-${hE}" onclick="updateOneStation('${hE}',this)">Update</button>
      </div>
      <div class="sv-log-box" id="sv-log-${hE}"></div>
    </div>`;
  }).join('');
  document.getElementById('sv-area').innerHTML = `
    <div style="font-size:11px;color:var(--muted);margin-bottom:8px">${_svHosts.length} stations</div>
    <div class="sv-cards">${cards}</div>`;
}

async function fetchVersionStatus(host) {
  try {
    const r = await fetch(`/api/updater/status/${host}`);
    if (!r.ok) { setVersionRow(host, null, 'error'); return; }
    const d = await r.json();
    setVersionRow(host, d, null);
  } catch(e) {
    setVersionRow(host, null, 'offline');
  }
}

function setVersionRow(host, d, forceStatus) {
  const chSel = document.getElementById(`sv-ch-${host}`);
  const locEl = document.getElementById(`sv-local-${host}`);
  const remEl = document.getElementById(`sv-remote-${host}`);
  const stEl  = document.getElementById(`sv-status-${host}`);
  if (!chSel) return;
  if (!d) {
    chSel.style.opacity = '.3';
    locEl.textContent = '—'; locEl.className = 'sv-ver none';
    remEl.textContent = '—'; remEl.className = 'sv-ver none';
    stEl.innerHTML = `<span class="sv-badge err">${forceStatus === 'offline' ? 'Offline' : 'Error'}</span>`;
    return;
  }
  if (d.channel) { chSel.value = d.channel; chSel.dataset.prevChannel = d.channel; chSel.style.opacity = '1'; }
  locEl.textContent = d.local || '—';
  locEl.className = d.local ? 'sv-ver' : 'sv-ver none';
  remEl.textContent = d.remote || '—';
  remEl.className = d.remote ? 'sv-ver' : 'sv-ver none';
  if (forceStatus) {
    stEl.innerHTML = `<span class="sv-badge busy">${forceStatus}</span>`;
  } else if (d.up_to_date) {
    stEl.innerHTML = `<span class="sv-badge ok">Up to date</span>`;
  } else if (d.remote && d.local && d.remote !== d.local) {
    stEl.innerHTML = `<span class="sv-badge upd">Update available</span>`;
  } else {
    stEl.innerHTML = `<span class="sv-badge dim">${escHtml(d.status || '—')}</span>`;
  }
}

async function saveChannel(host, sel) {
  const channel = sel.value;
  const prev = sel.dataset.prevChannel || channel;
  if (prev === channel) return;
  const st = (_cfg.stations || {})[host] || {};
  const label = st.label || host;
  if (!confirm(`Switch ${host} (${label}) from "${prev}" to "${channel}"?`)) {
    sel.value = prev;
    return;
  }
  const statusEl = document.getElementById(`sv-ch-status-${host}`);
  sel.disabled = true;
  statusEl.textContent = '…'; statusEl.style.color = 'var(--muted)';
  try {
    const r = await fetch(`/api/settings/${host}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({update_channel: channel}),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    sel.dataset.prevChannel = channel;
    statusEl.textContent = '✓'; statusEl.style.color = 'var(--green)';
    setTimeout(() => { statusEl.textContent = ''; }, 2000);
  } catch(e) {
    sel.value = prev;
    statusEl.textContent = '✗'; statusEl.style.color = 'var(--red)';
    setTimeout(() => { statusEl.textContent = ''; }, 3000);
  } finally {
    sel.disabled = false;
  }
}

async function checkOneVersion(host, btn) {
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  const stEl = document.getElementById(`sv-status-${host}`);
  if (stEl) stEl.innerHTML = `<span class="sv-badge busy">Checking…</span>`;
  try {
    const r = await fetch(`/api/updater/check/${host}`, {method:'POST'});
    const d = await r.json();
    setVersionRow(host, r.ok ? d : null, r.ok ? null : 'error');
  } catch(e) {
    setVersionRow(host, null, 'offline');
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
}

async function refreshAllVersions(btn) {
  btn.disabled = true; btn.textContent = '…';
  _svBatch = { active: true, total: _svHosts.length, done: 0, label: 'Refreshing' };
  svUpdateProgress();
  await Promise.all(_svHosts.map(async h => {
    await checkOneVersion(h, document.getElementById(`sv-check-btn-${h}`) || {disabled:false, textContent:'Check'});
    _svBatch.done++;
    svUpdateProgress();
  }));
  _svBatch.active = false;
  svUpdateProgress();
  btn.disabled = false; btn.textContent = '⟳ Refresh All';
}

async function updateOneStation(host, btn, skipConfirm) {
  if (!skipConfirm) {
    const st = (_cfg.stations || {})[host] || {};
    const label = st.label || host;
    const chSel = document.getElementById(`sv-ch-${host}`);
    const channel = chSel ? chSel.value : 'unknown';
    if (!confirm(`Update ${host} (${label})?\n\nThis will pull and install the latest "${channel}" bundle.`)) return;
  }
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  const stEl = document.getElementById(`sv-status-${host}`);
  const logEl = document.getElementById(`sv-log-${host}`);
  if (stEl) stEl.innerHTML = `<span class="sv-badge busy">Updating…</span>`;
  if (logEl) { logEl.textContent = ''; logEl.classList.add('visible'); }

  if (_svPolling[host]) { clearInterval(_svPolling[host]); delete _svPolling[host]; }

  try {
    const r = await fetch(`/api/updater/run/${host}`, {method:'POST'});
    if (!r.ok) {
      const e = await r.json().catch(()=>({}));
      if (stEl) stEl.innerHTML = `<span class="sv-badge err">Failed</span>`;
      if (logEl) logEl.textContent = e.error || `HTTP ${r.status}`;
      btn.disabled = false; btn.textContent = orig;
      return;
    }
  } catch(e) {
    if (stEl) stEl.innerHTML = `<span class="sv-badge err">Offline</span>`;
    if (logEl) logEl.textContent = String(e);
    btn.disabled = false; btn.textContent = orig;
    return;
  }

  return new Promise(resolve => {
    const done = () => {
      clearInterval(_svPolling[host]); delete _svPolling[host];
      btn.disabled = false; btn.textContent = orig;
      resolve();
      if (logEl) setTimeout(() => { logEl.classList.remove('visible'); logEl.textContent = ''; }, 5000);
    };
    const timeout = setTimeout(done, 5 * 60 * 1000);
    _svPolling[host] = setInterval(async () => {
      try {
        const r2 = await fetch(`/api/updater/log/${host}`);
        const d2 = await r2.json();
        if (logEl) logEl.textContent = (d2.lines || []).join('\n');
        if (logEl) logEl.scrollTop = logEl.scrollHeight;
        if (!d2.running) { clearTimeout(timeout); await fetchVersionStatus(host); done(); }
      } catch(e2) { clearTimeout(timeout); done(); }
    }, 2000);
  });
}

async function updateAllStations(btn) {
  if (!confirm(`Update ALL ${_svHosts.length} stations?\n\nThis will push the latest software bundles to every station in the fleet.`)) return;
  btn.disabled = true; btn.textContent = '⬆ Updating…';
  _svBatch = { active: true, total: _svHosts.length, done: 0, label: 'Updating' };
  svUpdateProgress();
  await Promise.all(_svHosts.map(async h => {
    const updBtn = document.getElementById(`sv-upd-btn-${h}`);
    await updateOneStation(h, updBtn || {disabled: false, textContent: 'Update'}, true);
    _svBatch.done++;
    svUpdateProgress();
  }));
  _svBatch.active = false;
  svUpdateProgress();
  btn.disabled = false; btn.textContent = '⬆ Update All';
  await Promise.all(_svHosts.map(h => fetchVersionStatus(h)));
}

// ── Users ─────────────────────────────────────────────────────────────────────

let _usersData = [];

let _usersActivity = {};

async function loadUsers() {
  const area = document.getElementById('users-area');
  area.innerHTML = '<div class="loading">Loading…</div>';
  try {
    // Activity is log-derived and slower to compute; don't let it block the
    // basic table — render whatever it returns (or {} on failure).
    const [ru, ra] = await Promise.all([
      fetch('/api/admin/users'),
      fetch('/api/admin/users/activity').catch(() => null),
    ]);
    _usersData = await ru.json();
    _usersActivity = (ra && ra.ok) ? await ra.json() : {};
    renderUsersTable();
  } catch(e) {
    area.innerHTML = '<div class="offline">Failed to load users</div>';
  }
}

function _expiresCell(iso) {
  if (!iso) return '<span style="color:var(--muted)">—</span>';
  const t = Date.parse(iso);
  if (isNaN(t)) return '<span style="color:var(--muted)">—</span>';
  if (t < Date.now()) return '<span class="usr-badge expired">expired</span>';
  return `<span style="font-size:11px;color:var(--muted)">${escHtml(fmtRelFuture(iso))}</span>`;
}

function renderUsersTable() {
  const area = document.getElementById('users-area');
  if (!_usersData.length) {
    area.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:16px">No users yet.</div>';
    return;
  }
  const rows = _usersData.map(u => {
    const act = _usersActivity[u.username] || {};
    const linkBadge = u.has_magic_link
      ? ' <span class="usr-badge link" title="A one-click login link is active">link</span>' : '';
    const magicBtn = u.role === 'admin' ? '' :
      `<button class="usr-magic-btn users-magic-btn" data-username="${escHtml(u.username)}">Magic Link</button>`;
    return `
    <tr>
      <td style="font-family:'SF Mono',monospace;font-weight:600">${escHtml(u.username)}${linkBadge}</td>
      <td>${escHtml(u.display_name || '—')}</td>
      <td><span class="usr-role ${escHtml(u.role)}">${escHtml(u.role)}</span></td>
      <td style="font-family:'SF Mono',monospace;font-size:11px;color:var(--muted)">${escHtml((u.stations||[]).join(', ') || '—')}</td>
      <td style="font-size:11px;color:var(--muted)" title="${escHtml(act.last_login || '')}">${escHtml(fmtRelTime(act.last_login))}</td>
      <td style="font-size:11px;color:var(--muted)">${escHtml(fmtDuration(act.active_seconds))}</td>
      <td>${_expiresCell(u.expires_at)}</td>
      <td>
        <div class="usr-act-row">
          <button class="settings-save-btn secondary users-edit-btn" style="font-size:11px;padding:3px 10px"
            data-username="${escHtml(u.username)}">Edit</button>
          ${magicBtn}
          <button class="settings-save-btn secondary users-reset-btn" style="font-size:11px;padding:3px 10px"
            data-username="${escHtml(u.username)}">Reset PW</button>
          <button class="danger-btn users-delete-btn" style="font-size:11px;padding:3px 10px"
            data-username="${escHtml(u.username)}">Delete</button>
        </div>
      </td>
    </tr>`;
  }).join('');
  area.innerHTML = `<table class="usr-table">
    <thead><tr>
      <th>Username</th><th>Display Name</th><th>Role</th><th>Stations</th>
      <th>Last Login</th><th>Time Spent</th><th>Expires</th><th>Actions</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function usersOpenCreate() {
  document.getElementById('user-modal-title').textContent = 'Add User';
  document.getElementById('user-modal-mode').value = 'create';
  document.getElementById('user-modal-orig-username').value = '';
  document.getElementById('user-username').value = '';
  document.getElementById('user-username').disabled = false;
  document.getElementById('user-displayname').value = '';
  document.getElementById('user-role').value = 'host';
  usersPopulateStationChecks([]);
  document.getElementById('user-expires').value = '';
  document.getElementById('user-password').value = '';
  document.getElementById('user-password-label').textContent = 'Password';
  document.getElementById('user-modal-err').textContent = '';
  document.getElementById('user-modal-note').classList.add('hidden');
  usersRoleChange();
  document.getElementById('user-modal').classList.remove('hidden');
}

function usersOpenEdit(username) {
  const u = _usersData.find(x => x.username === username);
  if (!u) return;
  document.getElementById('user-modal-title').textContent = 'Edit User';
  document.getElementById('user-modal-mode').value = 'edit';
  document.getElementById('user-modal-orig-username').value = username;
  document.getElementById('user-username').value = username;
  document.getElementById('user-username').disabled = true;
  document.getElementById('user-displayname').value = u.display_name || '';
  document.getElementById('user-role').value = u.role;
  usersPopulateStationChecks(u.stations || []);
  document.getElementById('user-expires').value = _isoToLocal(u.expires_at);
  document.getElementById('user-password').value = '';
  document.getElementById('user-password-label').textContent = 'New Password (leave blank to keep)';
  document.getElementById('user-modal-err').textContent = '';
  // Make the propagation behaviour explicit: role/station/password/expiry
  // edits force the user's live session to revalidate (audit H3).
  document.getElementById('user-modal-note').classList.remove('hidden');
  usersRoleChange();
  document.getElementById('user-modal').classList.remove('hidden');
}

function usersPopulateStationChecks(selected) {
  const container = document.getElementById('user-stations-checks');
  const hosts = Object.keys(_cfg ? _cfg.stations || {} : {}).sort();
  const sel = new Set(selected);
  container.innerHTML = hosts.map(h => `
    <label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer;white-space:nowrap">
      <input type="checkbox" value="${escHtml(h)}"${sel.has(h) ? ' checked' : ''}>
      ${escHtml(h)}
    </label>`).join('');
  if (!hosts.length) container.innerHTML = '<span style="color:var(--muted);font-size:11px">No stations configured</span>';
}

function usersRoleChange() {
  const role = document.getElementById('user-role').value;
  const sf = document.getElementById('user-stations-field');
  sf.style.display = role === 'host' ? '' : 'none';
}

function _isoToLocal(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function _localToIso(val) {
  if (!val) return null;
  const d = new Date(val);
  if (isNaN(d.getTime())) return null;
  return d.toISOString();
}

function usersCloseModal() {
  document.getElementById('user-modal').classList.add('hidden');
}

async function usersSave() {
  const mode = document.getElementById('user-modal-mode').value;
  const origUsername = document.getElementById('user-modal-orig-username').value;
  const username = document.getElementById('user-username').value.trim();
  const displayName = document.getElementById('user-displayname').value.trim();
  const role = document.getElementById('user-role').value;
  const stations = Array.from(document.querySelectorAll('#user-stations-checks input[type=checkbox]:checked')).map(cb => cb.value);
  const password = document.getElementById('user-password').value;
  const expiresAt = _localToIso(document.getElementById('user-expires').value);
  const errEl = document.getElementById('user-modal-err');
  errEl.textContent = '';

  try {
    if (mode === 'create') {
      if (!username) { errEl.textContent = 'Username required'; return; }
      if (!password) { errEl.textContent = 'Password required'; return; }
      const r = await fetch('/api/admin/users', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({username, display_name: displayName, role, stations, password, expires_at: expiresAt}),
      });
      const data = await r.json();
      if (!r.ok) { errEl.textContent = data.error || 'Error'; return; }
    } else {
      const body = {display_name: displayName, role, stations, expires_at: expiresAt};
      if (password) body.password = password;
      const r = await fetch(`/api/admin/users/${encodeURIComponent(origUsername)}`, {
        method: 'PATCH',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok) { errEl.textContent = data.error || 'Error'; return; }
    }
    usersCloseModal();
    loadUsers();
  } catch(e) {
    errEl.textContent = 'Network error saving user';
  }
}

async function usersDelete(username) {
  if (!confirm(`Delete user "${username}"?`)) return;
  try {
    const r = await fetch(`/api/admin/users/${encodeURIComponent(username)}`, {method: 'DELETE'});
    if (!r.ok) { alert('Delete failed'); return; }
    loadUsers();
  } catch(e) {
    alert('Network error deleting user');
  }
}

async function usersResetPassword(username) {
  if (!confirm(`Generate a one-time reset token for "${username}"?\nTheir current password will still work until they use the token.`)) return;
  const r = await fetch(`/api/admin/users/${encodeURIComponent(username)}/reset-password`, {method: 'POST'});
  const data = await r.json();
  if (!r.ok) { alert(data.error || 'Failed to generate token'); return; }
  document.getElementById('reset-pw-token').textContent = data.token;
  document.getElementById('reset-pw-copy-msg').textContent = '';
  document.getElementById('reset-pw-modal').classList.remove('hidden');
}

function resetPwCopy() {
  const token = document.getElementById('reset-pw-token').textContent;
  navigator.clipboard.writeText(token).then(() => {
    document.getElementById('reset-pw-copy-msg').textContent = 'Copied to clipboard ✓';
  });
}

document.getElementById('users-area').addEventListener('click', function(e) {
  const row = e.target.closest('[data-username]');
  if (!row) return;
  const username = row.dataset.username;
  if (e.target.closest('.users-edit-btn')) usersOpenEdit(username);
  else if (e.target.closest('.users-magic-btn')) magicOpen(username);
  else if (e.target.closest('.users-reset-btn')) usersResetPassword(username);
  else if (e.target.closest('.users-delete-btn')) usersDelete(username);
});

// ── Magic login links ───────────────────────────────────────────────────────
let _magicUser = null;

function magicOpen(username) {
  _magicUser = username;
  const u = _usersData.find(x => x.username === username);
  document.getElementById('magic-user-label').textContent = username;
  document.getElementById('magic-days').value = '7';
  document.getElementById('magic-result').style.display = 'none';
  document.getElementById('magic-url').textContent = '';
  document.getElementById('magic-copy-msg').textContent = '';
  document.getElementById('magic-err').textContent = '';
  document.getElementById('magic-revoke-btn').style.display =
    (u && u.has_magic_link) ? '' : 'none';
  document.getElementById('magic-modal').classList.remove('hidden');
}

function magicClose() {
  document.getElementById('magic-modal').classList.add('hidden');
}

async function magicGenerate(btn) {
  const errEl = document.getElementById('magic-err');
  errEl.textContent = '';
  const days = parseInt(document.getElementById('magic-days').value, 10);
  if (!days || days < 1 || days > 3650) {
    errEl.textContent = 'Enter a number of days between 1 and 3650.'; return;
  }
  btn.disabled = true;
  try {
    const r = await fetch(`/api/admin/users/${encodeURIComponent(_magicUser)}/magic-link`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({days}),
    });
    const data = await r.json();
    if (!r.ok) { errEl.textContent = data.error || 'Failed to generate link'; return; }
    document.getElementById('magic-url').textContent = data.url;
    document.getElementById('magic-result').style.display = '';
    document.getElementById('magic-revoke-btn').style.display = '';
    document.getElementById('magic-copy-msg').textContent = '';
    loadUsers();
  } finally {
    btn.disabled = false;
  }
}

async function magicRevoke(btn) {
  if (!confirm(`Revoke the login link for "${_magicUser}"? Anyone holding it will be locked out immediately.`)) return;
  btn.disabled = true;
  try {
    const r = await fetch(`/api/admin/users/${encodeURIComponent(_magicUser)}/magic-link`, {method: 'DELETE'});
    if (!r.ok) { document.getElementById('magic-err').textContent = 'Revoke failed'; return; }
    document.getElementById('magic-result').style.display = 'none';
    document.getElementById('magic-revoke-btn').style.display = 'none';
    document.getElementById('magic-err').textContent = '';
    document.getElementById('magic-copy-msg').textContent = 'Link revoked ✓';
    document.getElementById('magic-copy-msg').style.color = 'var(--muted)';
    loadUsers();
  } finally {
    btn.disabled = false;
  }
}

function magicCopy() {
  const url = document.getElementById('magic-url').textContent;
  navigator.clipboard.writeText(url).then(() => {
    const m = document.getElementById('magic-copy-msg');
    m.style.color = 'var(--green)';
    m.textContent = 'Copied to clipboard ✓';
  });
}

// ── Usage stats ───────────────────────────────────────────────────────────────
async function loadUsage() {
  const days = document.getElementById('usage-range').value || '7';
  const featEl = document.getElementById('usage-features');
  const userEl = document.getElementById('usage-users');
  const dailyEl = document.getElementById('usage-daily');
  featEl.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const r = await fetch(`/api/admin/usage-stats?days=${encodeURIComponent(days)}`);
    if (!r.ok) throw new Error('http');
    const d = await r.json();
    renderUsageCards(d);
    renderUsageDaily(d, dailyEl);
    renderUsageFeatures(d, featEl);
    renderUsageUsers(d, userEl);
  } catch(e) {
    featEl.innerHTML = '<div class="offline">Failed to load usage statistics</div>';
    dailyEl.innerHTML = ''; userEl.innerHTML = '';
  }
}

function renderUsageCards(d) {
  const t = d.totals || {};
  const cards = [
    {v: (t.requests||0).toLocaleString(), l: 'Requests'},
    {v: t.active_users||0, l: 'Active users'},
    {v: t.sessions||0, l: 'Sessions'},
    {v: (d.features||[]).length, l: 'Features used'},
  ];
  document.getElementById('usage-cards').innerHTML = cards.map(c =>
    `<div class="usage-card"><div class="uc-val">${escHtml(String(c.v))}</div><div class="uc-lbl">${escHtml(c.l)}</div></div>`
  ).join('');
}

function renderUsageDaily(d, el) {
  const days = d.daily || [];
  if (!days.length) { el.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:12px">No activity in this window.</div>'; return; }
  const max = Math.max(1, ...days.map(x => x.hits));
  // Thin labels when the window is wide so they don't overlap.
  const step = days.length > 31 ? 7 : (days.length > 14 ? 2 : 1);
  el.innerHTML = `<div class="usage-day-chart">` + days.map((x, i) => {
    const h = Math.round((x.hits / max) * 100);
    const lbl = (i % step === 0) ? x.date.slice(5) : '';
    return `<div class="usage-day-col" title="${escHtml(x.date)}: ${escHtml(String(x.hits))} requests">
      <div class="usage-day-bar" style="height:${h}%"></div>
      <div class="usage-day-lbl">${escHtml(lbl)}</div>
    </div>`;
  }).join('') + `</div>`;
}

function renderUsageFeatures(d, el) {
  const feats = d.features || [];
  if (!feats.length) { el.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:12px">No feature activity recorded.</div>'; return; }
  const max = Math.max(1, ...feats.map(f => f.hits));
  el.innerHTML = feats.map(f => {
    const w = Math.round((f.hits / max) * 100);
    return `<div class="usage-bar-row">
      <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(f.feature)}</div>
      <div class="usage-bar-track"><div class="usage-bar-fill" style="width:${w}%"></div></div>
      <div class="usage-bar-meta">${escHtml(f.hits.toLocaleString())} · ${escHtml(String(f.users))} ${f.users===1?'user':'users'}</div>
    </div>`;
  }).join('');
}

let _usageUsersData = [];
let _usageUsersSortCol = 'active_seconds';
let _usageUsersSortDir = -1;

function _usageUsersSort(col) {
  if (_usageUsersSortCol === col) _usageUsersSortDir *= -1;
  else { _usageUsersSortCol = col; _usageUsersSortDir = col === 'user' ? 1 : -1; }
  _renderUsageUsersTable();
}

function _renderUsageUsersTable() {
  const el = document.getElementById('usage-users');
  if (!el || !_usageUsersData.length) return;
  const col = _usageUsersSortCol, dir = _usageUsersSortDir;
  const sorted = [..._usageUsersData].sort((a, b) => {
    let va, vb;
    switch (col) {
      case 'user': va = (a.user||'').toLowerCase(); vb = (b.user||'').toLowerCase(); break;
      case 'last_login': va = a.last_login ? new Date(a.last_login).getTime() : 0; vb = b.last_login ? new Date(b.last_login).getTime() : 0; break;
      case 'last_seen': va = a.last_seen ? new Date(a.last_seen).getTime() : 0; vb = b.last_seen ? new Date(b.last_seen).getTime() : 0; break;
      case 'requests': va = a.requests||0; vb = b.requests||0; break;
      case 'sessions': va = a.sessions||0; vb = b.sessions||0; break;
      case 'active_seconds': va = a.active_seconds||0; vb = b.active_seconds||0; break;
      default: return 0;
    }
    if (va < vb) return -dir;
    if (va > vb) return dir;
    return 0;
  });
  const arrow = (c) => c === col ? (dir === 1 ? ' ▲' : ' ▼') : '';
  const thStyle = 'cursor:pointer;user-select:none';
  const rows = sorted.map(u => `
    <tr>
      <td style="font-family:'SF Mono',monospace;font-weight:600">${escHtml(u.user)}</td>
      <td style="font-size:11px;color:var(--muted)" title="${escHtml(u.last_login||'')}">${escHtml(fmtRelTime(u.last_login))}</td>
      <td style="font-size:11px;color:var(--muted)" title="${escHtml(u.last_seen||'')}">${escHtml(fmtRelTime(u.last_seen))}</td>
      <td>${escHtml((u.requests||0).toLocaleString())}</td>
      <td>${escHtml(String(u.sessions||0))}</td>
      <td style="font-size:11px;color:var(--muted)">${escHtml(fmtDuration(u.active_seconds))}</td>
    </tr>`).join('');
  el.innerHTML = `<table class="usr-table">
    <thead><tr>
      <th style="${thStyle}" onclick="_usageUsersSort('user')">User${arrow('user')}</th>
      <th style="${thStyle}" onclick="_usageUsersSort('last_login')">Last Login${arrow('last_login')}</th>
      <th style="${thStyle}" onclick="_usageUsersSort('last_seen')">Last Seen${arrow('last_seen')}</th>
      <th style="${thStyle}" onclick="_usageUsersSort('requests')">Requests${arrow('requests')}</th>
      <th style="${thStyle}" onclick="_usageUsersSort('sessions')">Sessions${arrow('sessions')}</th>
      <th style="${thStyle}" onclick="_usageUsersSort('active_seconds')">Active Time${arrow('active_seconds')}</th>
    </tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderUsageUsers(d, el) {
  _usageUsersData = d.users || [];
  if (!_usageUsersData.length) { el.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:12px">No user activity recorded.</div>'; return; }
  _renderUsageUsersTable();
}

// ── Formatters ──────────────────────────────────────────────────────────────
function fmtDuration(sec) {
  sec = Number(sec) || 0;
  if (sec <= 0) return '—';
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.round(sec / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60), rm = m % 60;
  if (h < 24) return rm ? `${h}h ${rm}m` : `${h}h`;
  const dd = Math.floor(h / 24), rh = h % 24;
  return rh ? `${dd}d ${rh}h` : `${dd}d`;
}

function fmtRelTime(iso) {
  if (!iso) return 'never';
  const t = Date.parse(iso);
  if (isNaN(t)) return '—';
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  const d = Math.floor(diff/86400);
  if (d < 30) return `${d}d ago`;
  return new Date(t).toISOString().slice(0, 10);
}

function fmtRelFuture(iso) {
  const t = Date.parse(iso);
  if (isNaN(t)) return '—';
  const diff = (t - Date.now()) / 1000;
  if (diff <= 0) return 'expired';
  const d = Math.floor(diff/86400);
  if (d >= 1) return `in ${d}d`;
  const h = Math.floor(diff/3600);
  if (h >= 1) return `in ${h}h`;
  return `in ${Math.max(1, Math.floor(diff/60))}m`;
}

// ── Escape key: close topmost open modal ──────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const modals = document.querySelectorAll('.add-modal');
  for (const m of modals) {
    if (!m.classList.contains('hidden')) {
      m.classList.add('hidden');
      break;
    }
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────
loadConfig();

// ── Window exposure for onclick handlers ──────────────────────────────────────
window.switchAdmTab = switchAdmTab;
window.openAddModal = openAddModal;
window.closeAddModal = closeAddModal;
window.saveAddStation = saveAddStation;
window.addCamRow = addCamRow;
window.autoDetect = autoDetect;
window.ncSave = ncSave;
window.ncToggle = ncToggle;
window.ncRemove = ncRemove;
window.ncAutoDetectCams = ncAutoDetectCams;
window.ncAutoDetectCoords = ncAutoDetectCoords;
window.ncAddCamRow = ncAddCamRow;
window.checkOneVersion = checkOneVersion;
window.refreshAllVersions = refreshAllVersions;
window.updateOneStation = updateOneStation;
window.updateAllStations = updateAllStations;
window.saveChannel = saveChannel;
window.syncRotateAll = syncRotateAll;
window.usersSave = usersSave;
window.usersOpenCreate = usersOpenCreate;
window.usersCloseModal = usersCloseModal;
window.usersRoleChange = usersRoleChange;
window.magicGenerate = magicGenerate;
window.magicRevoke = magicRevoke;
window.magicCopy = magicCopy;
window.magicClose = magicClose;
window.resetPwCopy = resetPwCopy;
window.saveGlobalSettings = saveGlobalSettings;
window.loadUsage = loadUsage;
window._usageUsersSort = _usageUsersSort;
