/*
 * dashboard-common.js
 * ───────────────────
 * Shared utilities and state used across all dashboard pages. Loaded
 * first by every page; declares cross-page globals on `window`, page-
 * agnostic formatters, the logo/app-panel dropdown, and the UTC clock.
 *
 * Keep this file lean — it ships on every dashboard navigation.
 */

/* global closeCamModal, stackCloseModal, arcModalClose, ovPlotClose,
          lvCloseModal, vdbCloseModal, vdbCloseSyncedModal, closeLiveModal,
          closeModal */

/* ─────────────────────────────────────────
   Utility: clamp all <input type="date"> on the page to today (UTC).
   Captured nights can't be in the future. Call from each page init.
───────────────────────────────────────── */
export function clampDateInputsToToday(){
  const today = new Date().toISOString().slice(0,10); // UTC YYYY-MM-DD
  document.querySelectorAll('input[type="date"]').forEach(el => { el.max = today; });
}

/* ─────────────────────────────────────────
   State — populated from /api/stations
───────────────────────────────────────── */
export const state = {
  STATIONS_META: {},
  VDB_CAMERAS: {},
  IS_ADMIN: false,
  AUTH_USER: null,
  USER_ROLE: 'guest',
  USER_STATIONS: [],
  activeStation: null,
  // Overview map multi-select: set of station host keys currently selected.
  // activeStation tracks the most-recently-toggled host so legacy
  // single-station controls still resolve a host.
  selectedStations: new Set(),
  activeTab: 'rms',
  statusData: {},
  vitalsData: {},
  tlData: {},
};

/* ─────────────────────────────────────────
   Meteor shower IAU code → full name
   Source: IAU Meteor Data Center working list
───────────────────────────────────────── */
export const SHOWER_NAMES = {
  // ── Sporadic ────────────────────────────
  SPO: 'Sporadic',
  ANT: 'Antihelion Source',

  // ── January ─────────────────────────────
  QUA: 'Quadrantids',

  // ── February ────────────────────────────
  ACE: 'alpha Centaurids',

  // ── March ───────────────────────────────
  GNO: 'gamma Normids',

  // ── April ───────────────────────────────
  LYR: 'Lyrids',
  ELY: 'eta Lyrids',

  // ── May ─────────────────────────────────
  ETA: 'Eta Aquariids',
  XHE: 'chi Herculids',

  // ── June (mostly daytime) ───────────────
  ARI: 'Arietids',
  ZPE: 'zeta Perseids',
  BTA: 'beta Taurids',
  JBO: 'June Bootids',
  JPE: 'July Pegasids',

  // ── July ────────────────────────────────
  JXA: 'July xi Arietids',
  PAU: 'Piscis Austrinids',
  CAP: 'alpha Capricornids',
  SDA: 'Southern delta Aquariids',
  NDA: 'Northern delta Aquariids',

  // ── August ──────────────────────────────
  PER: 'Perseids',
  KCG: 'kappa Cygnids',
  MIC: 'Microscopiids',

  // ── September ───────────────────────────
  AUR: 'Aurigids',
  SPE: 'September epsilon Perseids',
  DSX: 'Daytime Sextantids',
  SSG: 'sigma Serpentids',

  // ── October ─────────────────────────────
  DRA: 'Draconids',
  ORI: 'Orionids',
  EGE: 'epsilon Geminids',
  STA: 'Southern Taurids',
  NTA: 'Northern Taurids',
  TAU: 'Taurids',
  LMI: 'Leonis Minorids',

  // ── November ────────────────────────────
  LEO: 'Leonids',
  AMO: 'alpha Monocerotids',
  AND: 'Andromedids',
  NOO: 'November Orionids',
  NPI: 'November iota Aurigids',

  // ── December ────────────────────────────
  MON: 'December Monocerotids',
  HYD: 'sigma Hydrids',
  GEM: 'Geminids',
  COM: 'Comae Berenicids',
  URS: 'Ursids',
  PHO: 'Phoenicids',
  ICE: 'iota Cassiopeiids',
};

