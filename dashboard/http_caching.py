"""HTTP response caching — ETag, 304 Not Modified, content-addressed cache."""

import hashlib
import json
import threading
from typing import Any

from flask import Response, request

# Bounded content-keyed cache of serialized payloads. Many hot endpoints
# (_compute_detections_payload, _archive_nights, _platepar_cache,
# _timelapses_swr_cache, etc.) store the dict and return the *same* object
# on each hit, so caching the (etag, body_bytes) by id() lets repeat
# callers skip the json.dumps + blake2b on every request.
#
# id() is reused by CPython after a payload is GC'd, so each cache entry
# carries a sanity tag (type, len) re-checked on lookup; a mismatch falls
# back to the slow path.
_JSON_CACHED_MAX_ENTRIES = 256
_json_cached_payload_cache: "dict[int, tuple[str, int, str, bytes]]" = {}
_json_cached_payload_lock = threading.Lock()


def _json_cached_payload_tag(payload: Any) -> tuple[str, int]:
    t = type(payload).__name__
    try:
        n = len(payload)  # type: ignore[arg-type]
    except TypeError:
        n = -1
    return t, n


def _json_cached(payload: Any, max_age: int) -> Response:
    """Return a Flask JSON response with ETag + Cache-Control, honouring
    If-None-Match for 304 short-circuits. The payload bytes are hashed so the
    ETag is stable across processes — restart the dashboard, browsers keep
    their cache. Use for read-mostly endpoints; do NOT use for endpoints that
    return a Set-Cookie or mutate session state.

    Uses Response.make_conditional rather than comparing the If-None-Match
    header literally: nginx, when it gzips a response that carries a strong
    ETag, rewrites it to a weak ETag (W/"…") because the wire bytes change.
    A direct == compare would miss the weak prefix on the round-trip;
    make_conditional treats weak and strong as equivalent for 304.
    """
    # Serialize and compute a content-based cache key so that id() reuse
    # after GC cannot serve stale data (the old id()-keyed approach was
    # vulnerable when CPython recycled an object address).
    body = json.dumps(payload, separators=(",", ":"), default=str)
    body_bytes = body.encode("utf-8")
    etag_value = hashlib.blake2b(body_bytes, digest_size=12).hexdigest()
    content_key = etag_value
    with _json_cached_payload_lock:
        hit = _json_cached_payload_cache.get(content_key)
        if hit is not None:
            body_bytes = hit[3]
        if len(_json_cached_payload_cache) >= _JSON_CACHED_MAX_ENTRIES:
            try:
                oldest = next(iter(_json_cached_payload_cache))
                _json_cached_payload_cache.pop(oldest, None)
            except StopIteration:
                pass
        type_tag, len_tag = _json_cached_payload_tag(payload)
        _json_cached_payload_cache[content_key] = (
            type_tag, len_tag, etag_value, body_bytes,
        )
    resp = Response(body_bytes, mimetype="application/json")
    resp.set_etag(etag_value)  # type: ignore[arg-type]
    resp.headers["Cache-Control"] = f"private, max-age={max_age}"
    return resp.make_conditional(request)
