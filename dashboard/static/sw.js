/*
 * ROVIMEN dashboard Service Worker
 * --------------------------------
 *
 * Purpose: cut the first-paint RTT for repeat visits, especially for
 * Romanian users hitting the Hetzner dashboard at ~70 ms. The page shell
 * (HTML + dashboard-common.js + dashboard-station.js + dashboard.css) is
 * served from cache while a background fetch refreshes it, so navigation
 * paints instantly and the fresh copy is ready for the next visit.
 *
 * Cache strategy per route class:
 *
 *   - Navigation requests (mode === 'navigate'):
 *       stale-while-revalidate against '/'. Serve cached shell immediately,
 *       update cache in the background. Offline keeps showing last-good UI.
 *
 *   - Static assets under '/static/' (and the SW-served '/sw.js'):
 *       cache-first with background revalidation. dashboard-*.js / .css
 *       are large and change on deploy, but we tolerate one stale paint
 *       after a deploy in exchange for instant load.
 *
 *   - API routes (/api/*, /thumbnail/*, /video/*, /timelapse/*, /stream/*,
 *     /download/*):
 *       NETWORK ONLY, no interception. These already have correct upstream
 *       Cache-Control / ETag headers, support SSE streams, ranged requests
 *       for video, etc.  Caching them in the SW would break freshness and
 *       streaming semantics.
 *
 *   - Cross-origin requests (unpkg.com Leaflet, etc.):
 *       pass through to network. We don't own these caches.
 *
 *   - Everything else (same-origin, unclassified):
 *       network-first with cache fallback so a flaky connection still has
 *       something to display.
 *
 * Cache versioning
 * ----------------
 * `CACHE_VERSION` is part of the cache name. Bump it whenever you change:
 *   - the list of pre-cached shell URLs,
 *   - the routing rules below,
 *   - or want to force every client to drop its old cache.
 *
 * The `activate` handler deletes any cache whose name starts with
 * `rovimen-shell-` but is not the current version, so the bump fully
 * evicts old shells on the next SW activation.
 */

// v22: force every client to drop its cached shell + precached JS after the
// fleet-wide read-access fix and the footage-grid changes, so stale dashboards
// (e.g. missing lock icons / cross-station views) pick up the new behaviour.
// v6: networkFirstWithCacheFallback returns a synthetic 503 Response on
// cold-miss + network failure instead of re-throwing, so the page's fetch
// resolves cleanly and the console no longer fills with "Uncaught (in
// promise) TypeError: Failed to fetch" and "FetchEvent ... resulted in a
// network error response: the promise was rejected" entries every time a
// background route is briefly unreachable (D2 / D7 in David's report).
// v5: ship NaN-guards in dashboard-station.js (vitals + storage card render
// "—" instead of "NaN GB / NaN GB · null%" when a station returns online=true
// but its psutil/disk fields are missing — e.g. gmn0007 with psutil uninstalled).
// v4: dropped '/' from SHELL_URLS + made navigation network-only so stale
// cached HTML cannot bypass the auth gate (see fetch handler below).
// TODO (#347): CACHE_VERSION is bumped manually on every deploy, which is
// error-prone and has caused stale-cache bugs when forgotten. Ideally this
// should be auto-generated from the build hash (BUILD_VERSION in
// rovimen_dashboard.py) — either by serving sw.js through a Flask route that
// injects the version, or by having the CI build step (build_deploy.py)
// stamp the value before bundling.
const CACHE_VERSION = 'v23';
const CACHE_NAME = `rovimen-shell-${CACHE_VERSION}`;

// Resources to warm the cache with on install. The HTML shell ('/') is
// deliberately NOT here — it is auth-gated and must always go to the
// network so the gate (and any 302 → /login) runs on every load. We only
// pre-cache static assets that are safe to serve to anyone.
const SHELL_URLS = [
  '/static/dashboard-common.js',
  '/static/twilight-slider.js',
  '/static/dashboard-rms.js',
  '/static/dashboard-vdb.js',
  '/static/dashboard-archive.js',
  '/static/dashboard-sysadmin.js',
  '/static/dashboard-station.js',
  '/static/dashboard.css',
];

// Route classes — paths under these prefixes are NEVER intercepted, so the
// browser talks straight to Flask / nginx. Order does not matter; matching
// is a simple "startsWith" against the URL pathname.
const NETWORK_ONLY_PREFIXES = [
  '/api/',
  '/thumbnail/',
  '/video/',
  '/timelapse/',
  '/stream/',
  '/download/',
];

// ─── install ────────────────────────────────────────────────────────────────
// Pre-warm the cache with the shell. skipWaiting() promotes this SW to
// active immediately, so a returning user gets the new behavior without
// having to close every tab.
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting()),
  );
});

