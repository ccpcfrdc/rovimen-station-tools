import { state, escHtml, fetchJson, openAppPanel, appPanelClose, _modalOpen, _modalClose } from './dashboard-common.js';
import { VideoModal } from './dashboard-video-modal.js';
/* global _gmnOpenClipModal, _gmnCloseClipModal */
(async function() {
  try {
    const auth = await fetchJson('/api/auth/status');
    // Record the session identity so the shared VideoModal knows whether to
    // show operator-only controls (it reads state.AUTH_USER). Anonymous
    // visitors keep {user:null} → playback-only.
    state.AUTH_USER = auth.user || null;
    if (auth.admin) {
      const el = document.getElementById('logo-dd-admin');
      if (el) el.style.display = '';
      const socialEl = document.getElementById('logo-dd-social');
      if (socialEl) socialEl.style.display = '';
    }
  } catch(e) {}
})();

let STATION_COORDS = {};
let PLATEPAR_DATA  = {};
let FOV_ALT_KM     = 25;
const R_EARTH_KM   = 6371;
let footprintLayer = null;
let mapData        = null;
let stationColors  = {};
let camPolygons    = {};  // { 'gmnro03_RO000H': polygon }
let camLabels      = {};  // { 'gmnro03_RO000H': L.marker label }
let fovVisible     = false;
// state.activeStation is declared in dashboard-common.js (shared across pages);
// assigning here without `let` mutates that binding.
state.activeStation = null;
// Map of host key → Leaflet marker, populated while drawing the overview map.
// Used to highlight selected markers and to look up info for the band.
const stationMarkers = {};
// Cached station info objects (from /api/stations) keyed by host, so the
// selection band can rebuild without re-reading the marker loop closure.
const stationInfoByKey = {};
// FOV-visibility state captured the first time a station is selected, so
// clearStationSelection can restore exactly what the user saw before.
let fovStateBeforeSelect = false;

// ── vector helpers ─────────────────────────────────────────────────────────────
function v3cross(a,b){return[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]}
function v3norm(a){return Math.sqrt(a[0]*a[0]+a[1]*a[1]+a[2]*a[2])}
function v3unit(a){const n=v3norm(a);return[a[0]/n,a[1]/n,a[2]/n]}
function v3add(a,b){return[a[0]+b[0],a[1]+b[1],a[2]+b[2]]}
function v3scale(a,s){return[a[0]*s,a[1]*s,a[2]*s]}

// Returns { polygon, center } — center is the true focal projection point (nx=0, ny=0)
// Equatorial projection: uses RA_d, dec_d, pos_angle_ref from platepar when available.
// This matches RMS's internal model and correctly handles wide-angle FOVs.
// Fallback (no equatorial params): uses alt-az great-circle rotation with rotation_from_horiz.
function cameraFootprint(lat, lon, azDeg, altDeg, rotDeg, fovH, fovV, color, H,
                          xPolyRaw, Fscale, xRes, yRes, equalAspect, asymCorr,
                          raDeg, decDeg, posAngleRef, distortionType, yPolyRaw,
                          maskContour) {
  const D2R=Math.PI/180, MIN_ELEV=5*D2R;
  const N=20;
  const azR=azDeg*D2R, altR=altDeg*D2R;

  const useDist = !!(xPolyRaw?.length >= 5 && Fscale && xRes && yRes);
  const isPoly = useDist && distortionType && distortionType.startsWith('poly3+radial');

  let x0n=0, y0n=0, xy=0, a1=0, a2=0, k1=0, k2=0, k3=0;
  if (useDist && !isPoly) {
    let i=0, p=xPolyRaw;
    x0n=p[i++]||0; y0n=p[i++]||0;
    if (!equalAspect) xy=p[i++]||0;
    if (asymCorr) { a1=p[i++]||0; a2=(p[i++]||0)*2*Math.PI; }
    k1=p[i++]||0; k2=p[i++]||0; k3=p[i++]||0;
  }

  function applyDist(px, py) {
    if (!useDist) return [px*(fovH/2), py*(fovV/2)];
    const xImg=px*(xRes/2), yImg=py*(yRes/2);

    if (isPoly) {
      const xp=xPolyRaw, yp=yPolyRaw||xPolyRaw;
      const x0p=xp[0]||0, y0p=yp[0]||0;
      const r=Math.sqrt((xImg-x0p)**2+(yImg-y0p)**2);
      let dx=x0p
        +(xp[1]||0)*xImg+(xp[2]||0)*yImg
        +(xp[3]||0)*xImg**2+(xp[4]||0)*xImg*yImg+(xp[5]||0)*yImg**2
        +(xp[6]||0)*xImg**3+(xp[7]||0)*xImg**2*yImg+(xp[8]||0)*xImg*yImg**2+(xp[9]||0)*yImg**3
        +(xp[10]||0)*xImg*r+(xp[11]||0)*yImg*r;
      let dy=y0p
        +(yp[1]||0)*xImg+(yp[2]||0)*yImg
        +(yp[3]||0)*xImg**2+(yp[4]||0)*xImg*yImg+(yp[5]||0)*yImg**2
        +(yp[6]||0)*xImg**3+(yp[7]||0)*xImg**2*yImg+(yp[8]||0)*xImg*yImg**2+(yp[9]||0)*yImg**3
        +(yp[10]||0)*yImg*r+(yp[11]||0)*xImg*r;
      if(distortionType.endsWith('+radial3')||distortionType.endsWith('+radial5')){
        dx+=(xp[12]||0)*xImg*r**3; dy+=(yp[12]||0)*yImg*r**3;
      }
      if(distortionType.endsWith('+radial5')){
        dx+=(xp[13]||0)*xImg*r**5; dy+=(yp[13]||0)*yImg*r**5;
      }
      return [(xImg+dx)/Fscale, (yImg+dy)/Fscale];
    }

    const x0=x0n*(xRes/2), y0=y0n*(yRes/2);
    const dxc=xImg-x0, dyc=(1+xy)*(yImg-y0);
    let r=Math.sqrt(dxc*dxc+dyc*dyc);
    r+=a1*dyc*Math.cos(a2)-a1*dxc*Math.sin(a2);
    r/=(xRes/2);
    const rc=r+k1*r**3+k2*r**5+k3*r**7;
    const rs=r>1e-9?(rc/r-1):0;
    const xc=xImg+((xImg-x0)*rs-x0);
    const yc=yImg+((yImg-y0)*rs*(1+xy)-y0*(1+xy)+yImg*xy);
    return [xc/Fscale, yc/Fscale];
  }

  // Ray direction (ENU) → geographic lat/lon at altitude H km.
  // Spherical geometry (law of sines for range, haversine for destination).
  function rayToGeo(dE, dN, dU) {
    const elev=Math.max(Math.atan2(dU,Math.sqrt(dE*dE+dN*dN)),MIN_ELEV);
    const bearing=Math.atan2(dE,dN);
    const rm=R_EARTH_KM+H;
    const beta=elev+Math.asin(R_EARTH_KM*Math.cos(elev)/rm);
    const ca=Math.PI/2-beta;
    if(ca<=0) return [lat,lon];
    const lat0=lat*D2R, lon0=lon*D2R;
    const sinCA=Math.sin(ca), cosCA=Math.cos(ca);
    const sinLat0=Math.sin(lat0), cosLat0=Math.cos(lat0);
    const lat2=Math.asin(sinLat0*cosCA+cosLat0*sinCA*Math.cos(bearing));
    const lon2=lon0+Math.atan2(Math.sin(bearing)*sinCA*cosLat0,cosCA-sinLat0*Math.sin(lat2));
    return [lat2/D2R, lon2/D2R];
  }

  let projectDeg;

  const useEquatorial = !!(raDeg!=null && decDeg!=null && posAngleRef!=null);
  if (useEquatorial) {
    // Equatorial projection matching RMS CyFunctions xyToRaDecPP
    const latR=lat*D2R, decR=decDeg*D2R, raR=raDeg*D2R, posR=posAngleRef*D2R;
    // Derive LST from stored physical az/alt + stored RA/Dec
    const sinHA=-Math.sin(azR)*Math.cos(altR)/Math.cos(decR);
    const cosHA=(Math.sin(altR)-Math.sin(latR)*Math.sin(decR))/(Math.cos(latR)*Math.cos(decR));
    const HA_c=Math.atan2(sinHA,cosHA);
    const LST=(raR+HA_c+2*Math.PI)%(2*Math.PI);

    projectDeg=function(xi_deg, eta_deg) {
      // Position angle in equatorial tangent plane (theta=0 → equatorial North)
      const theta=Math.PI/2-posR+Math.atan2(eta_deg,xi_deg);
      const radius=Math.sqrt(xi_deg*xi_deg+eta_deg*eta_deg)*D2R;
      if(radius<1e-9) return rayToGeo(Math.cos(altR)*Math.sin(azR),Math.cos(altR)*Math.cos(azR),Math.sin(altR));
      // Spherical cap → Dec
      const sinDec=Math.sin(decR)*Math.cos(radius)+Math.cos(decR)*Math.sin(radius)*Math.cos(theta);
      const decPix=Math.asin(Math.max(-1,Math.min(1,sinDec)));
      // → RA
      const sinT=Math.sin(theta)*Math.sin(radius)/Math.cos(decPix);
      const cosT=(Math.cos(radius)-sinDec*Math.sin(decR))/(Math.cos(decPix)*Math.cos(decR));
      const raPix=(raR-Math.atan2(sinT,cosT)+2*Math.PI)%(2*Math.PI);
      // RA/Dec → HA → alt/az → ENU ray
      const HA=(LST-raPix+2*Math.PI)%(2*Math.PI);
      const sinAlt=Math.sin(latR)*sinDec+Math.cos(latR)*Math.cos(decPix)*Math.cos(HA);
      const altPix=Math.asin(Math.max(-1,Math.min(1,sinAlt)));
      const altPixSafe=Math.max(altPix,MIN_ELEV);
      const azNum=-Math.cos(decPix)*Math.sin(HA)/Math.cos(altPixSafe);
      const azDen=(sinDec-Math.sin(altPixSafe)*Math.sin(latR))/(Math.cos(altPixSafe)*Math.cos(latR));
      const azPix=(Math.atan2(azNum,azDen)+2*Math.PI)%(2*Math.PI);
      return rayToGeo(Math.cos(altPixSafe)*Math.sin(azPix),Math.cos(altPixSafe)*Math.cos(azPix),Math.sin(altPixSafe));
    };
  } else {
    // Fallback: alt-az great-circle rotation (less accurate for wide-angle but works without RA/Dec)
    const rotR=-(rotDeg||0)*D2R;
    const oz=[Math.cos(altR)*Math.sin(azR),Math.cos(altR)*Math.cos(azR),Math.sin(altR)];
    let cr0=v3cross(oz,[0,0,1]);
    cr0=v3norm(cr0)<1e-6?[1,0,0]:v3unit(cr0);
    const cu0=v3unit(v3cross(cr0,oz));
    const cosR=Math.cos(rotR),sinR=Math.sin(rotR);
    const cr=v3add(v3scale(cr0,cosR),v3scale(cu0,-sinR));
    const cu=v3add(v3scale(cr0,sinR),v3scale(cu0, cosR));
    projectDeg=function(xi_deg, eta_deg) {
      const xi_r=xi_deg*D2R, eta_r=eta_deg*D2R;
      const radius=Math.sqrt(xi_r*xi_r+eta_r*eta_r);
      // Optical-axis short-circuit: project oz itself (matches the
      // equatorial branch above). Returning [lat,lon] here used to
      // stack all camera labels on the station icon when applyDist
      // returned exactly (0,0) — which happens for any placeholder
      // platepar without distortion polys.
      if(radius<1e-9) return rayToGeo(oz[0],oz[1],oz[2]);
      const sinRad=Math.sin(radius),cosRad=Math.cos(radius);
      const xi_n=xi_r/radius, eta_n=eta_r/radius;
      const d=[cosRad*oz[0]+sinRad*(cr[0]*xi_n+cu[0]*eta_n),
               cosRad*oz[1]+sinRad*(cr[1]*xi_n+cu[1]*eta_n),
               cosRad*oz[2]+sinRad*(cr[2]*xi_n+cu[2]*eta_n)];
      return rayToGeo(d[0],d[1],d[2]);
    };
  }

  function project(nx, ny) {
    const [xi,eta]=applyDist(nx,ny);
    return projectDeg(xi,eta);
  }

  const pts=[];
  // When the station supplies a mask contour (boundary of the unmasked sky,
  // in normalised sensor coords), project that loop so the rendered FOV reflects
  // the actual observable area (trees / buildings / horizon clipped out).
  // Otherwise walk the full sensor edges (original behaviour).
  const useMask = Array.isArray(maskContour) && maskContour.length >= 3;
  if (useMask) {
    for (const p of maskContour) {
      if (!Array.isArray(p) || p.length < 2) continue;
      pts.push(project(p[0], p[1]));
    }
  }
  if (pts.length < 3) {
    pts.length = 0;
    for(let k=0;k<=N;k++) pts.push(project((k/N)*2-1,-1));
    for(let k=1;k<=N;k++) pts.push(project(1,(k/N)*2-1));
    for(let k=1;k<=N;k++) pts.push(project(1-(k/N)*2,1));
    for(let k=1;k<=N;k++) pts.push(project(-1,1-(k/N)*2));
  }
  const center = project(0, 0);
  const polygon = L.polygon(pts,{color,fillColor:color,fillOpacity:0.18,weight:1.8,opacity:0.7});
  return { polygon, center };
}

function stationFovIds(key) {
  return Object.keys(camPolygons).filter(id=>id.startsWith(key+'_'));
}

// Pure helper: union of every FOV id in `allIds` belonging to any host in
// `keys` (ids are `<host>_<cam>`). Exported for unit tests.
export function _unionFovIds(keys, allIds) {
  const wanted = keys instanceof Set ? keys : new Set(keys);
  return allIds.filter(id => {
    const host = id.slice(0, id.lastIndexOf('_'));
    return wanted.has(host);
  });
}

function _addFovPair(id) {
  if(camPolygons[id]) footprintLayer.addLayer(camPolygons[id]);
  if(camLabels[id])   footprintLayer.addLayer(camLabels[id]);
}
function _removeFovPair(id) {
  if(camPolygons[id]) footprintLayer.removeLayer(camPolygons[id]);
  if(camLabels[id])   footprintLayer.removeLayer(camLabels[id]);
}

function restoreAllFov() {
  Object.keys(camPolygons).forEach(id => _addFovPair(id));
}

function hideAllFov() {
  footprintLayer.clearLayers();
}

function toggleAllFov(btn) {
  // The global toggle owns FOV outright, so drop any active selection first
  // (silent: this function manages the FOV/band state itself afterwards).
  if (state.selectedStations.size) clearStationSelection({ silent: true });
  fovVisible = !fovVisible;
  state.activeStation = null;
  if(fovVisible) restoreAllFov();
  else hideAllFov();
  btn.textContent = fovVisible ? 'Hide FOV' : 'Show FOV';
  btn.classList.toggle('active', fovVisible);
}

window.togglePanelFov = function(btn) {
  const host = btn?.dataset?.fovHost || state.activeStation;
  if(!host) return;
  const ids = stationFovIds(host);
  const anyVisible = ids.some(id=>footprintLayer.hasLayer(camPolygons[id]));
  if(anyVisible) {
    ids.forEach(id => _removeFovPair(id));
    btn.textContent = 'Show FOV';
  } else {
    ids.forEach(id => _addFovPair(id));
    if(!map.hasLayer(footprintLayer)) footprintLayer.addTo(map);
    btn.textContent = 'Hide FOV';
  }
};

/* ─────────────────────────────────────────
   Multi-select stations on the overview map
   (issue #511) — toggle without zoom, union FOV
───────────────────────────────────────── */

// Pure helper: mutate a Set by toggling `key`. Returns whether the key is
// now present. Exported for unit tests.
export function _toggleInSet(set, key) {
  if (set.has(key)) { set.delete(key); return false; }
  set.add(key);
  return true;
}

function _setMarkerSelected(key, selected) {
  const marker = stationMarkers[key];
  if (!marker) return;
  const el = marker.getElement?.();
  if (!el) return;
  const dot = el.querySelector?.('.ov-marker-dot') || el;
  dot.classList.toggle('ov-marker-selected', selected);
}

// Add or remove one station's FOV pairs from the footprint layer without
// touching any other station (union semantics).
function _setStationFov(key, visible) {
  const ids = stationFovIds(key);
  if (visible) {
    ids.forEach(id => _addFovPair(id));
    if (ids.length && !map.hasLayer(footprintLayer)) footprintLayer.addTo(map);
  } else {
    ids.forEach(id => _removeFovPair(id));
  }
}

