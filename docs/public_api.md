# ROVIMEN Public API

Versioned, read-only, anonymous JSON + media surface served by
`rovimen_dashboard.py`. Designed for third-party consumers — the
[astromania.org](https://astromania.org) live-meteor page is the
reference consumer, but any camera operator can wire this into their
own website.

Base URLs (production):

```
https://<DASHBOARD_HOST>/api/public/v1/...
https://<DASHBOARD_HOST>/media/v1/...
```

`<DASHBOARD_HOST>` is the public hostname the dashboard is reachable
on — the ops team has the current value (Cloudflare Tunnel / ROVIMEN
VPS DNS). The Tailscale-net hostname is for internal admin use only
and won't resolve from astromania.org or end-user browsers; swap in
the real public host before pasting these URLs into a third-party site.

For **internal dev testing over the GMN tailnet**, the dev instance
is at:

```
https://dashboard.example.net:8443/api/public/v1
```

(Port 8443 routes to `/opt/rovimen-dev/`; bare `:443` goes to prod.)

> **Operator note** — set `ROVIMEN_PUBLIC_BASE_URL=https://<host>` on
> the dashboard service so generated `clip_url`/`stack_url`/etc fields
> always carry the canonical public origin. Otherwise the API falls
> back to `request.host_url`, which can yield an `http://` URL if the
> proxy in front doesn't forward `X-Forwarded-Proto: https` — and a
> Mixed-Content browser will refuse to play it inside an HTTPS page.

## Authentication

JSON endpoints under `/api/public/v1/*` require an API key on every
request. The discovery index (`GET /api/public/v1`) is keyless so
consumers can sanity-check the host before wiring up auth. Media files
under `/media/v1/*` stay keyless — once a consumer has fetched a JSON
response with `clip_url` / `stack_url` URLs, browsers and `<video>`
tags fetch them directly without being able to attach headers.

Supply your key via either of:

- `X-API-Key: <secret>` request header (preferred)
- `Authorization: Bearer <secret>`

The `?key=` query-string form is **not** accepted — query strings leak
into server access logs and `Referer` headers. Requests that include
`?key=` receive `HTTP 400` with a hint to use the header form instead.

Missing or invalid key returns `HTTP 401` with a JSON body:

```json
{"error": "missing_or_invalid_api_key", "detail": "supply your API key via the X-API-Key header or Authorization: Bearer <key>"}
```

Keys are minted manually by ops (`tools/mint_api_key.py`). Email
[Alex](mailto:your-network@example.org) for one — include a short label for
what you're building so it shows up in the audit log.

CORS stays open (`Access-Control-Allow-Origin: *`) so a key holder can
call from any origin. Per-IP **and** per-key rate limits apply (see
"Rate limits" below).

Source code: [`dashboard/public_api.py`](../dashboard/public_api.py),
[`dashboard/api_keys.py`](../dashboard/api_keys.py).

## Schema

- **Version**: `1.0.0` — bumped on breaking changes; additive fields
  do not bump.
- **Date format**: ISO `YYYY-MM-DD` (UTC night anchor) on the wire.
  Internally we still use compact `YYYYMMDD`, but consumers never
  see that.
- **Timestamps**: ISO 8601 with timezone (`...+00:00`).
- **Magnitude**: lower = brighter. -2.5 is brighter than 0.0.
- **Identifiers**: stable strings — safe to store and dedupe by.
  - `detection.id`: `"YYYY-MM-DD:station_id:camera:filename"` —
    embeds enough to reconstruct media URLs without an extra call.
  - `event.id`: `"gmn:<trajectory_id>"` for GMN-resolved events,
    `"local:<iso_time>"` for ROVIMEN-correlated events that haven't
    been picked up by GMN's trajectory solver yet.

## Endpoints

### `GET /api/public/v1`

API index — lists every endpoint, schema version, and a link to this
doc. Useful for client discovery.

```bash
curl https://<DASHBOARD_HOST>/api/public/v1
```

### `GET /api/public/v1/stations`

All stations that have opted into public exposure
(`public: true` in `dashboard_config.yaml`).

Response shape:

```json
{
  "stations": [
    {
      "id": "gmnro05",
      "label": "Bistrița",
      "location_name": "Bistrița, Bistrița-Năsăud",
      "latitude": 47.08,
      "longitude": 24.39,
      "country": "RO",
      "online": true,
      "last_seen_utc": "2026-05-24T08:42:44+00:00",
      "cameras": [
        {"code": "RO0003", "label": null, "azimuth": null, "elevation": null},
        {"code": "RO0004", "label": null, "azimuth": null, "elevation": null}
      ]
    }
  ],
  "count": 9
}
```

Cache: `public, max-age=300`.

### `GET /api/public/v1/stations/<station_id>`

Single station. Returns the same shape as one entry above. 404 if
the station isn't public or doesn't exist.

### `GET /api/public/v1/detections`

Locked color meteor clips captured by a single station/camera. This
is the main "what was visible last night" feed.

Query params (all optional):

| Param      | Type    | Default            | Notes |
|------------|---------|--------------------|-------|
| `date`     | string  | yesterday UTC      | ISO `YYYY-MM-DD` |
| `station`  | string  | all public         | Repeatable or CSV; e.g. `gmnro05` |
| `shower`   | string  | all                | IAU 3-letter code (e.g. `PER`, `GEM`) |
| `min_mag`  | number  |                    | Bright threshold (lower = brighter). Excludes detections with no magnitude. |
| `max_mag`  | number  |                    | Dim threshold |
| `limit`    | integer | 100                | Max 500 |
| `offset`   | integer | 0                  | Pagination cursor |
| `order`    | string  | `time`             | One of `time`, `time_desc`, `mag` |

Response shape (truncated):

```json
{
  "date": "2026-05-23",
  "count": 82,
  "total": 82,
  "offset": 0,
  "limit": 100,
  "detections": [
    {
      "id": "2026-05-23:gmnro03:RO000J:RO000J_20260524_000426_color.mkv",
      "station_id": "gmnro03",
      "station_label": "Ciocârlia",
      "camera": "RO000J",
      "date": "2026-05-23",
      "time_utc": "2026-05-24T00:04:37.871884",
      "shower": "SPO",
      "peak_magnitude": -1.33,
      "absolute_magnitude": null,
      "duration_s": 0.123,
      "angular_velocity_deg_s": 19.62,
      "ra_radiant_deg": null,
      "dec_radiant_deg": null,
      "radiant_elevation_deg": null,
      "solar_longitude_deg": 62.498,
      "fps": 25.0,
      "lock_type": "detection",
      "detection_offset_s": 11.87,
      "clip_url":      "https://.../media/v1/clip/RO000J/2026-05-23/RO000J_20260524_000426_color.mkv",
      "stack_url":     "https://.../media/v1/stack/RO000J/2026-05-23/RO000J_20260524_000426_stack.webp",
      "thumbnail_url": "https://.../media/v1/stack/RO000J/2026-05-23/RO000J_20260524_000426_stack.webp"
    }
  ]
}
```

Cache: `max-age=60` when `date` is today UTC (still being locked);
`max-age=3600` for any past night (sealed). The default — yesterday
UTC — is always in the sealed-night bucket. See "Date semantics"
below.

`peak_magnitude` is RMS's apparent magnitude when available; falls back
to absolute. `shower` is the IAU code RMS assigned at the time of
detection — `"SPO"` for sporadic.

### `GET /api/public/v1/detections/<detection_id>`

Fetch a single detection by its stable id. Useful for permalinks (a
WordPress shortcode like `[meteor id="…"]` can fetch this without
loading the full feed).

### `GET /api/public/v1/events`

Multi-station correlated events. Includes:

1. **GMN-resolved events** with a real trajectory + orbit (`source:
   "gmn"`, `trajectory_id` set, witnesses include foreign GMN sites
   as `{rms_code, country, public: false}` markers).
2. **ROVIMEN-correlated events** — same meteor seen by two or more of
   our cameras but not yet picked up by GMN's trajectory solver
   (`source: "rovimen"`, `trajectory: null`, witnesses all `public: true`).

The two are de-duplicated by time: a GMN event within ±3 s of a local
event suppresses the local entry.

Query params:

| Param            | Type    | Default       | Notes |
|------------------|---------|---------------|-------|
| `date`           | string  | yesterday UTC | ISO `YYYY-MM-DD` |
| `has_trajectory` | boolean | false         | When `true`/`1`/`yes`, return only GMN events with a resolved orbit. |
| `limit`          | integer | 200           | Max 500 |
| `offset`         | integer | 0             |       |

Response shape (one GMN event):

```json
{
  "id": "gmn:20260523120000_abc12",
  "trajectory_id": "20260523120000_abc12",
  "time_utc": "2026-05-23T12:00:00",
  "date": "2026-05-23",
  "witness_count": 3,
  "witnesses": [
    {
      "station_id": "gmnro03",
      "station_label": "Ciocârlia",
      "rms_code": "RO000J",
      "camera": "RO000J",
      "latitude": 44.8,
      "longitude": 26.68,
      "country": "RO",
      "time_utc": "2026-05-23T12:00:00.123",
      "clip_url":  "https://.../media/v1/clip/RO000J/2026-05-23/...mkv",
      "stack_url": "https://.../media/v1/stack/RO000J/2026-05-23/...webp",
      "public": true
    },
    {
      "rms_code": "HU0006",
      "country": "HU",
      "public": false
    }
  ],
  "shower": "PER",
  "peak_magnitude": -2.1,
  "velocity_km_s": 58.3,
  "trajectory": {
    "lat_begin": 44.8, "lon_begin": 26.7, "altitude_begin_km": 110.2,
    "lat_end":   44.5, "lon_end":   26.5, "altitude_end_km":   84.1
  },
  "source": "gmn"
}
```

### `GET /api/public/v1/events/<event_id>?date=YYYY-MM-DD`

Single event by id. Date is required because events are indexed per
night.

### `GET /api/public/v1/timelapses`

One row per (station, camera, night) showing the dawn timelapse MP4
and (optional) night-long stack composite.

Query params: `date`, `station` (repeatable), `camera` (repeatable),
`limit` (default 200, max 500), `offset`.

Response shape:

```json
{
  "count": 29,
  "timelapses": [
    {
      "station_id": "gmnro05",
      "station_label": "Bistrița",
      "camera": "RO0003",
      "date": "2026-05-23",
      "filename": "RO0003_20260523_timelapse.mp4",
      "timelapse_url":  "https://.../media/v1/timelapse/RO0003/2026-05-23/RO0003_20260523_timelapse.mp4",
      "nightstack_url": "https://.../media/v1/nightstack/RO0003/2026-05-23/RO0003_20260523_night_stack.webp"
    }
  ]
}
```

### `GET /api/public/v1/nightstacks`

Same filter set as `/timelapses`, but only rows that have a night-stack
image (skips cameras without a stitched composite).

### `GET /api/public/v1/stats`

Aggregate counters for a hero strip or summary widget.

Query params:

| Param    | Default      | Notes |
|----------|--------------|-------|
| `period` | `tonight`    | One of `tonight`, `day`, `month`, `all`. `all` is capped at 90 days. |
| `date`   | yesterday UTC | Anchor for `day` and `month`. |

Response shape:

```json
{
  "period": "month",
  "anchor_date": "2026-05-23",
  "online_stations": 6,
  "total_stations": 9,
  "detection_count": 876,
  "per_station": {"gmnro05": 397, "gmnro03": 198, ...},
  "top_showers":  [{"code": "SPO", "count": 720}, ...],
  "brightest": {
    "detection_id": "2026-05-12:gmnro05:RO0005:RO0005_20260512_021207_color.mkv",
    "station_id": "gmnro05",
    "camera": "RO0005",
    "time_utc": "2026-05-12T02:12:14",
    "peak_magnitude": -4.8,
    "shower": "ETA"
  }
}
```

## Media files

```
GET /media/v1/clip/<camera>/<date>/<filename>.mkv
GET /media/v1/stack/<camera>/<date>/<filename>.webp
GET /media/v1/timelapse/<camera>/<date>/<filename>.mp4
GET /media/v1/nightstack/<camera>/<date>/<filename>.webp
```

- Served from the storage box archive (`/srv/rovimen/archive/`).
- `Cache-Control: public, max-age=86400, immutable`. Safe for any CDN.
- `Accept-Ranges: bytes` — `<video>` tags will scrub natively.
- 404 if the file isn't on the archive (yet — uploads can lag by a
  few minutes after a night ends).