export function showerFullName(code) {
  if (!code) return 'Sporadic';
  return SHOWER_NAMES[code.toUpperCase()] || code;
}

/* ─────────────────────────────────────────
   Formatters
───────────────────────────────────────── */
export function fmtBytes(b) {
  if (b == null) return '—';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(1) + '\u00a0' + u[i];
}
export function fmtDate(s) {
  if (!s) return '—';
  return s.slice(0,4) + '-' + s.slice(4,6) + '-' + s.slice(6,8);
}
export function parseFn(fn) {
  const m = fn.match(/_(\d{8})_(\d{6})/);
  if (!m) return '';
  return fmtDate(m[1]) + ' ' + m[2].slice(0,2)+':'+m[2].slice(2,4)+':'+m[2].slice(4,6)+' UTC';
}
export function barColor(pct) {
  return pct > 85 ? 'var(--red)' : pct > 70 ? 'var(--yellow)' : 'var(--green)';
}

/* ─────────────────────────────────────────
   App panel (About, etc.)
───────────────────────────────────────── */
const _APP_PANELS = {
  about: {
    title: 'About ROVIMEN',
    html: `<div class="app-panel-body-inner">
      <h3>ROVIMEN</h3>
      <p><strong style="color:var(--text)">ROVIMEN</strong> &mdash; the <em style="color:var(--blue);font-style:normal;font-weight:700">RO</em>manian <em style="color:var(--blue);font-style:normal;font-weight:700">VI</em>deo <em style="color:var(--blue);font-style:normal;font-weight:700">ME</em>teor <em style="color:var(--blue);font-style:normal;font-weight:700">NE</em>twork &mdash; started in 2013 as a small Romanian meteor-observation project and has since grown into a node of the <a href="https://globalmeteornetwork.org" target="_blank" rel="noopener">Global Meteor Network</a> (GMN). It runs automated all-sky cameras that capture and analyse meteor events every clear night across network member sites in Romania.</p>
      <h3>Dashboard</h3>
      <p>This dashboard provides real-time monitoring and management of all ROVIMEN stations. It includes live camera feeds, nightly RMS meteor detection results with per-camera breakdowns, automatic multi-station event correlation for triangulated trajectories, a searchable video database with colour-calibrated clip delivery, a long-term archive of stacked images and final data products, station health and vitals monitoring, and administrative tools for network configuration and user management.</p>
      <h3>Global Meteor Network</h3>
      <p>The <strong style="color:var(--text)">Global Meteor Network</strong> (GMN) is a science project that aims to establish a worldwide network of low-cost, all-sky meteor cameras that continuously monitor the night sky. Data collected from hundreds of stations is used to compute precise meteor trajectories, identify meteoroid streams, and study the composition of small solar system bodies. ROVIMEN is part of the Romanian node of the GMN, operating cameras in multiple locations across Romania.</p>
      <p>
        <a href="https://globalmeteornetwork.org" target="_blank" rel="noopener">globalmeteornetwork.org</a><br>
        <a href="https://github.com/CroatianMeteorNetwork/RMS" target="_blank" rel="noopener">RMS &mdash; Raspberry Pi Meteor Station (GitHub)</a>
      </p>
      <h3>Network Statistics</h3>
      <div id="about-network-stats" style="font-size:12px;color:var(--muted)">Loading coverage data&hellip;</div>
      <h3>Contact</h3>
      <p>&bull; ROVIMEN &mdash; <a href="mailto:your-network@example.org">your-network@example.org</a><br>
         &bull; Florin Dumitrescu &mdash; <a href="mailto:your-network@example.org">your-network@example.org</a><br>
         &bull; Alex Tudorica &mdash; <a href="mailto:your-network@example.org">your-network@example.org</a></p>
      <h3>License</h3>
      <p>&copy; 2013&ndash;2026 ROVIMEN. Source code licensed under the <a href="https://www.gnu.org/licenses/gpl-3.0.html" target="_blank" rel="noopener">GNU General Public License v3.0</a> &mdash; see the <code>LICENSE</code> file in the repository.</p>
    </div>`,
  },
};

