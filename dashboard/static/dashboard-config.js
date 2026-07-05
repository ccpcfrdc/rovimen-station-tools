import { state, escHtml, openAppPanel, appPanelClose } from './dashboard-common.js';

// toggleLogoMenu / openAppPanel / appPanelClose / _APP_PANELS now live in
// dashboard-common.js. Inline copies removed to avoid "Identifier
// '_APP_PANELS' has already been declared" SyntaxError.
(async function() {
  try {
    const auth = await (await fetch('/api/auth/status')).json();
    if (auth.admin) {
      const el = document.getElementById('logo-dd-admin');
      if (el) el.style.display = '';
      const socialEl = document.getElementById('logo-dd-social');
      if (socialEl) socialEl.style.display = '';
    }
  } catch(e) {}
})();

// Hide header when embedded in admin iframe
if (window.self !== window.top) {
  document.getElementById('cfg-header').style.display = 'none';
}

// ---------------------------------------------------------------------------
// Config field definitions
// Each field: { label, path: array of keys into config JSON, type: 'bool'|'number'|'string' }
// ---------------------------------------------------------------------------
const SECTIONS = [
  { name: 'Services', fields: [
    { label: 'stacker.enabled',      path: ['services','stacker','enabled'],         type: 'bool' },
    { label: 'stacker.realtime',     path: ['services','stacker','realtime'],        type: 'bool' },
    { label: 'stacker.nice',         path: ['services','stacker','nice'],            type: 'number', min:-20, max:19,  step:1 },
    { label: 'stacker.cpu_quota',    path: ['services','stacker','cpu_quota'],       type: 'number', min:5,   max:200, step:5 },
    { label: 'reencode.enabled',     path: ['services','reencode','enabled'],        type: 'bool' },
    { label: 'detection_lock',       path: ['services','detection_lock','enabled'],  type: 'bool' },
    { label: 'archive_upload',       path: ['services','archive_upload','enabled'],  type: 'bool' },
    { label: 'timelapse_build',      path: ['services','timelapse_build','enabled'], type: 'bool' },
  ]},
  { name: 'Location', fields: [
    { label: 'latitude',             path: ['latitude'],                    type: 'number', min:-90,   max:90,   step:0.001 },
    { label: 'longitude',            path: ['longitude'],                   type: 'number', min:-180,  max:180,  step:0.001 },
    { label: 'elevation (m)',        path: ['elevation'],                   type: 'number', min:0,     max:5000, step:1 },
  ]},
  { name: 'Capture', fields: [
    { label: 'segment_duration',     path: ['segment_duration'],            type: 'number', min:5,   max:120, step:5 },
    { label: 'rtsp_capture_delay_s', path: ['rtsp_capture_delay_s'],        type: 'number', min:0,   max:30,  step:1 },
    { label: 'ff_idle_timeout_min',  path: ['ff_idle_timeout_minutes'],     type: 'number', min:5,   max:120, step:5 },
    { label: 'min_disk_gb_free',     path: ['min_disk_gb_free'],            type: 'number', min:1,   max:100, step:1 },
    { label: 'encode_workers',       path: ['encode_workers'],              type: 'number', min:1,   max:8,   step:1 },
    { label: 'compression_level',    path: ['compression_level'],           type: 'number', min:0,   max:4,   step:1 },
  ]},
  { name: 'Detection', fields: [
    { label: 'pre_seconds',          path: ['detection','pre_seconds'],     type: 'number', min:0,   max:30,  step:1 },
    { label: 'post_seconds',         path: ['detection','post_seconds'],    type: 'number', min:0,   max:60,  step:1 },
  ]},
  { name: 'Retention (days)', fields: [
    { label: 'color_days',           path: ['retention','color_days'],      type: 'number', min:1,   max:30,  step:1 },
    { label: 'locked_days',          path: ['retention','locked_days'],     type: 'number', min:1,   max:90,  step:1 },
    { label: 'stacks_days',          path: ['retention','stacks_days'],     type: 'number', min:1,   max:90,  step:1 },
    { label: 'timelapse_days',       path: ['retention','timelapse_days'],  type: 'number', min:1,   max:90,  step:1 },
  ]},
  { name: 'Disk Thresholds (%)', fields: [
    { label: 'warn_pct',             path: ['disk','warn_pct'],             type: 'number', min:50,  max:95,  step:1 },
    { label: 'nuclear_pct',          path: ['disk','nuclear_pct'],          type: 'number', min:60,  max:98,  step:1 },
    { label: 'extreme_pct',          path: ['disk','extreme_pct'],          type: 'number', min:70,  max:99,  step:1 },
  ]},
  { name: 'Archive', fields: [
    { label: 'enabled',              path: ['archive','enabled'],           type: 'bool' },
    { label: 'host',                 path: ['archive','host'],              type: 'string' },
    { label: 'upload_meteors',       path: ['archive','upload_meteors'],    type: 'bool' },
    { label: 'upload_timelapses',    path: ['archive','upload_timelapses'], type: 'bool' },
    { label: 'upload_stacks',        path: ['archive','upload_stacks'],     type: 'bool' },
    { label: 'interval_minutes',     path: ['archive','interval_minutes'],  type: 'number', min:5, max:120, step:5 },
  ]},
  { name: 'Overlay', fields: [
    { label: 'enabled',              path: ['overlay','enabled'],           type: 'bool' },
    { label: 'style',                path: ['overlay','style'],             type: 'enum', options: ['standard','cinema'] },
    { label: 'network',              path: ['overlay','network'],           type: 'string' },
    { label: 'coords',               path: ['overlay','coords'],            type: 'string' },
    { label: 'font_size',            path: ['overlay','font_size'],         type: 'number', min:8,   max:48,  step:1 },
    { label: 'text_opacity',         path: ['overlay','text_opacity'],      type: 'number', min:0,   max:1,   step:0.05 },
    { label: 'logo_opacity',         path: ['overlay','logo_opacity'],      type: 'number', min:0,   max:1,   step:0.05 },
    { label: 'show_logo',            path: ['overlay','show_logo'],         type: 'bool' },
    { label: 'show_network',         path: ['overlay','show_network'],      type: 'bool' },
    { label: 'show_timestamp',       path: ['overlay','show_timestamp'],    type: 'bool' },
    { label: 'show_station',         path: ['overlay','show_station'],      type: 'bool' },
    { label: 'show_coords',          path: ['overlay','show_coords'],       type: 'bool' },
    { label: 'show_pointing',        path: ['overlay','show_pointing'],     type: 'bool' },
  ]},
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getPath(obj, path) {
  let cur = obj;
  for (const k of path) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[k];
  }
  return cur;
}