// Re-apply the union FOV for every currently-selected station. Called after
// the footprint geometry is rebuilt (e.g. altitude change).
function _applySelectionFov() {
  hideAllFov();
  for (const key of state.selectedStations) _setStationFov(key, true);
}

function toggleStationSelection(key, info) {
  if (info) stationInfoByKey[key] = info;
  // Capture the pre-selection FOV state once, before the first selection,
  // so Clear all can restore exactly what was on screen.
  if (state.selectedStations.size === 0) fovStateBeforeSelect = fovVisible;

  const nowSelected = _toggleInSet(state.selectedStations, key);
  _setMarkerSelected(key, nowSelected);
  _setStationFov(key, nowSelected);

  if (nowSelected) {
    state.activeStation = key;
  } else if (state.activeStation === key) {
    // Point activeStation at any remaining selection (legacy controls
    // expect a non-null host while a selection exists).
    const remaining = [...state.selectedStations];
    state.activeStation = remaining.length ? remaining[remaining.length - 1] : null;
  }

  if (state.selectedStations.size === 0) {
    clearStationSelection();
    return;
  }
  _setGlobalFovToggleDisabled(true);
  renderSelectionBand();
}

// Clears the whole selection: empties the Set, drops marker highlights,
// restores the pre-selection FOV state, hides/rebuilds the band, and resets
// activeStation. `silent` skips the FOV restore (used when the caller is
// already managing FOV, e.g. the global toggle).
function clearStationSelection({ silent = false } = {}) {
  for (const key of state.selectedStations) _setMarkerSelected(key, false);
  state.selectedStations.clear();
  state.activeStation = null;

  if (_sbThumbTimer) { clearInterval(_sbThumbTimer); _sbThumbTimer = null; }
  if (_sbAgeTimer) { clearInterval(_sbAgeTimer); _sbAgeTimer = null; }

  const band = document.getElementById('station-band');
  if (band) band.style.display = 'none';
  const sbInner = document.getElementById('sb-inner');
  if (sbInner) sbInner.replaceChildren();
  document.getElementById('map-wrap')?.classList.remove('ov-band-open');

  if (!silent) {
    if (fovStateBeforeSelect) restoreAllFov();
    else hideAllFov();
    fovVisible = fovStateBeforeSelect;
    const toggleBtn = document.getElementById('fov-toggle');
    if (toggleBtn) {
      toggleBtn.textContent = fovVisible ? 'Hide FOV' : 'Show FOV';
      toggleBtn.classList.toggle('active', fovVisible);
    }
  }
  fovStateBeforeSelect = false;
  _setGlobalFovToggleDisabled(false);
}

function setFovAlt(km, btn) {
  FOV_ALT_KM = km;
  document.querySelectorAll('.fov-control .fov-btn:not(#fov-toggle):not(#cov-toggle)').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  if(mapData) {
    drawFootprints(mapData, km);
    if(state.selectedStations.size) _applySelectionFov();
    else if(fovVisible) restoreAllFov();
  }
  // drawFootprints calls _precomputeCovGrid which auto-refreshes active coverage layers
}

const _covLayers = {1: null, 2: null, 3: null};
const _covActive = {1: false, 2: false, 3: false};
let _covPrecomp = null;

function toggleCovLayer(n, btn) {
  _covActive[n] = !_covActive[n];
  btn.classList.toggle('active', _covActive[n]);
  if (_covActive[n]) {
    _showCovLayer(n);
  } else if (_covLayers[n] && map) {
    map.removeLayer(_covLayers[n]);
    _covLayers[n] = null;
  }
}

function _showCovLayer(n) {
  if (!_covPrecomp || !map) return;
  if (_covLayers[n] && map) map.removeLayer(_covLayers[n]);
  _covLayers[n] = L.layerGroup();
  const p = _covPrecomp;

  if (n === 1) {
    // One polygon per station with fillRule nonzero so overlapping same-station
    // cameras fill uniformly instead of creating holes
    for (const rings of Object.values(p.stationRings)) {
      L.polygon(rings, {
        color: 'none', fillColor: '#a5d6ff', fillOpacity: 0.08,
        interactive: false, fillRule: 'nonzero',
      }).addTo(_covLayers[1]);
    }
  } else {
    // n=2: >=2 stations, n=3: >=3 stations
    const minN = n === 2 ? 2 : 3;
    const color = n === 2 ? '#58a6ff' : '#1f6feb';
    const opacity = n === 2 ? 0.10 : 0.15;
    const step = p.step, halfLat = step / 2;
    for (const cell of p.gridCells) {
      if (cell.n >= minN) {
        const halfLon = step / 2 / Math.cos(cell.lat * Math.PI / 180);
        L.rectangle(
          [[cell.lat - halfLat, cell.lon - halfLon], [cell.lat + halfLat, cell.lon + halfLon]],
          { color: 'none', fillColor: color, fillOpacity: opacity, interactive: false }
        ).addTo(_covLayers[n]);
      }
    }
  }
  _covLayers[n].addTo(map);
}

function _precomputeCovGrid() {
  _covPrecomp = null;
  const stationRings = {};
  for (const [id, poly] of Object.entries(camPolygons)) {
    const host = id.split('_')[0];
    if (!stationRings[host]) stationRings[host] = [];
    const latlngs = poly.getLatLngs();
    const ring = Array.isArray(latlngs[0]) ? latlngs[0] : latlngs;
    stationRings[host].push(ring.map(ll => [ll.lat, ll.lng]));
  }
  // 1-station data is ready immediately (just polygon refs)
  _covPrecomp = { stationRings, step: 0.08, gridCells: [] };
  if (_covActive[1]) _showCovLayer(1);

  // Grid for 2+/3+ computed async in chunks to avoid blocking UI
  const stationEntries = Object.entries(stationRings);
  if (stationEntries.length < 2) return;

  let minLat=90,maxLat=-90,minLon=180,maxLon=-180;
  for (const [, rings] of stationEntries) {
    for (const ring of rings) {
      for (const [lat,lon] of ring) {
        if (lat < minLat) minLat = lat;
        if (lat > maxLat) maxLat = lat;
        if (lon < minLon) minLon = lon;
        if (lon > maxLon) maxLon = lon;
      }
    }
  }

  // Build flat bounding boxes for quick rejection
  const stBounds = stationEntries.map(([, rings]) => {
    let sMinLat=90,sMaxLat=-90,sMinLon=180,sMaxLon=-180;
    for (const ring of rings) {
      for (const [lat,lon] of ring) {
        if (lat < sMinLat) sMinLat = lat;
        if (lat > sMaxLat) sMaxLat = lat;
        if (lon < sMinLon) sMinLon = lon;
        if (lon > sMaxLon) sMaxLon = lon;
      }
    }
    return {minLat: sMinLat, maxLat: sMaxLat, minLon: sMinLon, maxLon: sMaxLon, rings};
  });

  const step = 0.08, cells = [];
  const latSteps = [];
  for (let lat = minLat; lat <= maxLat; lat += step) latSteps.push(lat);
  let idx = 0;
  const CHUNK = 50;

  function processChunk() {
    const end = Math.min(idx + CHUNK, latSteps.length);
    for (; idx < end; idx++) {
      const lat = latSteps[idx];
      for (let lon = minLon; lon <= maxLon; lon += step) {
        let count = 0;
        for (const sb of stBounds) {
          if (lat < sb.minLat || lat > sb.maxLat || lon < sb.minLon || lon > sb.maxLon) continue;
          for (const ring of sb.rings) {
            if (_ptInRing(lat, lon, ring)) { count++; break; }
          }
        }
        if (count >= 2) cells.push({lat, lon, n: count});
      }
    }
    if (idx < latSteps.length) {
      setTimeout(processChunk, 0);
    } else {
      _covPrecomp.gridCells = cells;
      for (const k of [2,3]) { if (_covActive[k]) _showCovLayer(k); }
    }
  }
  setTimeout(processChunk, 0);
}

function _ptInRing(lat, lon, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const yi = ring[i][0], xi = ring[i][1];
    const yj = ring[j][0], xj = ring[j][1];
    if (((yi > lat) !== (yj > lat)) && (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi))
      inside = !inside;
  }
  return inside;
}

function _setGlobalFovToggleDisabled(disabled) {
  const btn = document.getElementById('fov-toggle');
  if(!btn) return;
  btn.disabled = disabled;
  btn.style.opacity = disabled ? '0.35' : '';
  btn.style.cursor  = disabled ? 'default' : '';
}

// Legacy entry point retained for the map background-click handler: closing
// the panel now means clearing the whole selection.
function closePanel() {
  if (!state.selectedStations.size) return;
  clearStationSelection();
}

// Per-station FOV toggle button (carries its host via data-fov-host so each
// row's button operates independently in the multi-selection band).
function _makeFovButton(key) {
  if (!stationFovIds(key).length) return null;
  const btn = document.createElement('button');
  btn.className = 'sb-btn';
  btn.dataset.fovHost = key;
  const anyVisible = stationFovIds(key).some(id => footprintLayer.hasLayer(camPolygons[id]));
  btn.textContent = anyVisible ? 'Hide FOV' : 'Show FOV';
  btn.onclick = function() { window.togglePanelFov(this); };
  return btn;
}

// Per-station remove (✕): deselect just this host, leaving the rest.
function _makeRemoveButton(key, info) {
  const btn = document.createElement('button');
  btn.className = 'sb-close';
  btn.textContent = '✕';
  btn.title = `Remove ${key} from selection`;
  btn.onclick = function() { toggleStationSelection(key, info); };
  return btn;
}

// Rich single-station card (thumbnails) — used when exactly one station is
// selected. No flyTo and no FOV mutation: selection logic owns the FOV.
function _renderSingleStationCard(key, info, sbInner) {
  const lat = info.lat ?? STATION_COORDS[key]?.lat;
  const lon = info.lon ?? STATION_COORDS[key]?.lon;
  const color = stationColors[key] || 'var(--blue)';

  const coordStr = (lat && lon)
    ? `${Math.abs(lat).toFixed(2)}°${lat>=0?'N':'S'} ${Math.abs(lon).toFixed(2)}°${lon>=0?'E':'W'}`
    : '';

  const bortleInfo = _bortleCache[key];

  const infoRow = document.createElement('div');
  infoRow.style.cssText = 'display:flex;align-items:center;gap:20px;flex-wrap:wrap;width:100%';

  const nameDiv = document.createElement('div');
  const nameLink = document.createElement('a');
  nameLink.className = 'sb-name';
  nameLink.href = `/station/${encodeURIComponent(key)}`;
  nameLink.style.cssText = 'color:inherit;text-decoration:none;border-bottom:1px dotted currentColor';
  nameLink.title = 'Go to station page';
  nameLink.textContent = key;
  nameDiv.appendChild(nameLink);
  if (info.label) {
    const labelSpan = document.createElement('span');
    labelSpan.className = 'sb-label';
    labelSpan.textContent = ` — ${info.label}`;
    nameDiv.appendChild(labelSpan);
  }
  infoRow.appendChild(nameDiv);

  if (coordStr) {
    const coordSpan = document.createElement('span');
    coordSpan.className = 'sb-coords';
    coordSpan.textContent = coordStr;
    infoRow.appendChild(coordSpan);
  }
  if (bortleInfo) {
    const bSpan = document.createElement('span');
    bSpan.title = `Bortle ${bortleInfo.cls} — ${bortleInfo.label}`;
    bSpan.style.cssText = `font-size:11px;padding:2px 8px;border-radius:3px;border:1px solid;color:${bortleColor(bortleInfo.cls)};border-color:${bortleColor(bortleInfo.cls)}55;font-family:'SF Mono',monospace;white-space:nowrap`;
    bSpan.textContent = `Bortle ${bortleInfo.cls}`;
    infoRow.appendChild(bSpan);
  }

  const camsDiv = document.createElement('div');
  camsDiv.className = 'sb-cams';
  for (const c of (info.cameras || [])) {
    const a = document.createElement('a');
    a.className = 'sb-cam-badge';
    a.href = `/station/${encodeURIComponent(key)}/rms?cam=${encodeURIComponent(c.code)}`;
    a.style.cssText = `color:${color};border-color:${color}33;text-decoration:none`;
    a.title = `Go to ${c.code} RMS detections`;
    a.textContent = c.code;
    camsDiv.appendChild(a);
  }
  infoRow.appendChild(camsDiv);

  const actionsDiv = document.createElement('div');
  actionsDiv.className = 'sb-actions';
  const fovBtn = _makeFovButton(key);
  if (fovBtn) actionsDiv.appendChild(fovBtn);
  const goLink = document.createElement('a');
  goLink.href = `/station/${encodeURIComponent(key)}`;
  goLink.className = 'sb-btn go';
  goLink.textContent = 'Go to station →';
  actionsDiv.appendChild(goLink);
  actionsDiv.appendChild(_makeRemoveButton(key, info));
  infoRow.appendChild(actionsDiv);

  sbInner.appendChild(infoRow);

  // FF maxpixel thumbnails — shown when any RMS service on the station is active
  const rmsActive = _isRmsActive(info.services);
  const activeCams = rmsActive ? (info.cameras || []) : [];
  if (activeCams.length) {
    const thumbRow = document.createElement('div');
    thumbRow.className = 'sb-thumbs';
    sbInner.appendChild(thumbRow);
    _sbThumbRefresh(key, activeCams, thumbRow);
  }
}

// Compact one-line row per station — used when ≥2 stations are selected.
function _renderCompactRow(key, info, sbInner) {
  const color = stationColors[key] || 'var(--blue)';
  const row = document.createElement('div');
  row.className = 'sb-row';
  row.style.cssText = 'display:flex;align-items:center;gap:12px;flex-wrap:wrap;width:100%';

  const nameLink = document.createElement('a');
  nameLink.className = 'sb-name';
  nameLink.href = `/station/${encodeURIComponent(key)}`;
  nameLink.style.cssText = 'color:inherit;text-decoration:none;border-bottom:1px dotted currentColor';
  nameLink.title = info.label ? `${info.label} — go to station page` : 'Go to station page';
  nameLink.textContent = key;
  row.appendChild(nameLink);

  const camsDiv = document.createElement('div');
  camsDiv.className = 'sb-cams';
  for (const c of (info.cameras || [])) {
    const a = document.createElement('a');
    a.className = 'sb-cam-badge';
    a.href = `/station/${encodeURIComponent(key)}/rms?cam=${encodeURIComponent(c.code)}`;
    a.style.cssText = `color:${color};border-color:${color}33;text-decoration:none`;
    a.title = `Go to ${c.code} RMS detections`;
    a.textContent = c.code;
    camsDiv.appendChild(a);
  }
  row.appendChild(camsDiv);

  const actionsDiv = document.createElement('div');
  actionsDiv.className = 'sb-actions';
  const fovBtn = _makeFovButton(key);
  if (fovBtn) actionsDiv.appendChild(fovBtn);
  actionsDiv.appendChild(_makeRemoveButton(key, info));
  row.appendChild(actionsDiv);

  sbInner.appendChild(row);
}

// Rebuild the selection band from state.selectedStations.
function renderSelectionBand() {
  const sbInner = document.getElementById('sb-inner');
  if (!sbInner) return;
  // Tear down single-card thumbnail timers before re-rendering.
  if (_sbThumbTimer) { clearInterval(_sbThumbTimer); _sbThumbTimer = null; }
  if (_sbAgeTimer) { clearInterval(_sbAgeTimer); _sbAgeTimer = null; }
  sbInner.replaceChildren();

  const keys = [...state.selectedStations];
  if (!keys.length) {
    closePanel();
    return;
  }

  if (keys.length === 1) {
    const key = keys[0];
    _renderSingleStationCard(key, stationInfoByKey[key] || {}, sbInner);
  } else {
    const list = document.createElement('div');
    list.className = 'sb-list';
    list.style.cssText = 'display:flex;flex-direction:column;gap:8px;width:100%';
    for (const key of keys) _renderCompactRow(key, stationInfoByKey[key] || {}, list);
    sbInner.appendChild(list);

    const footer = document.createElement('div');
    footer.className = 'sb-band-footer';
    footer.style.cssText = 'display:flex;align-items:center;gap:10px;width:100%;margin-top:6px';
    const count = document.createElement('span');
    count.className = 'sb-coords';
    count.textContent = `${keys.length} stations selected`;
    footer.appendChild(count);
    sbInner.appendChild(footer);
  }

  // Single "Clear all" control, always present while a selection exists.
  const clearBtn = document.createElement('button');
  clearBtn.className = 'sb-btn';
  clearBtn.id = 'sb-clear-all';
  clearBtn.textContent = 'Clear all';
  clearBtn.style.marginLeft = 'auto';
  clearBtn.onclick = function() { clearStationSelection(); };
  if (keys.length > 1) {
    sbInner.querySelector('.sb-band-footer')?.appendChild(clearBtn);
  } else {
    const actions = sbInner.querySelector('.sb-actions');
    if (actions) actions.insertBefore(clearBtn, actions.firstChild);
    else sbInner.appendChild(clearBtn);
  }

  document.getElementById('station-band').style.display = 'block';
  document.getElementById('map-wrap')?.classList.add('ov-band-open');
}