export function openAppPanel(id) {
  document.getElementById('logo-dropdown')?.classList.remove('open');
  document.getElementById('logo-btn')?.setAttribute('aria-expanded', 'false');
  const panel = _APP_PANELS[id];
  if (!panel) return;
  // Not every template ships the app-panel DOM (events.html / live_view.html
  // don't). Bail quietly instead of throwing — those pages just don't host
  // the menu panels.
  const titleEl = document.getElementById('app-panel-title');
  const bodyEl  = document.getElementById('app-panel-body');
  const wrapEl  = document.getElementById('app-panel');
  if (!titleEl || !bodyEl || !wrapEl) return;
  titleEl.textContent = panel.title;
  bodyEl.innerHTML = panel.html;
  _modalOpen(wrapEl);
  if (id === 'about') _loadNetworkStats();
}

async function _loadNetworkStats() {
  const el = document.getElementById('about-network-stats');
  if (!el) return;
  try {
    const r = await fetch('/api/network-stats');
    if (!r.ok) { el.textContent = 'Could not load network statistics.'; return; }
    const d = await r.json();
    const s = d.network_summary || {};
    const dbl = d.double_station_coverage_km2 || {};
    const fmt = (v) => v != null ? Math.round(v).toLocaleString() : '—';
    const perStation = d.per_station || {};
    const stRows = Object.entries(perStation).map(([k, v]) =>
      `<tr><td style="font-family:'SF Mono',monospace;font-weight:600">${escHtml(k)}</td>` +
      `<td>${escHtml(v.label)}</td>` +
      `<td style="text-align:right">${v.cameras}</td>` +
      `<td style="text-align:right">${v.cameras_with_platepar}</td>` +
      `<td style="text-align:right">${fmt(v.covered_area_km2)}</td></tr>`
    ).join('');
    el.innerHTML = `
      <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:14px">
        <div><b style="font-size:18px;color:var(--text)">${s.total_stations}</b><br>Stations</div>
        <div><b style="font-size:18px;color:var(--text)">${s.total_cameras}</b><br>Cameras</div>
        <div><b style="font-size:18px;color:var(--text)">${s.cameras_with_platepar}</b><br>Calibrated</div>
      </div>
      <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:14px">
        <div><b style="font-size:16px;color:var(--blue)">${fmt(d.total_covered_area_km2)} km&sup2;</b><br>Total sky coverage at 100 km</div>
        <div><b style="font-size:16px;color:var(--green)">${fmt(dbl['100km'])} km&sup2;</b><br>Double-station at 100 km</div>
        <div><b style="font-size:16px;color:var(--text)">${fmt(d.atmospheric_volume_km3)} km&sup3;</b><br>Atmospheric volume (25&ndash;130 km)</div>
        <div><b style="font-size:16px;color:var(--yellow)">${fmt(d.expected_meteoric_flux_per_hour)}/hr</b><br>Expected sporadic flux (&gt;+4.5 mag)</div>
      </div>
      <p style="font-size:11px;color:var(--muted);margin-bottom:10px">Double-station coverage by altitude:</p>
      <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:14px">
        <div>25 km: <b>${fmt(dbl['25km'])} km&sup2;</b></div>
        <div>70 km: <b>${fmt(dbl['70km'])} km&sup2;</b></div>
        <div>100 km: <b>${fmt(dbl['100km'])} km&sup2;</b></div>
      </div>
      <details style="margin-top:10px">
        <summary style="cursor:pointer;color:var(--blue);font-weight:600;font-size:12px">Per-station coverage at 100 km</summary>
        <table style="width:100%;border-collapse:collapse;font-size:11px;margin-top:8px">
          <thead><tr style="color:var(--muted);text-align:left;border-bottom:1px solid var(--border)">
            <th style="padding:4px 8px">Station</th><th style="padding:4px 8px">Label</th>
            <th style="padding:4px 8px;text-align:right">Cameras</th>
            <th style="padding:4px 8px;text-align:right">Calibrated</th>
            <th style="padding:4px 8px;text-align:right">Area (km&sup2;)</th>
          </tr></thead>
          <tbody>${stRows}</tbody>
        </table>
      </details>
      <p style="font-size:10px;color:var(--muted);margin-top:10px">Computed from calibrated platepars using a 10-km grid. Sporadic flux scaled to +4.5 limiting mag from ZHR ~10/hr (Rendtel 2006).</p>`;
  } catch(e) {
    el.textContent = 'Could not load network statistics.';
  }
}

