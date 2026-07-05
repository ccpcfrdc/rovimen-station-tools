#!/usr/bin/env python3
"""config_migrate.py — Merge new default fields into config.json.

Called automatically by updater.sh after each script update.
Adds missing keys from config_defaults.json without touching existing values.
Writes atomically (tmp → rename).

Usage:
    python3 config_migrate.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPTS_DIR / "config.json"
DEFAULTS_PATH = SCRIPTS_DIR / "config_defaults.json"

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# Top-level keys that have been removed from the schema and should be purged
# from any config.json that still contains them.
# NOTE: only add keys that are no longer read anywhere in the codebase.
# Keys that still appear as backward-compat fallbacks (e.g. color_capture_path,
# color_retention_days) must NOT be listed here until those fallbacks are removed.
# Nested keys to remove: { parent_key: [child_keys...] }
# Parent block is also removed if it becomes empty after purging.
REMOVED_NESTED_KEYS: dict[str, list[str]] = {
    "capabilities": ["vaapi", "vaapi_driver", "vaapi_device"],
}

REMOVED_KEYS: list[str] = [
    "clip_retention_days",      # superseded by retention.locked_days
    "retention_days",           # old generic retention key, unused
    "clips_output_path",        # old path key, removed
    "output_path",              # old path key, removed
    "color_stack_path",         # old path key, removed
    "color_timelapse_path",     # old path key, removed
    "clips_path",               # old explicit path key, removed
    "stacks_path",              # old explicit path key, removed
    "thumbnails_path",          # old explicit path key, removed
    "timelapse_path",           # old explicit path key, removed
    "fireball_candidate_path",  # old path key, removed
    # gmn0004 old schema (pre-rewrite)
    "buffer_duration_seconds",
    "detection_timeout_seconds",
    "min_stars_for_detection",
    "ml_filtering_enabled",
    "ml_model_path",
    "ml_threshold",
]


def deep_merge(base: dict, defaults: dict, _path: str = "") -> tuple[dict, list[str]]:
    """Return (merged_dict, list_of_added_key_paths).

    Existing values in base always win.  Missing keys are filled from defaults.
    Recurses into nested dicts.  The 'stations' key is intentionally left
    untouched — station entries are station-specific and have no defaults.
    """
    result = base.copy()
    added: list[str] = []

    for key, default_val in defaults.items():
        full_key = f"{_path}.{key}" if _path else key

        if key not in result:
            result[key] = default_val
            added.append(full_key)
        elif isinstance(default_val, dict) and not isinstance(result[key], dict):
            log.warning("Type mismatch at %s: expected dict, got %s — skipping merge",
                        full_key, type(result[key]).__name__)
        elif isinstance(default_val, dict) and isinstance(result[key], dict):
            # Never recurse into the stations block — it is entirely
            # station-specific and defaults contain no station entries.
            if key == "stations":
                continue
            result[key], sub_added = deep_merge(result[key], default_val, full_key)
            added.extend(sub_added)

    return result, added


def migrate(dry_run: bool = False) -> int:
    """Merge defaults into config.json.  Returns number of fields added."""
    if not CONFIG_PATH.exists():
        log.info("No config.json found — skipping migration")
        return 0
    if not DEFAULTS_PATH.exists():
        log.info("No config_defaults.json found — skipping migration")
        return 0

    config = json.loads(CONFIG_PATH.read_text())
    defaults = json.loads(DEFAULTS_PATH.read_text())

    merged, added = deep_merge(config, defaults)

    purged = [k for k in REMOVED_KEYS if k in merged]
    for k in purged:
        del merged[k]

    purged_nested: list[str] = []
    for parent, children in REMOVED_NESTED_KEYS.items():
        if not isinstance(merged.get(parent), dict):
            continue
        for child in children:
            if child in merged[parent]:
                del merged[parent][child]
                purged_nested.append(f"{parent}.{child}")
        if not merged[parent]:
            del merged[parent]
            purged_nested.append(f"{parent} (empty, removed)")

    if not added and not purged and not purged_nested:
        log.info("config.json is up to date")
        return 0

    if added:
        log.info("config.json: adding %d field(s): %s", len(added), ", ".join(added))
    if purged:
        log.info("config.json: removing %d obsolete field(s): %s", len(purged), ", ".join(purged))
    if purged_nested:
        log.info("config.json: removing %d nested obsolete field(s): %s", len(purged_nested), ", ".join(purged_nested))

    if dry_run:
        log.info("[dry-run] no changes written")
        return len(added)

    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=4))
    os.replace(tmp, CONFIG_PATH)
    log.info("config.json updated successfully")
    return len(added)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be added without writing",
    )
    args = parser.parse_args()
    sys.exit(0 if migrate(dry_run=args.dry_run) >= 0 else 1)


if __name__ == "__main__":
    main()
