/**
 * DomeWebGL — real-time all-sky dome projection in WebGL2.
 *
 * Projects N synchronized <video> elements onto an equidistant azimuthal
 * dome using each camera's platepar geometry.  Geometry is identical to
 * celestial_dome.py: dome_xy_to_altaz → altaz_to_pixel.
 *
 * Usage:
 *   const d = new DomeWebGL(canvasEl);
 *   d.setCameras([{ platepar, videoEl }, …]);   // up to 8
 *   // in RAF:
 *   d.render();
 *   // teardown:
 *   d.destroy();
 */

const _VERT = /* glsl */`#version 300 es
in vec2 a_pos;
out vec2 v_uv;
void main() {
  v_uv = a_pos * 0.5 + 0.5;
  gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

// HORIZON_FRAC / BACKGROUND_RGB must match celestial_dome.py
const _FRAG = /* glsl */`#version 300 es
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

// Returns camera UV for a sky point (az_deg, alt_deg).
// Matches altaz_to_pixel() in celestial_dome.py.
vec2 altazToCamUV(float alt_deg, float az_deg, vec4 a, vec4 b) {
  float alt0 = radians(a.y), az0 = radians(a.x);
  float pa   = radians(a.w), fs  = a.z;
  float alt  = radians(alt_deg), az = radians(az_deg);
  float daz  = az - az0;
  float cos_rho = clamp(sin(alt)*sin(alt0) + cos(alt)*cos(alt0)*cos(daz), -1.0, 1.0);
  float rho  = acos(cos_rho);
  float y_pa = sin(daz)*cos(alt);
  float x_pa = cos(alt0)*sin(alt) - sin(alt0)*cos(alt)*cos(daz);
  float theta = atan(y_pa, x_pa);
  float rho_d = degrees(rho);
  float d_az  = rho_d * sin(theta);
  float d_alt = rho_d * cos(theta);
  float cr = cos(pa), sr = sin(pa);
  float dx = (d_az*cr + d_alt*sr) * fs;
  float dy = (d_az*sr - d_alt*cr) * fs;
  float px = b.x * 0.5 + dx;
  float py = b.y * 0.5 + dy;
  // flip Y: WebGL textures have origin at bottom-left, images at top-left
  return vec2(px / b.x, 1.0 - py / b.y);
}