export function appPanelClose(e) {
  const wrapEl = document.getElementById('app-panel');
  if (!wrapEl) return;
  if (e && e.target !== wrapEl) return;
  _modalClose(wrapEl);
}

/* ─────────────────────────────────────────
   Modal accessibility — shared helpers
   ─────────────────────────────────────────
   Lives in dashboard-common.js (loaded by every page) so the same modal
   open/close path is available from any template — overview.html,
   events.html, live_view.html, dashboard.html — and from every lazy bundle.

   _modalOpen(el):  save the currently-focused element so we can restore it
                    on close, flip aria-hidden off, mark the modal `.open`,
                    lock body scroll (so the page beneath doesn't slide on
                    mobile swipe), move keyboard focus to the modal, and
                    install a Tab keydown handler that traps focus inside
                    the dialog.
   _modalClose(el): reverse all of the above, unlock body scroll, and
                    restore focus to the element that originally opened
                    the modal.

   Tab trapping loops focus between the first and last tabbable descendant
   of the modal. Shift+Tab on the first wraps to the last; Tab on the last
   wraps to the first. Buttons, links, inputs, selects, textareas and
   elements with explicit tabindex >= 0 are considered tabbable.

   Body scroll-lock uses a `position: fixed` strategy on <body> (instead of
   `overflow: hidden`) because iOS Safari ignores `overflow: hidden` on
   <body> when there's any momentum scroll. We save the scroll Y on first
   lock and restore it on final unlock so the user lands back where they
   were before the modal opened. A counter handles nested modals: the body
   stays locked as long as at least one modal is open.
───────────────────────────────────────── */
const _MODAL_TABBABLE_SEL = [
  'a[href]:not([disabled])',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"]):not([disabled])',
].join(',');

function _modalTabbables(el) {
  return Array.from(el.querySelectorAll(_MODAL_TABBABLE_SEL))
    .filter(n => n.offsetParent !== null || n === document.activeElement);
}

function _modalTrapHandler(el) {
  return function (e) {
    if (e.key !== 'Tab') return;
    const items = _modalTabbables(el);
    if (!items.length) { e.preventDefault(); el.focus(); return; }
    const first = items[0];
    const last  = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey) {
      if (active === first || !el.contains(active)) { e.preventDefault(); last.focus(); }
    } else {
      if (active === last || !el.contains(active))  { e.preventDefault(); first.focus(); }
    }
  };
}

if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

let _openModalCount = 0;
let _popstateClosing = false;

export function _lockBodyScroll() {
  if (_openModalCount === 0) {
    const y = window.scrollY || document.documentElement.scrollTop || 0;
    document.body.dataset.lockedScrollY = String(y);
    document.body.style.position = 'fixed';
    document.body.style.top = `-${y}px`;
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.width = '100%';
    document.body.style.overflow = 'hidden';
  }
  _openModalCount++;
}

export function _unlockBodyScroll() {
  _openModalCount = Math.max(0, _openModalCount - 1);
  if (_openModalCount === 0) {
    const y = parseInt(document.body.dataset.lockedScrollY || '0', 10);
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.width = '';
    document.body.style.overflow = '';
    delete document.body.dataset.lockedScrollY;
    window.scrollTo(0, y);
  }
}