**Container format note.** Color clips are uploaded as H.264-in-MKV
(`video/x-matroska`) — Chrome and Firefox play them in a `<video>` tag
without transcoding. If you need universal `<video>` compatibility
(older Safari, embedded WebViews), transcode to MP4 in your own
pipeline.

## Consumer examples

### WordPress shortcode (PHP / fetch) — recommended

Keep the secret on your WordPress server (in `wp-config.php`); never
echo it into browser-side JS.

```php
$api_key = ROVIMEN_API_KEY;  // define in wp-config.php
$res = wp_remote_get(
    'https://<DASHBOARD_HOST>/api/public/v1/detections?date=' . gmdate('Y-m-d', strtotime('-1 day')) . '&limit=20&order=mag',
    ['headers' => ['X-API-Key' => $api_key]]
);
$data = json_decode(wp_remote_retrieve_body($res), true);
foreach ($data['detections'] as $d) {
    // clip_url and stack_url stay keyless — embed them as-is.
    printf('<video controls poster="%s" src="%s"></video>',
           esc_attr($d['stack_url']), esc_attr($d['clip_url']));
}
```

### Plain HTML + JS

Browser JS holding an API key means anyone Viewing-Source has the
secret. Only use this pattern for keys that are intentionally scoped
to a single low-traffic site (and rotate them if leaked).

