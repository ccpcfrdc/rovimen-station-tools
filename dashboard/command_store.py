"""command_store.py — durable queue for the server->station command channel.

Backs the command channel (``docs/reversed_http_push_design.md`` §3). An admin
enqueues a signed command for a station; the station long-polls, verifies the
ed25519 signature, dispatches an allowlisted action, and POSTs an ack. The queue
is a single SQLite table co-located with the detection index / ``station_state``
(same WAL connection pool), so the dashboard reads command status without IPC.

Row shape::

    commands(
        id PK, host_key, type, args(JSON), created_by, issued_at, not_after,
        sig, status(pending|acked|expired), result(JSON), acked_at, created_at
    )

Idempotency (§7): ``id`` is a one-shot ``cmd_<hex>``; a re-delivered id is acked
without re-execution station-side, and the ack endpoint is a no-op on an already
terminal row. ``not_after`` is a hard TTL — expired-but-unacked commands are
never handed to a station and are surfaced as ``expired``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(
    os.environ.get(
        "ROVIMEN_COMMAND_DB",
        os.environ.get(
            "ROVIMEN_STATION_STATE_DB",
            os.environ.get("ROVIMEN_DETECTIONS_DB", "/opt/rovimen/detections.db"),
        ),
    )
)

_BUSY_TIMEOUT_MS = 30_000

# The STRICT allowlist of command types (§3.2). Each maps to an existing station
# capability — never arbitrary shell. There is deliberately no exec/shell type.
ALLOWED_TYPES: frozenset[str] = frozenset({
    "restart_service",
    "reboot",
    "patch_settings",
    "lock_clip",
    "trigger_upload",
    "run_updater",
    "restart_services",
})

STATUS_PENDING = "pending"
STATUS_ACKED = "acked"
STATUS_EXPIRED = "expired"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    id          TEXT PRIMARY KEY,
    host_key    TEXT NOT NULL,
    type        TEXT NOT NULL,
    args        TEXT,
    created_by  TEXT,
    issued_at   TEXT NOT NULL,
    not_after   TEXT NOT NULL,
    sig         TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    result      TEXT,
    acked_at    TEXT,
    created_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_commands_host ON commands(host_key, status);
"""


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), check_same_thread=False, timeout=_BUSY_TIMEOUT_MS / 1000)
    con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    if not read_only:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    return con


def ensure_schema(path: Path = DB_PATH) -> None:
    """Create the commands table if missing. Safe to call repeatedly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(path)) as con:
        con.executescript(_SCHEMA)
        con.commit()


def new_command_id() -> str:
    """One-shot command id: ``cmd_<32 hex>``."""
    return f"cmd_{secrets.token_hex(16)}"


def _now() -> float:
    import time

    return time.time()


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def enqueue(
    *,
    id: str,
    host_key: str,
    type: str,
    args: dict[str, Any],
    created_by: str,
    issued_at: str,
    not_after: str,
    sig: str,
    path: Path = DB_PATH,
) -> None:
    """Persist a signed, pending command. The caller has already validated the
    type against :data:`ALLOWED_TYPES` and signed the canonical message."""
    ensure_schema(path)
    with closing(_connect(path)) as con:
        with con:
            con.execute(
                """INSERT INTO commands
                   (id, host_key, type, args, created_by, issued_at, not_after,
                    sig, status, result, acked_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?)""",
                (
                    id, host_key, type, json.dumps(args or {}, sort_keys=True),
                    created_by, issued_at, not_after, sig, _now(),
                ),
            )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("args"):
        try:
            d["args"] = json.loads(d["args"])
        except (ValueError, TypeError):
            d["args"] = {}
    else:
        d["args"] = {}
    if d.get("result"):
        try:
            d["result"] = json.loads(d["result"])
        except (ValueError, TypeError):
            d["result"] = None
    return d


def pending_for_station(host_key: str, path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Return pending, non-expired commands for ``host_key`` in signed-envelope
    shape (id/issued_at/issued_by/type/args/not_after/sig), oldest first.

    Commands whose ``not_after`` has passed are transitioned to ``expired`` in the
    same call and excluded — a station never receives a command it could not
    legitimately still run."""
    ensure_schema(path)
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    expired_ids: list[str] = []
    with closing(_connect(path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM commands WHERE host_key=? AND status='pending' ORDER BY created_at ASC",
            (host_key,),
        ).fetchall()
        for row in rows:
            d = _row_to_dict(row)
            na = _parse_ts(d.get("not_after"))
            if na is not None and na < now:
                expired_ids.append(d["id"])
                continue
            out.append({
                "id": d["id"],
                "issued_at": d["issued_at"],
                "issued_by": d["created_by"],
                "type": d["type"],
                "args": d["args"],
                "not_after": d["not_after"],
                "sig": d["sig"],
            })
        if expired_ids:
            with con:
                con.executemany(
                    "UPDATE commands SET status='expired' WHERE id=? AND status='pending'",
                    [(i,) for i in expired_ids],
                )
    return out


def get_command(id: str, path: Path = DB_PATH) -> dict[str, Any] | None:
    ensure_schema(path)
    with closing(_connect(path, read_only=True)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM commands WHERE id=?", (id,)).fetchone()
    return _row_to_dict(row) if row is not None else None


def ack(
    id: str,
    host_key: str,
    result: dict[str, Any],
    path: Path = DB_PATH,
) -> bool:
    """Mark a pending command acked with the station-reported ``result``.

    Returns True if a pending row for ``(id, host_key)`` was updated. An already
    terminal row (acked/expired) or an id belonging to a different host returns
    False — the ack endpoint maps that to a no-op 200 (idempotent redelivery)."""
    ensure_schema(path)
    acked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect(path)) as con:
        with con:
            cur = con.execute(
                """UPDATE commands
                   SET status='acked', result=?, acked_at=?
                   WHERE id=? AND host_key=? AND status='pending'""",
                (json.dumps(result or {}, default=str), acked_at, id, host_key),
            )
        return cur.rowcount > 0


def list_for_station(
    host_key: str,
    limit: int = 100,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return recent commands for a station (any status) for the dashboard view."""
    ensure_schema(path)
    with closing(_connect(path, read_only=True)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM commands WHERE host_key=? ORDER BY created_at DESC LIMIT ?",
            (host_key, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
