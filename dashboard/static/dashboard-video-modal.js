/**
 * dashboard-video-modal.js — shared video modal component.
 *
 * Each page instantiates VideoModal into a bare container div.
 * The constructor injects all HTML; the caller never touches the DOM directly.
 *
 * Single-video mode: VDB, Archive, Highlights, Overview GMN, RMS detections.
 * Multi-video mode:  Events page (synchronized multi-camera scrubber).
 *
 * Usage:
 *   import { VideoModal } from './dashboard-video-modal.js';
 *   const modal = new VideoModal(document.getElementById('my-container'), {
 *     trim: true, stitch: true, nav: true
 *   });
 *   modal.open({ src, title, detOffset, trimStart, trimEnd, detection, ... });
 *   modal.close();
 */

import { _modalOpen, _modalClose, _toast, state } from './dashboard-common.js';

/**
 * True for an anonymous public visitor (no logged-in session). Read off
 * state.AUTH_USER, which every page seeds from /api/auth/status BEFORE any
 * modal can open (see #648). Operator-only actions — Download clip, Crop,
 * loop/frame download, Show/Download stack, multi-download/stitch — are
 * hidden when this is true so the public sees playback controls only. The
 * server already gates those actions; this only stops us SHOWING dead
 * controls. Fail-safe: defaults to treating a missing signal as anon so we
 * never surface an operator control to an unauthenticated viewer.
 */
function _vmIsAnon() {
  return !state.AUTH_USER;
}

let _vmCount = 0;

export class VideoModal {
  static _instances = new Set();

  /**
   * @param {HTMLElement} mountEl  - Container div to inject into.
   * @param {object}      config
   *   trim   {boolean} - Show draggable trim handles.
   *   stitch {boolean} - Show stitch-prev/stitch-next row (VDB only).
   *   nav    {boolean} - Show prev/next navigation buttons in header.
   *   multi  {boolean} - Multi-video synchronized mode (events page).
   */
  constructor(mountEl, config = {}) {
    this._mount  = mountEl;
    this._id     = ++_vmCount;
    this._cfg    = {
      trim:   config.trim   ?? false,
      stitch: config.stitch ?? false,
      nav:    config.nav    ?? false,
      multi:  config.multi  ?? false,
    };
    this._el            = null;   // backdrop element
    this._opts          = null;   // current open() opts
    this._trim          = null;   // live trim-state object (single mode)
    this._raf           = null;   // playhead RAF id
    this._slowActive    = false;
    this._loopActive    = false;
    this._stackVisible  = false;
    this._infoExpanded  = false;
    this._frameTime     = null;   // intended position for frame-stepping
    this._frameSeeking  = false;  // true while a frame-step seek is in flight
    // Crop state
    this._cropActive    = false;
    this._cropBox       = null;   // { x, y, w, h } normalized 0-1 in video content space
    this._cropDragStart = null;   // pointer-down start norm coord
    // Multi-video state
    this._multiVideos   = [];
    this._multiPlaying  = false;
    this._multiRaf      = null;
    this._multiWinPre   = 5;
    this._multiWinPost  = 5;

    this._ensureMount();
    VideoModal._instances.add(this);
  }

  // ── Static helpers ─────────────────────────────────────────────────────────

