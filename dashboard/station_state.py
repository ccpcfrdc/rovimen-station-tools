"""station_state.py — durable VPS-side mirror for pushed station telemetry.

The reversed-HTTP push data plane (see ``docs/reversed_http_push_design.md``)
has stations POST their own status/vitals/detections/media pointers to the VPS
ingest API. Status and vitals live in the in-process :class:`StationCache` that
the dashboard already reads, but that cache is lost on restart. This module adds
a small durable mirror so the dashboard survives a restart with last-known
values, and carries the per-station ``last_seq`` used for envelope idempotency
(§2.3): a body whose ``seq`` is ``<= last_seq`` for that station is a replay and
is dropped.

Two tables, kept deliberately separate from ``detections`` (which the ingest
handler writes through :func:`detection_db.upsert_detections` unchanged):

    station_state(host_key PK, last_seq, last_seen_at, last_status, last_vitals)
    media_pointers(host_key, cam, date, kind, filename PK-tuple, ...)

Storage is the same SQLite file as the detection index by default so a single
WAL connection pool serves both, but the path is overridable for tests.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cache_backend import RedisStationStateBackend

logger = logging.getLogger(__name__)

# The per-push seq/replay guard is the hottest write on the ingest path (every
# heartbeat/vitals/detection/media POST checks + bumps it). On a single-writer
# SQLite DB, ~50+ writes/min across the fleet hits the write lock and thread-
# starves the worker (docs/reversed_http_push_design.md §9). When
# ``ROVIMEN_REDIS_URL`` is set we move JUST this seq state onto Redis, where the
# check-and-bump is an atomic Lua script. When it is unset the seq stays in
# SQLite, exactly as before — zero behaviour change for single-worker dev/tests.
#
# The durable last-value mirror (last_status/last_vitals) and the media pointers
# stay in SQLite regardless: they are low-frequency (status/vitals coalesce, the
# mirror is only a restart fallback; media is 300s/900s cadence) and carry no
# single-writer contention worth moving.
_KIND_SEQ = "seq"

_seq_backend: RedisStationStateBackend | None = None
_seq_backend_ready = False
_seq_backend_lock = threading.Lock()


def _get_seq_backend() -> RedisStationStateBackend | None:
    """Return the shared Redis seq backend, or ``None`` for the SQLite path.

    Built once, lazily, from ``ROVIMEN_REDIS_URL``. ``None`` means "keep the
    historical SQLite seq path". Safe to call from many threads.
    """
    global _seq_backend, _seq_backend_ready
    if _seq_backend_ready:
        return _seq_backend
    with _seq_backend_lock:
        if not _seq_backend_ready:
            from cache_backend import make_seq_backend

            _seq_backend = make_seq_backend()
            _seq_backend_ready = True
    return _seq_backend


def reset_seq_backend_for_tests() -> None:
    """Drop the memoised seq backend so a test can re-select via env. Test-only."""
    global _seq_backend, _seq_backend_ready
    with _seq_backend_lock:
        _seq_backend = None
        _seq_backend_ready = False

# Defaults to the same DB as the detection index — the ingest handler already
# writes detections there, so co-locating station_state keeps everything the
# dashboard reads on one connection pool.
DB_PATH = Path(
    os.environ.get(
        "ROVIMEN_STATION_STATE_DB",
        os.environ.get("ROVIMEN_DETECTIONS_DB", "/opt/rovimen/detections.db"),
    )
)

_BUSY_TIMEOUT_MS = 30_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS station_state (
    host_key      TEXT PRIMARY KEY,
    last_seq      INTEGER NOT NULL DEFAULT 0,
    last_seen_at  TEXT,
    last_status   TEXT,
    last_vitals   TEXT,
    updated_at    REAL
);

CREATE TABLE IF NOT EXISTS media_pointers (
    host_key      TEXT NOT NULL,
    cam           TEXT NOT NULL,
    date          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    filename      TEXT NOT NULL,
    extra         TEXT,
    updated_at    REAL,
    PRIMARY KEY (cam, date, kind, filename)
);

CREATE INDEX IF NOT EXISTS idx_media_host ON media_pointers(host_key);
CREATE INDEX IF NOT EXISTS idx_media_date ON media_pointers(date);
"""


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(
        str(path), check_same_thread=False, timeout=_BUSY_TIMEOUT_MS / 1000
    )
    con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    if not read_only:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    return con


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open (or create) the station_state DB, ensuring the schema exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(path)
    con.executescript(_SCHEMA)
    con.commit()
    return con