function _isRmsActive(services) {
  if (!services) return false;
  for (const [key, val] of Object.entries(services)) {
    if (key.startsWith('rms-')) {
      if (val === 'active' || val?.active === true || val?.state === 'active') return true;
    }
  }
  return false;
}

let _sbThumbTimer = null;
let _sbAgeTimer = null;

function _sbThumbRefresh(hostKey, cams, container) {
  if (_sbThumbTimer) clearInterval(_sbThumbTimer);
  if (_sbAgeTimer) clearInterval(_sbAgeTimer);
  const load = () => {
    if (!document.contains(container)) {
      clearInterval(_sbThumbTimer); _sbThumbTimer = null;
      clearInterval(_sbAgeTimer); _sbAgeTimer = null;
      return;
    }
    for (const c of cams) {
      const url = `/api/latest_ff_maxpixel/${encodeURIComponent(hostKey)}/${encodeURIComponent(c.code)}`;
      fetch(url).then(resp => {
        if (!resp.ok) {
          const old = container.querySelector(`[data-cam="${c.code}"]`);
          if (old) old.remove();
          return;
        }
        const ffTs = resp.headers.get('X-FF-Timestamp');
        return resp.blob().then(blob => {
          let card = container.querySelector(`[data-cam="${c.code}"]`);
          if (!card) {
            card = document.createElement('div');
            card.className = 'sb-thumb';
            card.dataset.cam = c.code;
            const img = document.createElement('img');
            img.alt = c.code;
            if (c.rotate) img.style.transform = 'rotate(180deg)';
            const lbl = document.createElement('div');
            lbl.className = 'sb-thumb-label';
            card.append(img, lbl);
            container.appendChild(card);
          }
          if (ffTs) card.dataset.ts = ffTs;
          const img = card.querySelector('img');
          const oldSrc = img.src;
          img.src = URL.createObjectURL(blob);
          if (oldSrc.startsWith('blob:')) URL.revokeObjectURL(oldSrc);
          _sbUpdateLabel(card);
        });
      }).catch(() => {});
    }
  };
  load();
  _sbThumbTimer = setInterval(load, 15000);
  _sbAgeTimer = setInterval(_sbUpdateAllLabels, 5000);
}

function _sbUpdateLabel(card) {
  const lbl = card.querySelector('.sb-thumb-label');
  if (!lbl) return;
  const code = card.dataset.cam || '';
  const ts = card.dataset.ts;
  lbl.textContent = ts ? code + '  ' + _ffAge(ts) : code;
}

function _sbUpdateAllLabels() {
  for (const card of document.querySelectorAll('.sb-thumb[data-ts]')) _sbUpdateLabel(card);
}

function _ffAge(isoStr) {
  const sec = Math.max(0, Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000));
  if (sec < 60) return 't-' + sec + 's';
  const min = Math.floor(sec / 60);
  return 't-' + min + 'min';
}

// ── Live View grid ──────────────────────────────────────────────────────────

let _liveTimer = null;
let _liveAgeTimer = null;
let _liveVisible = false;

function toggleLiveView() {
  const el = document.getElementById('ov-live');
  const btn = document.getElementById('ov-live-bar-btn');
  if (_liveVisible) {
    _liveVisible = false;
    el.style.display = 'none';
    if (_liveTimer) { clearInterval(_liveTimer); _liveTimer = null; }
    if (_liveAgeTimer) { clearInterval(_liveAgeTimer); _liveAgeTimer = null; }
    if (btn) btn.textContent = btn.textContent.replace('Hide ', '');
  } else {
    _liveVisible = true;
    el.style.display = '';
    _liveRefresh();
    _liveTimer = setInterval(_liveRefresh, 15000);
    _liveAgeTimer = setInterval(_liveUpdateAges, 5000);
    el.scrollIntoView({behavior: 'smooth', block: 'nearest'});
    if (btn && !btn.textContent.startsWith('Hide')) btn.textContent = 'Hide ' + btn.textContent;
  }
}

function ovLiveUpdateZoom(val) {
  const px = parseInt(val, 10) || 160;
  document.querySelectorAll('.ov-live-card').forEach(c => { c.style.width = px + 'px'; });
  document.querySelectorAll('.ov-live-card img').forEach(img => {
    img.style.width = px + 'px';
    img.style.height = Math.round(px * 0.75) + 'px';
  });
}

// TODO(#340): _liveRefresh fires bare fetch() calls without an AbortController.
// If the tab is hidden or the panel toggled off mid-flight, in-progress requests
// complete and update the DOM after teardown. Consider wiring an AbortController
// that is aborted in toggleLiveView's hide branch.
function _liveRefresh() {
  if (!mapData) return;
  const grid = document.getElementById('ov-live-grid');
  let count = 0;
  const stationColors = {};
  const palette = ['#58a6ff','#3fb950','#d29922','#bc8cff','#f85149','#79c0ff','#e3b341','#ff7b72','#7ee787','#a5d6ff'];
  let ci = 0;
  for (const [hostKey, info] of Object.entries(mapData)) {
    if (!_isRmsActive(info.services)) continue;
    stationColors[hostKey] = palette[ci++ % palette.length];
    let groupEl = document.getElementById('live-group-' + hostKey);
    if (!groupEl) {
      groupEl = document.createElement('div');
      groupEl.className = 'ov-live-group';
      groupEl.id = 'live-group-' + hostKey;
      groupEl.style.borderColor = stationColors[hostKey];
      const label = document.createElement('div');
      label.className = 'ov-live-group-label';
      label.style.color = stationColors[hostKey];
      label.textContent = (info.label || hostKey);
      groupEl.appendChild(label);
      const inner = document.createElement('div');
      inner.className = 'ov-live-group-cards';
      groupEl.appendChild(inner);
      grid.appendChild(groupEl);
    }
    const inner = groupEl.querySelector('.ov-live-group-cards');
    for (const c of (info.cameras || [])) {
      count++;
      const cardId = 'live-' + hostKey + '-' + c.code;
      const url = '/api/latest_ff_maxpixel/' + encodeURIComponent(hostKey) + '/' + encodeURIComponent(c.code);
      fetch(url).then(resp => {
        if (!resp.ok) return;
        const ffTs = resp.headers.get('X-FF-Timestamp');
        return resp.blob().then(blob => {
          let card = document.getElementById(cardId);
          if (!card) {
            card = document.createElement('div');
            card.className = 'ov-live-card';
            card.id = cardId;
            card.dataset.host = hostKey;
            card.dataset.cam = c.code;
            card.onclick = () => _liveOpenVideo(hostKey, c.code);
            const img = document.createElement('img');
            img.alt = c.code;
            if (c.rotate) img.style.transform = 'rotate(180deg)';
            const meta = document.createElement('div');
            meta.className = 'ov-live-meta';
            const codeSpan = document.createElement('span');
            codeSpan.textContent = c.code;
            const ageSpan = document.createElement('span');
            ageSpan.className = 'ov-live-age';
            meta.append(codeSpan, ageSpan);
            card.append(img, meta);
            inner.appendChild(card);
          }
          if (ffTs) card.dataset.ts = ffTs;
          const img = card.querySelector('img');
          const oldSrc = img.src;
          img.src = URL.createObjectURL(blob);
          if (oldSrc.startsWith('blob:')) URL.revokeObjectURL(oldSrc);
          _liveUpdateCardAge(card);
        });
      }).catch(() => {});
    }
  }
  document.getElementById('ov-live-count').textContent =
    count > 0 ? count + ' cameras capturing' : 'no active cameras';
  for (const card of [...grid.querySelectorAll('.ov-live-card')]) {
    const h = card.dataset.host;
    if (!mapData[h] || !_isRmsActive(mapData[h].services)) {
      card.remove();
      const group = document.getElementById('live-group-' + h);
      if (group && !group.querySelector('.ov-live-card')) group.remove();
    }
  }
}

function _liveUpdateCardAge(card) {
  const ts = card.dataset.ts;
  const age = card.querySelector('.ov-live-age');
  if (!age || !ts) return;
  age.textContent = _ffAge(ts);
  const dt = new Date(ts);
  const secsAgo = (Date.now() - dt.getTime()) / 1000;
  if (secsAgo > 600) age.style.color = 'var(--red)';
  else if (secsAgo > 300) age.style.color = '#e89b3c';
  else age.style.color = '';
}

function _liveUpdateAges() {
  for (const card of document.querySelectorAll('.ov-live-card[data-ts]')) {
    _liveUpdateCardAge(card);
  }
}

function _liveOpenVideo(hostKey, camCode) {
  fetchJson('/api/latest_chunk/' + encodeURIComponent(hostKey) + '/' + encodeURIComponent(camCode))
    .then(data => {
      if (data.error || !data.video_url) return;
      const video = document.getElementById('ov-live-video');
      const label = document.getElementById('ov-live-video-label');
      video.src = data.video_url;
      label.textContent = camCode + '  ' + (data.time || '') + ' UTC';
      _modalOpen(document.getElementById('ov-live-modal'));
    })
    .catch(() => {});
}

function closeLiveModal() {
  const video = document.getElementById('ov-live-video');
  if (video) { video.pause(); video.src = ''; }
  _modalClose(document.getElementById('ov-live-modal'));
}

const COLORS=['#58a6ff','#3fb950','#d29922','#bc8cff','#f85149','#79c0ff','#e3b341'];

function drawFootprints(data, H) {
  if(!footprintLayer) return;
  footprintLayer.clearLayers();
  camPolygons = {};
  camLabels   = {};
  const _ce=Math.cos(10*Math.PI/180);
  const _beta=10*Math.PI/180+Math.asin(R_EARTH_KM*_ce/(R_EARTH_KM+H));
  const capKm=R_EARTH_KM*(Math.PI/2-_beta);
  let i=0;
  for(const [key,info] of Object.entries(data)){
    const lat=info.lat??STATION_COORDS[key]?.lat;
    const lon=info.lon??STATION_COORDS[key]?.lon;
    if(!lat||!lon||info.show_on_map===false){i++;continue}
    const color=COLORS[i%COLORS.length];
    stationColors[key]=color;
    const pps=PLATEPAR_DATA[key]||{};
    for(const cam of (info.cameras||[])){
      const pp=pps[cam.code];
      const az=pp?.az_centre??cam.az, alt=pp?.alt_centre??cam.alt;
      if(az==null||alt==null) continue;
      const result=cameraFootprint(lat,lon,az,alt,pp?.rotation_from_horiz??0,pp?.fov_h??90,pp?.fov_v??50,color,H,
        pp?.x_poly_fwd??pp?.x_poly, pp?.F_scale, pp?.X_res, pp?.Y_res,
        pp?.equal_aspect??true, pp?.asymmetry_corr??true,
        pp?.RA_d, pp?.dec_d, pp?.pos_angle_ref,
        pp?.distortion_type, pp?.y_poly_fwd??pp?.y_poly,
        pp?.mask_contour);
      if(!result) continue;
      {
        const cosLat=Math.cos(lat*Math.PI/180);
        const SEGS=24;
        const orig=result.polygon.getLatLngs()[0];
        const dense=[];
        for(let j=0;j<orig.length;j++){
          const a=orig[j], b=orig[(j+1)%orig.length];
          for(let t=0;t<SEGS;t++){
            const f=t/SEGS;
            dense.push(L.latLng(a.lat+(b.lat-a.lat)*f, a.lng+(b.lng-a.lng)*f));
          }
        }
        const capped=dense.map(ll=>{
          const dLat=ll.lat-lat, dLon=ll.lng-lon;
          const dist=Math.sqrt((dLat*111)**2+(dLon*111*cosLat)**2);
          if(dist<=capKm) return ll;
          const s=capKm/dist;
          return L.latLng(lat+dLat*s, lon+dLon*s);
        });
        result.polygon.setLatLngs(capped);
      }
      const id=`${key}_${cam.code}`;
      camPolygons[id]=result.polygon;
      camLabels[id]=L.marker(result.center,{
        icon:L.divIcon({
          className:'',
          html:`<span class="cam-fov-label">${cam.code}</span>`,
          iconSize:null,iconAnchor:null,
        }),
        interactive:false,
      });
    }
    i++;
  }
  _precomputeCovGrid();
}

// ── Light pollution / Bortle ──────────────────────────────────────────────────

const LP_Z = 6;          // max native zoom in tile URLs (Leaflet maxNativeZoom:8 with zoomOffset:-2 → URL z=6)
const LP_TILE_W = 1024;  // tile size in pixels

// Reference palette for Lorenz 2024 atlas (RGB → Bortle class).
// Colors derived from the atlas colorbar; refined empirically.
// cls=Bortle class, pr/pg/pb=reference pixel color (empirically sampled from Lorenz 2024 atlas)
const BORTLE_REF = [
  { cls:1, pr:0,   pg:0,   pb:0,   label:'Pristine dark sky' },          // ocean/wilderness
  { cls:2, pr:34,  pg:34,  pb:34,  label:'Excellent dark sky' },          // remote desert (dark gray)
  { cls:2, pr:20,  pg:47,  pb:114, label:'Excellent dark sky' },          // Sahara (dark navy)
  { cls:3, pr:15,  pg:87,  pb:20,  label:'Rural sky' },                   // rural France (dark green)
  { cls:3, pr:33,  pg:84,  pb:216, label:'Rural sky' },                   // rural France (dark blue)
  { cls:4, pr:110, pg:100, pb:30,  label:'Rural/suburban transition' },   // Valcele/Bistrita (olive)
  { cls:5, pr:31,  pg:161, pb:42,  label:'Suburban sky' },                // suburban fringe (bright green)
  { cls:5, pr:184, pg:166, pb:37,  label:'Suburban sky' },                // Ciocarlia (yellow-green)
  { cls:6, pr:191, pg:100, pb:30,  label:'Bright suburban sky' },         // Vaslui (orange)
  { cls:7, pr:251, pg:153, pb:138, label:'Suburban/urban transition' },   // Bucharest suburb (salmon)
  { cls:8, pr:160, pg:160, pb:160, label:'City sky' },                    // Bucharest (gray)
  { cls:9, pr:242, pg:242, pb:242, label:'Inner-city sky' },              // London/Paris (near-white)
];

function _rgbToBortle(r, g, b) {
  let best = BORTLE_REF[0], bestDist = Infinity;
  for (const ref of BORTLE_REF) {
    const d = (r-ref.pr)**2 + (g-ref.pg)**2 + (b-ref.pb)**2;
    if (d < bestDist) { bestDist = d; best = ref; }
  }
  return { cls: best.cls, label: best.label };
}

function _latLonToTilePixel(lat, lon) {
  const n = Math.pow(2, LP_Z);
  const latR = lat * Math.PI / 180;
  const sec = 1 / Math.cos(latR);
  const xTile = Math.floor((lon + 180) / 360 * n);
  const yTile = Math.floor((1 - Math.log(Math.tan(latR) + sec) / Math.PI) / 2 * n);
  const xFrac = (lon + 180) / 360 * n - xTile;
  const yFrac = (1 - Math.log(Math.tan(latR) + sec) / Math.PI) / 2 * n - yTile;
  return { xTile, yTile, px: Math.floor(xFrac * LP_TILE_W), py: Math.floor(yFrac * LP_TILE_W) };
}

const _bortleCache = {};   // host_key → {bortle, label, r, g, b}
const LP_LS_KEY = 'ov_bortle_v4';

function _saveBortleCache() {
  try { localStorage.setItem(LP_LS_KEY, JSON.stringify(_bortleCache)); } catch(e) {}
}

(function _loadBortleCache() {
  try {
    const saved = JSON.parse(localStorage.getItem(LP_LS_KEY) || '{}');
    Object.assign(_bortleCache, saved);
  } catch(e) {}
})();

async function _sampleBortle(lat, lon) {
  const { xTile, yTile, px, py } = _latLonToTilePixel(lat, lon);
  const url = `https://djlorenz.github.io/astronomy/image_tiles/tiles2024/tile_${LP_Z}_${xTile}_${yTile}.png`;
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      try {
        const cv = document.createElement('canvas');
        cv.width = img.width; cv.height = img.height;
        cv.getContext('2d').drawImage(img, 0, 0);
        const [r, g, b] = cv.getContext('2d').getImageData(px, py, 1, 1).data;
        const { cls, label } = _rgbToBortle(r, g, b);
        resolve({ cls, label, r, g, b });
      } catch(e) { reject(e); }
    };
    img.onerror = reject;
    img.src = url;
  });
}