  /** Close every open VideoModal instance. Called by _closeAllOpenModals(). */
  static closeAll() {
    for (const inst of VideoModal._instances) {
      if (inst._el?.classList.contains('open')) inst.close();
    }
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /**
   * Open the modal.
   *
   * Single-video opts:
   *   src          {string}   Video URL.
   *   title        {string}   Header title text.
   *   detOffset    {number}   Detection time within clip (seconds). Shows marker.
   *   trimStart    {number}   Initial trim-in point (seconds).
   *   trimEnd      {number}   Initial trim-out point (seconds).
   *   duration     {number}   Known duration hint (updated from video metadata).
   *   station, camera, date, filename  — forwarded to download handler.
   *   download     {object}   { onClick: async fn }
   *   stack        {object}   { url: string } — enables "Show stack".
   *   detection    {object}   RMS detection fields for collapsible info section.
   *   witnesses    {Array}    [{cam, station, url, time, label}] — other cameras.
   *   stitch       {object}   { hasPrev, hasNext, onPrev, onNext, onUnstitch }
   *   nav          {object}   { hasPrev, hasNext, onPrev, onNext }
   *   onClose      {function} Called when the modal closes.
   *
   * Multi-video opts:
   *   title        {string}
   *   videos       {Array}    [{cam, station, url, offset, label, downloadUrl, stackUrl, filename}]
   *   windowSec    {number}   Playback window ±N seconds around detection (default 5).
   */
  open(opts) {
    // Reset stack DOM before clearing _stackVisible — _openSingle checks it too late
    if (this._stackVisible && !this._cfg.multi) {
      this._q('.vm-video-el').style.display     = '';
      this._q('.vm-stack-img').style.display    = 'none';
      this._q('.vm-controls-row').style.display = '';
      const _sb = this._q('.vm-stack-show-btn');
      const _bb = this._q('.vm-backvideo-btn');
      const _sd = this._q('.vm-stack-dl-btn');
      if (_sb) _sb.style.display = '';
      if (_bb) _bb.style.display = 'none';
      if (_sd) _sd.style.display = 'none';
    }

    this._opts          = opts;
    this._slowActive    = false;
    this._loopActive    = false;
    this._stackVisible  = false;
    this._videoDeferred = false;
    this._infoExpanded  = false;
    this._frameTime     = null;
    this._frameSeeking  = false;
    this._multiPlaying  = false;

    if (this._cfg.multi) {
      this._openMulti(opts);
    } else {
      this._openSingle(opts);
    }
    _modalOpen(this._el);
  }

  /** Close the modal, pausing all media and clearing sources. */
  close() {
    if (!this._el?.classList.contains('open')) return;
    this._stopRaf();
    this._multiStopRaf();

    // Single-video cleanup
    const vid = this._q('.vm-video-el');
    if (vid) { vid.pause(); vid.src = ''; vid.load(); }

    // Multi-video cleanup
    for (const v of this._multiVideos) {
      try { v.el.pause(); v.el.src = ''; v.el.load(); } catch (_) {}
    }
    this._multiVideos = [];

    _modalClose(this._el);
    this._opts?.onClose?.();
  }

  /** Navigate prev (dir=-1) or next (dir=1). Single-video mode only. */
  navigate(dir) {
    const cb = dir < 0 ? this._opts?.nav?.onPrev : this._opts?.nav?.onNext;
    cb?.();
  }

  /**
   * Merge partial trim-state updates from outside (e.g. after VDB stitching).
   * Call after fetching a stitched video URL to update duration, stitchAt, etc.
   */
  updateTrim(partial) {
    if (!this._trim) return;
    Object.assign(this._trim, partial);
    this._trimDraw();
    this._durLabel();
  }

  /**
   * Update stitch row button states.
   * Call whenever the stitch availability changes (e.g. after a stitch operation).
   * @param {object} opts  { hasPrev, hasNext, label, isStitched }
   */
  updateStitch(opts) {
    const prevBtn = this._q('.vm-stitch-prev');
    const nextBtn = this._q('.vm-stitch-next');
    const unstBtn = this._q('.vm-stitch-unstitch');
    const label   = this._q('.vm-stitch-label');
    if (!prevBtn) return;
    if (opts.isStitched) {
      prevBtn.style.display = 'none';
      nextBtn.style.display = 'none';
      unstBtn.style.display = '';
      if (label) label.textContent = opts.label || '';
    } else {
      prevBtn.style.display = '';
      nextBtn.style.display = '';
      unstBtn.style.display = 'none';
      prevBtn.disabled = !opts.hasPrev;
      nextBtn.disabled = !opts.hasNext;
      if (label) label.textContent = '';
    }
  }

  /** Returns a snapshot of current trim state (used by download handlers). */
  getTrimState() {
    return this._trim ? { ...this._trim } : null;
  }

  /** Returns the <video> element (used by VDB RAF loop for time reading). */
  getVideoEl() {
    return this._q('.vm-video-el');
  }

  // ── Mount ──────────────────────────────────────────────────────────────────

  _ensureMount() {
    this._el = document.createElement('div');
    this._el.className = 'vm-backdrop';
    this._el.setAttribute('role', 'dialog');
    this._el.setAttribute('aria-modal', 'true');
    this._el.setAttribute('aria-hidden', 'true');
    this._el.dataset.vmId = this._id;
    this._el.innerHTML = this._html();
    this._mount.appendChild(this._el);
    this._bindShell();
  }

  _html() {
    const c = this._cfg;
    // Trim handle visibility is set via CSS display, not hidden attribute,
    // so pointer events don't fire when handles are hidden.
    const navHide = c.nav ? '' : 'display:none;';
    return `
<div class="vm-dialog">

  <div class="vm-header">
    <button class="vm-nav-btn vm-nav-prev" aria-label="Previous" style="${navHide}">&#8592;</button>
    <span class="vm-title"></span>
    <div class="vm-header-right">
      <div class="vm-multi-badges" style="display:none"></div>
      <div class="vm-multi-dl-btns" style="display:none"></div>
      <button class="vm-nav-btn vm-nav-next" aria-label="Next" style="${navHide}">&#8594;</button>
      <button class="vm-close-btn" aria-label="Close">&#10005; Close</button>
    </div>
  </div>

  <!-- ── Single-video layout ── -->
  <div class="vm-single-section" style="${c.multi ? 'display:none' : ''}">

    <div class="vm-video-section">
      <div class="vm-video-wrap">
        <video class="vm-video-el" playsinline></video>
        <img class="vm-stack-img" alt="FF stack" style="display:none">
        <div class="vm-video-loading">Loading video&#x2026;</div>
        <div class="vm-video-err" style="display:none">Clip not available</div>
        <canvas class="vm-crop-canvas"></canvas>
      </div>
    </div>

    <div class="vm-controls-row">
      <div class="vm-ctrl-btns">
        <button class="vm-ctrl-btn vm-btn-play" title="Play / Pause">&#9654;</button>
        <button class="vm-ctrl-btn vm-btn-slow" title="0.5&#215; slow motion">&#189;&#215;</button>
        <button class="vm-ctrl-btn vm-btn-rewind" title="Rewind to start">&#x23EE;</button>
        <button class="vm-ctrl-btn vm-btn-rewind-det" title="Jump to detection" style="display:none">&#x23EE;&#9670;</button>
        <button class="vm-ctrl-btn vm-btn-frame-back"  title="Previous frame">&#9664;&#9474;</button>
        <button class="vm-ctrl-btn vm-btn-frame-fwd"   title="Next frame">&#9474;&#9654;</button>
        <button class="vm-ctrl-btn vm-btn-loop" title="Loop between trim points">&#8635;</button>
      </div>
      <div class="vm-trackbar-wrap">
        <div class="vm-trackbar">
          <div class="vm-trackbar-fill"></div>
          <div class="vm-trackbar-det-marker"></div>
          <div class="vm-trackbar-stitch-marker"></div>
          <div class="vm-trackbar-playhead"></div>
          <div class="vm-trackbar-start-handle"></div>
          <div class="vm-trackbar-end-handle"></div>
        </div>
        <div class="vm-trackbar-labels">
          <span class="vm-lbl-start"></span>
          <span class="vm-lbl-det"></span>
          <span class="vm-lbl-end"></span>
        </div>
      </div>
      <div class="vm-time-display">
        <span class="vm-time-current">0:00</span>
        <span>/</span>
        <span class="vm-time-total">0:00</span>
      </div>
    </div>

    <div class="vm-stitch-row" style="display:none">
      <button class="vm-stitch-btn vm-stitch-prev">&#8592; Stitch prev</button>
      <span class="vm-stitch-label"></span>
      <button class="vm-stitch-btn vm-stitch-unstitch" style="display:none">&#10005; Unstitch</button>
      <button class="vm-stitch-btn vm-stitch-next">Stitch next &#8594;</button>
    </div>

    <div class="vm-actions-row">
      <span class="vm-dur-label"></span>
      <button class="vm-action-btn vm-switch-multi-btn" style="display:none">All stations &#8594;</button>
      <button class="vm-action-btn vm-download-btn" style="display:none">&#8595; Download clip</button>
      <button class="vm-action-btn vm-frame-dl-btn" style="display:none">&#8595; Download frame</button>
      <div class="vm-loop-dl-wrap" style="display:none">
        <div class="vm-loop-dl-popover" style="display:none">
          <span class="vm-loop-dl-label">Iterations</span>
          <div class="vm-loop-stepper">
            <button class="vm-loop-step-btn vm-loop-step-dec">&#8722;</button>
            <input type="number" class="vm-loop-count" min="1" max="10" value="3" readonly>
            <button class="vm-loop-step-btn vm-loop-step-inc">+</button>
          </div>
          <button class="vm-action-btn vm-loop-dl-go">&#8595; Download</button>
        </div>
        <button class="vm-action-btn vm-loop-dl-btn">&#8593; Download loop</button>
      </div>
      <button class="vm-action-btn secondary vm-crop-btn" style="display:none">&#9701; Crop</button>
      <a class="vm-action-btn vm-stack-dl-btn" style="display:none" download>&#8595; Download stack</a>
      <button class="vm-action-btn secondary vm-stack-show-btn" style="display:none">&#9632; Show stack</button>
      <button class="vm-action-btn secondary vm-backvideo-btn" style="display:none">&#9654; Back to video</button>
    </div>

    <div class="vm-info-section" style="display:none">
      <div class="vm-info-summary">
        <span class="vm-info-summary-text"></span>
        <button class="vm-info-expand-btn">&#9660; Expand info</button>
      </div>
      <div class="vm-info-full">
        <div class="vm-info-grid"></div>
      </div>
    </div>

    <div class="vm-witnesses-section" style="display:none">
      <div class="vm-witnesses-label">Other cameras</div>
      <div class="vm-witnesses-list"></div>
    </div>

  </div><!-- end vm-single-section -->

  <!-- ── Multi-video layout ── -->
  <div class="vm-multi-section" style="${c.multi ? '' : 'display:none'}">
    <div class="vm-multi-status">Loading videos&#x2026;</div>
    <div class="vm-multi-grid"></div>
    <div class="vm-multi-bar" style="display:none">
      <button class="vm-ctrl-btn vm-multi-pp-btn">&#9654;</button>
      <div class="vm-multi-scrubber-wrap">
        <input type="range" class="vm-multi-scrubber" min="-500" max="500" value="-500" step="1">
        <div style="position:relative;height:0">
          <div class="vm-multi-scrubber-marker"></div>
        </div>
        <div class="vm-multi-scrubber-ticks"></div>
      </div>
      <span class="vm-multi-pos-label">&#8722;5.0s</span>
    </div>
  </div>

</div>`;
  }

  _q(sel) { return this._el?.querySelector(sel) ?? null; }

  // ── Shell event bindings ───────────────────────────────────────────────────

  _bindShell() {
    this._el.addEventListener('click', (e) => {
      if (e.target === this._el) this.close();
    });
    this._q('.vm-close-btn').addEventListener('click', () => this.close());

    if (this._cfg.nav) {
      this._q('.vm-nav-prev')?.addEventListener('click', () => this.navigate(-1));
      this._q('.vm-nav-next')?.addEventListener('click', () => this.navigate(1));
    }

    if (this._cfg.multi) {
      this._bindMultiControls();
    } else {
      this._bindSingleControls();
    }
  }

  _bindSingleControls() {
    const video = this._q('.vm-video-el');

    this._q('.vm-video-wrap').addEventListener('click', (e) => {
      // Don't fire if clicking on the error overlay
      if (e.target !== this._q('.vm-video-el') && e.target !== this._q('.vm-video-wrap')) return;
      this._togglePlay();
    });

    video.addEventListener('play',  () => {
      this._frameTime    = null;
      this._frameSeeking = false;
      this._q('.vm-video-wrap').classList.add('vm-playing');
      this._q('.vm-btn-play').innerHTML = '&#9646;&#9646;';
      this._syncFrameDlBtn(false);
    });
    video.addEventListener('pause', () => {
      this._q('.vm-video-wrap').classList.remove('vm-playing');
      this._q('.vm-btn-play').innerHTML = '&#9654;';
      this._syncFrameDlBtn(true);
    });
    video.addEventListener('ended', () => {
      this._q('.vm-video-wrap').classList.remove('vm-playing');
      this._q('.vm-btn-play').innerHTML = '&#9654;';
      this._syncFrameDlBtn(true);
    });
    video.addEventListener('error', () => {
      this._q('.vm-video-loading')?.style && (this._q('.vm-video-loading').style.display = 'none');
      const err = this._q('.vm-video-err');
      if (err) err.style.display = 'flex';
      video.controls = true;
    });
    video.addEventListener('seeked', () => {
      if (this._frameTime === null) { this._frameSeeking = false; return; }
      // If _frameTime moved while this seek was in flight, do one more seek
      if (Math.abs(video.currentTime - this._frameTime) > 0.001) {
        video.currentTime = this._frameTime;
      } else {
        this._frameSeeking = false;
      }
    });

    this._q('.vm-btn-play').addEventListener('click',       () => this._togglePlay());
    this._q('.vm-btn-slow').addEventListener('click',       () => this._toggleSlow());
    this._q('.vm-btn-rewind').addEventListener('click',     () => this._rewind());
    this._q('.vm-btn-rewind-det').addEventListener('click', () => this._rewindToDet());
    this._q('.vm-btn-frame-back').addEventListener('click', () => this._stepFrame(-1));
    this._q('.vm-btn-frame-fwd').addEventListener('click',  () => this._stepFrame(+1));
    this._q('.vm-btn-loop').addEventListener('click',       () => this._toggleLoop());
    this._q('.vm-frame-dl-btn')?.addEventListener('click',  () => this._downloadFrame());

    this._bindTrackDrag();

    this._q('.vm-stack-show-btn')?.addEventListener('click', () => this._toggleStack());
    this._q('.vm-backvideo-btn')?.addEventListener('click',  () => this._toggleStack());
    this._q('.vm-info-expand-btn')?.addEventListener('click', () => this._toggleInfo(!this._infoExpanded));
    this._q('.vm-crop-btn')?.addEventListener('click', () => this._toggleCrop());
    this._bindCropCanvas();

    // Stop clicks inside the popover from bubbling to the outside-click handler
    this._q('.vm-loop-dl-popover')?.addEventListener('click', (e) => e.stopPropagation());
    this._q('.vm-loop-dl-btn')?.addEventListener('click', (e) => {
      e.stopPropagation();
      const pop = this._q('.vm-loop-dl-popover');
      if (pop) pop.style.display = pop.style.display === 'none' ? '' : 'none';
    });
    this._q('.vm-loop-step-dec')?.addEventListener('click', () => {
      const inp = this._q('.vm-loop-count');
      if (inp) inp.value = Math.max(1, parseInt(inp.value, 10) - 1);
    });
    this._q('.vm-loop-step-inc')?.addEventListener('click', () => {
      const inp = this._q('.vm-loop-count');
      if (inp) inp.value = Math.min(10, parseInt(inp.value, 10) + 1);
    });
    this._q('.vm-loop-dl-go')?.addEventListener('click', () => {
      const n = Math.max(1, Math.min(10, parseInt(this._q('.vm-loop-count')?.value, 10) || 3));
      this._downloadLoopClip(n);
    });
    // Close popover when clicking outside the wrap
    document.addEventListener('click', () => {
      if (!this._el?.classList.contains('open')) return;
      const pop = this._q('.vm-loop-dl-popover');
      if (pop) pop.style.display = 'none';
    });
  }

  _bindMultiControls() {
    this._q('.vm-multi-pp-btn')?.addEventListener('click', () => this._multiTogglePlay());
    this._q('.vm-multi-scrubber')?.addEventListener('input', () => this._multiScrubDrag());
  }

  // ── Single-video open ──────────────────────────────────────────────────────

  _openSingle(opts) {
    const video = this._q('.vm-video-el');

    // Header
    this._q('.vm-title').textContent = opts.title || '';
    if (this._cfg.nav && opts.nav) {
      this._q('.vm-nav-prev').disabled = !opts.nav.hasPrev;
      this._q('.vm-nav-next').disabled = !opts.nav.hasNext;
    }

    // Reset video state — clear both video and stack image so old content is gone immediately
    this._stopRaf();
    video.pause();
    video.src = '';
    video.load();
    const _stackImgReset = this._q('.vm-stack-img');
    if (_stackImgReset) _stackImgReset.src = '';
    const _loadingEl = this._q('.vm-video-loading');
    if (_loadingEl) _loadingEl.style.display = '';
    video.controls = false;
    video.playbackRate = 1.0;
    this._q('.vm-btn-slow')?.classList.remove('vm-btn-slow-active');
    this._q('.vm-btn-loop')?.classList.remove('vm-btn-loop-active');
    this._q('.vm-video-wrap').classList.remove('vm-playing');
    this._q('.vm-btn-play').innerHTML = '&#9654;';
    this._q('.vm-video-err').style.display = 'none';

    // Trim state — initialised here, updated in loadedmetadata
    this._trim = {
      start:    opts.trimStart  ?? 0,
      end:      opts.trimEnd    ?? 20,
      duration: opts.duration   ?? 20,
      detOffset: opts.detOffset ?? null,
      stitched: false,
      stitchAt: null,
      filename2: null,
      stitchAdjIsLater: false,
      // forwarded fields used by caller download handlers
      station:  opts.station  || '',
      camera:   opts.camera   || '',
      date:     opts.date     || '',
      filename: opts.filename || '',
      videoSrc: opts.src      || '',
    };

    // Wire handles visibility based on config
    const sh = this._q('.vm-trackbar-start-handle');
    const eh = this._q('.vm-trackbar-end-handle');
    if (sh) sh.style.display = this._cfg.trim ? '' : 'none';
    if (eh) eh.style.display = this._cfg.trim ? '' : 'none';

    this._trimDraw();
    this._durLabel();
    this._startRaf();

    // Load video (skipped when opening directly in stack view — deferred to "Back to video")
    const loadingEl = _loadingEl;   // already shown in reset block above
    if (!this._videoDeferred) {
      video.src = opts.src || '';
      video.load();
      video.addEventListener('loadedmetadata', () => {
        const dur = video.duration || this._trim.duration;
        this._trim.duration = dur;
        if (opts.trimEnd == null) this._trim.end = dur;
        this._trimDraw();
        this._durLabel();
        if (this._trim.start > 0) video.currentTime = this._trim.start;
        video.play().catch(() => {});
      }, { once: true });
      video.addEventListener('loadeddata', () => {
        if (loadingEl) loadingEl.style.display = 'none';
      }, { once: true });
    }

    // Detection marker / rewind-to-det button
    const rwdDet = this._q('.vm-btn-rewind-det');
    if (rwdDet) rwdDet.style.display = opts.detOffset != null ? '' : 'none';

    // Stitch row
    if (this._cfg.stitch && opts.stitch) {
      this._q('.vm-stitch-row').style.display = '';
      const prevBtn = this._q('.vm-stitch-prev');
      const nextBtn = this._q('.vm-stitch-next');
      const unstBtn = this._q('.vm-stitch-unstitch');
      if (prevBtn) prevBtn.onclick = () => opts.stitch.onPrev?.();
      if (nextBtn) nextBtn.onclick = () => opts.stitch.onNext?.();
      if (unstBtn) unstBtn.onclick = () => opts.stitch.onUnstitch?.();
      this.updateStitch({
        hasPrev: opts.stitch.hasPrev,
        hasNext: opts.stitch.hasNext,
        isStitched: false,
      });
    } else {
      this._q('.vm-stitch-row').style.display = 'none';
    }

    // Download button
    const dlBtn = this._q('.vm-download-btn');
    if (dlBtn) {
      if (opts.download?.onClick) {
        dlBtn.style.display = '';
        dlBtn.onclick = () => opts.download.onClick();
      } else {
        dlBtn.style.display = 'none';
      }
    }

    // Frame download / loop download — frame-dl shown only when paused (video starts playing)
    const frameDlBtn = this._q('.vm-frame-dl-btn');
    const loopWrap   = this._q('.vm-loop-dl-wrap');
    const loopPop    = this._q('.vm-loop-dl-popover');
    if (frameDlBtn) frameDlBtn.style.display = 'none';
    if (loopWrap) loopWrap.style.display = opts.loopDlPath ? '' : 'none';
    if (loopPop)  loopPop.style.display  = 'none';

    // Stack — stackDl only revealed when in stack view (_toggleStack)
    const showBtn  = this._q('.vm-stack-show-btn');
    const stackDl  = this._q('.vm-stack-dl-btn');
    const stackImg = this._q('.vm-stack-img');
    if (opts.stack?.url) {
      if (stackImg) stackImg.src = opts.stack.url;
      if (showBtn)  showBtn.style.display = '';
      if (stackDl) {
        stackDl.style.display = 'none';
        stackDl.href = opts.stack.url;
        stackDl.setAttribute('download', opts.stack.url.split('/').pop() || 'stack');
      }
    } else {
      if (showBtn)  showBtn.style.display = 'none';
      if (stackDl)  stackDl.style.display = 'none';
    }

    // Open directly in stack view — show stack image, defer video to "Back to video"
    if (opts.startInStack && opts.stack?.url) {
      this._videoDeferred = true;
      this._stackVisible  = true;
      video.style.display = 'none';
      if (loadingEl) loadingEl.style.display     = 'none';
      if (stackImg)  stackImg.style.display       = 'block';
      if (showBtn)   showBtn.style.display        = 'none';
      if (stackDl && stackDl.href) stackDl.style.display = '';
      this._q('.vm-backvideo-btn').style.display  = '';
      this._q('.vm-controls-row').style.display   = 'none';
      if (dlBtn)     dlBtn.style.display          = 'none';
      if (frameDlBtn) frameDlBtn.style.display    = 'none';
      if (loopWrap)  loopWrap.style.display       = 'none';
    }

    // Switch-to-multi button
    const switchBtn = this._q('.vm-switch-multi-btn');
    if (switchBtn) {
      if (opts.switchToMulti?.onClick) {
        switchBtn.style.display = '';
        switchBtn.textContent = opts.switchToMulti.label || 'All stations →';
        switchBtn.onclick = () => opts.switchToMulti.onClick();
      } else {
        switchBtn.style.display = 'none';
      }
    }

    // Reset crop state
    this._cropActive = false;
    this._cropBox    = null;
    const cropCanvas = this._q('.vm-crop-canvas');
    if (cropCanvas) cropCanvas.style.display = 'none';
    this._q('.vm-crop-btn')?.classList.remove('vm-crop-btn-active');
    this._cropApplyLabels(false);
    this._q('.vm-crop-btn').style.display = '';

    // Anonymous public visitors get playback only — hide every operator-only
    // action (download / crop / loop+frame download / stack download+show).
    // Runs LAST so it overrides the per-control visibility set above, and the
    // matching multi-mode gate lives in _openMulti. Server-side these actions
    // are already gated; this just stops us painting dead controls.
    this._applyAnonControlGate();

    // Detection info section
    const infoSec = this._q('.vm-info-section');
    if (opts.detection) {
      this._renderDetection(opts.detection);
      infoSec.style.display = '';
      this._q('.vm-info-full').classList.remove('expanded');
      const expBtn = this._q('.vm-info-expand-btn');
      if (expBtn) expBtn.innerHTML = '&#9660; Expand info';
    } else {
      infoSec.style.display = 'none';
    }

    // Witnesses
    const witSec = this._q('.vm-witnesses-section');
    if (opts.witnesses?.length) {
      this._renderWitnesses(opts.witnesses);
      witSec.style.display = '';
    } else {
      witSec.style.display = 'none';
    }
  }

  /**
   * Hide operator-only single-video controls for an anonymous visitor.
   * No-op for logged-in operators — they keep the full action set. Called at
   * the end of _openSingle so it wins over the per-control visibility logic.
   */
  _applyAnonControlGate() {
    if (!_vmIsAnon()) return;
    for (const sel of [
      '.vm-download-btn',      // Download clip
      '.vm-frame-dl-btn',      // Download frame
      '.vm-loop-dl-wrap',      // Download loop (+ popover)
      '.vm-crop-btn',          // Crop
      '.vm-stack-dl-btn',      // Download stack
      '.vm-stack-show-btn',    // Show stack
    ]) {
      const el = this._q(sel);
      if (el) el.style.display = 'none';
    }
    // Never leave an anon session in crop mode.
    this._cropActive = false;
    this._cropBox = null;
    const cropCanvas = this._q('.vm-crop-canvas');
    if (cropCanvas) cropCanvas.style.display = 'none';
  }

  // ── Multi-video open ───────────────────────────────────────────────────────

  _openMulti(opts) {
    const WIN = opts.windowSec ?? 5;
    this._multiWinPre  = WIN;
    this._multiWinPost = WIN;
    this._multiVideos  = [];

    this._q('.vm-title').textContent = opts.title || '';
    this._q('.vm-multi-bar').style.display = 'none';
    const status = this._q('.vm-multi-status');
    status.textContent = 'Loading videos…';
    status.className = 'vm-multi-status';
    this._q('.vm-multi-grid').innerHTML = '';

    // Badges
    const badges = this._q('.vm-multi-badges');
    const dlBtns = this._q('.vm-multi-dl-btns');
    if (badges) badges.style.display = '';
    if (dlBtns) dlBtns.style.display = '';
    if (badges) badges.innerHTML = '';
    if (dlBtns) dlBtns.innerHTML = '';

    const videos = opts.videos || [];
    // Entries with no URL are rendered as placeholder tiles but don't count
    // toward the metadata/seek synchronisation barrier.
    const count = videos.filter(w => w.url).length;

    if (count > 1 && badges) {
      const stations = new Set(videos.filter(w => w.url).map(v => v.station));
      badges.innerHTML =
        `<span class="vm-badge">${count} cameras</span>` +
        `<span class="vm-badge">${stations.size} station${stations.size !== 1 ? 's' : ''}</span>`;
    }

    // Scrubber setup
    const scrubber = this._q('.vm-multi-scrubber');
    if (scrubber) {
      scrubber.min   = -WIN * 100;
      scrubber.max   =  WIN * 100;
      scrubber.value = -WIN * 100;
    }
    this._multiSetT(-WIN);

    // Tick marks
    const ticks = this._q('.vm-multi-scrubber-ticks');
    if (ticks) {
      const marks = [];
      for (let t = -WIN; t <= WIN; t++) marks.push(t === 0 ? '0' : (t > 0 ? '+' : '') + t + 's');
      ticks.innerHTML = marks.map(m => `<span>${m}</span>`).join('');
    }

    // Per-witness download / stack-download buttons in header — operator-only.
    // Anonymous visitors get synchronized multi-camera playback but no
    // download controls (server-side downloads are gated regardless).
    const _anon = _vmIsAnon();
    for (const w of videos) {
      if (_anon) break;
      if (w.downloadUrl && dlBtns) {
        const btn = document.createElement('button');
        btn.className = 'vm-action-btn secondary';
        btn.style.cssText = 'font-size:11px;padding:3px 9px';
        btn.textContent = '⤓ ' + w.cam;
        btn.title = 'Download ' + w.cam;
        btn.addEventListener('click', () => this._multiDownload(btn, w));
        dlBtns.appendChild(btn);
      }
      if (w.stackUrl && dlBtns) {
        const a = document.createElement('a');
        a.className = 'vm-action-btn secondary';
        a.style.cssText = 'font-size:11px;padding:3px 9px';
        a.textContent = '⊟ ' + w.cam;
        a.href = w.stackUrl;
        a.download = '';
        dlBtns.appendChild(a);
      }
    }

    // Grid columns + video max-height by witness count
    const cols = count === 1 ? 1 : count <= 4 ? 2 : 3;
    const maxH = count === 1 ? '520px' : count === 2 ? '400px' : count <= 4 ? '300px' : '220px';
    const grid = this._q('.vm-multi-grid');
    grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;

    // Coordination counters
    const total = count;
    let metaDone = 0, seekDone = 0;

    const onSeeked = () => {
      seekDone++;
      status.textContent = `Buffering… ${seekDone}/${total}`;
      if (seekDone < total) return;
      status.className = 'vm-multi-status ready';
      status.textContent = 'Ready';
      this._q('.vm-multi-bar').style.display = 'flex';
      setTimeout(() => { status.className = 'vm-multi-status hidden'; }, 1500);
      this._multiPlay();
    };

    const onMeta = () => {
      metaDone++;
      if (metaDone < total) return;
      this._multiRecomputeBounds();
      const startT = -this._multiWinPre;
      this._multiSetT(startT);
      for (const v of this._multiVideos) {
        if (!isFinite(v.el.duration)) { onSeeked(); continue; }
        v.el.addEventListener('seeked', onSeeked, { once: true });
        v.el.currentTime = Math.max(0, v.offset + startT);
      }
    };

    for (const w of videos) {
      const offset = w.offset ?? 0;
      const cell  = document.createElement('div');
      cell.className = 'vm-multi-cell';

      const lbl = document.createElement('div');
      lbl.className = 'vm-multi-cell-label';
      let lblText = w.cam;
      if (w.label) lblText += ' · ' + w.label;
      if (offset !== 0) lblText += ' · @' + offset.toFixed(1) + 's';
      lbl.textContent = lblText;

      if (!w.url) {
        // Placeholder tile for cameras with no matching chunk — no countdown.
        const d = document.createElement('div');
        d.className = 'vm-multi-cell-empty';
        d.textContent = 'No clip in window';
        cell.appendChild(d);
        cell.appendChild(lbl);
        grid.appendChild(cell);
        continue;
      }

      const video = document.createElement('video');
      video.src     = w.url;
      video.preload = 'auto';
      video.controls = true;
      video.style.maxHeight = maxH;
      video.addEventListener('click', () => this._multiTogglePlay());
      video.onerror = () => {
        const d = document.createElement('div');
        d.style.cssText = 'display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:13px;height:80px';
        d.textContent = 'Video unavailable';
        cell.appendChild(d);
        onMeta(); // count as ready even on error
      };
      this._multiVideos.push({ el: video, offset });

      cell.appendChild(video);
      cell.appendChild(lbl);
      grid.appendChild(cell);

      if (video.readyState >= 1) onMeta();
      else video.addEventListener('loadedmetadata', onMeta, { once: true });
    }
  }

  async _multiDownload(btn, w) {
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {
      const resp = await fetch(w.downloadUrl);
      if (!resp.ok) { _toast('Download failed', 'error'); return; }
      const blob = await resp.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (w.filename || w.cam + '.mp4').replace(/\.mkv$/, '.mp4');
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    } catch (_) {
      _toast('Download failed', 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = origText;
    }
  }

  // ── Single-video playback ──────────────────────────────────────────────────

  _togglePlay() {
    const v = this._q('.vm-video-el');
    if (!v) return;
    if (v.paused) {
      if (v.ended) v.currentTime = this._trim?.start ?? 0;
      v.play().catch(() => {});
    } else {
      v.pause();
    }
  }

  _toggleSlow() {
    const v = this._q('.vm-video-el');
    if (!v) return;
    this._slowActive = !this._slowActive;
    v.playbackRate = this._slowActive ? 0.5 : 1.0;
    this._q('.vm-btn-slow')?.classList.toggle('vm-btn-slow-active', this._slowActive);
  }

  _rewind() {
    const v = this._q('.vm-video-el');
    if (!v) return;
    v.currentTime = 0;
    v.play().catch(() => {});
  }

  _rewindToDet() {
    const v = this._q('.vm-video-el');
    const det = this._trim?.detOffset;
    if (!v || det == null) return;
    v.currentTime = Math.max(0, det - 1);
    v.play().catch(() => {});
  }

  _stepFrame(dir) {
    const v = this._q('.vm-video-el');
    if (!v) return;
    v.pause();
    const fps = 25;
    const dur = this._trim?.duration || v.duration || 0;
    const base = this._frameTime ?? v.currentTime;
    this._frameTime = Math.max(0, Math.min(dur, base + dir / fps));
    // Only issue a seek if none is in flight; the seeked handler will
    // re-seek to the latest _frameTime if it moved while we were waiting.
    if (!this._frameSeeking) {
      this._frameSeeking = true;
      v.currentTime = this._frameTime;
    }
  }

  _syncFrameDlBtn(paused) {
    if (this._stackVisible) return;   // stack view has its own action set
    const frameDl  = this._q('.vm-frame-dl-btn');
    const loopWrap = this._q('.vm-loop-dl-wrap');
    const loopPop  = this._q('.vm-loop-dl-popover');
    const loopAvail = !!(this._opts?.loopDlPath);
    if (paused) {
      if (frameDl)  frameDl.style.display  = '';
      if (loopWrap) loopWrap.style.display = 'none';
      if (loopPop)  loopPop.style.display  = 'none';
    } else {
      if (frameDl)  frameDl.style.display  = 'none';
      if (loopWrap) loopWrap.style.display = loopAvail ? '' : 'none';
    }
  }

  _downloadFrame() {
    const v = this._q('.vm-video-el');
    if (!v || v.readyState < 2) return;
    if (this._cropActive && this._cropBox) {
      this._downloadCroppedFrame(this._frameTime ?? v.currentTime);
      return;
    }
    const t    = this._trim;
    const canvas = document.createElement('canvas');
    canvas.width  = v.videoWidth  || 1280;
    canvas.height = v.videoHeight || 720;
    canvas.getContext('2d').drawImage(v, 0, 0, canvas.width, canvas.height);
    canvas.toBlob(blob => {
      if (!blob) return;
      const filename = t?.filename || t?.videoSrc?.split('/').pop() || 'frame';
      const base     = filename.replace(/\.[^.]+$/, '');
      const sec      = (this._frameTime ?? v.currentTime).toFixed(2).replace('.', 's');
      const a        = document.createElement('a');
      a.href         = URL.createObjectURL(blob);
      a.download     = `${base}_${sec}.jpg`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    }, 'image/jpeg', 0.92);
  }

  async _downloadCroppedFrame(frameTime) {
    const ts   = this._trim;
    const crop = this._cropBox;
    if (!ts || !crop) return;
    const fn   = this._cropFilename();
    if (!fn) { _toast('Cannot determine file name', 'error'); return; }
    const btn  = this._q('.vm-frame-dl-btn');
    const orig = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
    try {
      const params = new URLSearchParams({
        x: crop.x.toFixed(4), y: crop.y.toFixed(4),
        w: crop.w.toFixed(4), h: crop.h.toFixed(4),
        ss: frameTime.toFixed(3),
      });
      const url  = `/download_cropped_frame/${ts.station}/${ts.camera}/${ts.date}/${encodeURIComponent(fn)}?${params}`;
      const resp = await fetch(url);
      if (!resp.ok) { _toast('Frame download failed', 'error'); return; }
      const blob = await resp.blob();
      const a    = document.createElement('a');
      a.href     = URL.createObjectURL(blob);
      const sec  = frameTime.toFixed(2).replace('.', 's');
      a.download = (fn.replace(/\.[^.]+$/, '') || 'crop') + `_crop_${sec}.jpg`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    } catch (_) {
      _toast('Frame download failed', 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = orig; }
    }
  }

  _toggleLoop() {
    this._loopActive = !this._loopActive;
    this._q('.vm-btn-loop')?.classList.toggle('vm-btn-loop-active', this._loopActive);
    // If loop just activated and video is past trim end, rewind to trim start
    if (this._loopActive) {
      const v = this._q('.vm-video-el');
      const end = this._trim?.end ?? (v?.duration ?? 0);
      if (v && v.currentTime >= end) v.currentTime = this._trim?.start ?? 0;
    }
  }

  async _downloadLoopClip(loops) {
    if (this._cropActive && this._cropBox) return this._downloadCroppedLoop(loops);
    const ts   = this._trim;
    const path = this._opts?.loopDlPath;
    if (!ts || !path) return;
    const go = this._q('.vm-loop-dl-go');
    const orig = go?.textContent;
    if (go) { go.disabled = true; go.textContent = '⏳'; }
    try {
      const p = new URLSearchParams({
        start: ts.start.toFixed(3), end: ts.end.toFixed(3), loops,
      });
      const resp = await fetch(`${path}?${p}`);
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        _toast(data.detail || 'Loop download failed', 'error');
        return;
      }
      const blob = await resp.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = ts.filename.replace(/\.[^.]+$/, '') + `_loop${loops}x.mp4`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
      const pop = this._q('.vm-loop-dl-popover');
      if (pop) pop.style.display = 'none';
    } catch (_) {
      _toast('Loop download failed', 'error');
    } finally {
      if (go) { go.disabled = false; go.textContent = orig; }
    }
  }

  // ── RAF / playhead / time display ──────────────────────────────────────────

  _startRaf() {
    if (this._raf) cancelAnimationFrame(this._raf);
    const tick = () => {
      this._rafTick();
      this._raf = requestAnimationFrame(tick);
    };
    this._raf = requestAnimationFrame(tick);
  }

  _stopRaf() {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
  }

  _rafTick() {
    const v   = this._q('.vm-video-el');
    const ph  = this._q('.vm-trackbar-playhead');
    const cur = this._q('.vm-time-current');
    const tot = this._q('.vm-time-total');
    const dur = this._trim?.duration || (v?.duration) || 0;

    if (v && ph && dur) {
      ph.style.left = `${Math.min(100, v.currentTime / dur * 100).toFixed(2)}%`;
    }
    if (cur) cur.textContent = this._fmtTime(v?.currentTime ?? 0);
    if (tot) tot.textContent = this._fmtTime(dur);

    // Loop: when active, wrap back to trim start when we hit trim end
    if (this._loopActive && v && !v.paused) {
      const end   = this._trim?.end   ?? dur;
      const start = this._trim?.start ?? 0;
      if (v.currentTime >= end) v.currentTime = start;
    }
  }

  _fmtTime(s) {
    if (!isFinite(s) || s < 0) return '0:00';
    const m = Math.floor(s / 60);
    return `${m}:${Math.floor(s % 60).toString().padStart(2, '0')}`;
  }

  // ── Track bar draw & drag ──────────────────────────────────────────────────

  _trimDraw() {
    const ts = this._trim;
    if (!ts?.duration) return;
    const pct = v => `${Math.max(0, Math.min(100, v / ts.duration * 100)).toFixed(2)}%`;

    const fill = this._q('.vm-trackbar-fill');
    if (fill) { fill.style.left = pct(ts.start); fill.style.width = pct(ts.end - ts.start); }

    const sh = this._q('.vm-trackbar-start-handle');
    const eh = this._q('.vm-trackbar-end-handle');
    if (sh) sh.style.left = pct(ts.start);
    if (eh) eh.style.left = pct(ts.end);

    const det = this._q('.vm-trackbar-det-marker');
    if (det) {
      const show = ts.detOffset != null && ts.detOffset >= 0 && ts.detOffset <= ts.duration;
      det.style.display = show ? 'block' : 'none';
      if (show) det.style.left = pct(ts.detOffset);
    }

    const sm = this._q('.vm-trackbar-stitch-marker');
    if (sm) {
      const show = ts.stitched && ts.stitchAt != null;
      sm.style.display = show ? 'block' : 'none';
      if (show) sm.style.left = pct(ts.stitchAt);
    }

    const ls = this._q('.vm-lbl-start');
    const ld = this._q('.vm-lbl-det');
    const le = this._q('.vm-lbl-end');
    if (ls) ls.textContent = ts.start.toFixed(1) + 's';
    if (le) le.textContent = ts.end.toFixed(1) + 's';
    if (ld) ld.textContent = ts.detOffset != null ? `◆ ${ts.detOffset.toFixed(1)}s` : '';
  }

  _durLabel() {
    const ts  = this._trim;
    const lbl = this._q('.vm-dur-label');
    if (!lbl || !ts) return;
    lbl.textContent = `${(ts.end - ts.start).toFixed(1)} s clip`;
  }

  _bindTrackDrag() {
    const bar   = this._q('.vm-trackbar');
    const video = this._q('.vm-video-el');

    // Click on empty bar to seek
    bar.addEventListener('pointerdown', (e) => {
      if (e.target !== bar && e.target !== this._q('.vm-trackbar-fill')) return;
      e.preventDefault(); e.stopPropagation();
      const { left, width } = bar.getBoundingClientRect();
      video.currentTime = Math.max(0, Math.min(1, (e.clientX - left) / width)) * (this._trim?.duration || 0);
    });

    // Playhead drag
    const ph = this._q('.vm-trackbar-playhead');
    ph.addEventListener('pointerdown', (e) => {
      e.preventDefault(); e.stopPropagation();
      ph.setPointerCapture(e.pointerId);
      ph.onpointermove = (ev) => {
        const { left, width } = bar.getBoundingClientRect();
        video.currentTime = Math.max(0, Math.min(1, (ev.clientX - left) / width)) * (this._trim?.duration || 0);
      };
      ph.onpointerup = () => { ph.onpointermove = null; ph.onpointerup = null; };
    });

    if (!this._cfg.trim) return;

    // Trim handle drags
    for (const [sel, key] of [['.vm-trackbar-start-handle','start'],['.vm-trackbar-end-handle','end']]) {
      const handle = this._q(sel);
      if (!handle) continue;
      handle.addEventListener('pointerdown', (e) => {
        e.preventDefault(); e.stopPropagation();
        handle.setPointerCapture(e.pointerId);
        handle.onpointermove = (ev) => {
          const { left, width } = bar.getBoundingClientRect();
          const t  = Math.max(0, Math.min(1, (ev.clientX - left) / width)) * (this._trim?.duration || 0);
          const ts = this._trim;
          if (!ts) return;
          if (key === 'start') ts.start = Math.min(t, ts.end   - 0.1);
          else                  ts.end   = Math.max(t, ts.start + 0.1);
          this._trimDraw();
          this._durLabel();
        };
        handle.onpointerup = () => { handle.onpointermove = null; handle.onpointerup = null; };
      });
    }
  }

  // ── Stack toggle ───────────────────────────────────────────────────────────

  _toggleStack() {
    const video     = this._q('.vm-video-el');
    const img       = this._q('.vm-stack-img');
    const showBtn   = this._q('.vm-stack-show-btn');
    const backBtn   = this._q('.vm-backvideo-btn');
    const stackDl   = this._q('.vm-stack-dl-btn');
    const ctrlRow   = this._q('.vm-controls-row');
    const actRow    = this._q('.vm-actions-row');
    const stitchRow = this._q('.vm-stitch-row');
    this._stackVisible = !this._stackVisible;
    if (this._stackVisible) {
      video.pause();
      video.style.display = 'none';
      this._stitchWasVisible = stitchRow && stitchRow.style.display !== 'none';
      if (ctrlRow)   ctrlRow.style.display   = 'none';
      if (stitchRow) stitchRow.style.display = 'none';
      if (img)       img.style.display       = 'block';
      if (showBtn)   showBtn.style.display   = 'none';
      if (backBtn)   backBtn.style.display   = '';
      if (stackDl && stackDl.href) stackDl.style.display = '';
      // Hide video-only action buttons while in stack view
      if (actRow) {
        actRow.querySelector('.vm-frame-dl-btn').style.display  = 'none';
        actRow.querySelector('.vm-loop-dl-wrap').style.display  = 'none';
        actRow.querySelector('.vm-download-btn').style.display  = 'none';
        actRow.querySelector('.vm-crop-btn').style.display = 'none';
      }
      // Deactivate crop when switching to stack
      if (this._cropActive) {
        this._cropActive = false;
        this._cropBox = null;
        this._q('.vm-crop-btn')?.classList.remove('vm-crop-btn-active');
        this._cropApplyLabels(false);
      }
    } else {
      video.style.display = '';
      if (ctrlRow)   ctrlRow.style.display   = '';
      if (stitchRow) stitchRow.style.display = this._stitchWasVisible ? '' : 'none';
      if (img)       img.style.display       = 'none';
      if (showBtn)   showBtn.style.display   = '';
      if (backBtn)   backBtn.style.display   = 'none';
      if (stackDl)   stackDl.style.display   = 'none';
      // Restore download-clip and crop buttons
      const dlBtn = this._q('.vm-download-btn');
      if (dlBtn && this._opts?.download?.onClick) dlBtn.style.display = '';
      const cropBtn = this._q('.vm-crop-btn');
      if (cropBtn) cropBtn.style.display = '';
      if (this._videoDeferred) {
        // First time showing video — load it now
        this._videoDeferred = false;
        const loadingEl = this._q('.vm-video-loading');
        if (loadingEl) loadingEl.style.display = '';
        video.src = this._opts?.src || '';
        video.load();
        video.addEventListener('loadedmetadata', () => {
          const dur = video.duration || this._trim?.duration;
          if (this._trim) {
            this._trim.duration = dur;
            if (this._opts?.trimEnd == null) this._trim.end = dur;
          }
          this._trimDraw();
          this._durLabel();
          if (this._trim?.start > 0) video.currentTime = this._trim.start;
          video.play().catch(() => {});
        }, { once: true });
        video.addEventListener('loadeddata', () => {
          if (loadingEl) loadingEl.style.display = 'none';
        }, { once: true });
      } else {
        video.play().catch(() => {});
      }
      // play event fires → _syncFrameDlBtn(false) restores the loop button
    }
  }

  // ── Info section ───────────────────────────────────────────────────────────

  _toggleInfo(expand) {
    this._infoExpanded = expand;
    const full = this._q('.vm-info-full');
    const btn  = this._q('.vm-info-expand-btn');
    if (!full) return;
    full.classList.toggle('expanded', expand);
    if (btn) btn.innerHTML = expand ? '&#9650; Collapse info' : '&#9660; Expand info';
    if (expand) {
      setTimeout(() => {
        this._el?.scrollTo({ top: this._el.scrollHeight, behavior: 'smooth' });
      }, 260);
    }
  }

  _renderDetection(det) {
    const fmt    = (v, p = 2) => v != null ? Number(v).toFixed(p) : '—';
    const fmtMag = v => v != null ? (Number(v) >= 0 ? '+' : '') + Number(v).toFixed(1) : '—';
    const fmtRD  = (ra, dec, p = 2) => {
      if (ra == null && dec == null) return '—';
      return `${ra  != null ? fmt(ra,  p) + '°' : '—'}, ` +
             `${dec != null ? (Number(dec) >= 0 ? '+' : '') + fmt(dec, p) + '°' : '—'}`;
    };

    const shower = det.shower && det.shower !== 'SPO' ? det.shower : 'Sporadic';

    // Collapsed 1-line summary
    const sumParts = [];
    if (det.mag_apparent != null) sumParts.push('Mag ' + fmtMag(det.mag_apparent));
    sumParts.push(shower);
    if (det.duration_s != null) sumParts.push(fmt(det.duration_s, 1) + 's');
    this._q('.vm-info-summary-text').textContent = sumParts.join(' · ');

    const fmtTime = t => t ? t.replace('T', ' ').substring(0, 19) + ' UTC' : null;
    const stationStr = det.station && det.camera && det.station !== det.camera
      ? `${det.station} (${det.camera})`
      : (det.station || det.camera || null);

    // Full grid rows
    const rows = [
      ['Time',          fmtTime(det.time_utc)],
      ['Station',       stationStr],
      ['Magnitude',     det.mag_apparent != null
        ? fmtMag(det.mag_apparent) + ' app' + (det.mag_absolute != null ? ' / ' + fmtMag(det.mag_absolute) + ' abs' : '')
        : null],
      ['Shower',        shower],
      ['Duration',      det.duration_s != null
        ? fmt(det.duration_s) + 's'
          + (det.angular_velocity != null ? ' · ' + fmt(det.angular_velocity, 1) + '°/s' : '')
          + (det.num_segments     != null ? ' · ' + det.num_segments + ' frames @ ' + (det.fps || 25) + ' fps' : '')
        : null],
      ['GMN confirmed', det.gmn_confirmed != null ? (det.gmn_confirmed ? 'Yes ✓' : 'No') : null],
      ['RA/Dec begin',  fmtRD(det.ra_beg,  det.dec_beg) !== '—' ? fmtRD(det.ra_beg,  det.dec_beg) : null],
      ['RA/Dec end',    fmtRD(det.ra_end,  det.dec_end) !== '—' ? fmtRD(det.ra_end,  det.dec_end) : null],
      ['Az/Elev begin', det.azim_beg != null ? fmt(det.azim_beg,1) + '° / ' + fmt(det.elev_beg,1) + '°' : null],
      ['Az/Elev end',   det.azim_end != null ? fmt(det.azim_end,1) + '° / ' + fmt(det.elev_end,1) + '°' : null],
      ['Radiant',       det.ra_radiant != null
        ? 'RA ' + fmt(det.ra_radiant,1) + '°, Dec ' + (Number(det.dec_radiant) >= 0 ? '+' : '') + fmt(det.dec_radiant,1) + '°'
          + (det.radiant_elev != null ? ' · elev ' + fmt(det.radiant_elev,0) + '°' : '')
        : null],
      ['Solar lon.',    det.solar_lon != null ? fmt(det.solar_lon,3) + '°' : null],
    ].filter(([, v]) => v != null);

    const grid = this._q('.vm-info-grid');
    if (grid) {
      grid.innerHTML = rows.map(([k, v]) =>
        `<span class="vm-info-label">${k}</span><span>${v}</span>`
      ).join('');
    }
  }

  // ── Crop box ───────────────────────────────────────────────────────────────

  _toggleCrop() {
    this._cropActive = !this._cropActive;
    const canvas = this._q('.vm-crop-canvas');
    const btn    = this._q('.vm-crop-btn');
    if (this._cropActive) {
      canvas.style.display = 'block';
      btn?.classList.add('vm-crop-btn-active');
      this._cropBox = null;
      this._cropDraw();
    } else {
      canvas.style.display = 'none';
      btn?.classList.remove('vm-crop-btn-active');
      this._cropBox = null;
      this._cropApplyLabels(false);
    }
  }

  _cropApplyLabels(cropped) {
    const dlBtn   = this._q('.vm-download-btn');
    const frameDl = this._q('.vm-frame-dl-btn');
    const loopBtn = this._q('.vm-loop-dl-btn');
    if (dlBtn)   dlBtn.textContent   = cropped ? '⬇ Download cropped clip' : '⬇ Download clip';
    if (frameDl) frameDl.textContent = cropped ? '⬇ Download cropped frame' : '⬇ Download frame';
    if (loopBtn) loopBtn.textContent = cropped ? '⬆ Download cropped loop' : '⬆ Download loop';
    // Wire onclick for download button
    if (dlBtn) {
      if (cropped) {
        dlBtn.onclick = () => this._downloadCropped();
      } else {
        dlBtn.onclick = () => this._opts?.download?.onClick?.();
      }
    }
  }

  _getVideoContentRect() {
    const v      = this._q('.vm-video-el');
    const canvas = this._q('.vm-crop-canvas');
    if (!v || !v.videoWidth || !canvas) return null;
    const cw = canvas.clientWidth;
    const ch = canvas.clientHeight;
    if (!cw || !ch) return null;
    const aspectV = v.videoWidth / v.videoHeight;
    const aspectC = cw / ch;
    let vw, vh, vx, vy;
    if (aspectV > aspectC) {
      vw = cw; vh = cw / aspectV; vx = 0; vy = (ch - vh) / 2;
    } else {
      vh = ch; vw = ch * aspectV; vy = 0; vx = (cw - vw) / 2;
    }
    return { x: vx, y: vy, w: vw, h: vh };
  }

  _cropCanvasToNorm(cx, cy) {
    const vr = this._getVideoContentRect();
    if (!vr) return null;
    return {
      x: Math.max(0, Math.min(1, (cx - vr.x) / vr.w)),
      y: Math.max(0, Math.min(1, (cy - vr.y) / vr.h)),
    };
  }

  _cropDraw() {
    const canvas = this._q('.vm-crop-canvas');
    if (!canvas) return;
    const cw = canvas.clientWidth;
    const ch = canvas.clientHeight;
    canvas.width  = cw;
    canvas.height = ch;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, cw, ch);

    const vr = this._getVideoContentRect();
    if (!vr) return;

    ctx.fillStyle = 'rgba(0,0,0,0.5)';
    ctx.fillRect(0, 0, cw, ch);

    if (this._cropBox) {
      const bx = vr.x + this._cropBox.x * vr.w;
      const by = vr.y + this._cropBox.y * vr.h;
      const bw = this._cropBox.w * vr.w;
      const bh = this._cropBox.h * vr.h;
      ctx.clearRect(bx, by, bw, bh);
      ctx.strokeStyle = '#29aaff';
      ctx.lineWidth = 2;
      ctx.strokeRect(bx + 1, by + 1, bw - 2, bh - 2);
      const hs = 8;
      ctx.fillStyle = '#29aaff';
      for (const [hx, hy] of [[bx, by], [bx + bw - hs, by], [bx, by + bh - hs], [bx + bw - hs, by + bh - hs]]) {
        ctx.fillRect(hx, hy, hs, hs);
      }
    }
  }

