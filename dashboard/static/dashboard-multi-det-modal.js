/**
 * MultiDetModal — synchronized multi-camera detection viewer.
 *
 * All videos are controlled globally (no per-video controls).
 * Click any video cell to focus it (big); click again to return to grid.
 * Controls: play/pause · ½× speed · ⏮ rewind · ⏮◆ seek-to-detection · track bar
 * Stitched download mirrors current layout (grid or focused).
 * Dome button: real-time WebGL2 all-sky projection from synchronized videos.
 */

// ── WebGL2 dome renderer ──────────────────────────────────────────────────────
// Inlined to avoid relative-import resolution issues with the import-map setup.
// Geometry matches celestial_dome.py: dome_xy_to_altaz → altaz_to_pixel.

const _DOME_VERT = /* glsl */`#version 300 es
in vec2 a_pos;
out vec2 v_uv;
void main() {
  v_uv = a_pos * 0.5 + 0.5;
  gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

const _DOME_FRAG = /* glsl */`#version 300 es
precision highp float;
const float PI           = 3.141592653589793;
const float HORIZON_FRAC = 0.93;
const int   MAX_CAMS     = 8;
const vec3  BG           = vec3(5.0/255.0, 5.0/255.0, 16.0/255.0);
uniform int       u_n;
uniform sampler2D u_tex[8];
// u_cam_a[i] = (az_centre_deg, alt_centre_deg, F_scale, rotation_from_horiz_deg)
// u_cam_b[i] = (X_res, Y_res, fov_h_deg, fov_v_deg)
uniform vec4 u_cam_a[8];
uniform vec4 u_cam_b[8];
in  vec2 v_uv;
out vec4 fragColor;
vec2 altazToCamUV(float alt_deg, float az_deg, vec4 a, vec4 b) {
  float alt0=radians(a.y), az0=radians(a.x), pa=radians(a.w), fs=a.z;
  float alt=radians(alt_deg), az=radians(az_deg);
  float daz=az-az0;
  float cos_rho=clamp(sin(alt)*sin(alt0)+cos(alt)*cos(alt0)*cos(daz),-1.0,1.0);
  float rho=acos(cos_rho);
  float y_pa=sin(daz)*cos(alt);
  float x_pa=cos(alt0)*sin(alt)-sin(alt0)*cos(alt)*cos(daz);
  float theta=atan(y_pa,x_pa);
  float rho_d=degrees(rho);
  float d_az=rho_d*sin(theta), d_alt=rho_d*cos(theta);
  float cr=cos(pa), sr=sin(pa);
  float dx=(d_az*cr+d_alt*sr)*fs, dy=(d_az*sr-d_alt*cr)*fs;
  return vec2((b.x*0.5+dx)/b.x, 1.0-(b.y*0.5+dy)/b.y);
}
// Samplers must be accessed with a constant index on many GPU drivers.
// Use a helper with explicit if-else so the compiler sees constant indices.
vec4 sampleTex(int idx, vec2 uv) {
  if (idx==0) return texture(u_tex[0],uv);
  if (idx==1) return texture(u_tex[1],uv);
  if (idx==2) return texture(u_tex[2],uv);
  if (idx==3) return texture(u_tex[3],uv);
  if (idx==4) return texture(u_tex[4],uv);
  if (idx==5) return texture(u_tex[5],uv);
  if (idx==6) return texture(u_tex[6],uv);
  return texture(u_tex[7],uv);
}
void main() {
  vec2 dc=v_uv-0.5;
  float r=length(dc);
  if (r>=0.5) { fragColor=vec4(0.0,0.0,0.0,1.0); return; }
  float r_norm=r/0.5;
  if (r_norm>HORIZON_FRAC) { fragColor=vec4(BG,1.0); return; }
  float alt_deg=90.0*(1.0-r_norm/HORIZON_FRAC);
  float az_deg=mod(degrees(atan(dc.x,-dc.y)),360.0);
  vec3 color=vec3(0.0); float total_w=0.0;
  for (int i=0; i<MAX_CAMS; i++) {
    if (i>=u_n) break;
    vec4 a=u_cam_a[i], b=u_cam_b[i];
    vec2 uv_c=altazToCamUV(alt_deg,az_deg,a,b);
    if (any(lessThan(uv_c,vec2(0.0)))||any(greaterThan(uv_c,vec2(1.0)))) continue;
    float alt_r=radians(alt_deg), az_r=radians(az_deg);
    float daz_w=az_r-radians(a.x);
    float cr_w=clamp(sin(alt_r)*sin(radians(a.y))+cos(alt_r)*cos(radians(a.y))*cos(daz_w),-1.0,1.0);
    float rho_w=degrees(acos(cr_w));
    float diag=0.5*sqrt(b.z*b.z+b.w*b.w);
    float w=clamp(1.0-pow(rho_w/diag,2.0),0.0,1.0);
    if (w<=0.0) continue;
    color+=sampleTex(i,uv_c).rgb*w; total_w+=w;
  }
  fragColor=total_w>1e-6 ? vec4(color/total_w,1.0) : vec4(BG,1.0);
}`;

class DomeWebGL {
  constructor(canvas) {
    this._canvas=canvas; this._cameras=[]; this._textures=[]; this._n=0;
    const gl=canvas.getContext('webgl2',{antialias:false,alpha:false});
    if (!gl) throw new Error('WebGL2 not supported');
    this._gl=gl;
    this._prog=_domeCompileProgram(gl,_DOME_VERT,_DOME_FRAG);
    _domeInitQuad(gl,this._prog);
  }
  setCameras(cameras) {
    const gl=this._gl;
    for (const t of this._textures) gl.deleteTexture(t);
    this._textures=[];
    const n=Math.min(cameras.length,8);
    this._n=n; this._cameras=cameras.slice(0,n);
    for (let i=0;i<n;i++) {
      const tex=gl.createTexture();
      gl.activeTexture(gl.TEXTURE0+i);
      gl.bindTexture(gl.TEXTURE_2D,tex);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
      this._textures.push(tex);
    }
    gl.useProgram(this._prog);
    gl.uniform1i(gl.getUniformLocation(this._prog,'u_n'),n);
    for (let i=0;i<8;i++) {
      const loc=gl.getUniformLocation(this._prog,`u_tex[${i}]`);
      if (loc!=null) gl.uniform1i(loc,i);
    }
    for (let i=0;i<n;i++) {
      const pp=cameras[i].platepar||{};
      const aLoc=gl.getUniformLocation(this._prog,`u_cam_a[${i}]`);
      const bLoc=gl.getUniformLocation(this._prog,`u_cam_b[${i}]`);
      if (aLoc) gl.uniform4f(aLoc, pp.az_centre??180, pp.alt_centre??45, pp.F_scale??2, pp.rotation_from_horiz??0);
      if (bLoc) gl.uniform4f(bLoc, pp.X_res??1920, pp.Y_res??1080, pp.fov_h??60, pp.fov_v??40);
    }
  }
  render() {
    const gl=this._gl, canvas=this._canvas;
    const p=canvas.parentElement;
    if (p) { const s=Math.min(p.clientWidth,p.clientHeight); if (canvas.width!==s||canvas.height!==s){canvas.width=canvas.height=s;} }
    for (let i=0;i<this._n;i++) {
      const v=this._cameras[i]?.videoEl;
      if (!v||v.readyState<2||v.videoWidth===0) continue;
      gl.activeTexture(gl.TEXTURE0+i);
      gl.bindTexture(gl.TEXTURE_2D,this._textures[i]);
      gl.texImage2D(gl.TEXTURE_2D,0,gl.RGB,gl.RGB,gl.UNSIGNED_BYTE,v);
    }
    gl.viewport(0,0,canvas.width,canvas.height);
    gl.useProgram(this._prog);
    gl.drawArrays(gl.TRIANGLE_STRIP,0,4);
  }
  destroy() {
    const gl=this._gl;
    for (const t of this._textures) gl.deleteTexture(t);
    gl.deleteProgram(this._prog);
    this._n=0; this._cameras=[]; this._textures=[];
  }
}

function _domeCompileProgram(gl,vSrc,fSrc) {
  const vs=_domeCompileShader(gl,gl.VERTEX_SHADER,vSrc);
  const fs=_domeCompileShader(gl,gl.FRAGMENT_SHADER,fSrc);
  const p=gl.createProgram();
  gl.attachShader(p,vs); gl.attachShader(p,fs); gl.linkProgram(p);
  if (!gl.getProgramParameter(p,gl.LINK_STATUS)) throw new Error('Dome link: '+gl.getProgramInfoLog(p));
  gl.deleteShader(vs); gl.deleteShader(fs);
  return p;
}
function _domeCompileShader(gl,type,src) {
  const s=gl.createShader(type);
  gl.shaderSource(s,src); gl.compileShader(s);
  if (!gl.getShaderParameter(s,gl.COMPILE_STATUS)) throw new Error('Dome shader: '+gl.getShaderInfoLog(s));
  return s;
}
function _domeInitQuad(gl,prog) {
  const buf=gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER,buf);
  gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,1,1]),gl.STATIC_DRAW);
  const loc=gl.getAttribLocation(prog,'a_pos');
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc,2,gl.FLOAT,false,0,0);
}

function _domeWebgl2Supported() {
  try { return !!document.createElement('canvas').getContext('webgl2'); }
  catch(_) { return false; }
}
const _WEBGL2 = _domeWebgl2Supported();

import { state } from './dashboard-common.js';

const _CAM_RE  = /^[A-Z0-9]{1,16}$/;
const _DATE_RE = /^\d{8}$/;
const _FILE_RE = /^[A-Za-z0-9_]+\.(mkv|mp4)$/;

/** True for an anonymous public visitor (no logged-in session). */
function _mdmIsAnon() {
  return !state.AUTH_USER;
}

export class MultiDetModal {
  constructor(containerEl) {
    this._el         = containerEl;
    this._shell      = null;
    this._videos     = [];   // { el, offset, cam, date, filename, station, label }
    this._focusedIdx = -1;
    this._playing    = false;
    this._slowActive = false;
    this._winPre     = 5;
    this._winPost    = 5;
    this._trimStart  = null;
    this._trimEnd    = null;
    this._raf        = null;
    this._opts       = null;
    this._total      = 0;
    this._metaDone   = 0;
    this._seekDone   = 0;
    this._stackMode  = false;
    this._domeMode   = false;
    this._domeWebgl  = null;
    this._domeCanvas = null;
    this._platepars  = null;
    MultiDetModal._instances.add(this);
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  open(opts) {
    this._opts      = opts;
    this._platepars = opts.platepars || null;
    this._ensureMount();
    this._reset();

    this._winPre  = opts.windowPreSec  ?? opts.windowSec ?? 5;
    this._winPost = opts.windowPostSec ?? opts.windowSec ?? 5;
    this._trimStart = -this._winPre;
    this._trimEnd   =  this._winPost;
    this._q('.mdm-title').textContent = opts.title || '';
    this._setStatus('Loading videos…', true);
    this._q('.mdm-ctrl-row').style.visibility = 'hidden';

    // Hide detection button and trackbar marker when there is no detection reference.
    const hasDet = opts.showDetection ?? true;
    const detBtn = this._q('.mdm-det-btn');
    const detMrk = this._q('.mdm-trackbar-det');
    if (detBtn) detBtn.style.display = hasDet ? '' : 'none';
    if (detMrk) detMrk.style.display = hasDet ? '' : 'none';

    const videos = opts.videos || [];
    this._total    = videos.length;
    this._metaDone = 0;
    this._seekDone = 0;

    // Show stack toggle only when at least one video has a stack image.
    const hasStacks = videos.some(v => v.stackUrl);
    const stackBtn = this._q('.mdm-stack-btn');
    if (stackBtn) stackBtn.style.display = hasStacks ? '' : 'none';

    // "Download video grid" is an operator-only export — hide it (and remember
    // the anon state so _setStackMode below doesn't re-show it). Anonymous
    // visitors keep synchronized playback + stacks/dome view, download aside.
    this._anon = _mdmIsAnon();
    const stitchInit = this._q('.mdm-stitch-btn');
    if (stitchInit) stitchInit.style.display = this._anon ? 'none' : '';

    // Dome only when all videos are from the same station (platepar data is per-station).
    const videoStations = new Set(videos.filter(v => v.url).map(v => v.station).filter(Boolean));
    const domeBtn = this._q('.mdm-dome-btn');
    if (domeBtn) domeBtn.style.display = videoStations.size <= 1 ? '' : 'none';

    // Reset to video mode on every open.
    this._setStackMode(false);

    const grid = this._q('.mdm-grid');
    grid.innerHTML = '';
    this._setGridCols(videos.length);

    for (let i = 0; i < videos.length; i++) {
      const w      = videos[i];
      const offset = w.offset ?? 0;
      const cell   = document.createElement('div');
      cell.className   = 'mdm-cell';
      cell.dataset.idx = String(i);

      const video       = document.createElement('video');
      video.src         = w.url;
      video.preload     = 'auto';
      video.controls    = false;
      video.playsInline = true;
      video.muted       = true;
      video.addEventListener('click', e => { e.stopPropagation(); this._toggleFocus(i); });
      video.onerror = () => {
        cell.classList.add('mdm-cell-error');
        const d = document.createElement('div');
        d.className   = 'mdm-err-msg';
        d.textContent = 'Unavailable';
        cell.appendChild(d);
        this._onMeta();
      };

      this._videos.push({
        el: video, offset,
        cam: w.cam, date: w.date, filename: w.filename,
        station: w.station, label: w.label,
        downloadUrl: w.downloadUrl,
        stackUrl: w.stackUrl || null,
      });

      // Stack image — hidden until stack mode is activated.
      const img = document.createElement('img');
      img.className = 'mdm-stack-img';
      if (w.stackUrl) {
        img.src = w.stackUrl;
        img.addEventListener('click', e => { e.stopPropagation(); this._toggleFocus(i); });
      } else {
        img.classList.add('mdm-stack-img-missing');
      }

      cell.appendChild(video);
      cell.appendChild(img);
      grid.appendChild(cell);

      if (video.readyState >= 1) this._onMeta();
      else video.addEventListener('loadedmetadata', () => this._onMeta(), { once: true });
    }

    this._updateTrackBar();
    this._modalOpen();
    this._startRaf();
  }

  close() {
    this._stopRaf();
    this._globalPause();
    if (this._domeWebgl) { this._domeWebgl.destroy(); this._domeWebgl = null; }
    this._domeMode = false;
    for (const v of this._videos) { try { v.el.pause(); v.el.src = ''; } catch (_) {} }
    this._videos     = [];
    this._focusedIdx = -1;
    this._modalClose();
  }

  // ── Loading coordination ───────────────────────────────────────────────────

  _onMeta() {
    this._metaDone++;
    if (this._metaDone < this._total) return;
    const startT = -this._winPre;
    let anyQueued = false;
    for (const v of this._videos) {
      if (!isFinite(v.el.duration) || v.el.duration === 0) { this._onSeeked(); continue; }
      anyQueued = true;
      v.el.addEventListener('seeked', () => this._onSeeked(), { once: true });
      v.el.currentTime = Math.max(0, v.offset + startT);
    }
    if (!anyQueued) this._onSeeked();
  }

  _onSeeked() {
    this._seekDone++;
    this._setStatus(`Buffering… ${this._seekDone}/${this._total}`, true);
    if (this._seekDone < this._total) return;
    this._setStatus('', false);
    this._q('.mdm-ctrl-row').style.visibility = '';
    // Attempt autoplay; if the browser blocks it, fall back to paused state
    this._playing = true;
    const pp = this._q('.mdm-pp-btn');
    if (pp) pp.textContent = '⏸';
    const plays = this._videos
      .filter(v => isFinite(v.el.duration) && v.el.duration > 0)
      .map(v => v.el.play().catch(() => 'blocked'));
    if (plays.length === 0) { this._globalPause(); return; }
    Promise.all(plays).then(results => {
      if (results.every(r => r === 'blocked')) this._globalPause();
    });
  }

  // ── Shell ──────────────────────────────────────────────────────────────────

  _ensureMount() {
    if (this._shell) return;
    const div = document.createElement('div');
    div.innerHTML = `