def ensure_schema(path: Path = DB_PATH) -> None:
    """Create the tables if missing. Safe to call repeatedly."""
    with closing(open_db(path)):
        pass


def _now() -> float:
    import time

    return time.time()


def get_last_seq(host_key: str, path: Path = DB_PATH) -> int:
    """Return the highest accepted ``seq`` for ``host_key`` (0 if unseen)."""
    backend = _get_seq_backend()
    if backend is not None:
        return backend.seq_get(host_key, _KIND_SEQ)
    ensure_schema(path)
    with closing(_connect(path, read_only=True)) as con:
        row = con.execute(
            "SELECT last_seq FROM station_state WHERE host_key=?", (host_key,)
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def is_replay(host_key: str, seq: int, path: Path = DB_PATH) -> bool:
    """True if ``seq`` is a replay/duplicate for ``host_key`` (``<= last_seq``).

    A fresh station (no prior state) has ``last_seq == 0``; any positive seq is
    accepted. Envelope ordering is by ``seq``, never wall-clock (§7 clock skew).

    NOTE: this is a *read-only* probe. It races against a concurrent bump and is
    kept for callers/tests that only want to peek. Ingest handlers must instead
    use :func:`accept_seq`, whose check-and-bump is atomic.
    """
    return seq <= get_last_seq(host_key, path)


def accept_seq(host_key: str, seq: int, path: Path = DB_PATH) -> bool:
    """Atomically decide + record whether ``seq`` advances ``host_key``.

    Returns ``True`` iff ``seq`` is strictly greater than the last accepted seq
    (the push is accepted and the stored seq is advanced to ``seq``), ``False``
    when it is a replay/older seq (the caller must drop). This collapses the old
    ``is_replay`` check and the subsequent bump into ONE atomic operation so two
    concurrent pushes with the same seq can never both be accepted.

    * Redis path (``ROVIMEN_REDIS_URL`` set): a single atomic Lua check-and-bump,
      no SQLite write at all — this is the whole point, removing the per-push
      seq write from the single-writer SQLite DB.
    * SQLite path (unset): the check-and-bump runs inside one transaction on a
      ``BEGIN IMMEDIATE`` write lock, which serialises concurrent writers, so it
      is atomic there too. Behaviour is identical to the historical
      ``is_replay`` + ``_bump_seq`` sequence.
    """
    backend = _get_seq_backend()
    if backend is not None:
        return backend.seq_bump(host_key, _KIND_SEQ, seq)
    ensure_schema(path)
    with closing(_connect(path)) as con:
        with con:
            # BEGIN IMMEDIATE takes the write lock up front so the read below
            # can't be undercut by a concurrent writer between check and bump.
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT last_seq FROM station_state WHERE host_key=?", (host_key,)
            ).fetchone()
            current = int(row[0]) if row and row[0] is not None else 0
            if seq <= current:
                return False
            _bump_seq(con, host_key, seq)
    return True


def _bump_seq(con: sqlite3.Connection, host_key: str, seq: int) -> None:
    """Advance the SQLite ``last_seq`` to ``max(current, seq)`` inside an open
    txn, ensuring the row exists. When the Redis seq backend is active this is a
    no-op wrt the seq (Redis owns it), but callers still need the row to exist so
    the last-value mirror UPDATE below has a target — hence :func:`_ensure_row`.
    """
    if _get_seq_backend() is None:
        con.execute(
            """
            INSERT INTO station_state (host_key, last_seq, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(host_key) DO UPDATE SET
                last_seq = MAX(station_state.last_seq, excluded.last_seq),
                updated_at = excluded.updated_at
            """,
            (host_key, seq, _now()),
        )
    else:
        _ensure_row(con, host_key)


def _ensure_row(con: sqlite3.Connection, host_key: str) -> None:
    """Create the station_state row if missing without touching ``last_seq`` —
    used on the Redis seq path so the mirror UPDATE has a row to write into."""
    con.execute(
        """INSERT INTO station_state (host_key, updated_at)
           VALUES (?, ?)
           ON CONFLICT(host_key) DO NOTHING""",
        (host_key, _now()),
    )