async function loadBortleData(stationData) {
  for (const [key, info] of Object.entries(stationData)) {
    if (_bortleCache[key]) continue;   // already cached
    const lat = info.lat ?? STATION_COORDS[key]?.lat;
    const lon = info.lon ?? STATION_COORDS[key]?.lon;
    if (!lat || !lon) continue;
    try {
      _bortleCache[key] = await _sampleBortle(lat, lon);
    } catch(e) {
      console.warn(`Bortle sample failed for ${key}:`, e);
    }
  }
  _saveBortleCache();
}

function bortleColor(cls) {
  const colors = ['','#a8d8ea','#7ec8e3','#4db8c8','#52c452','#c8d448','#e8c820','#e88020','#e05050','#ff3030'];
  return colors[cls] || '#888';
}

// ── Map layers ────────────────────────────────────────────────────────────────

const MAP_LAYERS = {
  topo: {
    url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    opts: { attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, <a href="http://viewfinderpanoramas.org">SRTM</a> | <a href="https://opentopomap.org">OpenTopoMap</a>', maxZoom:17 },
  },
  short: {
    url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    opts: { attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>', subdomains:'abcd', maxZoom:19 },
  },
  std: {
    url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    opts: { attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom:19 },
  },
};

let _tileLayer  = null;
let _lpLayer    = null;
let _gmnLayer   = null;   // L.layerGroup holding track polylines + dot markers
let _gmnActive  = false;

const LP_TILE_URL = 'https://djlorenz.github.io/astronomy/image_tiles/tiles2024/tile_{z}_{x}_{y}.png';
const LP_OPTS = {
  tileSize: 1024, zoomOffset: -2, maxNativeZoom: 8, maxZoom: 19,
  opacity: 0.25, pane: 'lpPane',
  attribution: '<a href="https://djlorenz.github.io/astronomy/lp2020/" target="_blank">Light Pollution Atlas</a>',
};

function toggleLpLayer(btn) {
  if (_lpLayer) {
    map.removeLayer(_lpLayer);
    _lpLayer = null;
    btn.classList.remove('active');
    localStorage.setItem('ov_lp_layer', '0');
  } else {
    _lpLayer = L.tileLayer(LP_TILE_URL, LP_OPTS).addTo(map);
    btn.classList.add('active');
    localStorage.setItem('ov_lp_layer', '1');
  }
}

function setMapBrightness(val, save) {
  const tilePaneEl = document.querySelector('.leaflet-tile-pane');
  if (tilePaneEl) tilePaneEl.style.filter = `brightness(${val/100})`;
  if (save) localStorage.setItem('ov_map_brightness', val);
}

function toggleLayerMenu() {
  document.getElementById('map-layer-btns').classList.toggle('open');
}
function switchMapLayer(key, btn) {
  document.getElementById('map-layer-btns').classList.remove('open');
  document.querySelectorAll('.map-layer-btns .fov-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  if(_tileLayer) map.removeLayer(_tileLayer);
  const {url, opts} = MAP_LAYERS[key];
  _tileLayer = L.tileLayer(url, opts).addTo(map);
  _tileLayer.bringToBack();
  localStorage.setItem('ov_map_layer', key);
  const isDark = key === 'short';
  const tilePaneEl = document.querySelector('.leaflet-tile-pane');
  const brightnessRow = document.getElementById('brightness-row');
  if (isDark) {
    if (tilePaneEl) tilePaneEl.style.filter = 'none';
    if (brightnessRow) brightnessRow.classList.add('hidden');
  } else {
    const val = document.getElementById('map-brightness').value;
    if (tilePaneEl) tilePaneEl.style.filter = `brightness(${val/100})`;
    if (brightnessRow) brightnessRow.classList.remove('hidden');
  }
}

// ── Weather radar overlay (RainViewer) ───────────────────────────────────────

let _wxFrames = [];
let _wxLayers = {};   // idx -> L.tileLayer (preloaded cache)
let _wxCurrent = null; // currently visible layer
let _wxIdx = 0;
let _wxTimer = null;
let _wxActive = false;
let _wxRefreshTimer = null;

function toggleWeatherLayer(btn) {
  if (_wxActive) {
    _wxTeardown();
    btn.classList.remove('active');
    localStorage.setItem('ov_wx_layer', '0');
  } else {
    _wxActive = true;
    btn.classList.add('active');
    localStorage.setItem('ov_wx_layer', '1');
    _wxFetch();
  }
}

function _wxTeardown() {
  _wxActive = false;
  if (_wxTimer) { clearInterval(_wxTimer); _wxTimer = null; }
  if (_wxRefreshTimer) { clearTimeout(_wxRefreshTimer); _wxRefreshTimer = null; }
  for (const layer of Object.values(_wxLayers)) map.removeLayer(layer);
  _wxLayers = {};
  _wxCurrent = null;
  _wxFrames = [];
  document.getElementById('wx-inline').classList.remove('visible');
}

function _wxMakeLayer(url) {
  return L.tileLayer(url, {
    opacity: 0,
    tileSize: 512,
    zoomOffset: -1,
    maxNativeZoom: 7,
    maxZoom: 19,
    pane: 'wxPane',
    attribution: '<a href="https://www.rainviewer.com/" target="_blank">RainViewer</a>',
  });
}

function _wxPreloadAll() {
  for (let i = 0; i < _wxFrames.length; i++) {
    if (_wxLayers[i]) continue;
    const layer = _wxMakeLayer(_wxFrames[i].url);
    layer.setOpacity(0);
    layer.addTo(map);
    _wxLayers[i] = layer;
  }
}

function _wxFetch() {
  fetch('https://api.rainviewer.com/public/weather-maps.json')
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(data => {
      if (!_wxActive) return;
      const host = data.host || 'https://tilecache.rainviewer.com';
      const frames = (data.radar?.past || []).map(f => ({
        time: f.time,
        url: host + f.path + '/512/{z}/{x}/{y}/6/1_1.png',
      }));
      if (!frames.length) return;
      // Clean old layers if frame set changed
      const oldKeys = _wxFrames.map(f => f.url);
      const newKeys = frames.map(f => f.url);
      if (JSON.stringify(oldKeys) !== JSON.stringify(newKeys)) {
        for (const layer of Object.values(_wxLayers)) map.removeLayer(layer);
        _wxLayers = {};
        _wxCurrent = null;
      }
      _wxFrames = frames;
      _wxIdx = frames.length - 1;
      _wxPreloadAll();
      _wxShowFrame(_wxIdx);
      const slider = document.getElementById('wx-slider');
      slider.max = frames.length - 1;
      slider.value = _wxIdx;
      document.getElementById('wx-inline').classList.add('visible');
    })
    .catch(() => {});
  _wxRefreshTimer = setTimeout(() => { if (_wxActive) _wxFetch(); }, 600000);
}

function _wxShowFrame(idx) {
  const frame = _wxFrames[idx];
  if (!frame) return;
  if (_wxCurrent !== null && _wxLayers[_wxCurrent]) {
    _wxLayers[_wxCurrent].setOpacity(0);
  }
  if (_wxLayers[idx]) {
    _wxLayers[idx].setOpacity(0.5);
  }
  _wxCurrent = idx;
  const d = new Date(frame.time * 1000);
  document.getElementById('wx-time').textContent =
    d.getUTCHours().toString().padStart(2,'0') + ':' +
    d.getUTCMinutes().toString().padStart(2,'0') + ' UTC';
}

function wxSetFrame(idx) {
  _wxIdx = idx;
  _wxShowFrame(idx);
}

function wxTogglePlay() {
  const btn = document.getElementById('wx-play');
  if (_wxTimer) {
    clearInterval(_wxTimer);
    _wxTimer = null;
    btn.textContent = '▶';
  } else {
    btn.textContent = '⏸';
    _wxTimer = setInterval(() => {
      _wxIdx = (_wxIdx + 1) % _wxFrames.length;
      _wxShowFrame(_wxIdx);
      document.getElementById('wx-slider').value = _wxIdx;
    }, 700);
  }
}

// ── GMN multi-station orbit layer ────────────────────────────────────────────

function _gmnDefaultDate() {
  // Last completed UTC night = today UTC - 1 day
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);  // YYYY-MM-DD
}

function _gmnFormatTime(iso) {
  // "2026-05-21T01:23:45Z" => "2026-05-21 01:23:45 UTC"
  try {
    return iso.replace('T', ' ').replace('Z', '') + ' UTC';
  } catch(e) { return iso; }
}

/* YYYYMMDD => YYYYMMDD of the previous UTC day. */
function _gmnPrevYmd(ymd) {
  return _gmnShiftYmd(ymd, -1);
}

/* YYYYMMDD + offset days (UTC) => YYYYMMDD. */
function _gmnShiftYmd(ymd, off) {
  const y = +ymd.slice(0, 4), m = +ymd.slice(4, 6), d = +ymd.slice(6, 8);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + (off || 0));
  return dt.toISOString().slice(0, 10).replace(/-/g, '');
}

/* Whether a GMN station code is one of ours is decided by the config-derived
   code→host map (hostByCode, built from /api/stations) — no hard-coded RO/DE
   prefixes, so witness links work for any network's own station set. */
function _gmnStationList(stations, ymd, hostByCode) {
  if (!stations || !stations.length) return '—';
  return stations.map(s => {
    const sHtml = escHtml(s);
    const host = hostByCode && hostByCode[s];
    if (!host) return sHtml;  // not one of our stations → plain text, no link
    // Deep-link to the station's RMS / Detection tab, pre-selected to this
    // camera and scrolled to the relevant night. Even if no local clip is
    // matched, the user can browse that night's locked detections directly.
    const href = `/station/${encodeURIComponent(host)}/rms?cam=${encodeURIComponent(s)}&date=${encodeURIComponent(ymd)}`;
    return `<a href="${escHtml(href)}" target="_blank" class="gmn-witness-link"><b>${sHtml}</b></a>`;
  }).join(', ');
}

/* Compact "1.23" / "12.3" / "—" formatter. Drops trailing zeros at given
   precision and surfaces "—" for null / NaN so the popup grid is uniform. */
function _gmnFmt(v, digits) {
  if (v == null || Number.isNaN(v)) return '—';
  return Number(v).toFixed(digits ?? 2);
}

function _gmnPopupHtml(ev, localMatches, ymd, hostByCode) {
  const shower = escHtml(ev.shower || 'Sporadic');
  const mag = ev.peak_mag != null ? ev.peak_mag.toFixed(1) : '—';
  const vel = ev.velocity  != null ? ev.velocity.toFixed(1)  : '—';
  const altB = ev.altitude_begin != null ? ev.altitude_begin.toFixed(1) : '—';
  const altE = ev.altitude_end   != null ? ev.altitude_end.toFixed(1)   : '—';

  // Orbital elements (heliocentric) + geocentric radiant.
  // Only render the orbit block when we actually have at least one of
  // these values — keeps the popup tight on single-station / no-fit
  // events where GMN didn't compute a full orbit.
  let orbitBlock = '';
  if (ev.orbit_a != null || ev.orbit_e != null || ev.orbit_i != null) {
    const a    = _gmnFmt(ev.orbit_a, 2);
    const e    = _gmnFmt(ev.orbit_e, 3);
    const i    = _gmnFmt(ev.orbit_i, 1);
    const peri = _gmnFmt(ev.orbit_peri, 1);
    const node = _gmnFmt(ev.orbit_node, 1);
    const q    = _gmnFmt(ev.orbit_q, 3);
    const ra   = _gmnFmt(ev.ra_geo, 1);
    const dec  = _gmnFmt(ev.dec_geo, 1);
    orbitBlock =
      `<div style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border);font-size:11px;line-height:1.6">` +
      `<div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px">Orbit</div>` +
      `<div>Radiant: <b>RA ${ra}&deg; Dec ${dec}&deg;</b></div>` +
      `<div>a = <b>${a}</b> AU &middot; e = <b>${e}</b> &middot; i = <b>${i}&deg;</b></div>` +
      `<div>&omega; = <b>${peri}&deg;</b> &middot; &Omega; = <b>${node}&deg;</b> &middot; q = <b>${q}</b> AU</div>` +
      `</div>`;
  }
  // Local matches: all matching clips side-by-side in a horizontal flex row
  // (scrolls horizontally if more clips than fit). Each clip has its own
  // station link above the video player.
  let localBlock = '';
  if (localMatches && localMatches.length) {
    const cells = localMatches.map(m => {
      const videoUrl = m.source === 'archive'
        ? `/api/archive/file/${encodeURIComponent(m.cam)}/${m.date}/meteors/${encodeURIComponent(m.filename)}`
        : `/video/${encodeURIComponent(m.host)}/${encodeURIComponent(m.cam)}/${m.date}/${encodeURIComponent(m.filename)}`;
      const stationUrl = `/station/${encodeURIComponent(m.host)}/rms?cam=${encodeURIComponent(m.cam)}&date=${encodeURIComponent(m.date)}`;
      // window = [offset - 2 s, offset + duration + 3 s], clamped to 0.
      const winStart = Math.max(0, m.offset_s - 2);
      const winEnd   = m.offset_s + m.duration_s + 3;
      const clipLabel = `${escHtml(m.host)} / ${escHtml(m.cam)}`;
      return `<div class="gmn-clip">
        <div class="gmn-clip-label">
          <a href="${escHtml(stationUrl)}" target="_blank">${clipLabel}</a>
        </div>
        <video src="${escHtml(videoUrl)}#t=${winStart},${winEnd}" controls preload="auto" muted playsinline
               data-win-start="${winStart}" data-win-end="${winEnd}"
               data-video-url="${escHtml(videoUrl)}"
               data-station-url="${escHtml(stationUrl)}"
               data-clip-label="${clipLabel}"
               title="Click to enlarge"
               onclick="_gmnMaybeOpenClipModal(event,this)"
               class="gmn-sync-video"></video>
      </div>`;
    }).join('');
    localBlock =
      `<div style="display:flex;align-items:center;gap:8px;margin-top:6px">` +
      `<div style="font-size:10px;color:var(--muted)">ROVIMEN local clip${localMatches.length > 1 ? 's' : ''} (${localMatches.length}) — −2s before to +3s after meteor</div>` +
      `<button class="gmn-sync-btn" onclick="_gmnTogglePlayAll(this)" style="margin-left:auto;background:var(--blue);color:#0d1117;border:none;border-radius:3px;padding:3px 10px;font-size:11px;font-weight:700;cursor:pointer;font-family:inherit">&#9654; Play all</button>` +
      `</div><div class="gmn-clips">${cells}</div>`;
  }
  return `<div style="font-family:'SF Mono',Consolas,monospace;font-size:12px;line-height:1.6;color:var(--text)">` +
    `<div style="margin-bottom:4px;font-weight:700;color:#e6edf3">${escHtml(_gmnFormatTime(ev.time))}</div>` +
    `<div>Shower: <b>${shower}</b></div>` +
    `<div>Peak mag: <b>${mag}</b></div>` +
    `<div>Velocity: <b>${vel} km/s</b></div>` +
    `<div>Altitude: <b>${altB} &rarr; ${altE} km</b></div>` +
    orbitBlock +
    `<div style="margin-top:4px;font-size:11px;line-height:1.5">Stations: ${_gmnStationList(ev.stations, ymd, hostByCode)}</div>` +
    localBlock +
    `</div>`;
}

/* Build a host -> our station codes lookup from /api/stations, used by
   _gmnMatchLocalChunks to translate a GMN station code (e.g. RO000A) into
   our dashboard's host_key. Cached at module scope so we don't refetch it
   every popup. */
let _gmnHostForCode = null;
async function _gmnBuildHostMap() {
  if (_gmnHostForCode) return _gmnHostForCode;
  _gmnHostForCode = {};
  try {
    const meta = await fetchJson('/api/stations');
    for (const [host, m] of Object.entries(meta || {})) {
      for (const c of (m.cameras || [])) {
        if (c.code) _gmnHostForCode[c.code] = host;
      }
    }
  } catch(e) {}
  return _gmnHostForCode;
}

/* Find local chunks whose meteor_time falls within +/-2 s of the GMN event
   begin time. detectionsByDate is a {ymd: payload} map covering BOTH the
   GMN UTC date and the previous one, because color_capture's night folder
   rolls over at noon UTC — a 01:14 UTC meteor on May 21 lives in the
   20260520/ folder. eventStations is the GMN witness list. */
function _gmnMatchLocalChunks(ev, detectionsByDate, hostMap) {
  if (!detectionsByDate || !ev.time) return [];
  // Normalize event time to ms.
  const t = Date.parse(ev.time.endsWith('Z') ? ev.time : ev.time.replace(' ', 'T') + 'Z');
  if (Number.isNaN(t)) return [];
  const out = [];
  for (const code of ev.stations || []) {
    const host = hostMap[code];
    if (!host) continue;
    // Walk both candidate night folders and emit any chunk whose meteor
    // time lands within ±10 s of GMN's triangulated event time. The old
    // ±2 s window was too tight: RMS records detections at FF block
    // boundaries (~10 s granularity) and GMN's solver rounds its
    // emitted time, so a meaningful fraction of true matches sit
    // 3-10 s apart even when the same physical meteor is the subject.
    // The same chunk can't exist in two date folders so dedup isn't
    // needed.
    for (const [ymd, data] of Object.entries(detectionsByDate)) {
      const st = data && data.by_station && data.by_station[host];
      if (!st || !st.cameras) continue;
      const chunks = st.cameras[code] || [];
      for (const c of chunks) {
        if (!c.meteor_time) continue;
        const ct = Date.parse(c.meteor_time.endsWith('Z') ? c.meteor_time : c.meteor_time + 'Z');
        if (Number.isNaN(ct)) continue;
        if (Math.abs(ct - t) > 10000) continue;
        // detection_offset_s = seconds into the clip where the meteor begins.
        // duration_s (from RMS metadata) defaults to a safe 0.5 s.
        const offset = (typeof c.detection_offset_s === 'number') ? c.detection_offset_s : 0;
        const dur = (c.rms && typeof c.rms.duration_s === 'number') ? c.rms.duration_s : 0.5;
        out.push({
          host, cam: code, date: ymd,
          filename: c.filename, meteor_time: c.meteor_time,
          offset_s: offset, duration_s: dur,
          source: c.source || 'station',
        });
      }
    }
  }
  return out;
}

/* Tracks the timeupdate listeners so we can detach them when a popup closes. */
let _gmnSyncListeners = [];

function _gmnAttachSyncedPlayback(e) {
  const popupEl = e.popup && e.popup.getElement();
  if (!popupEl) return;
  const videos = Array.from(popupEl.querySelectorAll('video.gmn-sync-video'));
  if (!videos.length) return;
  // Each video has its own meteor window. Seek + play simultaneously.
  // Autoplay only works while muted — controls remain visible so the user
  // can unmute / scrub manually if they want sound or a different range.
  for (const v of videos) {
    const winStart = parseFloat(v.dataset.winStart || '0');
    const winEnd   = parseFloat(v.dataset.winEnd   || '0');
    const onTimeUpdate = () => {
      if (winEnd > 0 && v.currentTime >= winEnd) {
        v.pause();
        v.currentTime = winStart;  // rewind so a second click on Play replays cleanly
      }
    };
    v.addEventListener('timeupdate', onTimeUpdate);
    _gmnSyncListeners.push([v, onTimeUpdate]);
    // Surface 404 / decode errors inline so a broken clip shows a useful
    // message instead of a black box that quietly does nothing.
    const onError = () => {
      v.dataset.gmnErrored = '1';
      const cell = v.closest('.gmn-clip');
      if (cell && !cell.querySelector('.gmn-clip-error')) {
        const msg = document.createElement('div');
        msg.className = 'gmn-clip-error';
        msg.textContent = 'Clip unavailable';
        cell.appendChild(msg);
      }
    };
    v.addEventListener('error', onError, { once: true });
    _gmnSeekAndPlayOne(v);
  }
}

function _gmnPausePopupVideos(e) {
  const popupEl = e.popup && e.popup.getElement();
  if (popupEl) popupEl.querySelectorAll('video').forEach(v => v.pause());
  // Detach timeupdate listeners so they don't leak across popup opens.
  for (const [v, fn] of _gmnSyncListeners) v.removeEventListener('timeupdate', fn);
  _gmnSyncListeners = [];
}

/* Seek one video to its window start and resume playback. Defers the seek
   until the element has metadata so a click that lands before the file is
   loaded actually takes effect (previous version swallowed the
   InvalidStateError from currentTime= and then play()'d from t=0). */
function _gmnSeekAndPlayOne(v) {
  if (v.dataset.gmnErrored === '1') return;            // bad clip; don't fight it
  const ws = parseFloat(v.dataset.winStart || '0');
  const seek = () => {
    try { v.currentTime = ws; } catch (_) {}
    const p = v.play();
    if (p && typeof p.catch === 'function') {
      p.catch(err => {
        // Autoplay block (NotAllowedError) is the realistic failure case for
        // a synthetic click chain. Surface it so the user knows to use the
        // native controls rather than wondering why nothing happens.
        console.warn('[gmn] play() rejected:', err && err.name, err && err.message);
      });
    }
  };
  if (v.readyState >= 1) seek();
  else v.addEventListener('loadedmetadata', seek, { once: true });
}

/* ── Enlarged clip modal (D3b) ─────────────────────────────────────────
   Click a witness clip in the popup → open the same clip in a large
   modal player. Mirrors the per-station detection-page modal but kept
   self-contained on this page (no dashboard-station.js dependency).
   Note: native <video> controls eat their own click for play/pause, so
   we only open the modal on clicks outside the controls strip — checked
   by event.offsetY < (videoHeight - controls strip). Place rotate(...)
   as the rightmost / first-applied op so user pan operates in screen
   coordinates rather than the rotated image frame. */
window._gmnMaybeOpenClipModal = function(ev, vidEl) {
  // Browsers don't expose the controls hit area directly, but the strip
  // is ~32 px tall along the bottom. Skip the modal open if the click
  // landed in that band so the native Play / Mute / scrub controls work
  // as-expected; otherwise (click on the video frame itself) enlarge.
  const rect = vidEl.getBoundingClientRect();
  const localY = ev.clientY - rect.top;
  if (rect.height > 0 && localY > rect.height - 40) return;
  ev.preventDefault();
  ev.stopPropagation();
  _gmnOpenClipModal(vidEl);
};

// GMN clip modal — shared VideoModal instance (no trim, no nav)
const _gmnClipModal = new VideoModal(
  document.getElementById('gmn-clip-modal-container'),
  { trim: false, nav: false }
);
window._VideoModalCloseAll = () => VideoModal.closeAll();

window._gmnOpenClipModal = function(vidEl) {
  const url      = vidEl.dataset.videoUrl;
  const winStart = parseFloat(vidEl.dataset.winStart || '0');
  const winEnd   = parseFloat(vidEl.dataset.winEnd   || '0');
  const label    = vidEl.dataset.clipLabel  || '';
  const stationUrl = vidEl.dataset.stationUrl || '';
  if (!url) return;
  // Pause every inline witness video so the modal player gets the focus.
  document.querySelectorAll('.gmn-sync-video').forEach(v => v.pause());
  // Build title — keep the station link if available
  const titleHtml = stationUrl
    ? `<a href="${escHtml(stationUrl)}" target="_blank" style="color:inherit">${escHtml(label)}</a>`
    : escHtml(label);
  // Use #t=start,end fragment so the browser seeks automatically
  _gmnClipModal.open({
    src:   `${url}#t=${winStart},${winEnd}`,
    title: label,
  });
  // Overwrite title span innerHTML to support the station hyperlink
  const titleEl = _gmnClipModal._el?.querySelector('.vm-title');
  if (titleEl) titleEl.innerHTML = titleHtml;
};

window._gmnCloseClipModal = function() {
  _gmnClipModal.close();
};

/* Play-all button: re-seek every video to its window start and play them
   together. Useful after the auto-play first loop has paused. */
window._gmnTogglePlayAll = function(btn) {
  const popupEl = btn.closest('.leaflet-popup-content');
  if (!popupEl) return;
  const videos = Array.from(popupEl.querySelectorAll('video.gmn-sync-video'))
    .filter(v => v.dataset.gmnErrored !== '1');
  if (!videos.length) return;
  // Toggle: if any video is currently playing, pause everything; else seek
  // all to their window starts and play together.
  const playing = videos.some(v => !v.paused);
  if (playing) {
    videos.forEach(v => v.pause());
    btn.innerHTML = '&#9654; Play all';
  } else {
    videos.forEach(_gmnSeekAndPlayOne);
    btn.innerHTML = '&#10073;&#10073; Pause';
  }
};

async function _gmnFetchAndDraw(date) {
  const banner    = document.getElementById('gmn-banner');
  const dateInput = document.getElementById('gmn-date-input');
  if (!_gmnActive) return;

  // Clear existing layer
  if (_gmnLayer) { map.removeLayer(_gmnLayer); _gmnLayer = null; }
  _gmnLayer = L.layerGroup().addTo(map);

  if (banner) { banner.style.display = 'block'; banner.textContent = 'Loading GMN data…'; }

  // Fan out fetches in parallel: GMN events + local detections + cam-code map.
  // Local detections are fetched for a 4-day window centered on the picked
  // night (-2 ... +1 day in UTC) so chunks from the night before / after
  // are also available — meteor_time on the GMN side is the trajectory
  // begin UTC, which can sit on either calendar day around midnight, and
  // the storage-box archive fallback in _compute_detections_payload makes
  // older dates still match if the station has long-rolled the locked
  // chunk off local disk.
  const ymd = date.replace(/-/g, '');
  const dateRange = [-2, -1, 0, 1].map(off => _gmnShiftYmd(ymd, off));
  const fetches = [
    fetchJson(`/api/gmn/multistation/${date}`),
    _gmnBuildHostMap(),
    ...dateRange.map(d => fetchJson(`/api/detections/${d}`).catch(() => null)),
  ];
  const results = await Promise.allSettled(fetches);
  const gmnRes = results[0];
  const hostMap = results[1];
  const data = gmnRes.status === 'fulfilled' ? gmnRes.value : null;
  const hostByCode = hostMap.status === 'fulfilled' ? hostMap.value : {};
  const detectionsByDate = {};
  for (let i = 0; i < dateRange.length; i++) {
    const r = results[i + 2];
    if (r.status === 'fulfilled' && r.value) detectionsByDate[dateRange[i]] = r.value;
  }

  if (!data) {
    if (banner) banner.textContent = 'GMN data unavailable — network error';
    return;
  }

  if (data.error) {
    if (banner) {
      banner.textContent = 'GMN upstream unavailable — retrying every 2 h';
      banner.title = data.error;
    }
    return;
  }
  if (!Array.isArray(data.events) || !data.events.length) {
    if (banner) {
      banner.textContent = 'No orbits for this night — try another date';
      banner.style.color = 'var(--red)';
    }
    return;
  }

  const blueColor = '#388bfd';
  const grayColor = '#6e7681';

  // Magnitude-scaled traces — bumped contrast pass.
  // Brighter meteors (more negative absolute magnitude) get thicker, more
  // opaque polylines. Width range is now 2..10 px (was 1..6) so the
  // brightest events really pop on the map. Clamp peak_mag to [-5, +5].
  // Sporadic / null mag falls back to a sensible mid weight so events
  // without GMN photometry stay visible. DE-only events keep the muted
  // overlay treatment but still scale.
  const _gmnTraceFromMag = function(peakMag, deOnly) {
    if (peakMag == null || Number.isNaN(peakMag)) {
      return deOnly ? { weight: 2, opacity: 0.40 }
                    : { weight: 3, opacity: 0.85 };
    }
    const clamped = Math.max(-5, Math.min(5, peakMag));
    const t = (clamped + 5) / 10;            // 0 (bright) .. 1 (dim)
    const widthBright = 10, widthDim = 2;
    const w = Math.max(2, widthDim + (widthBright - widthDim) * (1 - t));
    const a = deOnly ? (0.20 + 0.45 * (1 - t))   // 0.20 dim .. 0.65 bright
                     : (0.45 + 0.55 * (1 - t));  // 0.45 dim .. 1.00 bright
    return { weight: w, opacity: a };
  };

  /* Direction arrow — small triangle marker at the trace's end point
     rotated to the bearing from begin → end so users can see which way
     the meteor flew without inferring it from the dot. Falls back to no
     arrow if either endpoint is missing (defensive — the parent guard
     already filters those events out). */
  const _gmnArrowMarker = function(latlngBegin, latlngEnd, color, opacity, weight) {
    const [lat1, lon1] = latlngBegin;
    const [lat2, lon2] = latlngEnd;
    // Spherical bearing in degrees clockwise from north.
    const φ1 = lat1 * Math.PI / 180, φ2 = lat2 * Math.PI / 180;
    const Δλ = (lon2 - lon1) * Math.PI / 180;
    const y = Math.sin(Δλ) * Math.cos(φ2);
    const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
    const bearing = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
    // Scale arrow with line weight so brighter (thicker) traces also get
    // a beefier arrowhead. Clamp so it's never microscopic on dim events.
    const size = Math.max(10, Math.round(weight * 1.8 + 6));
    const half = size / 2;
    const icon = L.divIcon({
      className: 'gmn-arrow',
      html: `<svg width="${size}" height="${size}" viewBox="0 0 20 20"
                  style="transform:rotate(${bearing}deg);transform-origin:50% 50%;display:block">
               <polygon points="10,1 18,17 10,13 2,17"
                        fill="${color}" fill-opacity="${opacity}"
                        stroke="#0d1117" stroke-width="1.2"/>
             </svg>`,
      iconSize: [size, size],
      iconAnchor: [half, half],
    });
    return L.marker(latlngEnd, { icon, interactive: false, keyboard: false });
  };

  for (const ev of data.events) {
    if (ev.lat_begin == null || ev.lon_begin == null ||
        ev.lat_end   == null || ev.lon_end   == null) continue;

    const color   = ev.de_only ? grayColor : blueColor;
    const { weight, opacity } = _gmnTraceFromMag(ev.peak_mag, ev.de_only);

    const localMatches = _gmnMatchLocalChunks(ev, detectionsByDate, hostByCode);
    // Popup width grows when we have an embedded video player.
    const popup     = _gmnPopupHtml(ev, localMatches, ymd, hostByCode);
    // 50 % bigger than before (was 320 / 260) so single-column clip stack
    // and metadata text breathe — readable on phone landscape too.
    const popupOpts = { maxWidth: localMatches.length ? 480 : 390 };

    const latlngs = [[ev.lat_begin, ev.lon_begin], [ev.lat_end, ev.lon_end]];

    // Invisible thick hitbox under the visible polyline so clicking
    // anywhere within ~12 px of the track opens the popup — the visible
    // 1.5-2.5 px line is way too narrow to hit reliably.
    const hitbox = L.polyline(latlngs, {
      color: '#000', weight: 14, opacity: 0,
      interactive: true, bubblingMouseEvents: false,
    }).bindPopup(popup, { ...popupOpts, className: 'gmn-popup' });

    const line = L.polyline(latlngs, {
      color, weight, opacity,
      interactive: false,
    });

    const dot = L.circleMarker([ev.lat_begin, ev.lon_begin], {
      radius: 4, color, fillColor: color, fillOpacity: 0.9,
      weight: 1, opacity
    }).bindPopup(popup, { ...popupOpts, className: 'gmn-popup' });

    _gmnLayer.addLayer(hitbox);
    _gmnLayer.addLayer(line);
    _gmnLayer.addLayer(dot);
    _gmnLayer.addLayer(_gmnArrowMarker(latlngs[0], latlngs[1], color, opacity, weight));
  }

  // Auto-sync videos when a popup opens: seek each to its win-start, then
  // play them simultaneously (muted, the only way browsers permit autoplay).
  // Each video pauses itself when it crosses win-end so the clips loop
  // through their meteor window cleanly.
  _gmnLayer.on('popupopen', _gmnAttachSyncedPlayback);
  _gmnLayer.on('popupclose', _gmnPausePopupVideos);

  // Fetch monthly count for the selected date's month
  let monthCount = null;
  const selectedMonth = date.slice(0, 7);
  try {
    const mdata = await fetchJson(`/api/gmn/monthly_count?month=${selectedMonth}`);
    monthCount = mdata.ro_orbit_count;
  } catch(e) {}

  if (banner) {
    banner.style.color = '';
    if (monthCount != null) {
      const now = new Date();
      const isCurrentMonth = selectedMonth === `${now.getUTCFullYear()}-${String(now.getUTCMonth()+1).padStart(2,'0')}`;
      const label = isCurrentMonth ? 'This month' : selectedMonth;
      banner.textContent = `${label} RO cameras contributed to the calculation of ${monthCount} orbits`;
    } else {
      banner.textContent = `Showing ${data.events.length} GMN orbits for ${date}`;
    }
  }
}

function toggleGmnLayer(btn) {
  _gmnActive = !_gmnActive;
  btn.classList.toggle('active', _gmnActive);
  localStorage.setItem('ov_gmn_layer', _gmnActive ? '1' : '0');

  const dateRow   = document.getElementById('gmn-date-row');
  const banner    = document.getElementById('gmn-banner');
  const dateInput = document.getElementById('gmn-date-input');

  if (_gmnActive) {
    if (dateInput && !dateInput.value) dateInput.value = _gmnDefaultDate();
    if (dateRow) dateRow.style.display = 'flex';
    if (banner)  banner.style.display  = 'block';
    _gmnFetchAndDraw(dateInput ? dateInput.value : _gmnDefaultDate());
  } else {
    if (_gmnLayer) { map.removeLayer(_gmnLayer); _gmnLayer = null; }
    if (dateRow) dateRow.style.display = 'none';
    if (banner)  banner.style.display  = 'none';
  }
}

function onGmnDateChange(val) {
  if (!_gmnActive || !val) return;
  _gmnFetchAndDraw(val);
}

let map;
function initMap(data) {
  mapData=data;
  map=L.map('map',{zoomControl:false,scrollWheelZoom:true,zoomSnap:0.25,zoomDelta:0.25,wheelPxPerZoomLevel:200});
  // Fit to all visible stations on first load so the fleet is in view
  // regardless of where future stations land. Falls back to a Europe-wide
  // view if no station has coords yet.
  const _bpts = [];
  for (const [k, info] of Object.entries(data || {})) {
    const lat = info.lat ?? STATION_COORDS[k]?.lat;
    const lon = info.lon ?? STATION_COORDS[k]?.lon;
    if (lat == null || lon == null || info.show_on_map === false) continue;
    _bpts.push([lat, lon]);
  }
  if (_bpts.length >= 2) {
    map.fitBounds(L.latLngBounds(_bpts), {padding: [40, 40], maxZoom: 7});
  } else if (_bpts.length === 1) {
    map.setView(_bpts[0], 7);
  } else {
    map.setView([46.5, 25.5], 6);
  }
  L.control.zoom({position: window.innerWidth <= 768 ? 'bottomright' : 'topright'}).addTo(map);
  // Custom pane for LP overlay — sits above tiles but not dimmed by tile brightness filter
  map.createPane('lpPane');
  map.createPane('wxPane');
  // Restore saved layer + brightness
  const savedLayer = localStorage.getItem('ov_map_layer') || 'std';
  const savedBrightness = parseInt(localStorage.getItem('ov_map_brightness') || '50', 10);
  const brightnessSlider = document.getElementById('map-brightness');
  if (brightnessSlider) brightnessSlider.value = savedBrightness;
  const savedLayerBtn = document.getElementById(`ml-${savedLayer}`);
  if (savedLayerBtn) {
    document.querySelectorAll('.map-layer-btns .fov-btn').forEach(b=>b.classList.remove('active'));
    savedLayerBtn.classList.add('active');
  }
  const {url, opts} = MAP_LAYERS[savedLayer] || MAP_LAYERS.std;
  _tileLayer = L.tileLayer(url, opts).addTo(map);
  const brightnessRow = document.getElementById('brightness-row');
  if (savedLayer === 'short') {
    if (brightnessRow) brightnessRow.classList.add('hidden');
  } else {
    document.querySelector('.leaflet-tile-pane') && (document.querySelector('.leaflet-tile-pane').style.filter = `brightness(${savedBrightness/100})`);
  }

  // Restore LP overlay state
  if (localStorage.getItem('ov_lp_layer') === '1') {
    _lpLayer = L.tileLayer(LP_TILE_URL, LP_OPTS).addTo(map);
    const lpBtn = document.getElementById('ml-lp');
    if (lpBtn) lpBtn.classList.add('active');
  }

  // Restore GMN orbit layer state
  if (localStorage.getItem('ov_gmn_layer') === '1') {
    _gmnActive = true;
    const gmnBtn    = document.getElementById('ml-gmn');
    const dateRow   = document.getElementById('gmn-date-row');
    const dateInput = document.getElementById('gmn-date-input');
    if (gmnBtn) gmnBtn.classList.add('active');
    if (dateInput && !dateInput.value) dateInput.value = _gmnDefaultDate();
    if (dateRow) dateRow.style.display = 'flex';
    // defer draw until after map is fully set up (footprintLayer etc.)
    setTimeout(() => { _gmnFetchAndDraw(dateInput ? dateInput.value : _gmnDefaultDate()); }, 0);
  }

  if (localStorage.getItem('ov_wx_layer') === '1') {
    _wxActive = true;
    const wxBtn = document.getElementById('ml-wx');
    if (wxBtn) wxBtn.classList.add('active');
    _wxFetch();
  }

  footprintLayer=L.layerGroup().addTo(map);
  drawFootprints(data, FOV_ALT_KM);

  map.on('click', ()=>{ closePanel(); });

  let i=0;
  for(const [key,info] of Object.entries(data)){
    const lat=info.lat??STATION_COORDS[key]?.lat;
    const lon=info.lon??STATION_COORDS[key]?.lon;
    if(!lat||!lon||info.show_on_map===false){i++;continue}
    const online=info.online;
    const isCommissioning = info.status === 'commissioning';
    const dotColor = isCommissioning ? '#e89b3c' : (online ? '#3fb950' : '#f85149');
    const icon=L.divIcon({
      className:'',
      html:`<div class="ov-marker-dot" style="width:14px;height:14px;border-radius:50%;
        background:${dotColor};
        border:2px solid #0d1117;box-shadow:0 0 6px ${dotColor}"></div>`,
      iconSize:[14,14],iconAnchor:[7,7],
    });
    const marker=L.marker([lat,lon],{icon}).addTo(map);
    stationMarkers[key] = marker;
    stationInfoByKey[key] = info;
    marker.on('click', e=>{
      L.DomEvent.stopPropagation(e);
      // Multi-select: toggle this station in/out of the selection WITHOUT
      // zooming or recentring the map (issue #511).
      toggleStationSelection(key, info);
      // Re-assert the highlight: Leaflet may have re-rendered the icon element.
      _setMarkerSelected(key, state.selectedStations.has(key));
    });
    i++;
  }
}

// ── Network Status table (merged from /network page) ─────────────────────────
let _nwRows = [];
let _nwSortCol = 'online';
let _nwSortDir = -1;
// Offline smoothing is handled server-side (3-consecutive-failures)

function _nwFmtAge(isoStr) {
  if (!isoStr) return { text: '—', cls: 'na' };
  const dt = new Date(isoStr);
  if (isNaN(dt)) return { text: '—', cls: 'na' };
  const secs = Math.floor((Date.now() - dt) / 1000);
  if (secs < 0) return { text: 'just now', cls: 'fresh' };
  if (secs < 90)  return { text: secs + 's ago', cls: 'fresh' };
  if (secs < 3600) return { text: Math.floor(secs / 60) + 'm ago', cls: 'recent' };
  if (secs < 86400) return { text: Math.floor(secs / 3600) + 'h ago', cls: 'stale' };
  return { text: Math.floor(secs / 86400) + 'd ago', cls: 'old' };
}
function _nwMetricClass(val, warn, crit) {
  if (val == null) return 'na';
  return val >= crit ? 'crit' : val >= warn ? 'warn' : 'ok';
}
function _nwFmtPct(val) {
  if (val == null) return { text: '—', cls: 'na' };
  const n = Math.round(val);
  return { text: n + '%', cls: _nwMetricClass(n, 75, 90) };
}
function _nwFmtTemp(val) {
  if (val == null) return { text: '—', cls: 'na' };
  const n = Math.round(val);
  return { text: n + '°', cls: _nwMetricClass(n, 65, 80) };
}
function _nwFmtDisk(pct) {
  if (pct == null || Number.isNaN(pct)) return { text: '—', cls: 'na' };
  const n = Math.round(pct);
  return { text: n + '%', cls: _nwMetricClass(n, 80, 93) };
}
function _nwCamSvcStatus(camCode, services) {
  if (!services || !camCode) return 'svc-unknown';
  for (const [k, v] of Object.entries(services)) {
    if (k.includes(camCode)) {
      return (v.active === true || v.state === 'active' || v.running === true) ? 'svc-ok' : 'svc-dead';
    }
  }
  return 'svc-unknown';
}

function nwBuildRows(overviewData, statusAllData) {
  const rows = [];
  for (const [key, info] of Object.entries(overviewData)) {
    const status = (statusAllData || {})[key] || {};
    const services = status.services || {};
    const cameras = (info.cameras || []).map(c => ({
      code: c.code, svcCls: _nwCamSvcStatus(c.code, services),
    }));
    let diskPct = null;
    if (info.disk != null) {
      if (typeof info.disk === 'object') {
        if (info.disk.pct != null) diskPct = info.disk.pct;
        else if (info.disk.total_mb) diskPct = (info.disk.used_mb / info.disk.total_mb) * 100;
        else if (info.disk.total_gb) diskPct = (info.disk.used_gb / info.disk.total_gb) * 100;
      } else if (typeof info.disk === 'number') diskPct = info.disk;
    }
    const stationStatus = info.status || 'active';
    rows.push({
      key, label: info.label || '', online: info.online ? 1 : 0,
      stationStatus,
      last_seen: info.last_updated || null,
      cpu: info.cpu_pct ?? null, ram: info.ram_pct ?? null, temp: info.temp_c ?? null,
      diskPct, cameras,
    });
  }
  return rows;
}

function nwSort(col) {
  if (_nwSortCol === col) _nwSortDir *= -1;
  else { _nwSortCol = col; _nwSortDir = col === 'key' ? 1 : -1; }
  document.querySelectorAll('.nw-table th').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.col === col) th.classList.add(_nwSortDir === 1 ? 'sort-asc' : 'sort-desc');
  });
  _nwRenderTable();
}