function setPath(obj, path, value) {
  // Build a nested object for PATCH payload
  const result = {};
  let cur = result;
  for (let i = 0; i < path.length - 1; i++) {
    cur[path[i]] = {};
    cur = cur[path[i]];
  }
  cur[path[path.length - 1]] = value;
  return result;
}

function consensus(values) {
  // Most common value among non-undefined entries
  const counts = new Map();
  for (const v of values) {
    if (v === undefined) continue;
    const k = JSON.stringify(v);
    counts.set(k, (counts.get(k) || 0) + 1);
  }
  if (!counts.size) return undefined;
  let best = null, bestCount = 0;
  for (const [k, c] of counts) {
    if (c > bestCount) { bestCount = c; best = k; }
  }
  return JSON.parse(best);
}

function formatVal(val, type) {
  if (val === undefined) return { text: '—', cls: 'missing' };
  if (type === 'bool') {
    return { text: '', cls: val ? 'bool-true val-bool-true' : 'bool-false val-bool-false' };
  }
  return { text: String(val), cls: 'ok' };
}

// ---------------------------------------------------------------------------
// Editing helpers
// ---------------------------------------------------------------------------

let isAdmin = false;
let _configDirty = false;

// escAttr() was a local & + " escaper; superseded by the canonical escHtml()
// from dashboard-common.js, which also handles <, >, and '.

