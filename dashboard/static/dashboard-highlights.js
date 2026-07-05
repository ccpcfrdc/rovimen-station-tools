import { state, openAppPanel, appPanelClose, escHtml, _modalOpen, _modalClose, showerFullName } from './dashboard-common.js';
import { VideoModal } from './dashboard-video-modal.js';

// ── Helpers ───────────────────────────────────────────────────────────────

function el(id) { return document.getElementById(id); }

function fmtShower(code) { return showerFullName(code); }

function fmtMag(v) {
    if (v == null) return '—';
    const n = parseFloat(v);
    return (n > 0 ? '+' : '') + n.toFixed(1);
}

function fmtDur(v) {
    if (v == null) return '—';
    return parseFloat(v).toFixed(1) + ' s';
}

function fmtUtc(iso) {
    if (!iso) return '—';
    const s = String(iso).replace(' ', 'T');
    const d = new Date(s.endsWith('Z') ? s : s + 'Z');
    return d.toISOString().slice(0, 19).replace('T', ' ') + ' UTC';
}

// ── Modal ─────────────────────────────────────────────────────────────────

let _hlTop10 = [];   // current top-10 array, set after each query
let _hlIdx   = -1;   // currently open card index

const _hlModal = new VideoModal(
    document.getElementById('hl-modal-container'),
    { trim: true, nav: true }
);
window._VideoModalCloseAll = () => VideoModal.closeAll();

function _hlOpenModal(idx) {
    const det = _hlTop10[idx];
    if (!det) return;
    _hlIdx = idx;

    const detOffset = det.detection_offset_s ?? null;
    const mag = det.peak_magnitude != null ? 'mag ' + fmtMag(det.peak_magnitude) : '';
    const sh  = fmtShower(det.shower || 'SPO');
    const sta = det.station_label || det.camera || '';

    _hlModal.open({
        src:       det.clip_url,
        title:     [sh, mag, sta, fmtUtc(det.time_utc)].filter(Boolean).join(' · '),
        detOffset,
        trimStart: detOffset != null ? Math.max(0, detOffset - 2) : 0,
        trimEnd:   detOffset != null ? detOffset + 5 : undefined,
        camera:    det.camera,
        date:      det.date,
        filename:  det.filename,
        loopDlPath: det.camera && det.date && det.filename
          ? `/loop_clip_archive/${det.camera}/${det.date}/${encodeURIComponent(det.filename)}`
          : null,
        download:  { onClick: () => _hlTrimDownload() },
        stack:     det.stack_url ? { url: det.stack_url } : null,
        detection: {
            mag_apparent:  det.peak_magnitude,
            shower:        det.shower || 'SPO',
            duration_s:    det.duration_s,
            time_utc:      det.time_utc,
            station:       det.station_label,
            camera:        det.camera,
            gmn_confirmed: det.gmn_confirmed,
        },
        nav: {
            hasPrev: idx > 0,
            hasNext: idx < _hlTop10.length - 1,
            onPrev:  () => _hlOpenModal(idx - 1),
            onNext:  () => _hlOpenModal(idx + 1),
        },
        onClose: () => { _hlIdx = -1; },
    });
}

function _hlCloseModal() {
    _hlModal.close();
    _hlIdx = -1;
}
window._hlCloseModal = _hlCloseModal;

async function _hlTrimDownload() {
    const ts = _hlModal.getTrimState();
    if (!ts) return;
    const dlBtn = _hlModal._el?.querySelector('.vm-download-btn');
    if (dlBtn) { dlBtn.disabled = true; dlBtn.textContent = 'Preparing…'; }
    try {
        const { camera, date, filename, start, end } = ts;
        const url = `/api/highlights/shortclip/${camera}/${date}/${encodeURIComponent(filename)}?ss=${start.toFixed(2)}&t=${(end - start).toFixed(2)}`;
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const cd = resp.headers.get('content-disposition') || '';
        const m  = cd.match(/filename="?([^";\n]+)"?/);
        const blob = await resp.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = m ? m[1] : 'clip.mp4';
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    } catch (e) {
        alert('Download failed: ' + e.message);
    } finally {
        if (dlBtn) { dlBtn.disabled = false; dlBtn.textContent = '↓ Download clip'; }
    }
}

// No-op — modal init is handled inside VideoModal constructor
function _hlModalInit() {}

// ── Bulk downloads ────────────────────────────────────────────────────────