function _nwSortRows(rows) {
  const col = _nwSortCol, dir = _nwSortDir;
  return [...rows].sort((a, b) => {
    let va, vb;
    switch (col) {
      case 'key':       va = a.key; vb = b.key; break;
      case 'online':    va = a.online; vb = b.online; break;
      case 'last_seen': va = a.last_seen ? new Date(a.last_seen).getTime() : 0;
                        vb = b.last_seen ? new Date(b.last_seen).getTime() : 0; break;
      case 'cpu':  va = a.cpu  ?? -1; vb = b.cpu  ?? -1; break;
      case 'ram':  va = a.ram  ?? -1; vb = b.ram  ?? -1; break;
      case 'temp': va = a.temp ?? -1; vb = b.temp ?? -1; break;
      case 'disk': va = a.diskPct ?? -1; vb = b.diskPct ?? -1; break;
      default: return 0;
    }
    if (va < vb) return -dir;
    if (va > vb) return dir;
    return a.key.localeCompare(b.key);
  });
}

function _nwRenderTable() {
  const tbody = document.getElementById('nw-tbody');
  const cardsEl = document.getElementById('nw-cards');
  if (!tbody || !_nwRows.length) return;
  const sorted = _nwSortRows(_nwRows);
  let html = '', cardHtml = '';
  for (const r of sorted) {
    const age = _nwFmtAge(r.last_seen), cpu = _nwFmtPct(r.cpu), ram = _nwFmtPct(r.ram);
    const temp = _nwFmtTemp(r.temp), disk = _nwFmtDisk(r.diskPct);
    const isComm = r.stationStatus === 'commissioning';
    const onCls = isComm ? 'comm' : (r.online ? 'up' : 'down');
    const onTxt = isComm ? 'Commissioning' : (r.online ? 'Online' : 'Offline');
    const onColor = isComm ? '#e89b3c' : (r.online ? 'var(--green)' : 'var(--red)');
    const camDots = r.cameras.map(c =>
      `<div class="nw-cam-dot" title="${escHtml(c.code)}"><div class="nw-cam-dot-circle ${c.svcCls}"></div><span class="nw-cam-dot-label">${escHtml(c.code)}</span></div>`
    ).join('');
    html += `<tr class="${r.online ? '' : 'offline'}">
      <td><a class="nw-station-key" href="/station/${encodeURIComponent(r.key)}">${escHtml(r.key)}</a><div class="nw-station-label">${escHtml(r.label)}</div></td>
      <td><div class="nw-online-cell"><span class="nw-dot ${onCls}"></span><span class="nw-online-text ${onCls}">${onTxt}</span></div></td>
      <td><span class="nw-age ${age.cls}">${age.text}</span></td>
      <td><span class="nw-metric ${cpu.cls}">${cpu.text}</span></td>
      <td><span class="nw-metric ${ram.cls}">${ram.text}</span></td>
      <td><span class="nw-metric ${temp.cls}">${temp.text}</span></td>
      <td><span class="nw-metric ${disk.cls}">${disk.text}</span></td>
      <td><div class="nw-cam-dots">${camDots || '<span style="color:var(--muted)">—</span>'}</div></td>
    </tr>`;
    cardHtml += `<div class="nw-card ${r.online ? '' : 'offline'}">
      <div class="nw-card-top"><span class="nw-dot ${onCls}"></span><a class="nw-card-key" href="/station/${encodeURIComponent(r.key)}">${escHtml(r.key)}</a><span class="nw-card-label">${escHtml(r.label)}</span></div>
      <div class="nw-card-row"><span class="nw-card-field">Status: <b style="color:${onColor}">${onTxt}</b></span><span class="nw-card-field">Last polled: <b class="nw-age ${age.cls}">${age.text}</b></span></div>
      <div class="nw-card-row"><span class="nw-card-field">CPU: <b class="nw-metric ${cpu.cls}">${cpu.text}</b></span><span class="nw-card-field">RAM: <b class="nw-metric ${ram.cls}">${ram.text}</b></span><span class="nw-card-field">Temp: <b class="nw-metric ${temp.cls}">${temp.text}</b></span><span class="nw-card-field">Disk: <b class="nw-metric ${disk.cls}">${disk.text}</b></span></div>
      <div class="nw-cam-dots" style="margin-top:4px">${camDots || '<span style="color:var(--muted)">No cameras</span>'}</div>
    </div>`;
  }
  tbody.innerHTML = html;
  if (cardsEl) cardsEl.innerHTML = cardHtml;
}