function markDirty(el) {
  const changed = el.value !== el.dataset.orig;
  el.style.borderColor = changed ? 'var(--yellow)' : '';
  el.closest('td').style.background = changed ? 'rgba(210,153,34,.08)' : '';
  if (changed) _configDirty = true;
}

window.addEventListener('beforeunload', e => {
  if (_configDirty) { e.preventDefault(); }
});

function cfgStep(id, delta, min, max) {
  const el = document.getElementById(id);
  if (!el) return;
  const v = Math.round((parseFloat(el.value || 0) + delta) * 10000) / 10000;
  el.value = Math.min(max, Math.max(min, v));
  markDirty(el);
}

function renderCell(val, field, hostKey, si, fi) {
  const orig = val === undefined ? '' : String(val);
  const id = `c-${si}-${fi}-${hostKey}`;
  // hostKey is admin-managed (dashboard_config.yaml). si/fi are local indices
  // but escape the lot for consistency in attribute context.
  const tdAttrs = `data-section="${si}" data-fi="${fi}" data-hk="${escHtml(hostKey)}"`;

  if (!isAdmin || val === undefined) {
    const { text, cls } = formatVal(val, field.type);
    return `<td class="val ${cls}" ${tdAttrs}>${escHtml(text)}</td>`;
  }

  if (field.type === 'bool') {
    return `<td class="val" ${tdAttrs}>
      <select id="${escHtml(id)}" class="val-input" data-orig="${escHtml(orig)}" onchange="markDirty(this)" style="padding:2px 4px;width:68px">
        <option value="true"  ${val===true ?'selected':''}>true</option>
        <option value="false" ${val===false?'selected':''}>false</option>
      </select></td>`;
  }

  if (field.type === 'enum') {
    // field.options is a SECTIONS constant; orig is the API value to compare.
    const opts = (field.options||[]).map(o=>`<option value="${escHtml(o)}"${orig===o?' selected':''}>${escHtml(o)}</option>`).join('');
    return `<td class="val" ${tdAttrs}>
      <select id="${escHtml(id)}" class="val-input" data-orig="${escHtml(orig)}" onchange="markDirty(this)" style="padding:2px 4px">
        ${opts}
      </select></td>`;
  }

  if (field.type === 'number') {
    const mn = field.min??'', mx = field.max??'', st = field.step??1;
    const lo = field.min??-1e9, hi = field.max??1e9;
    return `<td class="val" ${tdAttrs}>
      <div style="display:inline-flex;align-items:center;gap:2px">
        <button type="button" class="spinner-btn" style="width:20px;height:20px;font-size:12px"
                onclick="cfgStep('${escHtml(id)}',${-st},${lo},${hi})">&#x2212;</button>
        <input type="number" id="${escHtml(id)}" min="${mn}" max="${mx}" step="${st}" value="${escHtml(orig)}"
               data-orig="${escHtml(orig)}" oninput="markDirty(this)"
               style="width:${st < 0.01 ? '80' : '52'}px;text-align:center;padding:2px 3px;background:var(--card);
                      border:1px solid var(--border);color:var(--text);font-family:monospace;
                      font-size:11px;border-radius:3px;outline:none">
        <button type="button" class="spinner-btn" style="width:20px;height:20px;font-size:12px"
                onclick="cfgStep('${escHtml(id)}',${st},${lo},${hi})">+</button>
      </div></td>`;
  }

  // string -- escHtml supersedes escAttr (handles <, >, ' in addition to & and ")
  return `<td class="val" ${tdAttrs}>
    <input type="text" id="${escHtml(id)}" class="val-input" value="${escHtml(orig)}"
           data-orig="${escHtml(orig)}" oninput="markDirty(this)" style="width:110px;padding:2px 5px">
  </td>`;
}