<div class="mdm-backdrop" aria-hidden="true" style="display:none" role="dialog" aria-modal="true">
  <div class="mdm-inner">
    <div class="mdm-header">
      <span class="mdm-title"></span>
      <div class="mdm-header-actions">
        <button class="mdm-stack-btn" title="Switch to stacks" style="display:none">🖼 Stacks</button>
        <button class="mdm-dome-btn"  title="All-sky dome view" style="display:none">🌍 Dome</button>
        <button class="mdm-stitch-btn" title="Download stitched video grid">⬇ Download video grid</button>
        <button class="mdm-close-btn" aria-label="Close">✕</button>
      </div>
    </div>
    <div class="mdm-video-area">
      <div class="mdm-status" style="display:none"></div>
      <div class="mdm-grid"></div>
      <canvas class="mdm-dome-canvas" style="display:none"></canvas>
    </div>
    <div class="mdm-ctrl-row" style="visibility:hidden">
      <button class="mdm-btn mdm-pp-btn" title="Play / Pause">⏸</button>
      <button class="mdm-btn mdm-slow-btn" title="Half speed">½×</button>
      <button class="mdm-btn mdm-rewind-btn" title="Rewind to start">⏮</button>
      <button class="mdm-btn mdm-det-btn" title="Jump to detection">⏮◆</button>
      <div class="mdm-trackbar-wrap">
        <div class="mdm-trackbar">
          <div class="mdm-trackbar-fill"></div>
          <div class="mdm-trackbar-det"></div>
          <div class="mdm-trackbar-ph"></div>
          <div class="mdm-trackbar-trim-start"></div>
          <div class="mdm-trackbar-trim-end"></div>
        </div>
      </div>
      <span class="mdm-time-lbl"></span>
    </div>
  </div>