function _nwRenderSummary(rows) {
  const comm = rows.filter(r => r.stationStatus === 'commissioning').length;
  const online = rows.filter(r => r.online && r.stationStatus !== 'commissioning').length;
  const total = rows.length;
  const offline = total - online - comm;
  const totalCams = rows.reduce((s, r) => s + r.cameras.length, 0);
  const el = document.getElementById('nw-summary');
  if (!el) return;
  let html = `
    <div class="nw-summary-item green"><b>${online}</b>Online</div>
    <div class="nw-summary-item red"><b>${offline}</b>Offline</div>`;
  if (comm > 0) html += `
    <div class="nw-summary-item" style="color:#e89b3c"><b>${comm}</b>Commissioning</div>`;
  html += `
    <div class="nw-summary-item blue"><b>${totalCams}</b>Cameras</div>`;
  el.innerHTML = html;
}

async function ovLoadNetStats() {
  const el = document.getElementById('ov-net-stats');
  if (!el) return;
  try {
    const r = await fetch('/api/network-stats');
    if (!r.ok) return;
    const d = await r.json();
    const s = d.network_summary || {};
    const dbl = d.double_station_coverage_km2 || {};
    const fmt = v => v != null ? Math.round(v).toLocaleString() : '—';
    el.style.display = '';
    let html = `
      <div class="ns-item"><span class="ns-val blue">${fmt(d.total_covered_area_km2)}</span><span class="ns-lbl">km&sup2; coverage (100 km)</span></div>
      <div class="ns-item"><span class="ns-val green">${fmt(dbl['100km'])}</span><span class="ns-lbl">km&sup2; double-station</span></div>
      <div class="ns-item"><span class="ns-val">${fmt(d.atmospheric_volume_km3)}</span><span class="ns-lbl">km&sup3; volume (25–130 km)</span></div>
      <div class="ns-item"><span class="ns-val yellow">${fmt(d.expected_meteoric_flux_per_hour)}/hr</span><span class="ns-lbl">sporadic flux (&gt;+4.5 mag)</span></div>`;
    const oc = d.orbit_counts;
    if (oc) {
      html += `
        <div class="ns-item"><span class="ns-val blue">${fmt(oc.last_12_months)}</span><span class="ns-lbl">ROVIMEN orbits (12 mo)</span></div>
        <div class="ns-item"><span class="ns-val">${fmt(oc.last_30_days)}</span><span class="ns-lbl">orbits (30 d)</span></div>
        <div class="ns-item"><span class="ns-val green">${fmt(oc.last_night)}</span><span class="ns-lbl">orbits ${oc.last_night_date ? '(' + oc.last_night_date + ')' : '(last night)'}</span></div>
        <div class="ns-item"></div>`;
    }
    el.innerHTML = html;
  } catch(e) {}
}

