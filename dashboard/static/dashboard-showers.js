import { openAppPanel, appPanelClose, showerFullName } from '/static/dashboard-common.js';

window.openAppPanel = openAppPanel;
window.appPanelClose = appPanelClose;

const MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const MONTH_FULL = ['January','February','March','April','May','June',
                    'July','August','September','October','November','December'];

function peakMonthFull(peak) {
  if (!peak) return null;
  const idx = MONTH_ABBR.indexOf(peak.slice(0, 3));
  return idx >= 0 ? MONTH_FULL[idx] : null;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ── Active-now detection ─────────────────────────────────────────────────── */

function _parseDateStr(s) {
  if (!s) return null;
  const parts = s.trim().split(/\s+/);
  const mi = MONTH_ABBR.indexOf(parts[0]);
  const day = parseInt(parts[1]);
  if (mi < 0 || isNaN(day)) return null;
  return mi * 100 + day; // month-major comparable integer
}

function isActiveNow(info) {
  const begin = _parseDateStr(info.activity_begin);
  const end   = _parseDateStr(info.activity_end);
  if (begin == null || end == null) return false;
  const now = new Date();
  const val = now.getMonth() * 100 + now.getDate();
  return begin <= end
    ? (val >= begin && val <= end)      // normal range
    : (val >= begin || val <= end);     // wraps year boundary (e.g. Nov–Jan)
}

/* ── Card builder ─────────────────────────────────────────────────────────── */

function buildCard(code, info, yearCount) {
  const name = info.name || showerFullName(code);
  const active = isActiveNow(info);
  const activity = (info.activity_begin && info.activity_end)
    ? `${info.activity_begin}–${info.activity_end}` : null;

  // Order matters: grid-auto-flow:column fills col1 (Activity+Peak), col2 (Speed+Parent), col3 (ZHR).
  let rows = '';
  if (activity)
    rows += `<div class="sh-row"><span class="sh-lbl">Activity</span><span class="sh-val">${escHtml(activity)}</span></div>`;
  if (info.peak)
    rows += `<div class="sh-row"><span class="sh-lbl">Peak</span><span class="sh-val">${escHtml(info.peak)}</span></div>`;
  if (info.vg != null)
    rows += `<div class="sh-row"><span class="sh-lbl">Speed</span><span class="sh-val">${escHtml(String(info.vg))} km/s</span></div>`;
  if (info.parent)
    rows += `<div class="sh-row"><span class="sh-lbl">Parent</span><span class="sh-val-parent">${escHtml(info.parent)}</span></div>`;
  if (info.zhr != null)
    rows += `<div class="sh-row sh-row-zhr"><span class="sh-lbl">ZHR</span><span class="sh-val sh-val-zhr">${escHtml(String(info.zhr))}/hr</span></div>`;

  const activeCls = active ? ' sh-card--active' : '';
  const activeBadge = active
    ? `<span class="sh-badge-active">&#9679; Active now</span>`
    : '';
  const countBadge = yearCount != null
    ? `<span class="sh-badge-count" title="${yearCount} ROVIMEN detection${yearCount !== 1 ? 's' : ''} in ${new Date().getFullYear()}">${yearCount} in ${new Date().getFullYear()}</span>`
    : '';

  return `<div class="sh-card${activeCls}" role="button" tabindex="0"
      data-code="${escHtml(code)}" data-name="${escHtml(name.toLowerCase())}"
      onclick="location.href='/events?shower=${encodeURIComponent(code)}'"
      onkeydown="if(event.key==='Enter'||event.key===' ')location.href='/events?shower=${encodeURIComponent(code)}'"
      title="View ${escHtml(name)} detections in the events page">
    <div class="sh-card-head">
      <span class="sh-code">${escHtml(code)}</span>
      <span class="sh-name">${escHtml(name)}</span>
      <div class="sh-badges">${activeBadge}${countBadge}</div>
    </div>
    ${rows ? `<div class="sh-card-body">${rows}</div>` : ''}
  </div>`;
}

/* ── Render ───────────────────────────────────────────────────────────────── */

function render(mdc, yearCounts) {
  const wrap = document.getElementById('sh-page-wrap');
  const loading = document.getElementById('sh-loading');
  if (loading) loading.remove();

  const byMonth = { Sporadic: [] };
  for (const m of MONTH_FULL) byMonth[m] = [];
  const other = [];

  for (const [code, info] of Object.entries(mdc)) {
    if (code === 'SPO' || code === 'ANT') {
      byMonth['Sporadic'].push([code, info]);
      continue;
    }
    const month = peakMonthFull(info.peak);
    if (month) {
      byMonth[month].push([code, info]);
    } else {
      other.push([code, info]);
    }
  }

  const peakDay = (info) => parseInt(info.peak?.slice(4)?.trim()) || 0;
  for (const arr of Object.values(byMonth)) {
    // Active showers first within each month, then by peak date.
    arr.sort((a, b) => {
      const ao = isActiveNow(a[1]) ? 0 : 1;
      const bo = isActiveNow(b[1]) ? 0 : 1;
      if (ao !== bo) return ao - bo;
      return peakDay(a[1]) - peakDay(b[1]);
    });
  }

  const groups = ['Sporadic', ...MONTH_FULL];
  let html = '';

  for (const month of groups) {
    const showers = byMonth[month];
    if (!showers.length) continue;
    const activeCount = showers.filter(([, i]) => isActiveNow(i)).length;
    const activePip = activeCount
      ? ` <span class="sh-section-active">${activeCount} active</span>` : '';
    html += `<section class="sh-section" data-month="${escHtml(month)}">
      <h2 class="sh-section-title">
        ${escHtml(month)}
        <span class="sh-section-count">${showers.length} shower${showers.length !== 1 ? 's' : ''}</span>
        ${activePip}
      </h2>
      <div class="sh-grid">
        ${showers.map(([c, i]) => buildCard(c, i, yearCounts[c] ?? null)).join('')}
      </div>
    </section>`;
  }

  if (other.length) {
    html += `<section class="sh-section" data-month="Other">
      <h2 class="sh-section-title">
        Other
        <span class="sh-section-count">${other.length} showers</span>
      </h2>
      <div class="sh-grid">
        ${other.map(([c, i]) => buildCard(c, i, yearCounts[c] ?? null)).join('')}
      </div>
    </section>`;
  }

  wrap.insertAdjacentHTML('beforeend', html);
  _updateCount();
}

/* ── Filter / search ──────────────────────────────────────────────────────── */

let _activeMonth = 'all';
let _searchQuery = '';
let _onlyDetections = false;

function _applyFilter() {
  const q = _searchQuery.toLowerCase();
  let visibleCount = 0;

  document.querySelectorAll('.sh-section').forEach(sec => {
    const monthMatch = _activeMonth === 'all' || sec.dataset.month === _activeMonth;
    if (!monthMatch) { sec.style.display = 'none'; return; }
    sec.style.display = '';

    let sectionVisible = false;
    sec.querySelectorAll('.sh-card').forEach(card => {
      let hide = false;
      if (q) {
        const match = (card.dataset.code || '').toLowerCase().includes(q) ||
                      (card.dataset.name || '').includes(q);
        if (!match) hide = true;
      }
      if (!hide && _onlyDetections) {
        const cnt = parseInt(card.dataset.count);
        // If count not yet loaded (NaN), keep card visible until counts arrive.
        if (!isNaN(cnt) && cnt === 0) hide = true;
      }
      card.style.display = hide ? 'none' : '';
      if (!hide) { sectionVisible = true; visibleCount++; }
    });

    if (!sectionVisible) sec.style.display = 'none';
  });

  _updateCount(visibleCount);
}

function _updateCount(n) {
  if (n == null) n = document.querySelectorAll('.sh-card').length;
  const el = document.getElementById('sh-count');
  if (el) el.textContent = `${n} shower${n !== 1 ? 's' : ''}`;
}

/* ── Init ─────────────────────────────────────────────────────────────────── */

(async function initAuth() {
  try {
    const auth = await (await fetch('/api/auth/status')).json();
    const el = document.getElementById('ov-auth');
    if (el) {
      if (auth.user) {
        const color = auth.admin ? 'var(--green)' : 'var(--blue)';
        el.innerHTML = `<span style="color:${color}">${escHtml(auth.user)}</span>
          <a href="/logout" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Logout</a>`;
      } else {
        el.innerHTML = `<a href="/login" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Login</a>`;
      }
    }
    if (auth.admin) {
      const adminEl = document.getElementById('logo-dd-admin');
      if (adminEl) adminEl.style.display = '';
      const socialEl = document.getElementById('logo-dd-social');
      if (socialEl) socialEl.style.display = '';
    }
  } catch (e) {}
})();

document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('sh-filter-bar')?.addEventListener('click', e => {
    const btn = e.target.closest('.sh-month-btn');
    if (!btn) return;
    document.querySelectorAll('.sh-month-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    _activeMonth = btn.dataset.month;
    _applyFilter();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  document.getElementById('sh-search')?.addEventListener('input', e => {
    _searchQuery = e.target.value.trim();
    _applyFilter();
  });

  document.getElementById('sh-only-detections')?.addEventListener('change', e => {
    _onlyDetections = e.target.checked;
    _applyFilter();
  });

  // Render cards immediately from MDC data; patch year counts in async.
  try {
    const r = await fetch('/api/showers');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const mdc = await r.json();
    if (!Object.keys(mdc).length) throw new Error('empty');
    render(mdc, {});
  } catch (_) {
    const el = document.getElementById('sh-loading');
    if (el) el.textContent = 'Could not load shower data. Please log in and try again.';
    return;
  }

  // Year counts: fetch independently, patch badges and data-count when ready.
  try {
    const r = await fetch('/api/shower-year-counts');
    if (!r.ok) return;
    const counts = await r.json();
    const year = new Date().getFullYear();
    // Stamp every card with its count (0 for those not in the response).
    document.querySelectorAll('.sh-card[data-code]').forEach(card => {
      card.dataset.count = counts[card.dataset.code] ?? 0;
    });
    for (const [code, n] of Object.entries(counts)) {
      if (!n) continue;
      const card = document.querySelector(`.sh-card[data-code="${code}"]`);
      if (!card) continue;
      let badges = card.querySelector('.sh-badges');
      if (!badges) continue;
      const existing = badges.querySelector('.sh-badge-count');
      const html = `<span class="sh-badge-count" title="${n} ROVIMEN detection${n !== 1 ? 's' : ''} in ${year}">${n} in ${year}</span>`;
      if (existing) existing.outerHTML = html;
      else badges.insertAdjacentHTML('beforeend', html);
    }
    // Re-apply filter now that counts are set (checkbox may already be checked).
    _applyFilter();
  } catch (_) { /* counts are optional */ }
});