```html
<div id="meteors"></div>
<script>
const KEY = 'your-secret-here';  // see warning above
async function load() {
  const r = await fetch('https://<DASHBOARD_HOST>/api/public/v1/detections?limit=10',
                        { headers: { 'X-API-Key': KEY } });
  const data = await r.json();
  document.getElementById('meteors').innerHTML =
    data.detections.map(d => `
      <figure>
        <video controls poster="${d.stack_url}" src="${d.clip_url}"></video>
        <figcaption>${d.station_label} ${d.camera} ${d.time_utc} (mag ${d.peak_magnitude})</figcaption>
      </figure>`).join('');
}
load();
</script>
```

### Polling for "new detections tonight"

```js
let seen = new Set();
async function poll() {
  const r = await fetch('https://.../api/public/v1/detections?date=' + isoToday() + '&order=time_desc&limit=50',
                        { headers: { 'X-API-Key': KEY } });
  const data = await r.json();
  for (const d of data.detections) {
    if (seen.has(d.id)) continue;
    seen.add(d.id);
    prepend(d);  // your render function
  }
  setTimeout(poll, 60_000);  // server caches /detections?date=tonight for 60s
}
```

### Curl quickies

```bash
# Save your key once so the examples stay clean
export KEY=<your-secret>
H="X-API-Key: $KEY"

# Stations
curl -H "$H" https://.../api/public/v1/stations | jq '.stations[] | .id'

# Tonight's brightest
curl -H "$H" 'https://.../api/public/v1/detections?order=mag&limit=5' | jq '.detections[] | {time_utc, station_id, peak_magnitude, shower}'

# Multi-station events with a resolved orbit
curl -H "$H" 'https://.../api/public/v1/events?date=2026-05-23&has_trajectory=true'

# This month's stats
curl -H "$H" 'https://.../api/public/v1/stats?period=month'
```