async function nwFetch() {
  try {
    const [overview, statusAll] = await Promise.all([fetchJson('/api/overview'), fetchJson('/api/status/all')]);
    _nwRows = nwBuildRows(overview, statusAll);
    _nwRenderSummary(_nwRows);
    _nwRenderTable();
    document.querySelectorAll('.nw-table th').forEach(th => {
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.dataset.col === _nwSortCol) th.classList.add(_nwSortDir === 1 ? 'sort-asc' : 'sort-desc');
    });
    const el = document.getElementById('nw-last-fetch');
    if (el) {
      const now = new Date();
      el.textContent = String(now.getUTCHours()).padStart(2,'0') + ':' +
        String(now.getUTCMinutes()).padStart(2,'0') + ':' +
        String(now.getUTCSeconds()).padStart(2,'0') + ' UTC';
    }
  } catch(e) {
    const tbody = document.getElementById('nw-tbody');
    if (tbody && !_nwRows.length) tbody.innerHTML = '<tr><td colspan="8" class="nw-error">Failed to load station data</td></tr>';
  }
}

function renderSummary(data) {
  const entries=Object.entries(data);
  const comm=entries.filter(([,d])=>d.status==='commissioning').length;
  const active=entries.filter(([,d])=>d.status!=='commissioning');
  const onlineSt=active.filter(([,d])=>d.online).length;
  const offlineSt=active.length-onlineSt;
  const onlineCams=active.filter(([,d])=>d.online).reduce((s,[,d])=>s+(d.cameras?.length||0),0);
  const offlineCams=active.filter(([,d])=>!d.online).reduce((s,[,d])=>s+(d.cameras?.length||0),0);
  const commCams=entries.filter(([,d])=>d.status==='commissioning').reduce((s,[,d])=>s+(d.cameras?.length||0),0);
  const summary=document.getElementById('ov-summary');
  summary.innerHTML =
    '<div class="ov-summary-hdr"></div>' +
    '<div class="ov-summary-hdr">Online</div>' +
    '<div class="ov-summary-hdr">Comm.</div>' +
    '<div class="ov-summary-hdr">Offline</div>' +
    '<div class="ov-summary-label">Stations</div>' +
    '<div class="ov-summary-cell green">' + onlineSt + '</div>' +
    '<div class="ov-summary-cell orange">' + comm + '</div>' +
    '<div class="ov-summary-cell red">' + offlineSt + '</div>' +
    '<div class="ov-summary-label">Cameras</div>' +
    '<div class="ov-summary-cell green">' + onlineCams + '</div>' +
    '<div class="ov-summary-cell orange">' + commCams + '</div>' +
    '<div class="ov-summary-cell red">' + offlineCams + '</div>';
  const capturingCams = entries.reduce((s, [,d]) => s + (_isRmsActive(d.services) ? (d.cameras?.length || 0) : 0), 0);
  const liveBar = document.getElementById('ov-live-bar');
  if (capturingCams > 0) {
    liveBar.style.display = 'flex';
    document.getElementById('ov-live-bar-btn').textContent = _liveVisible
      ? 'Hide Live View (' + capturingCams + ')'
      : 'Live View (' + capturingCams + ')';
  } else {
    liveBar.style.display = 'none';
  }
}

let _ovStationSelBuilt = false;
function ovBuildStationSelector(data) {
  if (_ovStationSelBuilt) return;
  _ovStationSelBuilt = true;
  const el = document.getElementById('ov-station-sel');
  if (!el) return;
  const hosts = Object.entries(data).sort((a,b) => a[0].localeCompare(b[0]));
  const opts = hosts.map(([k, info]) => {
    const dot = info.online ? 'dot-up' : 'dot-down';
    return `<a class="ov-station-opt" href="/station/${encodeURIComponent(k)}"><span class="pill-dot ${dot}"></span>${escHtml(k)} <span style="color:var(--muted);margin-left:auto;font-size:11px">${escHtml(info.label||'')}</span></a>`;
  }).join('');
  el.style.display = '';
  el.innerHTML = `<button class="ov-station-sel-btn" onclick="this.nextElementSibling.classList.toggle('open');event.stopPropagation()">Go to station &#9660;</button><div class="ov-station-menu" id="ov-station-menu">${opts}</div>`;
  document.addEventListener('click', () => { const m = document.getElementById('ov-station-menu'); if (m) m.classList.remove('open'); });
}

// Build skeleton HTML — one shimmering card per camera across all online
// stations, grouped by host so the cold-start layout previews the real grid.
// Only used by fetchAndRenderStacks on its first call (retryCount === 0).
function _ovBuildSkeletons(overviewData){
  let html = '';
  for (const [host, info] of Object.entries(overviewData || {})) {
    if (!info.online) continue;
    const cams = info.cameras || [];
    if (!cams.length) continue;
    let row = '';
    for (let i = 0; i < cams.length; i++) {
      row += `<div class="ov-stack-skeleton" aria-hidden="true">
        <div class="ov-stack-skeleton-img"></div>
        <div class="ov-stack-skeleton-meta"></div>
      </div>`;
    }
    html += `<div class="ov-stack-skeleton-group" aria-hidden="true">
      <div class="ov-stack-skeleton-label"></div>
      <div class="ov-stack-skeleton-row">${row}</div>
    </div>`;
  }
  return html;
}

/* Camera 180° rotation map keyed by ``host__cam``. Populated whenever
   fetchAndRenderStacks runs from overviewData.cameras[].rotate. RMS plot
   images (FF native sensor orientation) are now rotated client-side via
   the ``.thumb-rotated`` CSS class instead of via a server-side PIL pass.
   Station/archive colour stacks are pre-rotated upstream so they never
   get the class. The colour meteor stack filename is excluded for the
   same reason. See dashboard.css. */
const COLOR_METEOR_STACK_FILENAME = '__color_meteor_stack__.webp';

/* Colour the per-card date label by how recent the captured night is,
   so an offline-for-days station is visually obvious in the stacks grid.
   `dateYmd` is the upstream YYYYMMDD captured-night date (= the *start*
   of the night, so 20260521 = night of May 21→22 UTC). "Last night" =
   today UTC − 1 day → green; one day older → yellow; older still → red. */
function _ovDateAgeClass(dateYmd) {
  if (!dateYmd || dateYmd.length !== 8) return '';
  const y = +dateYmd.slice(0,4), m = +dateYmd.slice(4,6), d = +dateYmd.slice(6,8);
  const captured = Date.UTC(y, m - 1, d);
  const now = new Date();
  const todayUtc = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const days = Math.round((todayUtc - captured) / 86400000);
  if (days <= 1) return 'fresh';   // last completed night
  if (days === 2) return 'stale';  // one extra day old
  return 'old';
}

let _rotateByCam = {};
function _ovBuildRotateMap(overviewData) {
  const m = {};
  for (const [host, info] of Object.entries(overviewData || {})) {
    for (const c of (info.cameras || [])) {
      if (c && c.code) m[`${host}__${c.code}`] = !!c.rotate;
    }
  }
  return m;
}
function _ovCameraRotates(host, cam) {
  return !!_rotateByCam[`${host}__${cam}`];
}
function _ovImgUrlNeedsRotation(host, cam, imgUrl) {
  if (!_ovCameraRotates(host, cam)) return false;
  // The `rotate` flag describes the camera's *sensor* orientation. Only
  // raw frames inherit it — FF captured stacks, max/avepixel. Matplotlib
  // charts (radiants, fieldsums, astrometry/calibration variation,
  // FF intervals, observing periods) carry axis labels that must stay
  // upright. The colour meteor stack is pre-rotated upstream by the
  // station stacker, so it never qualifies either.
  try {
    const u = new URL(imgUrl, location.href);
    const parts = u.pathname.split('/');
    const i = parts.indexOf('plot_image');
    if (i < 0 || parts.length < i + 5) return false;
    const fn = decodeURIComponent(parts[i + 4]);
    if (fn === COLOR_METEOR_STACK_FILENAME) return false;
    return /_(stack|maxpixel|avepixel)\./i.test(fn);
  } catch (e) {
    return false;
  }
}

async function fetchAndRenderStacks(overviewData, retryCount = 0) {
  const grid = document.getElementById('ov-stacks-grid');
  _rotateByCam = _ovBuildRotateMap(overviewData);
  // On the very first call (cold start), paint skeleton placeholders so the
  // stacks section is never blank while /api/overview/stacks is in flight.
  // Subsequent retries leave the existing "Loading stacks…" message in place.
  if (retryCount === 0 && grid) {
    const skeletons = _ovBuildSkeletons(overviewData);
    if (skeletons) grid.innerHTML = skeletons;
  }
  try {
    const stacks = await fetchJson('/api/overview/stacks');
    if (!stacks.length) {
      // Cold dashboard restart — backend cache empty, prefetch in flight.
      // Retry a few times with backoff before giving up.
      if (retryCount < 6) {
        const delay = [500, 1500, 3000, 5000, 8000, 12000][retryCount];
        grid.innerHTML = `<div class="ov-stacks-loading">Loading stacks… (cache warming, retry ${retryCount + 1}/6)</div>`;
        setTimeout(() => fetchAndRenderStacks(overviewData, retryCount + 1), delay);
        return;
      }
      grid.innerHTML = '<div class="ov-stacks-loading">No captured stacks available</div>';
      return;
    }
    // Group by host
    const byHost = {};
    for (const s of stacks) {
      (byHost[s.host] = byHost[s.host] || []).push(s);
    }
    // Also include offline stations (no stacks) for completeness
    // Track cameras that have stacks so we can fetch their plot lists
    const camPlotTargets = [];
    let html = '';
    for (const [host, info] of Object.entries(overviewData)) {
      const online = info.online;
      const cams = byHost[host] || [];
      const offlineCams = (info.cameras || []).filter(c => !cams.find(s => s.cam === c.code));
      if (!cams.length && !offlineCams.length) continue;

      html += `<div class="ov-station-group">
        <div class="ov-station-label">
          <span class="dot ${online ? 'up' : 'down'}"></span>
          ${escHtml(host)} <span class="ov-station-loc">${escHtml(info.label)}</span>
        </div>
        <div class="ov-cam-grid">`;

      // Cameras with stacks — card without per-camera button row.
      // Plot switching is driven by the global toggle bar above the grid.
      // Click opens fullscreen modal; the small "↗" badge navigates to
      // the per-station detail page.
      for (const s of cams) {
        const imgUrl = `/api/rms/plot_image/${encodeURIComponent(s.host)}/${encodeURIComponent(s.cam)}/${encodeURIComponent(s.date)}/${encodeURIComponent(s.filename)}`;
        const dateStr = String(s.date || '').replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3');
        const ageCls = _ovDateAgeClass(s.date);
        const imgId = `ov-cam-img-${s.host}-${s.cam}`;
        // Captured stacks are FF-derived (native sensor orientation). Apply
        // the 180° client-side rotation class when the camera's rotate flag
        // is set; the colour meteor stack filename never qualifies (it's
        // already station-rotated upstream).
        const rotCls = _ovImgUrlNeedsRotation(s.host, s.cam, imgUrl) ? ' class="thumb-rotated"' : '';
        // JSON.stringify gives a properly JS-escaped string literal; escHtml
        // then makes it safe to embed inside an HTML attribute value.
        const onclickArgs = `${escHtml(JSON.stringify(imgId))},${escHtml(JSON.stringify(s.host))},${escHtml(JSON.stringify(s.cam))},${escHtml(JSON.stringify(dateStr))}`;
        html += `<div class="ov-cam-card-wrap">
          <div class="ov-cam-card" role="button" tabindex="0"
               title="${escHtml(s.cam)} — ${escHtml(dateStr)} (click to expand)"
               onclick="ovOpenCardImg(${onclickArgs})"
               onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();ovOpenCardImg(${onclickArgs})}">
            <a class="ov-cam-jump" href="/station/${encodeURIComponent(s.host)}/rms?cam=${encodeURIComponent(s.cam)}"
               title="Open station page" onclick="event.stopPropagation()">↗</a>
            <img id="${escHtml(imgId)}"${rotCls} src="${escHtml(imgUrl)}" data-default-src="${escHtml(imgUrl)}" data-host="${escHtml(s.host)}" data-cam="${escHtml(s.cam)}" alt="${escHtml(s.cam)}" loading="lazy" decoding="async"
                 onerror="this.style.opacity='0.3';this.onerror=null">
            <div class="ov-cam-code">${escHtml(s.cam)} <span class="ov-cam-date ${ageCls}">${escHtml(dateStr)}</span></div>
          </div>
        </div>`;
        camPlotTargets.push({ host: s.host, cam: s.cam, date: s.date, imgId });
      }

      // Cameras without stacks (offline or no data)
      for (const c of offlineCams) {
        html += `<div class="ov-cam-card-wrap">
          <a href="/station/${encodeURIComponent(host)}/rms?cam=${encodeURIComponent(c.code)}" class="ov-cam-card no-stack" title="${escHtml(c.code)} — no data">
            <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='120'%3E%3C/svg%3E" alt="${escHtml(c.code)}" decoding="async">
            <div class="ov-cam-code">${escHtml(c.code)} <span class="ov-cam-date">no data</span></div>
          </a>
        </div>`;
      }

      html += '</div></div>';
    }
    grid.innerHTML = html;

    // Fetch every camera's plot list in parallel, then build ONE global
    // toggle row from the union of labels, rendered above the stacks grid.
    // PRE-POPULATE plotsByCam with an empty entry for every imgId so failed
    // fetches still appear in Object.entries() — otherwise the lazy refresh
    // can't see them and they stay frozen forever.
    const plotsByCam = {};  // imgId -> { label -> url }
    for(const t of camPlotTargets) plotsByCam[t.imgId] = {};
    await Promise.all(camPlotTargets.map(async (t) => {
      try {
        const r = await fetch(`/api/rms/plots/${t.host}/${t.cam}/${t.date}`);
        if (!r.ok) return;
        const plots = await r.json();
        const m = {};
        for (const p of plots) {
          m[p.label] = `/api/rms/plot_image/${t.host}/${t.cam}/${t.date}/${encodeURIComponent(p.filename)}`;
        }
        plotsByCam[t.imgId] = m;
      } catch(e) {}
    }));
    renderOverviewPlotToggle(plotsByCam);
    // Cards whose plot-list fetch failed have empty {} entries here. Schedule
    // aggressive early retries (3s, 8s, 20s) in addition to the 60s
    // setInterval — the dashboard-side cache typically warms within a few
    // seconds of restart, but during that window the first JS fetch may
    // race ahead of the dashboard's own prefetch loop.
    [3000, 8000, 20000].forEach(ms =>
      setTimeout(() => { try { ovRefreshMissingPlotLists(); } catch(e) {} }, ms)
    );
  } catch (e) {
    grid.innerHTML = '<div class="ov-stacks-loading">Failed to load stacks</div>';
  }
}

// Short labels keyed by the server's label string. Order here controls the
// displayed order of buttons in the global toggle bar.
// Strict whitelist — only these plot labels render as toggle buttons on the
// overview. Anything else returned by /api/rms/plots is ignored. Order here
// controls the displayed order.
const OV_PLOT_LABEL_ORDER = [
  'Captured stack', 'Meteor stack', 'Radiants', 'Field sums',
  'Astrometry calibration', 'Calibration variation',
  'FF intervals', 'Observing periods',
];
const OV_PLOT_SHORT = {
  'Captured stack':          'All stack',
  'Meteor stack':            'Meteor stack',
  'Radiants':                'Radiants',
  'Field sums':              'Fieldsums',
  'Astrometry calibration':  'Astrometry',
  'Calibration variation':   'Calib var',
  'FF intervals':            'FF intervals',
  'Observing periods':       'Obs periods',
};