export function _modalOpen(el) {
  if (!el) return;
  if (el.classList.contains('open')) return;
  el._opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  el.setAttribute('aria-hidden', 'false');
  el.classList.add('open');
  _lockBodyScroll();
  // Push a history entry on the first modal so the mobile back gesture
  // closes the modal instead of navigating away from the page.
  if (_openModalCount === 1) {
    history.pushState({ _rovModal: true }, '');
  }
  if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1');
  const items = _modalTabbables(el);
  (items[0] || el).focus({ preventScroll: true });
  el._trapHandler = _modalTrapHandler(el);
  el.addEventListener('keydown', el._trapHandler);
}

export function _modalClose(el) {
  if (!el) return;
  if (!el.classList.contains('open')) return;
  el.classList.remove('open');
  _unlockBodyScroll();
  el.setAttribute('aria-hidden', 'true');
  if (el._trapHandler) {
    el.removeEventListener('keydown', el._trapHandler);
    el._trapHandler = null;
  }
  // Pop the history entry we pushed in _modalOpen when the last modal
  // closes — but only when the close was programmatic (button click,
  // Escape key). If popstate triggered the close, the entry is already
  // gone and calling history.back() would navigate away.
  if (_openModalCount === 0 && !_popstateClosing) {
    if (history.state && history.state._rovModal) {
      // Delay one frame so the scroll-restore paint settles before the
      // back-navigation fires. Without this, some browsers briefly show
      // scroll=0 between the popstate and our scrollTo call.
      requestAnimationFrame(() => {
        if (history.state && history.state._rovModal) history.back();
      });
    }
  }
  const opener = el._opener;
  el._opener = null;
  if (opener && typeof opener.focus === 'function' && document.body.contains(opener)) {
    opener.focus({ preventScroll: true });
  }
}

// Expose to other bundles / inline template scripts that don't share
// lexical scope with this file.

/* ─────────────────────────────────────────
   Close-on-route-change helper
   ─────────────────────────────────────────
   Any page navigation (station switch, tab switch, browser back) should
   clear out modals that were left open — otherwise a video player or
   plot can persist on top of fresh content. Single source of truth so
   the various switch sites all dismiss the same set of modal classes.
───────────────────────────────────────── */
export function _closeAllOpenModals() {
  // Covers every modal class that uses the shared .open toggle:
  //   .cam-modal       — live cam, RMS plot, dashboard stack-modal
  //   .vm-backdrop     — shared VideoModal component (VDB, Archive, Highlights, Overview, Events)
  //   .mdm-backdrop    — MultiDetModal (Events multi-station, VDB All Cams)
  //   .app-panel       — Stats / GMN / About app panels
  //   .lv-modal        — live_view.html zoomed snapshot
  //   .ov-plot-modal   — overview plot zoom modal
  // Leaflet popups (.gmn-popup) are not full-screen modals — they're
  // dismissed by clicking the map or pressing Esc; intentionally skipped.
  //
  // Several modals own resources beyond the .open class (video buffering,
  // wheel/mousedown listeners, intervalled refreshes). Route through the
  // page-local "real close" function when one exists so those tear down
  // cleanly — falling back to bare _modalClose for plain dialogs.

  // Close all VideoModal instances first (they own their own teardown logic).
  if (typeof window._VideoModalCloseAll === 'function') window._VideoModalCloseAll();

  const dispatchers = {
    'cam-modal':      () => typeof closeCamModal       === 'function' ? closeCamModal()  : _modalClose(document.getElementById('cam-modal')),
    'stack-modal':    () => typeof stackCloseModal     === 'function' ? stackCloseModal() : _modalClose(document.getElementById('stack-modal')),
    'ov-plot-modal':  () => typeof ovPlotClose         === 'function' ? ovPlotClose()     : _modalClose(document.getElementById('ov-plot-modal')),
    'lv-modal':       () => typeof lvCloseModal        === 'function' ? lvCloseModal()    : _modalClose(document.getElementById('lv-modal')),
    'app-panel':      () => typeof appPanelClose       === 'function' ? appPanelClose()   : _modalClose(document.getElementById('app-panel')),
    'ov-live-modal':  () => typeof closeLiveModal      === 'function' ? closeLiveModal()  : _modalClose(document.getElementById('ov-live-modal')),
  };
  const open = document.querySelectorAll(
    '.cam-modal.open, .app-panel.open, ' +
    '.lv-modal.open, .ov-plot-modal.open, .ov-live-modal-wrap.open'
  );
  open.forEach(el => {
    const dispatch = dispatchers[el.id];
    try {
      if (dispatch) dispatch();
      else _modalClose(el);
    } catch (e) {
      // Last-resort fallback so a broken page-local close doesn't leave
      // a modal stuck on top of fresh content.
      _modalClose(el);
    }
  });
}