function _hlDefaultTrim(det) {
    const offset = det.detection_offset_s ?? null;
    return {
        start: offset != null ? Math.max(0, offset - 2) : 0,
        end:   offset != null ? offset + 5 : 20,
    };
}

async function _hlDownloadAllClips() {
    const btn = el('hl-dl-all-clips');
    if (btn.disabled) return;
    btn.disabled = true;
    const clips = _hlTop10.filter(d => d.clip_url);
    for (let i = 0; i < clips.length; i++) {
        btn.textContent = `↓ Clip ${i + 1}/${clips.length}…`;
        const det = clips[i];
        const { start, end } = _hlDefaultTrim(det);
        const url = `/api/highlights/shortclip/${det.camera}/${det.date}/${encodeURIComponent(det.filename)}?ss=${start.toFixed(2)}&t=${(end - start).toFixed(2)}`;
        try {
            const resp = await fetch(url);
            if (!resp.ok) continue;
            const cd = resp.headers.get('content-disposition') || '';
            const m  = cd.match(/filename="?([^";\n]+)"?/);
            const blob = await resp.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = m ? m[1] : `meteor_clip_${i + 1}.mp4`;
            document.body.appendChild(a);
            a.click();
            setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        } catch (_) { /* skip */ }
    }
    btn.disabled = false;
    btn.textContent = '↓ All clips';
}

async function _hlDownloadAllStacks() {
    const btn = el('hl-dl-all-stacks');
    if (btn.disabled) return;
    btn.disabled = true;
    const stacks = _hlTop10.filter(d => d.stack_url);
    for (let i = 0; i < stacks.length; i++) {
        btn.textContent = `↓ Stack ${i + 1}/${stacks.length}…`;
        const det = stacks[i];
        const a = document.createElement('a');
        a.href = det.stack_url;
        a.download = det.stack_url.split('/').pop();
        document.body.appendChild(a);
        a.click();
        await new Promise(r => setTimeout(r, 400));
        a.remove();
    }
    btn.disabled = false;
    btn.textContent = '↓ All stacks';
}

// ── Rendering ─────────────────────────────────────────────────────────────

function setLoading(on) {
    const btn = el('hl-submit');
    btn.disabled = on;
    btn.textContent = on ? 'Loading…' : 'Load data';
    el('hl-overlay').style.display = on ? 'flex' : 'none';

    document.querySelectorAll('.hl-stat-value').forEach(el => {
        if (on) el.classList.add('loading');
        else    el.classList.remove('loading');
    });
    if (on) {
        el('hl-grid').innerHTML = '';
        el('hl-months-wrap').style.display = 'none';
        el('hl-result-wrap').style.display = 'block';
    }
}

function renderStats(data) {
    const map = {
        'hl-val-detections':    data.total_detections?.toLocaleString('ro-RO') ?? '—',
        'hl-val-events':        data.dual_station_events?.toLocaleString('ro-RO') ?? '—',
        'hl-val-orbits':        data.new_orbits?.toLocaleString('ro-RO') ?? '—',
        'hl-val-orbits-month':  data.orbits_month_to_date?.toLocaleString('ro-RO') ?? '—',
        'hl-val-shower':        fmtShower(data.main_shower),
        'hl-val-cameras':       data.active_cameras ?? '—',
        'hl-val-locations':     data.active_locations ?? '—',
        'hl-val-coverage':      data.coverage_90km != null ? data.coverage_90km.toFixed(1) + '%' : '—',
    };
    for (const [id, val] of Object.entries(map)) {
        const node = el(id);
        if (node) {
            node.textContent = val;
            node.classList.remove('loading');
        }
    }
}

function renderMonthly(data) {
    const wrap = el('hl-months-wrap');
    const tbody = el('hl-months-tbody');
    const totals = data.monthly_totals || [];
    if (totals.length <= 1) { wrap.style.display = 'none'; return; }

    tbody.innerHTML = totals.map(row => `
        <tr>
            <td>${escHtml(row.month)}</td>
            <td>${(row.detections || 0).toLocaleString('ro-RO')}</td>
            <td>${(row.events     || 0).toLocaleString('ro-RO')}</td>
        </tr>
    `).join('');
    wrap.style.display = 'block';
}