async function saveSection(si, btn) {
  const section = SECTIONS[si];
  btn.disabled = true; btn.textContent = 'Saving…';

  const patches = {};
  document.querySelectorAll(`td[data-section="${si}"]`).forEach(td => {
    const hk = td.dataset.hk;
    const fi  = parseInt(td.dataset.fi);
    const field = section.fields[fi];
    if (!hk || !field) return;
    const input = td.querySelector('input,select');
    if (!input || input.value === input.dataset.orig) return;

    if (!patches[hk]) patches[hk] = {};
    let value = input.value;
    if (field.type === 'bool')        value = value === 'true';
    else if (field.type === 'number') value = Number(value);

    let cur = patches[hk];
    field.path.slice(0, -1).forEach(k => { if (!cur[k]) cur[k] = {}; cur = cur[k]; });
    cur[field.path[field.path.length - 1]] = value;
  });

  if (!Object.keys(patches).length) {
    btn.textContent = 'No changes';
    setTimeout(() => { btn.textContent = 'Save'; btn.disabled = false; }, 1500);
    return;
  }

  const sectionEl = btn.closest('.cfg-section');
  const sectionBody = btn.closest('.cfg-section-body');
  try {
    await Promise.all(Object.entries(patches).map(([hk, payload]) =>
      fetch(`/api/settings/${hk}`, {
        method: 'PATCH',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload),
      }).then(r => { if (!r.ok) throw new Error(`${hk}: HTTP ${r.status}`); })
    ));
    _configDirty = false;
    // Show loading in this section while the full reload happens
    if (sectionBody) sectionBody.innerHTML = '<div class="cfg-loading">Loading config…</div>';
    loadAndRender();
  } catch(e) {
    // Show error banner without destroying the form fields
    let errBanner = sectionEl ? sectionEl.querySelector('.cfg-save-err-banner') : null;
    if (!errBanner && sectionEl) {
      errBanner = document.createElement('div');
      errBanner.className = 'cfg-save-err-banner cfg-error';
      errBanner.style.cssText = 'margin:8px 14px 0;font-size:12px';
      sectionEl.insertBefore(errBanner, sectionBody);
    }
    if (errBanner) errBanner.textContent = 'Save failed: ' + e.message;
    btn.disabled = false;
    btn.textContent = 'Save';
    if (errBanner) setTimeout(() => { errBanner.textContent = ''; }, 5000);
  }
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function renderMatrix(stations, stationMeta, configs) {
  const onlineStations = stations.filter(hk => configs[hk]?.ok);
  let totalDiffs = 0;

  const statusBar = document.getElementById('cfg-status-bar');
  statusBar.innerHTML = stations.map(hk => {
    const r = configs[hk];
    const label = stationMeta[hk]?.label || hk;
    const cls = !r ? 'offline' : r.ok ? 'online' : 'error';
    const title = r?.error ? ` title="${escHtml(r.error)}"` : '';
    return `<span class="cfg-badge ${cls}"${title}>${escHtml(hk)} · ${escHtml(label)}</span>`;
  }).join('');

  const sections = SECTIONS.map((section, si) => {
    let sectionDiffs = 0;

    const diffFields = section.fields.map((field, fi) => {
      const vals = stations.map(hk => {
        const r = configs[hk];
        if (!r || !r.ok) return undefined;
        return getPath(r.config, field.path);
      });
      const con = consensus(vals);
      const hasDiff = onlineStations.length > 1 &&
        onlineStations.some(hk => {
          const v = getPath(configs[hk].config, field.path);
          return v !== undefined && JSON.stringify(v) !== JSON.stringify(con);
        });
      if (hasDiff) sectionDiffs++;
      return hasDiff;
    });
    totalDiffs += sectionDiffs;

    const headerCells = section.fields.map((field, fi) => {
      const diffMark = diffFields[fi] ? `<span style="color:var(--yellow);font-size:9px;margin-left:2px">▲</span>` : '';
      return `<th style="text-align:center;font-size:11px;white-space:nowrap">${escHtml(field.label)}${diffMark}</th>`;
    }).join('');

    const rows = stations.map(hk => {
      const r = configs[hk];
      const label = stationMeta[hk]?.label || '';
      const cells = section.fields.map((field, fi) => {
        const val = r?.ok ? getPath(r.config, field.path) : undefined;
        return renderCell(val, field, hk, si, fi);
      }).join('');
      return `<tr>
        <td class="station-name">${escHtml(hk)}<span class="st-lbl">${escHtml(label)}</span></td>
        ${cells}
      </tr>`;
    }).join('');

    const diffBadge = sectionDiffs > 0
      ? `<span class="diff-count">${sectionDiffs} diff${sectionDiffs > 1 ? 's' : ''}</span>` : '';

    const saveBtn = isAdmin
      ? `<div style="padding:10px 14px;border-top:1px solid var(--border);display:flex;justify-content:flex-end">
           <button class="sync-btn" style="padding:4px 18px;font-size:12px"
                   onclick="saveSection(${si}, this)">Save</button>
         </div>`
      : '';

    return `<div class="cfg-section">
      <div class="cfg-section-hdr" onclick="toggleSection(this)">
        <span>${escHtml(section.name)}</span>${diffBadge}
        <span class="chevron">▾</span>
      </div>
      <div class="cfg-section-body">
        <div style="overflow-x:auto">
        <table class="cfg-table">
          <thead><tr>
            <th style="position:sticky;left:0;background:rgba(48,54,61,.5);z-index:2">Station</th>
            ${headerCells}
          </tr></thead>
          <tbody>${rows}</tbody>
        </table></div>
        ${saveBtn}
      </div>
    </div>`;
  }).join('');

  const banner = document.getElementById('cfg-diff-banner');
  if (totalDiffs > 0) {
    banner.style.display = 'block';
    banner.textContent = `${totalDiffs} inconsistenc${totalDiffs > 1 ? 'ies' : 'y'} detected across station configs.`;
  } else {
    banner.style.display = 'none';
  }

  document.getElementById('cfg-body').innerHTML = sections;
}

