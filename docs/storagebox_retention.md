# Storage-box retention

The Hetzner storage box (`/srv/rovimen/archive/`) is the cold tier: every
detection clip a station uploads is kept there. Historically nothing ever
deleted from it, so it grows without bound — it reached 99% on the original
1 TB box in June 2026 (since upgraded to 5 TB). Color-video MKVs are the bulk;
stack `.webp` images and `rms/` metadata are tiny by comparison.

`dashboard/storagebox_janitor.py` enforces a **value-based** retention policy
so the box self-prunes instead of silently filling again.

## Policy

A clip's color-video MKV is pruned once it is **older than `--retention-days`
(default 180 ≈ 6 months)**, *unless* the clip is worth keeping forever by **any**
of these criteria:

| Keep reason     | Rule |
|-----------------|------|
| `manual`        | operator manually locked it (`lock_type == "manual"`) |
| `multistation`  | part of a multi-station event — GMN-confirmed **or** internally correlated across ≥2 of our cameras |
| `bright`        | in the top `--bright-percentile` % by peak magnitude (lower mag = brighter) |
| `long`          | in the top `--long-percentile` % by duration |

Percentile thresholds are computed across the **whole archive** (all ages), so
"top 20% brightest" means brightest of all time — not brightest of the old
cohort.

**Only the `*_color.mkv` video is ever removed.** The stack `.webp`, the
`rms/` analysis, and `state.json` are always kept, so the dashboard still shows
the detection (stack + metadata) — just without the full-resolution clip. Each
pruned clip is marked `"video_pruned": true` in its `state.json` chunk entry,
which makes re-runs idempotent.

## Safety

- **Dry-run by default.** Without `--apply` the janitor deletes nothing and
  just reports what it *would* prune and how much space that reclaims.
- A `(camera, date)` whose `state.json` can't be read is **skipped entirely** —
  the janitor never prunes blind.
- `--max-deletes N` caps deletions per run; re-runs continue where they stopped.
- Recent clips (younger than the retention window) are never touched.

## Running by hand

Always dry-run first and eyeball the report:

```
cd /opt/rovimen
venv/bin/python storagebox_janitor.py --report /tmp/janitor_dry.json
cat /tmp/janitor_dry.json     # check reclaimable GB + keep/prune counts
```

When the numbers look right, apply (throttled):

```
venv/bin/python storagebox_janitor.py --apply --max-deletes 20000 \
    --report /opt/rovimen/storagebox_janitor_report.json
```

## Scheduled runs (systemd)

Unit files live in `deployment/vps/`. They are **not** auto-installed by the
deploy workflow — install them once on the VPS after a dry-run looks good:

```
cp /opt/rovimen/deployment/vps/rovimen-storagebox-janitor.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now rovimen-storagebox-janitor.timer
```

The timer runs the prune weekly (Sun 04:30 UTC). Inspect a run with
`journalctl -u rovimen-storagebox-janitor.service` and the latest
`/opt/rovimen/storagebox_janitor_report.json`.

## Monitoring

Independently of the janitor, alert on box utilisation so we are warned at
~80% rather than discovering 99% by accident:

```
df -h /srv/rovimen/archive
```

(Future: wire this into the dashboard `/api/storagewatch` surface for a
fleet-level disk widget.)