// Browser back/forward (and the system back gesture on mobile) should
// dismiss any open modal instead of navigating away. The _popstateClosing
// flag tells _modalClose not to call history.back() again (the entry was
// already consumed by the back gesture).
window.addEventListener('popstate', () => {
  if (_openModalCount > 0) {
    _popstateClosing = true;
    _closeAllOpenModals();
    _popstateClosing = false;
  }
});

/* ─────────────────────────────────────────
   HTML escape — used by every page that renders user-controlled strings.
   Escapes both element-text and attribute-value metacharacters so the same
   helper is safe in either context. Returns "" for null/undefined so callers
   can drop the `?? ''` boilerplate at every interpolation site.
   Canonical implementation; admin.html / events.html previously had their
   own copies (deduped 2026-05-25 as part of the XSS sweep, P1-1).
───────────────────────────────────────── */
export function escHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* ─────────────────────────────────────────
   UTC Clock + Next RMS Capture
───────────────────────────────────────── */
(function startUTCClock() {
  function tick() {
    const el = document.getElementById('utc-clock');
    if (!el) return;
    const now = new Date();
    const h = String(now.getUTCHours()).padStart(2, '0');
    const m = String(now.getUTCMinutes()).padStart(2, '0');
    const s = String(now.getUTCSeconds()).padStart(2, '0');
    el.textContent = `${h}:${m}:${s} UTC`;
  }
  tick();
  setInterval(tick, 1000);
})();

// ── Toast notification ─────────────────────────────────────────────────────────

export function _toast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast toast-' + (type || 'info');
  t.textContent = msg;
  Object.assign(t.style, {
    position: 'fixed', bottom: '24px', left: '50%', transform: 'translateX(-50%)',
    padding: '10px 20px', borderRadius: '6px', zIndex: '99999', fontSize: '13px',
    color: '#fff', background: type === 'error' ? '#c33' : '#333', opacity: '0.95',
    pointerEvents: 'none', transition: 'opacity 0.3s',
  });
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 400); }, 3600);
}

// ── Burger menu ───────────────────────────────────────────────────────────────

(function initBurgerMenu() {
  const nav = document.querySelector('.main-nav');
  if (!nav) return;
  const burger = document.createElement('button');
  burger.className = 'nav-burger';
  burger.setAttribute('aria-label', 'Toggle navigation');
  burger.setAttribute('aria-expanded', 'false');
  burger.innerHTML = '&#9776;';
  nav.before(burger);

  function toggle() {
    const open = nav.classList.toggle('nav-open');
    burger.setAttribute('aria-expanded', String(open));
    burger.innerHTML = open ? '&#x2715;' : '&#9776;';
  }
  function close() {
    nav.classList.remove('nav-open');
    burger.setAttribute('aria-expanded', 'false');
    burger.innerHTML = '&#9776;';
  }

  burger.addEventListener('click', e => { e.stopPropagation(); toggle(); });
  nav.addEventListener('click', e => { if (e.target.closest('a, button:not(.nav-burger)')) close(); });
  document.addEventListener('click', e => {
    if (!e.target.closest('.main-nav') && !e.target.closest('.nav-burger')) close();
  });
})();