  _bindCropCanvas() {
    const canvas = this._q('.vm-crop-canvas');
    if (!canvas) return;
    canvas.addEventListener('pointerdown', (e) => {
      if (!this._cropActive) return;
      e.preventDefault(); e.stopPropagation();
      const rect = canvas.getBoundingClientRect();
      this._cropDragStart = this._cropCanvasToNorm(e.clientX - rect.left, e.clientY - rect.top);
      canvas.setPointerCapture(e.pointerId);
      this._cropBox = null;
      this._cropApplyLabels(false);
      this._cropDraw();
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!this._cropActive || !this._cropDragStart) return;
      const rect = canvas.getBoundingClientRect();
      const end  = this._cropCanvasToNorm(e.clientX - rect.left, e.clientY - rect.top);
      if (!end) return;
      const s  = this._cropDragStart;
      const vr = this._getVideoContentRect();
      // Constrain to 16:9 — compute w from mouse, derive h from aspect ratio
      // using the actual pixel dimensions of the video content area.
      let rawW = Math.abs(end.x - s.x);
      let rawH = rawW * (vr ? vr.w / vr.h : 16 / 9) * (9 / 16);
      // Clamp so the box stays within [0,1] in both axes
      const maxH = end.y > s.y ? 1 - s.y : s.y;
      if (rawH > maxH) { rawH = maxH; rawW = rawH * (vr ? vr.h / vr.w : 9 / 16) * (16 / 9); }
      const maxW = end.x > s.x ? 1 - s.x : s.x;
      rawW = Math.min(rawW, maxW);
      rawH = rawW * (vr ? vr.w / vr.h : 16 / 9) * (9 / 16);
      this._cropBox = {
        x: end.x > s.x ? s.x : s.x - rawW,
        y: end.y > s.y ? s.y : s.y - rawH,
        w: rawW, h: rawH,
      };
      this._cropDraw();
    });
    canvas.addEventListener('pointerup', () => {
      this._cropDragStart = null;
      if (this._cropBox && this._cropBox.w > 0.02 && this._cropBox.h > 0.02) {
        this._cropApplyLabels(true);
      } else {
        this._cropBox = null;
        this._cropDraw();
      }
    });
  }

  _cropFilename() {
    const ts = this._trim;
    return ts?.filename || ts?.videoSrc?.split('/').pop() || '';
  }

  async _downloadCropped() {
    const ts   = this._trim;
    const crop = this._cropBox;
    if (!ts || !crop) return;
    const fn   = this._cropFilename();
    if (!fn) { _toast('Cannot determine file name', 'error'); return; }
    const btn  = this._q('.vm-download-btn');
    const orig = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
    try {
      const params = new URLSearchParams({
        x: crop.x.toFixed(4), y: crop.y.toFixed(4),
        w: crop.w.toFixed(4), h: crop.h.toFixed(4),
        ss: ts.start.toFixed(3), t: Math.max(0.1, ts.end - ts.start).toFixed(3),
      });
      const url  = `/download_cropped/${ts.station}/${ts.camera}/${ts.date}/${encodeURIComponent(fn)}?${params}`;
      const resp = await fetch(url);
      if (!resp.ok) { _toast('Crop download failed', 'error'); return; }
      const blob = await resp.blob();
      const a    = document.createElement('a');
      a.href     = URL.createObjectURL(blob);
      a.download = (fn.replace(/\.[^.]+$/, '') || 'crop') + '_crop.mp4';
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    } catch (_) {
      _toast('Crop download failed', 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = orig; }
    }
  }

  async _downloadCroppedLoop(loops) {
    const ts   = this._trim;
    const crop = this._cropBox;
    if (!ts || !crop) return;
    const fn   = this._cropFilename();
    if (!fn) { _toast('Cannot determine file name', 'error'); return; }
    const go   = this._q('.vm-loop-dl-go');
    const orig = go?.textContent;
    if (go) { go.disabled = true; go.textContent = '⏳'; }
    try {
      const params = new URLSearchParams({
        x: crop.x.toFixed(4), y: crop.y.toFixed(4),
        w: crop.w.toFixed(4), h: crop.h.toFixed(4),
        ss: ts.start.toFixed(3), t: Math.max(0.1, ts.end - ts.start).toFixed(3),
        loops,
      });
      const url  = `/cropped_loop_clip/${ts.station}/${ts.camera}/${ts.date}/${encodeURIComponent(fn)}?${params}`;
      const resp = await fetch(url);
      if (!resp.ok) { _toast('Crop loop download failed', 'error'); return; }
      const blob = await resp.blob();
      const a    = document.createElement('a');
      a.href     = URL.createObjectURL(blob);
      a.download = (fn.replace(/\.[^.]+$/, '') || 'crop') + `_crop_loop${loops}x.mp4`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
      const pop = this._q('.vm-loop-dl-popover');
      if (pop) pop.style.display = 'none';
    } catch (_) {
      _toast('Crop loop download failed', 'error');
    } finally {
      if (go) { go.disabled = false; go.textContent = orig; }
    }
  }

  // ── Witnesses ──────────────────────────────────────────────────────────────

  _renderWitnesses(witnesses) {
    const list = this._q('.vm-witnesses-list');
    if (!list) return;
    list.innerHTML = '';
    for (const w of witnesses) {
      const btn = document.createElement('button');
      btn.className = 'vm-witness-btn';
      btn.textContent = '▶ ' + (w.label || w.cam);
      btn.title = [w.station, w.time].filter(Boolean).join(' · ');
      btn.addEventListener('click', () => {
        const video = this._q('.vm-video-el');
        if (video) { video.src = w.url; video.load(); video.play().catch(() => {}); }
        this._q('.vm-title').textContent = w.label || w.cam;
      });
      list.appendChild(btn);
    }
  }

  // ── Multi-video playback ───────────────────────────────────────────────────

  _multiGetT()  {
    const s = this._q('.vm-multi-scrubber');
    return s ? parseInt(s.value, 10) / 100 : 0;
  }

  _multiSetT(t) {
    const s   = this._q('.vm-multi-scrubber');
    const lbl = this._q('.vm-multi-pos-label');
    if (s)   s.value = Math.round(t * 100);
    if (lbl) lbl.textContent = (t >= 0 ? '+' : '') + t.toFixed(1) + 's';
  }

  _multiScrubDrag() {
    const t = this._multiGetT();
    this._multiSetT(t);
    this._multiSeekAll(t);
  }

  _multiSeekAll(t) {
    for (const v of this._multiVideos) {
      const target = Math.max(0, v.offset + t);
      if (isFinite(v.el.duration) && target <= v.el.duration) v.el.currentTime = target;
    }
  }

  _multiPlay() {
    this._multiPlaying = true;
    const btn = this._q('.vm-multi-pp-btn');
    if (btn) btn.innerHTML = '&#9646;&#9646;';
    const t = this._multiGetT();
    for (const v of this._multiVideos) {
      // Only start clips whose reference point has been reached (offset + t >= 0).
      // Late clips (negative offset, start after reference) are woken by the RAF.
      if (v.offset + t >= 0) v.el.play().catch(() => {});
    }
    this._multiStartRaf();
  }

  _multiPause() {
    this._multiPlaying = false;
    const btn = this._q('.vm-multi-pp-btn');
    if (btn) btn.innerHTML = '&#9654;';
    for (const v of this._multiVideos) v.el.pause();
    this._multiStopRaf();
  }

  _multiTogglePlay() {
    if (this._multiPlaying) {
      this._multiPause();
    } else {
      if (this._multiGetT() >= this._multiWinPost) {
        this._multiSetT(-this._multiWinPre);
        this._multiSeekAll(-this._multiWinPre);
      }
      this._multiPlay();
    }
  }

  _multiStartRaf() {
    if (this._multiRaf) cancelAnimationFrame(this._multiRaf);
    const tick = () => {
      if (!this._multiPlaying) return;
      // Leader = earliest-starting clip with known duration.
      let leader = null;
      for (const v of this._multiVideos) {
        if (isFinite(v.el.duration) && (!leader || v.offset > leader.offset)) leader = v;
      }
      if (leader) {
        const t = leader.el.currentTime - leader.offset;
        this._multiSetT(t);
        if (t >= this._multiWinPost) { this._multiPause(); return; }
        // Wake late clips (negative offset) when global time reaches their start.
        for (const v of this._multiVideos) {
          const local = v.offset + t;
          if (v.el.paused && local >= 0 && local < (isFinite(v.el.duration) ? v.el.duration : Infinity)) {
            v.el.currentTime = local;
            v.el.play().catch(() => {});
          }
        }
      }
      this._multiRaf = requestAnimationFrame(tick);
    };
    this._multiRaf = requestAnimationFrame(tick);
  }

  _multiStopRaf() {
    if (this._multiRaf) { cancelAnimationFrame(this._multiRaf); this._multiRaf = null; }
  }

  _multiRecomputeBounds() {
    const WIN = this._opts?.windowSec ?? 5;
    let minPre = Infinity, minPost = Infinity;
    for (const v of this._multiVideos) {
      if (!isFinite(v.el.duration)) continue;
      // Clips with negative offset start after the reference — they contribute
      // no pre-reference content (clamp to 0) but their full duration is post.
      minPre  = Math.min(minPre,  Math.max(0, v.offset));
      minPost = Math.min(minPost, v.el.duration - v.offset);
    }
    this._multiWinPre  = Math.min(WIN, isFinite(minPre)  ? minPre  : WIN);
    this._multiWinPost = Math.min(WIN, isFinite(minPost) ? minPost : WIN);
    const s = this._q('.vm-multi-scrubber');
    if (s) {
      s.min = Math.round(-this._multiWinPre  * 100);
      s.max = Math.round( this._multiWinPost * 100);
    }
  }
}