function renderTop10(data) {
    const grid     = el('hl-grid');
    const bulkBtns = el('hl-bulk-btns');
    _hlTop10 = data.top10 || [];
    if (!_hlTop10.length) {
        grid.innerHTML = '<div class="hl-empty">No multi-station detections with video available in the selected period.</div>';
        bulkBtns.style.display = 'none';
        return;
    }
    bulkBtns.style.display = 'flex';

    grid.innerHTML = _hlTop10.map((det, i) => {
        const thumb = det.thumbnail_url || det.stack_url;
        const mag   = fmtMag(det.peak_magnitude);
        const dur   = fmtDur(det.duration_s);
        const sh    = fmtShower(det.shower || 'SPO');
        const time  = fmtUtc(det.time_utc);
        const sta   = det.station_label || det.camera || '';

        return `
        <div class="hl-card" data-idx="${i}">
            <div class="hl-card-img-wrap">
                <div class="hl-card-rank">#${i + 1}</div>
                ${thumb
                    ? `<img class="hl-card-img" src="${escHtml(thumb)}" alt="Meteor" loading="lazy" onerror="this.style.display='none'">`
                    : `<div class="hl-card-img-placeholder">&#9732;</div>`
                }
                <div class="hl-card-play"></div>
            </div>
            <div class="hl-card-body">
                <div class="hl-card-mag">mag ${mag} · ${dur}</div>
                <div class="hl-card-meta">
                    <span>${escHtml(sh)}</span>
                    <span>${escHtml(sta)}</span>
                    <span>${escHtml(time)}</span>
                </div>
            </div>
        </div>`;
    }).join('');

    grid.querySelectorAll('.hl-card').forEach(card => {
        card.addEventListener('click', () => {
            const idx = parseInt(card.dataset.idx, 10);
            if (_hlTop10[idx]?.clip_url) _hlOpenModal(idx);
        });
    });
}

// ── Fetch & run ───────────────────────────────────────────────────────────

async function runQuery() {
    const start = el('hl-start').value;
    const end   = el('hl-end').value;
    if (!start || !end) { alert('Please select a date range.'); return; }
    if (start > end)    { alert('Start date must be before end date.'); return; }

    // Warn for long ranges
    const days = (new Date(end) - new Date(start)) / 86400000 + 1;
    const warn = el('hl-warn');
    if (days > 90) {
        warn.textContent = `⚠ ${days} zile — poate dura mai mult`;
        warn.style.display = 'inline';
    } else {
        warn.style.display = 'none';
    }

    setLoading(true);

    try {
        const resp = await fetch(`/api/highlights/data?start=${start}&end=${end}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.error || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        renderStats(data);
        renderMonthly(data);
        renderTop10(data);
    } catch (e) {
        el('hl-grid').innerHTML = `<div class="hl-error">Error: ${escHtml(String(e.message))}</div>`;
    } finally {
        setLoading(false);
    }
}

// ── Init ──────────────────────────────────────────────────────────────────

function init() {
    _hlModalInit();

    // Default date range: last 7 days
    const today = new Date();
    const fmt   = d => d.toISOString().slice(0, 10);
    const ago7  = new Date(today); ago7.setDate(today.getDate() - 7);
    el('hl-start').value = fmt(ago7);
    el('hl-end').value   = fmt(today);

    el('hl-submit').addEventListener('click', runQuery);
    el('hl-dl-all-clips').addEventListener('click', _hlDownloadAllClips);
    el('hl-dl-all-stacks').addEventListener('click', _hlDownloadAllStacks);

    ['hl-start', 'hl-end'].forEach(id => {
        el(id).addEventListener('keydown', e => { if (e.key === 'Enter') runQuery(); });
    });

}

(async function initAuth() {
    try {
        const auth = await (await fetch('/api/auth/status')).json();
        if (auth.admin) {
            const adminEl = document.getElementById('logo-dd-admin');
            if (adminEl) adminEl.style.display = '';
            const socialEl = document.getElementById('logo-dd-social');
            if (socialEl) socialEl.style.display = '';
        }
        const el   = document.getElementById('ov-auth');
        if (!el) return;
        if (auth.user) {
            const color = auth.admin ? 'var(--green)' : 'var(--blue)';
            el.innerHTML = `<span style="color:${color}">${escHtml(auth.user)}</span>
                <a href="/logout" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Logout</a>`;
        } else {
            el.innerHTML = `<a href="/login" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Login</a>`;
        }
    } catch (e) {}
})();

window._hlCloseModal = _hlCloseModal;

init();
