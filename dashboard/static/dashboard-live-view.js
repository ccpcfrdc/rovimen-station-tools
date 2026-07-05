import { _modalOpen, _modalClose, escHtml, fetchJson } from './dashboard-common.js';
/*
 * dashboard-live-view.js
 * ──────────────────────
 * Live View page: auto-refreshing grid of station thumbnails with
 * full-size modal viewer. Extracted from live_view.html inline script.
 */

let lvRefreshTimer = null;

function lvCloseModal(e) {
  if (e && e.target !== document.getElementById('lv-modal')) return;
  _modalClose(document.getElementById('lv-modal'));
  document.getElementById('lv-modal-img').src = '';
}

function lvOpenModal(src) {
  document.getElementById('lv-modal-img').src = src;
  _modalOpen(document.getElementById('lv-modal'));
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') lvCloseModal(null);
});

async function lvFetchAndRender() {
  try {
    const data = await fetchJson('/api/live-feed');
    renderLiveView(data);
  } catch(e) {
    document.getElementById('lv-status').textContent = 'Error loading data: ' + e.message;
  }
}

function renderLiveView(data) {
  const grid = document.getElementById('lv-grid');
  const status = document.getElementById('lv-status');
  const entries = Object.entries(data);
  const onlineCount = entries.filter(([, v]) => v.online).length;
  const now = new Date().toLocaleTimeString('en-GB', {timeZone: 'UTC'});
  status.textContent = `${onlineCount}/${entries.length} stations online · Last refresh ${now} UTC`;

  grid.innerHTML = entries.map(([host, info]) => {
    const cls = info.online ? 'online' : 'offline';
    const dotCls = info.online ? 'online' : 'offline';

    let cameraSections = '';
    if (info.online && info.cameras) {
      for (const [camCode, chunks] of Object.entries(info.cameras)) {
        const thumbs = (chunks || []).slice(0, 5).map(c => {
          const isDetection = c.lock_type === 'detection';
          const detClass = isDetection ? ' detection' : '';
          if (c.stack) {
            const stackUrl = `/stack/${encodeURIComponent(host)}/${encodeURIComponent(camCode)}/${encodeURIComponent(c.date || '')}/${encodeURIComponent(c.stack)}`;
            return `<div class="lv-thumb${detClass}" data-src="${escHtml(stackUrl)}">
              <img src="${escHtml(stackUrl)}" loading="lazy" alt="${escHtml(camCode + ' ' + (c.time || ''))}"
                   onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
              <div class="lv-thumb-placeholder" style="display:none">No img</div>
              ${c.time ? `<div class="lv-thumb-time">${escHtml(c.time)}</div>` : ''}
            </div>`;
          }
          return `<div class="lv-thumb${detClass}">
            <div class="lv-thumb-placeholder">${escHtml(c.time || 'No stack')}</div>
          </div>`;
        }).join('');

        cameraSections += `<div class="lv-cam-section">
          <div class="lv-cam-code">${escHtml(camCode)}</div>
          ${thumbs ? `<div class="lv-thumbs">${thumbs}</div>` : '<div class="lv-no-data">No recent clips</div>'}
        </div>`;
      }
    } else if (!info.online) {
      cameraSections = '<div class="lv-no-data">Station offline</div>';
    }

    return `<div class="lv-card ${cls}">
      <div class="lv-card-header">
        <div class="lv-dot ${dotCls}"></div>
        <span class="lv-name">${escHtml(host)}</span>
        <span class="lv-label">${escHtml(info.label || '')}</span>
      </div>
      ${cameraSections}
    </div>`;
  }).join('');
}

document.getElementById('lv-grid').addEventListener('click', function(e) {
  const thumb = e.target.closest('.lv-thumb[data-src]');
  if (thumb) lvOpenModal(thumb.dataset.src);
});

// Initial load
lvFetchAndRender();

// Auto-refresh every 30s
lvRefreshTimer = setInterval(lvFetchAndRender, 30000);

/* ─────────────────────────────────────────
   Window exposure — onclick handlers in the HTML template reference
   these functions by name, so they must be on the global scope.
───────────────────────────────────────── */
window.lvCloseModal = lvCloseModal;