// `plotsByCam` is { imgId: { label: url } }. Render one button per unique
// label that exists on at least one camera. Clicking a button swaps EVERY
// camera card's thumbnail to that plot (cameras missing that plot keep
// their default stack so the grid never goes blank).
function renderOverviewPlotToggle(plotsByCam){
  const bar = document.getElementById('ov-plot-toggle-bar');
  if(!bar) return;
  const union = new Set();
  for(const m of Object.values(plotsByCam)) for(const lbl of Object.keys(m)) union.add(lbl);
  // Strict whitelist: only render buttons for labels in OV_PLOT_LABEL_ORDER,
  // in that exact order. Anything else the API returns (thumbs, detected,
  // photometry, etc.) is intentionally hidden.
  const ordered = OV_PLOT_LABEL_ORDER.filter(l => union.has(l));
  if(!ordered.length){ bar.style.display = 'none'; return; }
  const plotsByCamJson = JSON.stringify(plotsByCam).replace(/"/g, '&quot;');
  bar.dataset.plotsByCam = plotsByCamJson;
  const buttons = ordered.map(lbl => {
    const short = OV_PLOT_SHORT[lbl] || lbl;
    const active = lbl === 'Captured stack' ? ' active' : '';
    return `<button class="ov-plot-toggle-btn${active}" data-label="${lbl}"
              onclick="ovGlobalPlotSwitch(this,'${lbl.replace(/'/g,"\\'")}')">${short}</button>`;
  }).join('');
  bar.innerHTML = `<span class="ov-plot-toggle-bar-label">Show plot:</span>${buttons}`;
  bar.style.display = 'flex';
}

function ovUpdateZoom(val){
  const px = parseInt(val, 10) || 160;
  // Scale every camera card + its image to the slider value.
  document.querySelectorAll('.ov-cam-card-wrap').forEach(w => { w.style.width = px + 'px'; });
  document.querySelectorAll('.ov-cam-card').forEach(c => { c.style.width = px + 'px'; });
  document.querySelectorAll('.ov-cam-card img').forEach(img => {
    img.style.width  = px + 'px';
    img.style.height = Math.round(px * 0.75) + 'px';  // keep the 4:3 feel
  });
}

/* Inline SVG placeholder rendered on cards that don't have the selected
   plot type (e.g. clicking 'Meteor stack' on a station with zero meteors).
   Without this, those cards keep showing their captured_stack thumbnail
   and the toggle looks broken. */
function _ovNoDataTile(label){
  const t = (label || 'plot').replace(/[<>&"]/g, '');
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 160 120'>
    <rect width='160' height='120' fill='#161b22'/>
    <text x='50%' y='46%' fill='#6e7681' font-family='monospace' font-size='10'
          text-anchor='middle' dominant-baseline='middle'>no data</text>
    <text x='50%' y='62%' fill='#484f58' font-family='monospace' font-size='9'
          text-anchor='middle' dominant-baseline='middle'>${t}</text>
  </svg>`;
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}

function ovGlobalPlotSwitch(btn, label){
  const bar = document.getElementById('ov-plot-toggle-bar');
  if(!bar) return;
  bar.querySelectorAll('.ov-plot-toggle-btn.active').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  bar.dataset.activeLabel = label;
  const noDataTile = _ovNoDataTile(label);
  let plotsByCam = {};
  try {
    plotsByCam = JSON.parse((bar.dataset.plotsByCam || '{}').replace(/&quot;/g, '"'));
  } catch(e) {}
  for(const [imgId, byLabel] of Object.entries(plotsByCam)){
    const img = document.getElementById(imgId);
    if(!img) continue;
    const url = byLabel[label];
    if(url){
      img.src = url;
      img.dataset.hasPlot = '1';
    } else if(label === 'Captured stack'){
      // Captured stack is the default; every camera has it (it's how they
      // ended up in the grid in the first place). Restore the default src.
      img.src = img.dataset.defaultSrc || img.src;
      img.dataset.hasPlot = '1';
    } else {
      // No data for this plot type on this camera — show the placeholder.
      img.src = noDataTile;
      img.dataset.hasPlot = '0';
    }
    // Re-evaluate the rotation class against the new src. The inline SVG
    // no-data placeholder never qualifies (it's not a station image).
    const h = img.dataset.host || '';
    const cam = img.dataset.cam || '';
    if (_ovImgUrlNeedsRotation(h, cam, img.src)) img.classList.add('thumb-rotated');
    else img.classList.remove('thumb-rotated');
  }
  // Lazy-refetch plot lists for any cards whose plotsByCam entry is empty
  // or stale — this typically happens when /api/rms/plots calls failed
  // during the initial fetchAndRenderStacks pass (cold cache, slow station).
  ovRefreshMissingPlotLists();
}

/* Open the currently-displayed thumbnail in the fullscreen modal.
   Pulls live src from the <img> so whatever plot is on screen — captured
   stack, meteor stack, radiants, etc. — is what zooms. The .thumb-rotated
   class is propagated to the modal img by ovPlotOpen so the 180° flip
   stays consistent between card and fullscreen view. */
function ovOpenCardImg(imgId, host, cam, dateStr){
  const img = document.getElementById(imgId);
  if(!img) return;
  const bar = document.getElementById('ov-plot-toggle-bar');
  const lbl = (bar && bar.dataset.activeLabel) || 'Captured stack';
  ovPlotOpen(img.src, `${cam} · ${dateStr} · ${lbl}`, _ovImgUrlNeedsRotation(host, cam, img.src));
}

/* Re-fetch /api/rms/plots for cards whose plotsByCam entry is empty or
   missing the labels we know exist on at least one camera. Patches the
   stored plotsByCam in-place and reapplies the active label. Cheap on
   warm cache; safe to call periodically. */
async function ovRefreshMissingPlotLists(){
  const bar = document.getElementById('ov-plot-toggle-bar');
  if(!bar) return;
  let plotsByCam = {};
  try {
    plotsByCam = JSON.parse((bar.dataset.plotsByCam || '{}').replace(/&quot;/g, '"'));
  } catch(e) { return; }
  const targets = [];
  for(const [imgId, byLabel] of Object.entries(plotsByCam)){
    if(!byLabel || !Object.keys(byLabel).length){
      const img = document.getElementById(imgId);
      if(!img) continue;
      // The img's default src is /api/rms/plot_image/<host>/<cam>/<date>/<file>
      // — parse it to recover host, cam, date for the refetch.
      const u = new URL(img.dataset.defaultSrc || img.src, location.href);
      const parts = u.pathname.split('/');
      // /api/rms/plot_image/<host>/<cam>/<date>/<file>
      const i = parts.indexOf('plot_image');
      if(i < 0 || parts.length < i + 4) continue;
      targets.push({ imgId, host: parts[i+1], cam: parts[i+2], date: parts[i+3] });
    }
  }
  if(!targets.length) return;
  let mutated = false;
  await Promise.all(targets.map(async t => {
    try {
      const r = await fetch(`/api/rms/plots/${t.host}/${t.cam}/${t.date}`);
      if(!r.ok) return;
      const plots = await r.json();
      const m = {};
      for(const p of plots){
        m[p.label] = `/api/rms/plot_image/${t.host}/${t.cam}/${t.date}/${encodeURIComponent(p.filename)}`;
      }
      if(Object.keys(m).length){
        plotsByCam[t.imgId] = m;
        mutated = true;
      }
    } catch(e) {}
  }));
  if(mutated){
    bar.dataset.plotsByCam = JSON.stringify(plotsByCam).replace(/"/g, '&quot;');
    // Re-render the toggle bar — if the initial fetch left every entry
    // empty (all stations cold-cached) the bar was hidden by
    // renderOverviewPlotToggle. Now that lazy refresh has filled in
    // labels, redraw it. Preserve the currently-active label so the
    // user's selection survives the redraw.
    const prevActive = bar.querySelector('.ov-plot-toggle-btn.active');
    const prevLabel = prevActive ? prevActive.dataset.label : (bar.dataset.activeLabel || 'Captured stack');
    renderOverviewPlotToggle(plotsByCam);
    // Restore active state on the matching button (default is 'Captured stack')
    if(prevLabel && prevLabel !== 'Captured stack'){
      bar.querySelectorAll('.ov-plot-toggle-btn').forEach(b => {
        if(b.dataset.label === prevLabel){
          bar.querySelectorAll('.ov-plot-toggle-btn.active').forEach(a => a.classList.remove('active'));
          b.classList.add('active');
        }
      });
    }
    // Reapply the active label to swap thumbnails for newly-fetched cameras
    const noDataTile = _ovNoDataTile(prevLabel);
    for(const [imgId, byLabel] of Object.entries(plotsByCam)){
      const img = document.getElementById(imgId);
      if(!img) continue;
      const url = byLabel[prevLabel];
      if(url){ img.src = url; }
      else if(prevLabel === 'Captured stack'){ img.src = img.dataset.defaultSrc || img.src; }
      else { img.src = noDataTile; }
      const h = img.dataset.host || '';
      const cam = img.dataset.cam || '';
      if (_ovImgUrlNeedsRotation(h, cam, img.src)) img.classList.add('thumb-rotated');
      else img.classList.remove('thumb-rotated');
    }
  }
}

(async function(){
  try{
    const auth=await fetchJson('/api/auth/status');
    const el=document.getElementById('ov-auth');
    if(auth.user){
      const color=auth.admin?'var(--green)':'var(--blue)';
      el.innerHTML=`<span style="color:${color}">${escHtml(auth.user)}</span>
        <a href="/logout" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Logout</a>`;
    }else{
      el.innerHTML=`<a href="/login" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Login</a>`;
    }
  }catch(e){}

  try{
    const meta=await fetchJson('/api/stations');
    for(const [key,m] of Object.entries(meta)){
      if(m.lat&&m.lon) STATION_COORDS[key]={lat:m.lat,lon:m.lon};
    }
  }catch(e){}

  try{
    // Progressive render: paint summary + map as soon as /api/overview lands
    // (one round-trip, ~140 ms TTFB from Romania), THEN fan out platepars in
    // parallel and refine the map markers when each arrives. Previously this
    // awaited every platepar before showing anything, blocking first paint
    // by ~N × RTT on top of the initial fetch.
    const data=await fetchJson('/api/overview');
    initMap(data);
    renderSummary(data);
    ovBuildStationSelector(data);
    // Stacks + Bortle don't block paint either — they fire in the background
    // and self-render once data arrives.
    fetchAndRenderStacks(data);
    loadBortleData(data);
    nwFetch();
    // Platepars: parallel, non-blocking. Each one refines its map polygon
    // when it arrives. Stations without a platepar just skip silently.
    // Network stats load AFTER all platepars are fetched (they populate
    // the server-side platepar_store that the stats endpoints read from).
    const onlineKeys=Object.entries(data).filter(([,i])=>i.online).map(([k])=>k);
    let _ppRedrawScheduled=false;
    function _schedulePlateparRedraw(){
      if(_ppRedrawScheduled) return;
      _ppRedrawScheduled=true;
      requestAnimationFrame(()=>{
        _ppRedrawScheduled=false;
        try{ if(footprintLayer&&map) drawFootprints(data, FOV_ALT_KM); }catch(e){}
      });
    }
    Promise.allSettled(onlineKeys.map(async key=>{
      try{
        const pp=await fetchJson(`/api/platepar/${key}`);
        if(pp&&typeof pp==='object'&&!pp.error){
          PLATEPAR_DATA[key]=pp;
          _schedulePlateparRedraw();
        }
      }catch(e){}
    })).then(() => { ovLoadNetStats(); });
    // Overview refresh — was 10 s, bumped to 30 s. Server cache TTL is 60 s
    // anyway and the response carries ETag/Cache-Control: max-age=30, so a
    // shorter interval just wastes round-trips. Also pause when tab hidden.
    setInterval(async()=>{
      if(document.hidden) return;
      try{ renderSummary(await fetchJson('/api/overview')); }catch(e){}
      try{ nwFetch(); }catch(e){}
    },30000);
    // Re-attempt plot-list fetches every 60s so cards whose initial fetch
    // missed (cold cache, slow station) eventually fill in. Cheap on the
    // warm dashboard-side cache.
    setInterval(()=>{
      if(document.hidden) return;
      try{ ovRefreshMissingPlotLists(); }catch(e){}
    }, 60000);
    // On tab refocus, catch up the paused pollers so the user doesn't stare
    // at up-to-30-s-stale data while the next setInterval tick is pending.
    document.addEventListener('visibilitychange', ()=>{
      if(document.hidden) return;
      (async()=>{ try{ renderSummary(await fetchJson('/api/overview')); }catch(e){} })();
      try{ nwFetch(); }catch(e){}
      try{ ovRefreshMissingPlotLists(); }catch(e){}
    });
    (function tickClock(){
      const el=document.getElementById('utc-clock');
      if(el){const n=new Date();el.textContent=String(n.getUTCHours()).padStart(2,'0')+':'+String(n.getUTCMinutes()).padStart(2,'0')+':'+String(n.getUTCSeconds()).padStart(2,'0')+' UTC';}
      setTimeout(tickClock,1000);
    })();
  }catch(e){
    document.getElementById('ov-summary').innerHTML='<div style="color:var(--red)">Failed to load station data</div>';
  }
})();
const _panelParam = new URLSearchParams(location.search).get('panel');
if (_panelParam) openAppPanel(_panelParam);

/* ── Overview plot modal with scroll-zoom ── */
let _ovPz = { scale: 1, tx: 0, ty: 0 };
/* When the card was 180°-rotated via the .thumb-rotated class, mirror that
   onto the modal img and bake rotate(180deg) into every inline transform
   the scroll-zoom handlers write (otherwise the inline style overrides the
   class and the modal flips to right-side-up mid-zoom). Place rotate(...)
   as the rightmost / first-applied op so user pan operates in screen
   coordinates rather than the rotated image frame. */
function _ovTransformSuffix(img) {
  return img.classList.contains('thumb-rotated') ? ' rotate(180deg)' : '';
}
function ovPlotOpen(imgUrl, label, rotated = false) {
  const modal = document.getElementById('ov-plot-modal');
  const img = document.getElementById('ov-plot-modal-img');
  document.getElementById('ov-plot-modal-title').textContent = label;
  img.src = imgUrl;
  document.getElementById('ov-plot-modal-dl').href = imgUrl;
  _ovPz = { scale: 1, tx: 0, ty: 0 };
  if (rotated) img.classList.add('thumb-rotated');
  else img.classList.remove('thumb-rotated');
  img.style.transform = rotated ? 'rotate(180deg)' : '';
  img.style.cursor = 'zoom-in';
  // _modalOpen handles the body-scroll lock now (in dashboard-common.js).
  _modalOpen(modal);
}
function ovPlotClose() {
  const img = document.getElementById('ov-plot-modal-img');
  _modalClose(document.getElementById('ov-plot-modal'));
  img.src = '';
  img.classList.remove('thumb-rotated');
  img.style.transform = '';
}
// Escape for ov-plot-modal is handled by the shared dispatcher in dashboard-common.js.
(function() {
  const img = document.getElementById('ov-plot-modal-img');
  img.addEventListener('wheel', e => {
    e.preventDefault();
    _ovPz.scale = Math.max(1, Math.min(10, _ovPz.scale * (e.deltaY < 0 ? 1.2 : 0.83)));
    if (_ovPz.scale <= 1.05) { _ovPz = { scale: 1, tx: 0, ty: 0 }; }
    img.style.transform = `scale(${_ovPz.scale})${_ovTransformSuffix(img)}`;
    img.style.cursor = _ovPz.scale > 1 ? 'grab' : 'zoom-in';
  }, { passive: false });
  let dragging = false, sx = 0, sy = 0;
  img.addEventListener('mousedown', e => {
    if (_ovPz.scale <= 1) return;
    dragging = true; sx = e.clientX - _ovPz.tx; sy = e.clientY - _ovPz.ty;
    img.style.cursor = 'grabbing';
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    _ovPz.tx = e.clientX - sx; _ovPz.ty = e.clientY - sy;
    img.style.transform = `translate(${_ovPz.tx}px,${_ovPz.ty}px) scale(${_ovPz.scale})${_ovTransformSuffix(img)}`;
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    img.style.cursor = _ovPz.scale > 1 ? 'grab' : 'zoom-in';
  });
})();

// ── Window exposure for HTML onclick/oninput handlers ────────────────────────
// Functions called from inline event handlers in the HTML template must be
// on the global (window) scope because ES module scope is not accessible
// from HTML attributes.
window.toggleAllFov = toggleAllFov;
window.setFovAlt = setFovAlt;
window.switchMapLayer = switchMapLayer;
window.toggleLayerMenu = toggleLayerMenu;
window.toggleCovLayer = toggleCovLayer;
window.toggleGmnLayer = toggleGmnLayer;
window.toggleLpLayer = toggleLpLayer;
window.toggleWeatherLayer = toggleWeatherLayer;
window.ovPlotClose = ovPlotClose;
window.ovPlotOpen = ovPlotOpen;
window.ovGlobalPlotSwitch = ovGlobalPlotSwitch;
window.ovOpenCardImg = ovOpenCardImg;
window.openAppPanel = openAppPanel;
window.appPanelClose = appPanelClose;
window.nwSort = nwSort;
window.closeLiveModal = closeLiveModal;
window.toggleLiveView = toggleLiveView;
window.wxTogglePlay = wxTogglePlay;
window.wxSetFrame = wxSetFrame;
window.onGmnDateChange = onGmnDateChange;
window.ovLiveUpdateZoom = ovLiveUpdateZoom;
window.ovUpdateZoom = ovUpdateZoom;
window.setMapBrightness = setMapBrightness;