## Operator opt-in

To expose a station via the public API, an admin sets `public: true`
on its entry in `dashboard_config.yaml`:

```yaml
stations:
  gmnro05:
    label: Bistrița
    location_name: Bistrița, Bistrița-Năsăud
    lat: 47.08
    lon: 24.39
    public: true
    # ... existing fields ...
```

Defaults to `false` so a new station never accidentally exposes media.
The `location_name` field is what shows up on the public surface — it
falls back to `label` when empty.

Non-public stations still participate internally (the overview map,
the GMN cross-join in `/events`, multi-station event correlation), but
their station id is not returned by `/stations`, their clips are not
served via `/media/v1/`, and their entries are filtered out of
`/detections` and `/events`.

If a multi-station event has a witness from a non-public station or
from a foreign GMN site (e.g. an Italian or Hungarian camera), the
witness is surfaced as a marker — `{rms_code, country, public: false}`
— so the trajectory still has its full participant count, but no
playable clip URL is emitted for them.

## Date semantics — "tonight" vs `date=`

Endpoints that take an optional `date=` query parameter default to
**yesterday UTC** when no date is supplied. This is the right anchor
for any caller asking after a meteor night has finished — Romanian
stations close their captures and upload around 04:00–10:00 UTC, so
"yesterday UTC" is the night that just wrapped up.

Important: the default flips at **00:00 UTC**. A caller at `23:58 UTC`
will receive yesterday's detections; the same code at `00:02 UTC` will
receive the previous-previous night, because the new "yesterday" is
now the night-just-finishing rather than the night-that-just-ended. If
you need deterministic results across that boundary (e.g. a poll loop
that has to keep returning rows from the same night), pass `date=`
explicitly.