function toggleSection(hdr) {
  const body = hdr.nextElementSibling;
  const collapsed = body.classList.toggle('hidden');
  hdr.classList.toggle('collapsed', collapsed);
}

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------

async function loadAndRender() {
  try {
    const [configsResp, stationsResp, authResp] = await Promise.all([
      fetch('/api/config/all'),
      fetch('/api/stations'),
      fetch('/api/auth/status'),
    ]);

    if (configsResp.status === 401) {
      document.getElementById('cfg-body').innerHTML =
        '<div class="cfg-error">Login required to view configs. <a href="/login">Log in</a></div>';
      return;
    }

    const configs = await configsResp.json();
    const stationMeta = await stationsResp.json();
    const auth = await authResp.json();
    isAdmin = auth.admin;

    const el = document.getElementById('cfg-auth');
    if (auth.admin) {
      el.innerHTML = `<span style="color:var(--green);margin-right:6px">${escHtml(auth.user)}</span>
        <a href="/logout">Logout</a>`;
    } else {
      el.innerHTML = `<a href="/login">Login</a>`;
    }

    const stations = Object.keys(stationMeta);
    renderMatrix(stations, stationMeta, configs);
  } catch(e) {
    document.getElementById('cfg-body').innerHTML =
      `<div class="cfg-error">Failed to load: ${escHtml(e.message)}</div>`;
  }
}

loadAndRender();

// ---------------------------------------------------------------------------
// Window exposure for onclick handlers in HTML and dynamic template literals
// ---------------------------------------------------------------------------
window.saveSection = saveSection;
window.cfgStep = cfgStep;
window.toggleSection = toggleSection;
window.markDirty = markDirty;
window.openAppPanel = openAppPanel;
window.appPanelClose = appPanelClose;