void main() {
  // Dome: centre at (0.5, 0.5), outer radius 0.5 in UV space
  vec2  dc     = v_uv - 0.5;
  float r      = length(dc);

  // Outside canvas circle → black border
  if (r >= 0.5) { fragColor = vec4(0.0, 0.0, 0.0, 1.0); return; }

  float r_norm = r / 0.5;
  // Between HORIZON_FRAC and canvas edge → background colour (below-horizon ring)
  if (r_norm > HORIZON_FRAC) { fragColor = vec4(BG, 1.0); return; }

  // dome_xy_to_altaz (equidistant: zenith at centre, horizon at HORIZON_FRAC)
  float alt_deg = 90.0 * (1.0 - r_norm / HORIZON_FRAC);
  float az_deg  = mod(degrees(atan(dc.x, -dc.y)), 360.0);

  vec3  color   = vec3(0.0);
  float total_w = 0.0;

  for (int i = 0; i < MAX_CAMS; i++) {
    if (i >= u_n) break;
    vec4 a = u_cam_a[i];
    vec4 b = u_cam_b[i];

    vec2 uv_c = altazToCamUV(alt_deg, az_deg, a, b);
    // Skip if outside camera frame
    if (any(lessThan(uv_c, vec2(0.0))) || any(greaterThan(uv_c, vec2(1.0)))) continue;

    // Blend weight: quadratic falloff from FOV centre → 0 at half-diagonal
    // Matches: w = clip(1 - (rho / rho_diag)^2, 0, 1) in celestial_dome.py
    float alt_r = radians(alt_deg), az_r = radians(az_deg);
    float daz_w = az_r - radians(a.x);
    float cr_w  = clamp(sin(alt_r)*sin(radians(a.y)) + cos(alt_r)*cos(radians(a.y))*cos(daz_w), -1.0, 1.0);
    float rho_w = degrees(acos(cr_w));
    float diag  = 0.5 * sqrt(b.z*b.z + b.w*b.w);  // hypot(fov_h/2, fov_v/2)
    float w     = clamp(1.0 - pow(rho_w / diag, 2.0), 0.0, 1.0);
    if (w <= 0.0) continue;

    color   += texture(u_tex[i], uv_c).rgb * w;
    total_w += w;
  }

  fragColor = total_w > 1e-6
    ? vec4(color / total_w, 1.0)
    : vec4(BG, 1.0);
}`;

export class DomeWebGL {
  constructor(canvas) {
    this._canvas   = canvas;
    this._cameras  = [];
    this._textures = [];
    this._n        = 0;

    const gl = canvas.getContext('webgl2', { antialias: false, alpha: false });
    if (!gl) throw new Error('WebGL2 not supported');
    this._gl = gl;

    this._prog = _buildProgram(gl, _VERT, _FRAG);
    _initFullscreenQuad(gl, this._prog);
  }

  /** cameras: [{platepar, videoEl}]  (up to 8) */
  setCameras(cameras) {
    const gl = this._gl;
    for (const t of this._textures) gl.deleteTexture(t);
    this._textures = [];

    const n = Math.min(cameras.length, 8);
    this._n = n;
    this._cameras = cameras.slice(0, n);

    for (let i = 0; i < n; i++) {
      const tex = gl.createTexture();
      gl.activeTexture(gl.TEXTURE0 + i);
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      this._textures.push(tex);
    }

    gl.useProgram(this._prog);
    gl.uniform1i(gl.getUniformLocation(this._prog, 'u_n'), n);

    for (let i = 0; i < 8; i++) {
      const loc = gl.getUniformLocation(this._prog, `u_tex[${i}]`);
      if (loc != null) gl.uniform1i(loc, i);
    }

    for (let i = 0; i < n; i++) {
      const pp   = cameras[i].platepar || {};
      const aLoc = gl.getUniformLocation(this._prog, `u_cam_a[${i}]`);
      const bLoc = gl.getUniformLocation(this._prog, `u_cam_b[${i}]`);
      if (aLoc) gl.uniform4f(aLoc,
        pp.az_centre          ?? 180,
        pp.alt_centre         ??  45,
        pp.F_scale            ??   2,
        pp.rotation_from_horiz ?? 0,
      );
      if (bLoc) gl.uniform4f(bLoc,
        pp.X_res  ?? 1920,
        pp.Y_res  ?? 1080,
        pp.fov_h  ??   60,
        pp.fov_v  ??   40,
      );
    }
  }

  render() {
    const gl     = this._gl;
    const canvas = this._canvas;

    // Keep canvas pixels matched to CSS size
    const container = canvas.parentElement;
    if (container) {
      const side = Math.min(container.clientWidth, container.clientHeight);
      if (canvas.width !== side || canvas.height !== side) {
        canvas.width = canvas.height = side;
      }
    }

    // Upload latest decoded video frame for each camera
    for (let i = 0; i < this._n; i++) {
      const v = this._cameras[i]?.videoEl;
      if (!v || v.readyState < 2 || v.videoWidth === 0) continue;
      gl.activeTexture(gl.TEXTURE0 + i);
      gl.bindTexture(gl.TEXTURE_2D, this._textures[i]);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, v);
    }

    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.useProgram(this._prog);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  destroy() {
    const gl = this._gl;
    for (const t of this._textures) gl.deleteTexture(t);
    gl.deleteProgram(this._prog);
    this._n = 0;
    this._cameras = [];
    this._textures = [];
  }
}

/** Returns true if WebGL2 is available in this browser. */
export function webgl2Supported() {
  try {
    const c = document.createElement('canvas');
    return !!c.getContext('webgl2');
  } catch (_) { return false; }
}

function _buildProgram(gl, vSrc, fSrc) {
  const vs = _compile(gl, gl.VERTEX_SHADER,   vSrc);
  const fs = _compile(gl, gl.FRAGMENT_SHADER, fSrc);
  const p  = gl.createProgram();
  gl.attachShader(p, vs); gl.attachShader(p, fs);
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS))
    throw new Error('Dome shader link error: ' + gl.getProgramInfoLog(p));
  gl.deleteShader(vs); gl.deleteShader(fs);
  return p;
}

function _compile(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
    throw new Error('Dome shader compile: ' + gl.getShaderInfoLog(s));
  return s;
}

function _initFullscreenQuad(gl, prog) {
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER,
    new Float32Array([-1, -1,  1, -1,  -1, 1,  1, 1]),
    gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, 'a_pos');
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
}