def record_status(
    host_key: str,
    seq: int,
    status: dict[str, Any],
    sent_at: str | None = None,
    path: Path = DB_PATH,
) -> None:
    """Persist last-known status (durable restart mirror of the cache).

    Advances the SQLite ``last_seq`` only on the SQLite seq path; when Redis owns
    the seq the bump already happened atomically in :func:`accept_seq`.
    """
    ensure_schema(path)
    seen = sent_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect(path)) as con:
        with con:
            _bump_seq(con, host_key, seq)
            con.execute(
                """UPDATE station_state
                   SET last_status=?, last_seen_at=?, updated_at=?
                   WHERE host_key=?""",
                (json.dumps(status, default=str), seen, _now(), host_key),
            )


def record_vitals(
    host_key: str,
    seq: int,
    vitals: dict[str, Any],
    sent_at: str | None = None,
    path: Path = DB_PATH,
) -> None:
    """Persist last-known vitals (durable restart mirror). Seq handling matches
    :func:`record_status`."""
    ensure_schema(path)
    seen = sent_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect(path)) as con:
        with con:
            _bump_seq(con, host_key, seq)
            con.execute(
                """UPDATE station_state
                   SET last_vitals=?, last_seen_at=?, updated_at=?
                   WHERE host_key=?""",
                (json.dumps(vitals, default=str), seen, _now(), host_key),
            )


def advance_seq(host_key: str, seq: int, path: Path = DB_PATH) -> None:
    """Advance seq only (used by detections/media ingest, which carry their own
    durable stores — the detection PK and the media_pointers PK).

    On the Redis seq path this bumps the shared Redis seq; on the SQLite path it
    bumps ``last_seq`` exactly as before. Kept for callers that have already
    decided to accept and only need the monotonic advance (as opposed to the
    accept-or-drop decision, which is :func:`accept_seq`)."""
    backend = _get_seq_backend()
    if backend is not None:
        backend.seq_bump(host_key, _KIND_SEQ, seq)
        return
    ensure_schema(path)
    with closing(_connect(path)) as con:
        with con:
            _bump_seq(con, host_key, seq)


def get_state(host_key: str, path: Path = DB_PATH) -> dict[str, Any] | None:
    """Return the durable state row for ``host_key`` (decoded), or None."""
    ensure_schema(path)
    with closing(_connect(path, read_only=True)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM station_state WHERE host_key=?", (host_key,)
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    for col in ("last_status", "last_vitals"):
        if out.get(col):
            try:
                out[col] = json.loads(out[col])
            except (ValueError, TypeError):
                out[col] = None
    return out


def upsert_media_pointers(
    host_key: str,
    pointers: list[dict[str, Any]],
    path: Path = DB_PATH,
) -> int:
    """Upsert media pointers, deduped on ``(cam, date, kind, filename)`` (§2.3).

    Each pointer dict must carry ``cam``, ``date``, ``kind``, ``filename``;
    any other keys are preserved verbatim in an ``extra`` JSON blob so the
    dashboard can surface e.g. ``locked`` / ``meteor_time`` / ``night_stack``.
    Returns the number of rows written.
    """
    if not pointers:
        return 0
    ensure_schema(path)
    n = 0
    with closing(_connect(path)) as con:
        with con:
            for p in pointers:
                cam = p.get("cam")
                date = p.get("date")
                kind = p.get("kind")
                filename = p.get("filename")
                if not (cam and date and kind and filename):
                    continue
                extra = {
                    k: v
                    for k, v in p.items()
                    if k not in ("cam", "date", "kind", "filename")
                }
                con.execute(
                    """INSERT OR REPLACE INTO media_pointers
                       (host_key, cam, date, kind, filename, extra, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        host_key,
                        cam,
                        date,
                        kind,
                        filename,
                        json.dumps(extra, default=str) if extra else None,
                        _now(),
                    ),
                )
                n += 1
    return n


def get_media_pointers(
    host_key: str | None = None,
    date: str | None = None,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return media pointers, optionally filtered by host_key and/or date."""
    ensure_schema(path)
    clauses: list[str] = []
    params: list[Any] = []
    if host_key:
        clauses.append("host_key=?")
        params.append(host_key)
    if date:
        clauses.append("date=?")
        params.append(date)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(_connect(path, read_only=True)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"SELECT * FROM media_pointers{where} ORDER BY date DESC, cam", params
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if d.get("extra"):
            try:
                d["extra"] = json.loads(d["extra"])
            except (ValueError, TypeError):
                d["extra"] = None
        out.append(d)
    return out
