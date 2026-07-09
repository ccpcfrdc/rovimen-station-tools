# Reference consumer: the astromania.org `[rovimen]` integration

How the [astromania.org](https://astromania.org) WordPress plugin consumes
the [ROVIMEN Public API](public_api.md) to render a live meteor-network
dashboard behind a `[rovimen]` shortcode. This is a **reference consumer
implementation** — if you're building your own site against the public API,
copy this architecture rather than reinventing it.

This doc covers the *consumer* side only, and describes an external site that
is **not part of this repository**. For the API contract itself (endpoints,
auth, caching, rate limits) see [`public_api.md`](public_api.md).

The `[rovimen]` feature is self-contained in the plugin under
`includes/rovimen/` plus `assets/{js,css}/rovimen.js` and `rovimen.css`; the
file paths below illustrate that layout.

## Why this architecture

Three constraints shaped every decision below:

1. **The API key must never reach the browser.** All calls to the ROVIMEN
   dashboard host happen server-side, in PHP. The browser only ever talks to
   WordPress's own `admin-ajax.php`.
2. **The public API has rate limits** (see `public_api.md`). A page with
   real visitor traffic hitting the upstream API on every page load would
   blow through them. So every upstream call is cached in a WP transient,
   and a cron job pre-warms those transients before visitors show up.
3. **Media must play in a `<video>` tag without exposing the upstream
   host.** Clip/stack/timelapse URLs come back from the API pointing at
   the dashboard's `/media/v1/*` paths; the plugin rewrites them to a
   WordPress proxy endpoint so the browser never needs a direct route to
   the ROVIMEN backend.

## Three-file architecture

```
includes/rovimen/
  class-rovimen-api.php            # thin HTTP client for the public API
  shortcode-rovimen-dashboard.php  # shortcode + AJAX handlers + HTML shell
  class-rovimen-cache-warmer.php   # WP-Cron job that pre-fills the transients
assets/js/rovimen.js                # all client-side rendering
assets/css/rovimen.css              # theming (Elementor-token-aware)
```

### 1. `class-rovimen-api.php` — the API client

A minimal wrapper class, `Astromania_Rovimen_API`, responsible only for
talking to the upstream API — no caching, no WordPress-specific rendering.

```php
private function base_url() {
    return defined('ROVIMEN_API_BASE_URL')
        ? rtrim(ROVIMEN_API_BASE_URL, '/')
        : self::DEFAULT_BASE;        // e.g. 'https://dashboard.example.org'
}

private function api_key() {
    return defined('ROVIMEN_API_KEY') ? ROVIMEN_API_KEY : '';
}
```

- Base URL and key are both **wp-config.php constants**, never hardcoded
  in a way that would leak into version control — see "Configuration"
  below.
- Generic `get($path, $query)` plus thin convenience wrappers:
  `stations()`, `stats($period)`, `detections($args)`, `events($args)`,
  `orbits($args)`, `fov($alt)` — one per public API endpoint the site uses.
- Every request: `wp_remote_get()` with `X-API-Key` header, 12s timeout,
  SSL verified, only HTTP 200 accepted. Any failure returns `null` — the
  caller decides how to degrade (empty state, stale cache, etc.).
- `proxy_media_url($url)` — rewrites any `.../media/v1/...` URL coming
  back from the API into a `admin-ajax.php?action=rovimen_media&path=...`
  URL, so the browser fetches media through WordPress instead of the
  dashboard host directly.

### 2. `shortcode-rovimen-dashboard.php` — glue + AJAX + caching

This file does three jobs at once — in a from-scratch build you'd likely
want these as separate concerns, but the pattern that matters is:

**Every upstream call is wrapped in a WP transient with a 1-hour TTL**
(`ROVIMEN_CACHE_TTL = HOUR_IN_SECONDS`). One AJAX action per API endpoint
the frontend needs:

| AJAX action | Public API call | Cache key |
|---|---|---|
| `rovimen_stats` | `/stats?period=month` | `rovimen_stats[_DATE]` |
| `rovimen_stats_night` | `/stats?period=tonight` | `rovimen_stats_night[_DATE]` |
| `rovimen_stations` | `/stations` | `rovimen_stations` |
| `rovimen_detections` | `/detections` | `rovimen_detections[_DATE]` |
| `rovimen_events` | `/events` | `rovimen_events[_DATE]` |
| `rovimen_hof` | `/events` (walks back up to 14 nights for brightest/longest) | `rovimen_hof_v2` |
| `rovimen_timelapses` | `/timelapses` | `rovimen_timelapses` |
| `rovimen_orbits` | `/orbit_stats` | `rovimen_orbit_stats_YYYY-MM` |
| `rovimen_globe` | `/orbits?month=` | `rovimen_globe_YYYY-MM` |
| `rovimen_fov` / `rovimen_fov40` | `/fov?alt=90` / `?alt=40` | `rovimen_fov` / `rovimen_fov40` |
| `rovimen_media` | `/media/v1/*` (streamed, not cached) | n/a |

All registered as both `wp_ajax_*` and `wp_ajax_nopriv_*` — this is a
public-facing dashboard, no login required.

**`rovimen_media` is the odd one out** — it's not a JSON call, it's a
byte-streaming proxy:
- Validates the requested path against `#^media/v1/[a-zA-Z0-9/_\-\.]+$#`
  before touching it — never pass the client-supplied path straight to
  a URL fetch.
- Detects iOS via User-Agent and appends `?format=mp4` to `.mkv` requests
  (Safari/iOS can't play Matroska; the color clips are H.264-in-MKV per
  `public_api.md`).
- Streams via cURL `HEADERFUNCTION`/`WRITEFUNCTION` callbacks rather than
  buffering the whole file in PHP memory, and forwards `Content-Length`,
  `Content-Range`, `ETag` so `<video>` scrubbing/seeking works.

The shortcode function itself (`astromania_rovimen_shortcode()`) just
enqueues `rovimen.css`/`rovimen.js` (versioned by `ASTROMANIA_VERSION`,
see below), localizes `RovimenData` (ajax URL + nonce) for the JS, and
prints the HTML shell (stat cards, map container, carousel container,
etc.) that the JS then populates.

### 3. `class-rovimen-cache-warmer.php` — the part that makes caching invisible

A WP-Cron job, scheduled hourly, that proactively calls every one of the
endpoints above and fills the transients **before** a real visitor's
request would trigger a cache miss. TTL here is set to 75 minutes
(longer than the hourly interval) so a delayed cron run doesn't create a
gap where the cache is empty.

This matters because WordPress's own cron only fires on incoming page
requests — on a low-traffic site it can silently skip its schedule. If
you replicate this, either accept the drift or add a real system cron
hitting `wp-cron.php?doing_wp_cron` on a schedule, bypassing WP's
request-triggered cron entirely.

The warmer also does the "last processed night" date arithmetic once,
here, so every action doesn't need to reimplement it: **before 12:00
UTC, use two days ago; after 12:00 UTC, use yesterday** — matching when
Romanian stations finish uploading a night's captures (see `public_api.md`
"Date semantics" for the API's own yesterday-UTC default, which is a
slightly different but related rule).