The cache TTL also flips on this boundary: when `date >= today UTC`,
the response is cached for 60 s (still being incrementally locked);
when `date < today UTC`, the response is cached for 1 h (sealed
night). The default (yesterday UTC) is always in the sealed-night
bucket, so an unparameterised `/detections` call returns
`Cache-Control: public, max-age=3600`.

## Caching cheat sheet

| Endpoint                                 | Cache-Control max-age   |
|------------------------------------------|-------------------------|
| `/api/public/v1`                         | `public, max-age=3600`  |
| `/api/public/v1/stations`                | `public, max-age=300`   |
| `/api/public/v1/detections` (default)    | `public, max-age=3600`  |
| `/api/public/v1/detections` (today UTC)  | `public, max-age=60`    |
| `/api/public/v1/events` (default)        | `public, max-age=3600`  |
| `/api/public/v1/events` (today UTC)      | `public, max-age=60`    |
| `/api/public/v1/timelapses`              | `public, max-age=300`   |
| `/api/public/v1/nightstacks`             | `public, max-age=300`   |
| `/api/public/v1/stats`                   | follows anchor (60 s for today, 3600 s otherwise) |
| `/media/v1/*`                            | `public, max-age=86400, immutable` |

Every JSON response carries a strong ETag — clients should send
`If-None-Match` on poll loops to get cheap `304 Not Modified` replies.

## Rate limits

Every endpoint is rate-limited (Flask-Limiter, fixed window, in-memory
storage on the dashboard host). Hitting a limit returns `HTTP 429 Too
Many Requests` with a JSON body and `Retry-After` header.

The limiter bucket is keyed by **API key id** for authenticated JSON
calls (so a legitimate consumer's traffic is independent of where it
originates) and by **client IP** for keyless paths (`/media/v1/*` and
the discovery index).

| Endpoint group                                            | Default budget          |
|-----------------------------------------------------------|-------------------------|
| Index, stations, detections, event detail                 | 60 / min, 1000 / hour   |
| Events, timelapses, nightstacks, stats (heavier compute)  | 20 / min, 200 / hour    |
| `/media/v1/*` (each `<video>` play adds one hit)          | 300 / min, 5000 / hour  |

Per-key overrides are possible — when ops mints your key they can
attach a `rate_limit_override` like `"240/minute;5000/hour"` if you
need more headroom for a high-traffic site. Ask when you request the
key.

Polling pattern guidance: a single consumer following
`/detections?date=tonight` once every 60 s and rendering 50 clips
sits well inside every bucket.

429 response shape:

```json
{
  "error": "rate_limit_exceeded",
  "detail": "20 per 1 minute",
  "retry_after_seconds": 60
}
```

## For operators — minting a key

```
# On the VPS, as the user that owns api_keys.yaml (typically rovimen):
sudo -u rovimen python3 /opt/rovimen-dev/tools/mint_api_key.py \
    --id astromania-prod \
    --label "astromania.org WordPress plugin" \
    --path /opt/rovimen-dev/api_keys.yaml
```

The script prints the freshly-minted secret to stdout exactly once —
copy it from there. The yaml is mode 0600. Use `--list` to see
existing keys (redacted) and `--disable <id>` to revoke without losing
the audit trail.

Required env on the dashboard service:

```
ROVIMEN_API_KEYS_PATH=/opt/rovimen-dev/api_keys.yaml
ROVIMEN_API_KEYS_REQUIRED=1      # set to 0 for local dev only
```

## Stability promise

Within v1:

- Existing field names will not change meaning or get removed.
- New fields may be added — clients should ignore unknown keys.
- New endpoints may be added under `/api/public/v1/`.
- Schema version (`schema_version` field in the index endpoint)
  reflects breaking changes only.

When v2 ships, both versions will be served in parallel for at least
6 months before v1 is retired.

## What v1 deliberately doesn't have

- **WebSocket / SSE push.** Poll `/detections?date=<tonight>` every
  60 s instead.
- **Webhooks** to push new detections to a consumer URL.
- **On-demand transcoded MP4** of color clips. Color clips are
  H.264-in-MKV; consumers that need pure MP4 should transcode on
  their side.
- **Multi-camera stitched / composite videos** for an event. Each
  witness clip is linked separately; lay them out client-side.