</div>`;
    this._shell = div.firstElementChild;
    this._el.appendChild(this._shell);
    this._bindShell();
  }

  _bindShell() {
    this._q('.mdm-close-btn').addEventListener('click', () => this.close());
    // this._shell IS the backdrop — use it directly (querySelector won't find itself)
    this._shell.addEventListener('click', e => {
      if (e.target === this._shell) this.close();
    });
    document.addEventListener('keydown', e => {
      if (this._shell?.getAttribute('aria-hidden') === 'false' && e.key === 'Escape') this.close();
    });
    this._q('.mdm-pp-btn').addEventListener('click',     () => this._globalTogglePlay());
    this._q('.mdm-slow-btn').addEventListener('click',   () => this._globalToggleSlow());
    this._q('.mdm-rewind-btn').addEventListener('click', () => this._globalRewind());
    this._q('.mdm-det-btn').addEventListener('click',    () => this._globalSeekToDet());
    this._q('.mdm-stack-btn').addEventListener('click',  () => this._toggleStackMode());
    this._q('.mdm-dome-btn').addEventListener('click',   () => this._toggleDomeMode());
    this._q('.mdm-stitch-btn').addEventListener('click', () => this._stitchDownload());

    const bar = this._q('.mdm-trackbar');
    const seek = e => {
      const { left, width } = bar.getBoundingClientRect();
      if (!width) return;
      const frac = Math.max(0, Math.min(1, (e.clientX - left) / width));
      this._globalSeekToT(-this._winPre + frac * (this._winPre + this._winPost));
    };
    let _trackActive = false;
    bar.addEventListener('pointerdown', e => {
      if (e.target !== bar && e.target !== this._q('.mdm-trackbar-fill')) return;
      e.preventDefault();
      _trackActive = true;
      bar.setPointerCapture(e.pointerId);
      seek(e);
    });
    bar.addEventListener('pointermove', e => { if (_trackActive) seek(e); });
    bar.addEventListener('pointerup',     () => { _trackActive = false; });
    bar.addEventListener('pointercancel', () => { _trackActive = false; });

    // Trim handle drags
    for (const [sel, which] of [['.mdm-trackbar-trim-start', 'start'], ['.mdm-trackbar-trim-end', 'end']]) {
      const handle = this._q(sel);
      if (!handle) continue;
      handle.addEventListener('pointerdown', e => {
        e.preventDefault(); e.stopPropagation();
        handle.setPointerCapture(e.pointerId);
        handle.onpointermove = ev => {
          const { left, width } = bar.getBoundingClientRect();
          if (!width) return;
          const totalWin = this._winPre + this._winPost;
          const t = -this._winPre + Math.max(0, Math.min(1, (ev.clientX - left) / width)) * totalWin;
          if (which === 'start') this._trimStart = Math.min(t, this._trimEnd - 0.5);
          else                    this._trimEnd   = Math.max(t, this._trimStart + 0.5);
          this._updateTrackBar();
        };
        handle.onpointerup = () => { handle.onpointermove = null; handle.onpointerup = null; };
      });
    }
  }

  _reset() {
    for (const v of this._videos) { try { v.el.pause(); v.el.src = ''; } catch (_) {} }
    this._videos = []; this._focusedIdx = -1;
    this._playing = false; this._slowActive = false;
    this._trimStart = null; this._trimEnd = null;
    this._metaDone = 0; this._seekDone = 0;
    const grid = this._q('.mdm-grid');
    if (grid) {
      grid.innerHTML = '';
      grid.classList.remove('mdm-focused', 'mdm-stack-mode');
      grid.style.gridTemplateRows = '';
    }
    this._q('.mdm-slow-btn')?.classList.remove('mdm-btn-active');
    const pp = this._q('.mdm-pp-btn');
    if (pp) pp.textContent = '⏸';
    const ctrl = this._q('.mdm-ctrl-row');
    if (ctrl) ctrl.style.visibility = '';
    const stitch = this._q('.mdm-stitch-btn');
    if (stitch) stitch.style.display = this._anon ? 'none' : '';
    const btn = this._q('.mdm-stack-btn');
    if (btn) btn.textContent = '🖼 Stacks';
    this._stackMode = false;
    this._setDomeMode(false);
  }

  _toggleStackMode() { this._setStackMode(!this._stackMode); }

  _setStackMode(on) {
    this._stackMode = on;
    const grid    = this._q('.mdm-grid');
    const ctrl    = this._q('.mdm-ctrl-row');
    const btn     = this._q('.mdm-stack-btn');
    const stitch  = this._q('.mdm-stitch-btn');
    if (grid)   grid.classList.toggle('mdm-stack-mode', on);
    if (ctrl)   ctrl.style.visibility = on ? 'hidden' : '';
    if (stitch) stitch.style.display  = (on || this._anon) ? 'none' : '';
    if (btn)    btn.textContent       = on ? '▶ Videos' : '🖼 Stacks';
    if (on) this._globalPause();
  }

  async _toggleDomeMode() {
    if (this._domeMode) { this._setDomeMode(false); return; }

    // Fetch platepars lazily on first activation (cached in this._platepars).
    if (!this._platepars && this._videos.length) {
      const host = this._videos[0].station;
      const btn  = this._q('.mdm-dome-btn');
      if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
      try {
        const r = await fetch(`/api/platepar/${host}`);
        if (r.ok) this._platepars = await r.json();
      } catch (_) { /* non-fatal */ }
      if (btn) { btn.disabled = false; btn.textContent = '🌍 Dome'; }
    }

    this._setDomeMode(true);
  }

  _setDomeMode(on) {
    this._domeMode = on;
    const grid    = this._q('.mdm-grid');
    const canvas  = this._q('.mdm-dome-canvas');
    const btn     = this._q('.mdm-dome-btn');

    if (!on) {
      if (canvas) canvas.style.display = 'none';
      if (grid)   grid.style.display   = '';
      if (btn)    btn.classList.remove('mdm-btn-active');
      if (this._domeWebgl) { this._domeWebgl.destroy(); this._domeWebgl = null; }
      return;
    }

    // Turning dome on: exit stack mode first
    if (this._stackMode) this._setStackMode(false);

    if (grid)   grid.style.display   = 'none';
    if (canvas) canvas.style.display = '';
    if (btn)    btn.classList.add('mdm-btn-active');

    // Build camera list: one entry per video, paired with its platepar
    const cameras = this._videos.map(v => ({
      videoEl:  v.el,
      platepar: this._platepars?.[v.cam] ?? null,
    })).filter(c => c.platepar && c.platepar.az_centre != null);

    if (!cameras.length) {
      this._setDomeMode(false);
      (window._toast || console.warn).call(null, 'No calibration data for these cameras');
      return;
    }

    try {
      if (!this._domeWebgl) this._domeWebgl = new DomeWebGL(canvas);
      this._domeWebgl.setCameras(cameras);
      this._domeWebgl.render();
    } catch (e) {
      console.error('DomeWebGL init failed:', e);
      this._setDomeMode(false);
      const msg = e.message?.includes('WebGL2') ? 'WebGL2 not available — try disabling Shields or hardware acceleration restrictions' : 'Dome render failed: ' + e.message;
      (window._toast || alert).call(null, msg);
    }
  }

  _q(sel) { return this._shell?.querySelector(sel); }

  _setStatus(msg, show) {
    const s = this._q('.mdm-status');
    if (!s) return;
    s.textContent   = msg;
    s.style.display = show ? '' : 'none';
  }

  _modalOpen() {
    this._shell.setAttribute('aria-hidden', 'false');
    this._shell.style.display = 'flex';
    document.body.style.overflow = 'hidden';
  }

  _modalClose() {
    if (!this._shell) return;
    this._shell.setAttribute('aria-hidden', 'true');
    this._shell.style.display = 'none';
    document.body.style.overflow = '';
  }

  // ── Grid columns ───────────────────────────────────────────────────────────

  _setGridCols(n) {
    const grid = this._q('.mdm-grid');
    if (!grid) return;
    const cols = n <= 2 ? n : n <= 4 ? 2 : 3;
    grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  }

  // ── Focus / Unfocus ────────────────────────────────────────────────────────
  // Cells stay in .mdm-grid at all times — CSS classes drive layout changes.
  // This avoids moving <video> elements (which resets them to black in Firefox/Chrome).

  _toggleFocus(idx) {
    if (this._focusedIdx === idx) this._unfocus();
    else this._focusVideo(idx);
  }

  _focusVideo(idx) {
    this._focusedIdx = idx;
    const grid = this._q('.mdm-grid');
    const n = this._videos.length;
    const sideCount = n - 1;

    // Mark which cell is focused
    grid.querySelectorAll('.mdm-cell').forEach(c => {
      c.classList.toggle('mdm-cell--focused', +c.dataset.idx === idx);
    });

    // Switch grid to focused layout: left column wide, right sidebar
    grid.classList.add('mdm-focused');
    grid.style.gridTemplateColumns = '1fr 184px';
    // 1fr rows so focused cell spans full container height; sidebar cells share proportionally
    grid.style.gridTemplateRows = `repeat(${sideCount || 1}, 1fr)`;
  }

  _unfocus() {
    this._focusedIdx = -1;
    const grid = this._q('.mdm-grid');
    grid.querySelectorAll('.mdm-cell').forEach(c => c.classList.remove('mdm-cell--focused'));
    grid.classList.remove('mdm-focused');
    grid.style.gridTemplateColumns = '';
    grid.style.gridTemplateRows = '';
    this._setGridCols(this._videos.length);
  }

  // ── Global playback ────────────────────────────────────────────────────────

  _globalPlay() {
    this._playing = true;
    const pp = this._q('.mdm-pp-btn');
    if (pp) pp.textContent = '⏸';
    for (const v of this._videos) v.el.play().catch(() => {});
  }

  _globalPause() {
    this._playing = false;
    const pp = this._q('.mdm-pp-btn');
    if (pp) pp.textContent = '▶';
    for (const v of this._videos) v.el.pause();
  }

  _globalTogglePlay() {
    if (this._playing) this._globalPause();
    else this._globalPlay();
  }

  _globalToggleSlow() {
    this._slowActive = !this._slowActive;
    const rate = this._slowActive ? 0.5 : 1.0;
    for (const v of this._videos) v.el.playbackRate = rate;
    this._q('.mdm-slow-btn')?.classList.toggle('mdm-btn-active', this._slowActive);
  }

  _globalRewind() {
    this._globalSeekToT(this._trimStart ?? -this._winPre);
    this._globalPlay();
  }

  _globalSeekToDet() {
    // 1 second before detection so user sees the approach
    this._globalSeekToT(-1);
    this._globalPlay();
  }

  _globalSeekToT(masterT) {
    for (const v of this._videos) {
      const t = Math.max(0, v.offset + masterT);
      if (isFinite(t)) v.el.currentTime = t;
    }
    this._updateTrackBar();
  }

  // ── RAF / track bar ────────────────────────────────────────────────────────

  _startRaf() {
    if (this._raf) return;
    const tick = () => { this._rafTick(); this._raf = requestAnimationFrame(tick); };
    this._raf = requestAnimationFrame(tick);
  }

  _stopRaf() {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
  }

  _rafTick() {
    this._updateTrackBar();
    if (this._domeMode && this._domeWebgl) this._domeWebgl.render();
    if (this._playing && this._videos.length > 0) {
      const stopT   = (this._trimEnd ?? this._winPost) + 0.15;
      const allDone = this._videos.every(v => v.el.paused || v.el.ended
        || (v.el.currentTime - v.offset) >= stopT);
      if (allDone) this._globalPause();
    }
  }

  _masterT() {
    for (const v of this._videos) {
      if (isFinite(v.el.currentTime) && v.el.duration > 0)
        return v.el.currentTime - v.offset;
    }
    return -this._winPre;
  }

  _updateTrackBar() {
    const mt       = this._masterT();
    const totalWin = this._winPre + this._winPost;
    const frac     = Math.max(0, Math.min(1, (mt + this._winPre) / totalWin));

    const ph = this._q('.mdm-trackbar-ph');
    if (ph) ph.style.left = (frac * 100).toFixed(2) + '%';

    const trimS = this._trimStart ?? -this._winPre;
    const trimE = this._trimEnd   ??  this._winPost;
    const sFrac = Math.max(0, Math.min(1, (trimS + this._winPre) / totalWin));
    const eFrac = Math.max(0, Math.min(1, (trimE + this._winPre) / totalWin));

    const fill = this._q('.mdm-trackbar-fill');
    if (fill) { fill.style.left = (sFrac * 100).toFixed(2) + '%'; fill.style.width = ((eFrac - sFrac) * 100).toFixed(2) + '%'; }

    const tsh = this._q('.mdm-trackbar-trim-start');
    const teh = this._q('.mdm-trackbar-trim-end');
    if (tsh) tsh.style.left = (sFrac * 100).toFixed(2) + '%';
    if (teh) teh.style.left = (eFrac * 100).toFixed(2) + '%';

    const det = this._q('.mdm-trackbar-det');
    if (det) det.style.left = ((this._winPre / totalWin) * 100).toFixed(2) + '%';

    const lbl = this._q('.mdm-time-lbl');
    if (lbl) lbl.textContent = (mt >= 0 ? '+' : '') + mt.toFixed(1) + 's';
  }

  // ── Stitched download ──────────────────────────────────────────────────────

  async _stitchDownload() {
    const btn     = this._q('.mdm-stitch-btn');
    const origTxt = btn.textContent;
    btn.disabled  = true;
    btn.textContent = '⏳ Composing…';
    try {
      const payload = {
        videos: this._videos.map(v => ({
          cam: v.cam, date: v.date, filename: v.filename, offset: v.offset,
        })),
        layout:       this._focusedIdx >= 0 ? 'focused' : 'grid',
        focused_idx:  this._focusedIdx,
        master_start: this._trimStart ?? -this._winPre,
        master_end:   this._trimEnd   ??  this._winPost,
      };
      const resp = await fetch('/api/stitch-multi', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        const j = await resp.json().catch(() => ({}));
        throw new Error(j.error || `HTTP ${resp.status}`);
      }
      const blob = await resp.blob();
      const a    = document.createElement('a');
      a.href     = URL.createObjectURL(blob);
      const slug = (this._opts?.title || '').replace(/[^a-z0-9]/gi, '_').slice(0, 24);
      a.download = `rovimen_multi_${slug || 'event'}.mp4`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
    } catch (e) {
      alert('Stitch failed: ' + e.message);
    } finally {
      btn.disabled    = false;
      btn.textContent = origTxt;
    }
  }

  // ── Static registry ────────────────────────────────────────────────────────

  static _instances = new Set();

  static closeAll() {
    for (const inst of MultiDetModal._instances) inst.close();
  }
}