### Frontend (`assets/js/rovimen.js`)

Plain IIFE, no framework, no build step. Structure worth copying:

- **`fetchDeduped()` + `fetchWithRetry()`** — in-flight request
  deduplication (so two widgets asking for the same data in the same
  tick trigger one network call) plus retry-with-backoff and
  `AbortController`-based timeouts. Small, but removes an entire class of
  "why did this fire three times" bugs.
- One loader function per section (`loadStats()`, `loadEvents()`,
  `loadTimelapses()`, `loadGlobe()`, …), each calling its own AJAX action
  and rendering into its own DOM subtree — sections fail independently.
- Leaflet (map + FOV overlay layers), Three.js (3D orbit globe) and
  TopoJSON (Romania border) are all bundled locally rather than pulled
  from a CDN, so the page doesn't depend on third-party asset uptime.

### Theming (`assets/css/rovimen.css`)

CSS custom properties that fall back to hardcoded astromania.org brand
colors when the site's Elementor theme doesn't define its global color
variables, e.g.:

```css
--rov-primary: var(--e-global-color-primary, #00AAC4);
```

This lets the dashboard inherit a site-wide theme change automatically,
without needing a plugin update, while still rendering correctly
standalone.

## Configuration

Two WordPress constants (defined in `wp-config.php` or, for the Docker
dev environment, injected via `WORDPRESS_CONFIG_EXTRA` from a `.env` file):

```php
define('ROVIMEN_API_KEY', getenv('ROVIMEN_API_KEY') ?: '');
define('ROVIMEN_API_BASE_URL', 'https://dashboard.example.org'); // your dashboard host; omit to use the client default
```

Get a key from ops per the "minting a key" section in `public_api.md`.
Never commit the key — it lives in `.env` (gitignored), with `.env.example`
committed as a template.

## Cache-busting checklist

`ASTROMANIA_VERSION` (a plain string constant in `astromania.php`) is
passed as the asset version to every `wp_enqueue_script`/`_style` call
for `rovimen.js`/`rovimen.css`. **Bump it on every PR that touches either
file** — WordPress appends it as `?ver=X.X.X`, and browsers cache the
old file indefinitely if the version string doesn't change. This has bitten
this project more than once (see repo history for `astromania.php`).

## Minimal path to a new consumer site

If you're building this against a different CMS or a static site instead
of WordPress, the shape to replicate is:

1. A tiny server-side HTTP client holding the API key (never client JS).
2. One cached endpoint per public API route you need, TTL'd to something
   ≥ the rate-limit window in `public_api.md` (an hour is generous and
   safe to start with).
3. Optional: a background job to pre-warm those caches if you expect real
   traffic, so no visitor request is the one that pays for a cache miss.
4. A tiny media-proxy route if you want to hide the dashboard host from
   `<video src>` URLs — optional, since `/media/v1/*` is keyless and
   already safe to link to directly (see `public_api.md`); the proxy here
   exists mainly for the iOS MKV→MP4 remux, not for security.
5. Client-side JS that renders `stations`, `detections`, `events`,
   `timelapses`/`nightstacks`, and `stats` — pick whichever subset your
   site needs; astromania.org is the only consumer using all of them
   plus the 3D orbit globe (built from `/orbits`, not a public-API field —
   it's client-side Three.js rendering of trajectory data).
