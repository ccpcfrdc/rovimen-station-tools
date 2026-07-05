"""Compilation (video cart) CRUD, build pipeline, and download routes.

Extracted from rovimen_dashboard.py as part of issue #328.  All routes
preserved verbatim -- same URLs, same behaviour, same decorators.
Wired in from ``create_app()`` via ``register_compilation_routes``.

Closure state (``_compilation_jobs``, ``_compilation_jobs_lock``) lives
inside the register function so each ``create_app()`` call gets its own
isolated build tracker.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, request, send_file, session

import compilation_store
import youtube_upload
from auth import require_admin
from cache_store import ARCHIVE_PATH, COMPILATIONS_ARCHIVE_PATH, COMPILATIONS_OUT_PATH, INTRO_PATH
from models import DashboardConfig
from station_client import StationCache
from tunnels import TunnelManager

logger = logging.getLogger(__name__)

# ── Shared validation regexes (defence-in-depth against path traversal) ──
#
# Mirrors of the module-level regexes in ``rovimen_dashboard.py``.  The
# compilation pipeline uses these to gate every user-supplied camera /
# date / filename / manifest-id value before interpolating it into a
# filesystem path.  The regex is a cheap pre-filter; the
# ``Path.resolve().is_relative_to()`` check is the actual safety net.
_COMPILATION_MANIFEST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_COMPILATION_CAMERA_RE = re.compile(r"^[A-Z0-9]{1,16}$")
_COMPILATION_DATE_COMPACT_RE = re.compile(r"^\d{8}$")
_COMPILATION_CLIP_FILENAME_RE = re.compile(r"^[A-Za-z0-9_]+\.(mkv|mp4)$")
_SLUG_RE = re.compile(r"[^a-z0-9-]+")

# Hard cap on clips per compilation manifest. Each clip is re-encoded to
# /tmp during the build (a 20 s chunk is ~20 MB), so 1000 clips would write
# ~20 GB before concat -- enough to fill the VPS disk. Reject manifests over
# the cap at create / patch time. Configurable via env.
MAX_CLIPS = int(os.environ.get("ROVIMEN_COMPILATION_MAX_CLIPS", "200"))

# Cap concurrent compilation builds. Each build fans out 6 ffmpeg workers and
# writes to /tmp; two simultaneous builds already saturate a small VPS, so a
# global semaphore prevents a burst of /build POSTs from piling on.
_COMPILATION_BUILD_CONCURRENCY = int(
    os.environ.get("ROVIMEN_COMPILATION_BUILD_CONCURRENCY", "2")
)
_compilation_build_sem = threading.BoundedSemaphore(_COMPILATION_BUILD_CONCURRENCY)


def _clips_over_cap(clips: Any) -> bool:
    """True if the caller-supplied clip list exceeds ``MAX_CLIPS``."""
    return isinstance(clips, list) and len(clips) > MAX_CLIPS


def register_compilation_routes(
    app: Flask,
    config: DashboardConfig,
    tunnels: TunnelManager,
    cache: StationCache,
) -> None:
    """Register all compilation (video cart) routes on *app*."""

    # ── Closure state ─────────────────────────────────────────────────
    _compilation_jobs: dict[str, dict] = {}
    _compilation_jobs_lock = threading.Lock()

    # ── Helper functions ──────────────────────────────────────────────

    def _slugify(s: str, fallback: str) -> str:
        s = (s or "").strip().lower().replace(" ", "-")
        s = _SLUG_RE.sub("", s)
        return s[:48] or fallback

    def _validate_clips(clips: Any) -> list[dict]:
        """Normalise a clip list from a user-supplied manifest.  Drops any
        clip that fails validation rather than aborting -- the UI surfaces
        the count discrepancy.

        Camera / date / filename are re-validated against the SAME regex
        shapes used by ``/media/v1/*`` in ``dashboard/public_api.py`` (see
        ``_COMPILATION_CAMERA_RE`` etc at module top). The manifest comes
        from an authenticated admin POST but is still attacker-shaped data
        once it's persisted: a tampered manifest on disk must not let
        ``_fetch_one_clip`` ffmpeg-read files outside ``ARCHIVE_PATH``.
        ``host_key`` is checked against ``config.stations`` (closed set);
        the rest are checked against bounded character classes."""
        out: list[dict] = []
        if not isinstance(clips, list):
            return out
        for c in clips:
            if not isinstance(c, dict):
                continue
            host_key = c.get("host_key", "")
            cam = c.get("cam", "")
            date = c.get("date", "")
            filename = c.get("filename", "")
            if not isinstance(host_key, str) or host_key not in config.stations:
                continue
            if not isinstance(cam, str) or not _COMPILATION_CAMERA_RE.match(cam):
                continue
            if not isinstance(date, str) or not _COMPILATION_DATE_COMPACT_RE.match(date):
                continue
            if not isinstance(filename, str) or not _COMPILATION_CLIP_FILENAME_RE.match(filename):
                continue
            try:
                pre = max(0.0, min(60.0, float(c.get("pre", 2))))
                post = max(0.5, min(300.0, float(c.get("post", 5))))
            except (TypeError, ValueError):
                continue
            try:
                offset_raw = c.get("detection_offset_s")
                detection_offset = float(offset_raw) if offset_raw is not None else None
            except (TypeError, ValueError):
                detection_offset = None
            out.append({
                "host_key": host_key,
                "cam": cam,
                "date": date,
                "filename": filename,
                "detection_offset_s": detection_offset,
                "pre": pre,
                "post": post,
                "label": str(c.get("label", ""))[:200],
            })
        return out

    def _compilation_set(manifest_id: str, **fields) -> None:
        """Update both the in-memory job state and the on-disk manifest."""
        with _compilation_jobs_lock:
            existing = _compilation_jobs.setdefault(manifest_id, {})
            existing.update(fields)
        persist_keys = {
            "status", "progress", "output_path", "error",
            "youtube_video_id", "youtube_url",
            "build_started_at", "build_finished_at",
        }
        to_persist = {k: v for k, v in fields.items() if k in persist_keys}
        if to_persist:
            compilation_store.patch(manifest_id, to_persist)

    def _fetch_one_clip(clip: dict, dest: Path) -> tuple[bool, str]:
        """Fetch one clip exclusively from the storagebox archive -- the
        VPS-side mirror is the gold standard. Stations are ephemeral
        (retention windows, offline boxes, slow tunnels) so we never
        touch them from the build pipeline.

        Each archived chunk is the full ~20 s capture re-encoded by the
        dawn pipeline (libx264, crf 20). We seek to (detection_offset
        - pre) and take (pre + post) seconds to honour the cart's
        per-clip trim, then re-encode to a uniform profile so concat
        with ``-c copy`` works downstream."""
        archive_path = ARCHIVE_PATH / clip["cam"] / clip["date"] / "meteors" / clip["filename"]
        # Belt-and-suspenders: re-validate the clip shape AND verify the
        # resolved path is still inside ARCHIVE_PATH before we hand it to
        # ffmpeg. ``_validate_clips`` already gates writes into the manifest
        # against the same regexes, but the manifest is on-disk JSON -- a
        # compromise or future regex hole that lets ``..`` through must not
        # become an arbitrary-file-read primitive via ffmpeg.
        if not (
            _COMPILATION_CAMERA_RE.match(clip.get("cam", "") or "")
            and _COMPILATION_DATE_COMPACT_RE.match(clip.get("date", "") or "")
            and _COMPILATION_CLIP_FILENAME_RE.match(clip.get("filename", "") or "")
        ):
            return False, "clip rejected: cam/date/filename failed re-validation"
        try:
            resolved_clip = archive_path.resolve()
            archive_root = ARCHIVE_PATH.resolve()
        except (OSError, RuntimeError) as exc:
            return False, f"path resolve failed: {str(exc)[:120]}"
        try:
            if not resolved_clip.is_relative_to(archive_root):
                return False, "clip path escapes archive root"
        except ValueError:
            return False, "clip path escapes archive root"
        if not archive_path.exists():
            return False, f"not in storagebox: {archive_path}"

        # detection_offset_s comes from the cart entry (carried over from
        # the chunk metadata at pick time). Fall back to mid-chunk if it
        # somehow wasn't recorded -- ~10 s is a reasonable centre for the
        # 20-second chunks the station produces.
        detection_offset = float(clip.get("detection_offset_s") or 10.0)
        pre = max(0.0, float(clip.get("pre", 2.0)))
        post = max(0.5, float(clip.get("post", 5.0)))
        seek_start = max(0.0, detection_offset - pre)
        duration = pre + post

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{seek_start:.3f}",
            "-i", str(archive_path),
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-an", "-movflags", "+faststart",
            str(dest),
        ]
        try:
            result = subprocess.run(cmd, stderr=subprocess.PIPE, timeout=600)
        except subprocess.TimeoutExpired:
            return False, "ffmpeg timed out after 600s"
        except FileNotFoundError:
            return False, "ffmpeg not installed on the VPS (apt-get install ffmpeg)"
        except Exception as exc:
            return False, f"subprocess: {str(exc)[:200]}"
        if result.returncode != 0:
            return False, f"ffmpeg: {result.stderr.decode(errors='replace')[:200]}"
        if dest.stat().st_size < 1024:
            return False, "output too small"
        return True, ""

    def _run_compilation_build(manifest_id: str) -> None:
        """Background pipeline. Updates manifest/progress as it goes.
        Wraps the whole flow in a top-level exception handler so any
        unexpected crash marks the manifest as ``error`` instead of leaving
        it dangling on ``building`` forever (which would block re-builds
        and leave the cart's progress widget stuck).

        A global semaphore caps concurrent builds so a burst of /build POSTs
        (or scheduled ticks) can't fan out unbounded ffmpeg workers and fill
        /tmp. This is a background daemon thread, so it waits its turn rather
        than failing fast; if it can't get a slot within the timeout the
        manifest is marked ``error`` so it never hangs on ``building``."""
        if not _compilation_build_sem.acquire(timeout=1800):
            logger.error("Compilation build %s timed out waiting for a build slot", manifest_id)
            _compilation_set(
                manifest_id, status="error",
                error="Timed out waiting for a free build slot (too many concurrent builds)",
            )
            return
        try:
            _run_compilation_build_inner(manifest_id)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            logger.exception("Compilation build crashed for %s", manifest_id)
            _compilation_set(
                manifest_id, status="error",
                error=f"Build crashed: {type(exc).__name__}: {str(exc)[:300]}",
            )
        finally:
            _compilation_build_sem.release()

    def _run_compilation_build_inner(manifest_id: str) -> None:
        manifest = compilation_store.get(manifest_id)
        if not manifest:
            return
        clips = manifest.get("clips", [])
        if not clips:
            _compilation_set(manifest_id, status="error", error="No clips in manifest")
            return

        # Belt-and-suspenders: ``api_compilation_create`` slug-validates the
        # caller-supplied ``id`` so this regex should always pass for any
        # manifest that came in through the public API. Manifests are stored
        # on disk though, so a future tampered manifest must not let the
        # build pipeline write outside ``/tmp`` or ``COMPILATIONS_OUT_PATH``.
        if not _COMPILATION_MANIFEST_ID_RE.match(manifest_id or ""):
            _compilation_set(
                manifest_id, status="error",
                error=f"manifest_id failed slug re-validation: {manifest_id!r}",
            )
            return

        tempdir = Path(f"/tmp/rovimen_compilation_{manifest_id}")
        try:
            tempdir_resolved = tempdir.resolve()
            tmp_root = Path("/tmp").resolve()
        except (OSError, RuntimeError) as exc:
            _compilation_set(
                manifest_id, status="error",
                error=f"tempdir resolve failed: {str(exc)[:200]}",
            )
            return
        try:
            tempdir_ok = tempdir_resolved.is_relative_to(tmp_root)
        except ValueError:
            tempdir_ok = False
        if not tempdir_ok:
            _compilation_set(
                manifest_id, status="error",
                error="tempdir escapes /tmp",
            )
            return
        tempdir.mkdir(parents=True, exist_ok=True)
        _compilation_set(
            manifest_id,
            status="building",
            error=None,
            progress={"phase": "fetch", "done": 0, "total": len(clips)},
            build_started_at=datetime.now(timezone.utc).isoformat(),
        )

        try:
            # Phase 1 -- parallel fetch of every shortclip (station-side ffmpeg
            # already trims + re-encodes to a uniform libx264/crf20 profile).
            clip_paths: list[Path | None] = [None] * len(clips)
            done = 0
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {
                    pool.submit(_fetch_one_clip, c, tempdir / f"{i:04d}.mp4"): (i, c)
                    for i, c in enumerate(clips)
                }
                for fut in as_completed(futures):
                    i, c = futures[fut]
                    ok, err = fut.result()
                    if not ok:
                        _compilation_set(
                            manifest_id, status="error",
                            error=f"Clip {i+1}/{len(clips)} ({c['cam']} {c['filename']}): {err}",
                        )
                        return
                    clip_paths[i] = tempdir / f"{i:04d}.mp4"
                    done += 1
                    _compilation_set(
                        manifest_id,
                        progress={"phase": "fetch", "done": done, "total": len(clips)},
                    )

            # Phase 2 -- concat. Try -c copy first (fast, lossless); fall back to
            # a one-pass libx264 re-encode if codec/profile mismatch makes
            # demuxer concat fail (most likely cause: an intro file that wasn't
            # encoded to the same profile as the shortclips).
            _compilation_set(
                manifest_id,
                progress={"phase": "concat", "done": 0, "total": 1},
            )
            list_file = tempdir / "list.txt"
            intro_path = INTRO_PATH if (INTRO_PATH.exists() and manifest.get("intro_enabled", True)) else None
            with open(list_file, "w") as f:
                if intro_path:
                    f.write(f"file '{intro_path}'\n")
                for p in clip_paths:
                    f.write(f"file '{p}'\n")

            COMPILATIONS_OUT_PATH.mkdir(parents=True, exist_ok=True)
            out_path = COMPILATIONS_OUT_PATH / f"{manifest_id}.mp4"
            # Final containment check on the output file -- defence-in-depth
            # against any future slip-up that lets a non-slug manifest_id
            # reach this far. With the regex above and the create-time
            # validation this should always pass.
            try:
                out_resolved = out_path.resolve()
                out_root = COMPILATIONS_OUT_PATH.resolve()
            except (OSError, RuntimeError) as exc:
                _compilation_set(
                    manifest_id, status="error",
                    error=f"output path resolve failed: {str(exc)[:200]}",
                )
                return
            try:
                out_ok = out_resolved.is_relative_to(out_root)
            except ValueError:
                out_ok = False
            if not out_ok:
                _compilation_set(
                    manifest_id, status="error",
                    error="output path escapes compilations directory",
                )
                return

            cmd_copy = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                "-movflags", "+faststart",
                "-y", str(out_path),
            ]
            result = subprocess.run(cmd_copy, stderr=subprocess.PIPE, timeout=600)
            if result.returncode != 0:
                cmd_reencode = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "concat", "-safe", "0",
                    "-i", str(list_file),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-an",
                    "-movflags", "+faststart",
                    "-y", str(out_path),
                ]
                result = subprocess.run(cmd_reencode, stderr=subprocess.PIPE, timeout=600)
                if result.returncode != 0:
                    err_tail = result.stderr.decode(errors="replace")[-800:]
                    _compilation_set(
                        manifest_id, status="error",
                        error=f"ffmpeg failed: {err_tail}",
                    )
                    return

            # Best-effort archive copy to storagebox (non-fatal).
            try:
                COMPILATIONS_ARCHIVE_PATH.mkdir(parents=True, exist_ok=True)
                archive_dest = COMPILATIONS_ARCHIVE_PATH / f"{manifest_id}.mp4"
                shutil.copy2(str(out_path), str(archive_dest))
                logger.info("Archived compilation to %s", archive_dest)
            except OSError as exc:
                logger.warning("Could not archive compilation to storagebox: %s", exc)

            # Phase 3 -- optional YouTube upload (videos.insert via the
            # google-api-python-client). Status flips to "uploading" so the
            # poller knows the build itself is done but the manifest isn't
            # finished. Failures here mark the manifest "error" but the
            # local MP4 is still kept locally for manual upload.
            yt_cfg = (manifest.get("youtube") or {})
            do_upload = bool(yt_cfg.get("upload_after_build"))
            yt_status = youtube_upload.is_configured() if do_upload else {"ready": False}

            if do_upload and not yt_status.get("ready"):
                _compilation_set(
                    manifest_id,
                    status="error",
                    error=f"YouTube upload requested but not ready: {yt_status.get('reason') or 'unknown'}",
                    output_path=str(out_path),
                )
                return

            if do_upload:
                _compilation_set(
                    manifest_id,
                    status="uploading",
                    progress={"phase": "youtube", "done": 0, "total": 100},
                    output_path=str(out_path),
                )
                title = manifest.get("title") or manifest_id
                stations = sorted({c.get("cam", "") for c in clips})
                showers_present = sorted({
                    (c.get("label") or "").split(" ", 1)[0]
                    for c in clips
                    if (c.get("label") or "").split(" ", 1)[0] not in ("", "·")
                })
                description = (
                    f"ROVIMEN meteor compilation — {title}\n\n"
                    f"{len(clips)} clip(s) from {len(stations)} camera(s).\n"
                    f"Cameras: {', '.join(stations)}\n"
                    "Generated automatically by the ROVIMEN dashboard."
                )
                tags = ["meteor", "ROVIMEN", "GMN", "Romania"]
                privacy = (yt_cfg.get("privacy") or "unlisted").lower()

                def _progress_cb(frac: float) -> None:
                    _compilation_set(
                        manifest_id,
                        progress={"phase": "youtube", "done": int(frac * 100), "total": 100},
                    )

                try:
                    result = youtube_upload.upload_video(
                        str(out_path),
                        title=title,
                        description=description,
                        tags=tags,
                        privacy=privacy,
                        progress_cb=_progress_cb,
                    )
                except Exception as exc:
                    _compilation_set(
                        manifest_id,
                        status="error",
                        error=f"YouTube upload failed: {str(exc)[:500]}",
                        output_path=str(out_path),
                    )
                    return

                _compilation_set(
                    manifest_id,
                    status="done",
                    output_path=str(out_path),
                    youtube_video_id=result.get("video_id"),
                    youtube_url=result.get("video_url"),
                    progress={"phase": "done", "done": len(clips), "total": len(clips)},
                    build_finished_at=datetime.now(timezone.utc).isoformat(),
                )
                return

            _compilation_set(
                manifest_id,
                status="done",
                output_path=str(out_path),
                progress={"phase": "done", "done": len(clips), "total": len(clips)},
                build_finished_at=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            shutil.rmtree(tempdir, ignore_errors=True)

    # ── Route handlers ────────────────────────────────────────────────

    @app.route("/api/compilation", methods=["GET"])
    @require_admin
    def api_compilation_list():
        return jsonify(compilation_store.load_all())

    @app.route("/api/compilation", methods=["POST"])
    @require_admin
    def api_compilation_create():
        body = request.get_json(force=True) or {}
        if _clips_over_cap(body.get("clips")):
            return jsonify({
                "error": "too_many_clips",
                "detail": f"a compilation may contain at most {MAX_CLIPS} clips",
            }), 400
        title = (body.get("title") or "").strip() or "Untitled compilation"
        slug = _slugify(title, fallback=secrets.token_hex(3))
        date_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")
        # Caller-supplied ``id`` is interpolated into filesystem paths
        # downstream (``/tmp/rovimen_compilation_<id>`` and
        # ``COMPILATIONS_OUT_PATH / <id>.mp4``). Reject anything that isn't
        # the slug shape BEFORE any path interpolation. The internal
        # generator ``{YYYYMMDD}-{slug}`` already matches the regex by
        # construction, so this only ever fires on caller-supplied IDs.
        raw_id = body.get("id")
        if raw_id is not None:
            if not isinstance(raw_id, str) or not _COMPILATION_MANIFEST_ID_RE.match(raw_id):
                return jsonify({
                    "error": "invalid_id",
                    "detail": "id must match [a-z0-9][a-z0-9._-]{0,63} (no slashes, no leading dot)",
                }), 400
            proposed_id = raw_id
        else:
            proposed_id = f"{date_prefix}-{slug}"
        # Disambiguate if id already exists
        existing_ids = {m.get("id") for m in compilation_store.load_all()}
        mid = proposed_id
        suffix = 1
        while mid in existing_ids:
            suffix += 1
            mid = f"{proposed_id}-{suffix}"
        manifest = {
            "id": mid,
            "title": title,
            "created_by": session.get("user", "admin"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scheduled_for": body.get("scheduled_for"),
            "status": "scheduled" if body.get("scheduled_for") else "draft",
            "progress": {"phase": "idle", "done": 0, "total": 0},
            "clips": _validate_clips(body.get("clips", [])),
            "intro_enabled": bool(body.get("intro_enabled", True)),
            "youtube": body.get("youtube") or {"upload_after_build": False, "privacy": "unlisted"},
            "output_path": None,
            "youtube_video_id": None,
            "youtube_url": None,
            "error": None,
        }
        return jsonify(compilation_store.upsert(manifest)), 201

    @app.route("/api/compilation/<manifest_id>", methods=["GET"])
    @require_admin
    def api_compilation_get(manifest_id: str):
        m = compilation_store.get(manifest_id)
        if not m:
            abort(404)
        return jsonify(m)

    @app.route("/api/compilation/<manifest_id>", methods=["PATCH"])
    @require_admin
    def api_compilation_patch(manifest_id: str):
        existing = compilation_store.get(manifest_id)
        if not existing:
            abort(404)
        if existing.get("status") in ("building", "uploading"):
            return jsonify({"error": "busy", "detail": "Cannot edit a manifest mid-build"}), 409
        body = request.get_json(force=True) or {}
        if _clips_over_cap(body.get("clips")):
            return jsonify({
                "error": "too_many_clips",
                "detail": f"a compilation may contain at most {MAX_CLIPS} clips",
            }), 400
        allowed = {"title", "scheduled_for", "clips", "intro_enabled", "youtube"}
        fields = {k: v for k, v in body.items() if k in allowed}
        if "clips" in fields:
            fields["clips"] = _validate_clips(fields["clips"])
        if "scheduled_for" in fields:
            if fields["scheduled_for"]:
                fields["status"] = "scheduled"
            elif existing.get("status") in ("scheduled", "error"):
                fields["status"] = "draft"
        return jsonify(compilation_store.patch(manifest_id, fields))

    @app.route("/api/compilation/<manifest_id>", methods=["DELETE"])
    @require_admin
    def api_compilation_delete(manifest_id: str):
        existing = compilation_store.get(manifest_id)
        if not existing:
            abort(404)
        if existing.get("status") in ("building", "uploading"):
            return jsonify({"error": "busy"}), 409
        compilation_store.delete(manifest_id)
        with _compilation_jobs_lock:
            _compilation_jobs.pop(manifest_id, None)
        return ("", 204)

    @app.route("/api/compilation/<manifest_id>/build", methods=["POST"])
    @require_admin
    def api_compilation_build(manifest_id: str):
        existing = compilation_store.get(manifest_id)
        if not existing:
            abort(404)
        if existing.get("status") in ("building", "uploading"):
            return jsonify({"error": "busy", "detail": "Already in progress"}), 409
        if not existing.get("clips"):
            return jsonify({"error": "empty", "detail": "No clips in manifest"}), 400
        compilation_store.patch(manifest_id, {"status": "building", "error": None})
        with _compilation_jobs_lock:
            _compilation_jobs[manifest_id] = {
                "status": "building",
                "progress": {"phase": "queued", "done": 0, "total": len(existing.get("clips", []))},
                "error": None,
                "output_path": None,
            }
        threading.Thread(
            target=_run_compilation_build, args=(manifest_id,), daemon=True,
        ).start()
        return jsonify({"id": manifest_id, "status": "building"}), 202

    @app.route("/api/compilation/<manifest_id>/status", methods=["GET"])
    @require_admin
    def api_compilation_status(manifest_id: str):
        with _compilation_jobs_lock:
            job = dict(_compilation_jobs.get(manifest_id, {}))
        m = compilation_store.get(manifest_id)
        if not m and not job:
            abort(404)
        # Prefer in-memory job state for live progress, fall back to disk.
        out = {
            "id": manifest_id,
            "status": job.get("status") or (m or {}).get("status"),
            "progress": job.get("progress") or (m or {}).get("progress"),
            "error": job.get("error") if "error" in job else (m or {}).get("error"),
            "output_path": job.get("output_path") or (m or {}).get("output_path"),
            "youtube_video_id": (m or {}).get("youtube_video_id"),
            "youtube_url": (m or {}).get("youtube_url"),
        }
        return jsonify(out)

    @app.route("/api/compilation/<manifest_id>/download", methods=["GET"])
    @require_admin
    def api_compilation_download(manifest_id: str):
        m = compilation_store.get(manifest_id)
        if not m or not m.get("output_path"):
            abort(404)
        out = Path(m["output_path"])
        # Containment check (mirror of the build-time guard at ~:319-341).
        # ``output_path`` comes from the manifest store; if a manifest is ever
        # tampered with it could point ``send_file`` at any file the dashboard
        # user can read. Refuse anything that resolves outside the compilations
        # output directory before touching the filesystem.
        try:
            out_resolved = out.resolve()
            out_root = COMPILATIONS_OUT_PATH.resolve()
        except (OSError, RuntimeError):
            abort(403)
        try:
            contained = out_resolved.is_relative_to(out_root)
        except ValueError:
            contained = False
        if not contained:
            abort(403)
        if not out_resolved.exists():
            abort(404)
        return send_file(
            out_resolved, mimetype="video/mp4", as_attachment=True,
            download_name=f"{manifest_id}.mp4", conditional=True,
        )

    @app.route("/api/youtube/status", methods=["GET"])
    @require_admin
    def api_youtube_status():
        """Reports whether the YouTube uploader is wired up -- the cart UI
        uses this to enable / grey-out the 'Upload to YouTube' checkbox."""
        return jsonify(youtube_upload.is_configured())

    @app.route("/internal/compilation/tick", methods=["POST", "GET"])
    def internal_compilation_tick():
        """Cron-driven trigger: fires any compilation whose status is
        ``scheduled`` and ``scheduled_for`` is in the past. Auth: shared
        secret in the X-Rovimen-Tick-Token header (env ROVIMEN_TICK_TOKEN).
        We can't trust remote_addr because the dashboard sits behind an
        nginx proxy that always forwards as 127.0.0.1; a header secret is
        the simplest robust gate. Without the env set or with a wrong
        token the endpoint 404s so it doesn't advertise itself."""
        expected = os.environ.get("ROVIMEN_TICK_TOKEN", "")
        provided = request.headers.get("X-Rovimen-Tick-Token", "")
        if not expected or not secrets.compare_digest(expected, provided):
            abort(404)
        now_iso = datetime.now(timezone.utc).isoformat()
        fired: list[str] = []
        for m in compilation_store.load_all():
            if m.get("status") != "scheduled":
                continue
            sched = m.get("scheduled_for")
            if not sched:
                continue
            if sched > now_iso:
                continue
            mid = m.get("id")
            if not mid:
                continue
            # Mark building before the thread starts so a concurrent tick
            # doesn't double-fire it.
            compilation_store.patch(mid, {"status": "building", "error": None})
            with _compilation_jobs_lock:
                _compilation_jobs[mid] = {
                    "status": "building",
                    "progress": {"phase": "queued", "done": 0, "total": len(m.get("clips", []))},
                    "error": None,
                    "output_path": None,
                }
            threading.Thread(
                target=_run_compilation_build, args=(mid,), daemon=True,
            ).start()
            fired.append(mid)
        return jsonify({"fired": fired, "now": now_iso})