// ─── activate ───────────────────────────────────────────────────────────────
// Drop every old `rovimen-shell-*` cache that isn't us, then claim all open
// clients so this SW controls them on the very first load (without needing
// a manual reload).
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(
      names
        .filter((name) => name.startsWith('rovimen-shell-') && name !== CACHE_NAME)
        .map((name) => caches.delete(name)),
    );
    await self.clients.claim();
  })());
});

// ─── helpers ────────────────────────────────────────────────────────────────

function isNetworkOnly(pathname) {
  for (const prefix of NETWORK_ONLY_PREFIXES) {
    if (pathname.startsWith(prefix)) return true;
  }
  return false;
}

function isStaticAsset(pathname) {
  return pathname.startsWith('/static/') || pathname === '/sw.js';
}

// Cache-first: return cached response if present, kick off a background
// refresh either way so the next call has fresh bytes. Only successful
// (status 200, basic/same-origin) responses are written back to the cache.
async function cacheFirstWithRevalidate(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  const networkFetch = fetch(request).then((response) => {
    if (response && response.status === 200 && response.type === 'basic') {
      cache.put(request, response.clone()).catch(() => {});
    }
    return response;
  }).catch(() => null);

  if (cached) {
    // Don't await — background revalidation, but swallow rejections so the
    // SW doesn't surface "unhandled" errors when offline.
    networkFetch.catch(() => {});
    return cached;
  }
  // Cold miss — wait for network. If network also fails, surface the error.
  const fresh = await networkFetch;
  if (fresh) return fresh;
  return new Response('Offline and not cached', { status: 503, statusText: 'Offline' });
}

// Stale-while-revalidate against the shell ('/'). Navigation requests can
// have any path (e.g. /station/foo, /admin) but our SPA-style shell at '/'
// is the same HTML for all of them, so we serve that one cached response
// while refreshing it in the background.
async function navigationStaleWhileRevalidate(request) {
  const cache = await caches.open(CACHE_NAME);
  const cachedShell = await cache.match('/');
  const networkFetch = fetch(request).then((response) => {
    // Only refresh the shell entry from same-origin GET responses for '/'.
    // Other navigation targets (/station/foo) return the same shell HTML
    // but we don't want to overwrite the canonical '/' entry with a URL-
    // specific response if Flask ever differentiates them.
    const url = new URL(request.url);
    if (response && response.status === 200 && url.pathname === '/') {
      cache.put('/', response.clone()).catch(() => {});
    }
    return response;
  }).catch(() => null);

  if (cachedShell) {
    networkFetch.catch(() => {});
    return cachedShell;
  }
  const fresh = await networkFetch;
  if (fresh) return fresh;
  return new Response('Offline and shell not cached', { status: 503, statusText: 'Offline' });
}

// Network-first fallback for anything else same-origin. Tries the network,
// falls back to whatever (if anything) is cached. When both fail, we return
// a synthetic 503 Response instead of re-throwing so the page's fetch()
// resolves to a real Response object and can be handled with normal
// `if (!resp.ok)` branches. Re-throwing surfaces as "Uncaught (in promise)
// TypeError: Failed to fetch" in DevTools even when the page does have
// a .catch — Chrome attributes the rejection to the SW handler, not the
// page — so production consoles fill with noise on every transient
// network blip.
async function networkFirstWithCacheFallback(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response && response.status === 200 && response.type === 'basic') {
      cache.put(request, response.clone()).catch(() => {});
    }
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    return new Response('Offline and not cached', {
      status: 503,
      statusText: 'Offline',
      headers: { 'Content-Type': 'text/plain' },
    });
  }
}

// ─── fetch router ───────────────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
  const request = event.request;

  // Service Workers see every request the page makes. We only want to touch
  // GETs — POSTs/PUTs to /api/* must never be cached or replayed.
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Cross-origin (Leaflet CDN, OSM tiles, etc.) — pass through, we don't
  // own the cache and can't read opaque responses meaningfully anyway.
  if (url.origin !== self.location.origin) return;

  // API / media / stream routes — never intercept. Lets ETag, Range, and
  // SSE flows work exactly as they do without a SW installed.
  if (isNetworkOnly(url.pathname)) return;

  // Navigation requests (top-level HTML) — ALWAYS network. Caching auth-
  // gated HTML would render the dashboard shell to users whose session
  // has expired (or was never authenticated), defeating the gate. The
  // freshness cost is a single HTTP round-trip on page load.
  if (request.mode === 'navigate') return;

  // Static assets (JS/CSS/images under /static/, and /sw.js itself) —
  // cache-first with background refresh.
  if (isStaticAsset(url.pathname)) {
    event.respondWith(cacheFirstWithRevalidate(request));
    return;
  }

  // Everything else: prefer fresh, fall back to cache if offline.
  event.respondWith(networkFirstWithCacheFallback(request));
});

// Named exports for unit-testing pure helpers (ignored by the SW runtime).
export { isNetworkOnly, isStaticAsset };