/* Padlock icons as inline SVG. Rendered instead of the 🔒/🔓 emoji
   (U+1F512/U+1F513) because those codepoints only show up when the user's
   OS ships a colour-emoji font — many minimal Linux desktops and locked-down
   corporate machines don't, so the emoji rendered as tofu or nothing. SVG
   renders identically everywhere and inherits the button colour via
   `currentColor`. Sized in `em` so it scales with the host element's font-size. */
export const LOCK_ICON =
  '<svg class="lock-ico" viewBox="0 0 24 24" width="1em" height="1em" fill="none" ' +
  'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><rect x="4" y="10.5" width="16" height="11" rx="2.2"/>' +
  '<path d="M7.5 10.5V7a4.5 4.5 0 0 1 9 0v3.5"/></svg>';
export const UNLOCK_ICON =
  '<svg class="lock-ico" viewBox="0 0 24 24" width="1em" height="1em" fill="none" ' +
  'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><rect x="4" y="10.5" width="16" height="11" rx="2.2"/>' +
  '<path d="M7.5 10.5V7a4.5 4.5 0 0 1 8.5-2"/></svg>';

/** True if the current session may lock/unlock clips for `host`. Mirrors the
 *  server-side `@require_station` gate in auth.py: admin (fleet-wide) or a host
 *  that owns this station. visitor/press are read-only and guests are anonymous,
 *  so the server 403s their POST /api/lock — we don't render a clickable toggle
 *  for them. `host` is the same host_key the lock request is sent to, so the UI
 *  grant can never diverge from what the backend actually authorises. */
export function canToggleLock(host) {
  if (state.USER_ROLE === 'admin') return true;
  return state.USER_ROLE === 'host' && state.USER_STATIONS.includes(host);
}

/* ─────────────────────────────────────────
   fetchJson — guarded fetch wrapper
   ─────────────────────────────────────────
   All internal API calls must go through this helper instead of calling
   fetch(url).then(r => r.json()) directly. The raw pattern silently
   propagates any non-JSON body (login-redirect HTML, 5xx error pages) into
   a SyntaxError, which surfaces as a cryptic "Unexpected token '<'" rather
   than a meaningful error or a redirect to /login.

   Behaviour:
   - 401  → for a logged-in session (state.AUTH_USER set), redirect to /login
            (the session expired). For an ANONYMOUS visitor on a public page
            (no AUTH_USER), a 401 just means "this particular endpoint is
            login-gated" — the visitor was never logged in, so bouncing them
            to /login would eject them from a public page they're allowed to
            see (e.g. /events, whose curated detection feed stays gated). In
            that case we throw the typed error instead and let the caller's
            catch degrade gracefully. Either way a typed error is re-thrown so
            callers in Promise.all / try-catch can detect it.
   - !ok  → throws Error('HTTP <status>') so catch blocks show something useful.
   - non-JSON content-type → throws Error('Not JSON: <content-type>') to catch
            accidental HTML pages that slip through without a 401.
   - ok + JSON → returns parsed response body.

   opts is forwarded verbatim to fetch() so POST/PATCH/etc. work unchanged.
───────────────────────────────────────── */
export async function fetchJson(url, opts) {
  const res = await fetch(url, opts);
  if (res.status === 401) {
    // Only a genuine session expiry (we had a user) warrants a login bounce.
    // Anonymous public visitors stay on the page; the caller handles the 401.
    if (state.AUTH_USER) {
      window.location.href = '/login';
    }
    const err = new Error(state.AUTH_USER ? 'Session expired' : 'Unauthorized');
    err.status = 401;
    throw err;
  }
  if (!res.ok) {
    const err = new Error('HTTP ' + res.status);
    err.status = res.status;
    throw err;
  }
  const ct = res.headers.get('content-type') || '';
  if (!ct.includes('application/json') && !ct.includes('text/json')) {
    throw new Error('Not JSON: ' + ct);
  }
  return res.json();
}

// Expose to global scope for inline onclick handlers
window.openAppPanel = openAppPanel;
window.appPanelClose = appPanelClose;
window.fetchJson = fetchJson;
