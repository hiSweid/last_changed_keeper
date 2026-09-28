"""Constants for Last Changed Keeper."""

DOMAIN = "last_changed_keeper"

CONF_ALL_ENTITIES = "all_entities"
CONF_DOMAINS = "domains"
CONF_ENTITIES = "entities"
CONF_LABELS = "labels"
CONF_AREAS = "areas"
CONF_EXCLUDE = "exclude"
CONF_GRACE = "grace_seconds"
CONF_RESTORE_LAST_UPDATED = "restore_last_updated"
CONF_RETRY_DELAYS = "retry_delays"
CONF_RESTORE_LAST_TRIGGERED = "restore_last_triggered"
CONF_VERIFY_AFTER_BOOT = "verify_after_boot"
CONF_BULK_BATCH_SIZE = "bulk_batch_size"

DEFAULT_RESTORE_LAST_UPDATED = False
DEFAULT_RESTORE_LAST_TRIGGERED = True
DEFAULT_VERIFY_AFTER_BOOT = False

# Delay (seconds) after the boot pass before the optional self-check verify
# pass runs — late enough that the retry passes (RETRY_DELAYS) are done.
VERIFY_AFTER_BOOT_DELAY = 300
# Default on: most users want everything covered, not a curated domain list.
DEFAULT_ALL_ENTITIES = True

# Generous bound on how long the boot pass will wait for the recorder to
# fully finish starting (see Recorder.async_recorder_ready) before running
# its own recorder-heavy queries anyway. A live schema migration - needed
# after most HA core upgrades - is deliberately deferred by the recorder
# itself until after HA reports "started", i.e. the same moment this
# integration's own boot trigger fires; without this wait the boot pass
# would run directly against a mid-migration recorder, competing for the
# same executor/DB the migration was specifically deferred to avoid
# competing with. Large/low-power installs (SD-card storage, huge
# databases) have been reported taking up to an hour for such a
# migration, hence the generous default - a failed migration never
# resolves the readiness signal at all, so an unconditional (unbounded)
# wait would hang the boot pass forever instead.
RECORDER_READY_TIMEOUT_SECONDS = 3600

# Bound on how long a single history query waits for the recorder's own
# write queue to flush (see _wait_for_recorder_commit) before querying
# anyway. Kept short, unlike RECORDER_READY_TIMEOUT_SECONDS: this guards
# a per-query race (ordinary commit lag, normally sub-second, a few
# seconds under real load - see the alert2.back_door_open field evidence,
# a 3-second gap) rather than a one-time boot gate, and this wait can be
# hit many times in a single pass (once per bulk batch, once per deep
# query) - a long per-call timeout would risk compounding into a much
# longer pass under sustained load instead of just occasionally, briefly
# querying with stale data.
RECORDER_COMMIT_WAIT_TIMEOUT_SECONDS = 10

# Minimum spacing (seconds) between actual recorder-commit waits (see
# _wait_for_recorder_commit) - a second call within this window of a prior
# one returns immediately without re-checking the recorder at all. This
# exists because every wait that finds the queue non-empty forces the
# recorder to commit early (Recorder.async_get_commit_future enqueues a
# SynchronizeTask, and every RecorderTask defaults to commit_before=True) -
# fine for one query, but this integration issues its recorder queries
# sequentially, one at a time, in a plain loop (one per bulk batch, and one
# per still-unresolved entity's deep/last_triggered query), so an
# unthrottled call before every single one turns the recorder's normal,
# efficient ~5s-batched commit behaviour (see recorder's own
# DEFAULT_COMMIT_INTERVAL) into a forced commit per query instead. On a
# large "track all entities" install (thousands of candidates, hundreds
# still unresolved and re-tried by the periodic sweep every 300s) this
# was field-observed to make the recorder fall so far behind under
# sustained real load that RECORDER_COMMIT_WAIT_TIMEOUT_SECONDS itself
# was repeatedly hit (the "commit still pending after 10s" warning
# appearing over and over) - a feedback loop, since each forced-but-stalled
# commit attempt only adds more load to an already-overloaded recorder -
# and, at wall-clock scale, hours of sensor history simply stopped being
# recorded. Coalescing to at most one genuine wait per this interval caps
# how often this integration can force an early commit, independent of how
# many queries a given pass issues, while still catching same-ballpark
# commit lag (the original bug's field evidence was a 3-second-old
# transition) well within this window.
RECORDER_COMMIT_SYNC_MIN_INTERVAL_SECONDS = 2

SERVICE_RESTORE_NOW = "restore_now"
SERVICE_VERIFY = "verify"

# Domains with their own separate last_triggered patch path: it's an
# attribute (not the state value), populated from a dedicated recorder
# lookup rather than the last_changed/state-value logic above.
LAST_TRIGGERED_DOMAINS = ("automation", "script")
ATTR_LAST_TRIGGERED = "last_triggered"

# Debounce for the incremental runtime store (see async_write_snapshot /
# _on_target_state_changed): coalesces bursts of real value changes (e.g. a
# "chatty" entity, or a scene turning off many lights at once) into a single
# store write instead of one per change.
INCREMENTAL_DEBOUNCE_SECONDS = 8
# Hard upper bound on how long a continuously-dirty debounce can be pushed
# back before it is flushed anyway, so a permanently chatty entity can never
# fully starve the incremental store.
INCREMENTAL_MAX_WAIT_SECONDS = 30

# Debounce for the re-registration burst drain (see _on_entity_reregistered /
# _drain_reregister_burst): a mass re-registration (a hub's config entry
# reloading with hundreds of entities, a Zigbee/Z-Wave coordinator coming
# back after an outage) fires one state-changed event per entity, spread
# across a short window rather than all in the same tick. Coalescing them
# into a single batched drain caps recorder load at a handful of bulk
# queries instead of one targeted query per entity. Short relative to
# INCREMENTAL_DEBOUNCE_SECONDS since a lone re-registration (the common
# case) should still feel close to immediate.
REREGISTER_DEBOUNCE_SECONDS = 2
# Hard upper bound on how long a continuously-arriving burst can push the
# debounce back before it is drained anyway, for the same reason
# INCREMENTAL_MAX_WAIT_SECONDS exists: a coordinator that keeps registering
# new entities for a while must not starve the drain of all of them
# indefinitely.
REREGISTER_MAX_WAIT_SECONDS = 10

# Debounce for the boot-pending recovery burst drain (see
# _on_pending_entity_recovered / _drain_pending_recovery_burst): a mass
# unavailable-to-real transition early in boot (a Zigbee/Z-Wave mesh
# finishing formation and announcing many devices within the same few
# seconds) fires one state-changed event per entity, and reacting to each
# with its own recorder query is the same thundering-herd risk
# REREGISTER_DEBOUNCE_SECONDS guards against, just at boot instead of
# during normal runtime. Kept as its own constant (same value) rather than
# reusing REREGISTER_DEBOUNCE_SECONDS so the two can be tuned independently
# if one ever needs to change without affecting the other.
PENDING_RECOVERY_DEBOUNCE_SECONDS = 2
# Hard upper bound on how long a continuously-arriving boot recovery burst
# can push the debounce back before it is drained anyway, for the same
# reason REREGISTER_MAX_WAIT_SECONDS exists.
PENDING_RECOVERY_MAX_WAIT_SECONDS = 10

# How often (seconds) _async_scan_for_new_targets ticks. That one timer
# drives two sweeps: (1) entities matching the configured target criteria
# that didn't exist yet when the boot pass took its one-time targets
# snapshot (see resolve_targets/_async_run_impl) - a coordinator-based
# integration (hub/mesh, cloud-polling) can take anywhere from seconds to
# tens of minutes after HA "started" to finish creating its entities, and
# until this sweep finds them they're invisible to every patch mechanism
# this integration has; and (2) entities that already existed but whose
# resolve attempt simply failed (self._unconfirmed) and would otherwise be
# abandoned forever, since nothing else ever revisits an already-known
# entity once its one resolve attempt has failed - including one that was
# unavailable/unknown at boot (self._pending): its own retry ladder
# (RETRY_DELAYS, max 180s) always finishes well before this timer's first
# tick, so this sweep also covers it once that ladder gives up.
NEW_TARGET_SCAN_INTERVAL_SECONDS = 300

EVENT_RESTORED = f"{DOMAIN}_restored"

# Snapshot store (fallback when the recorder no longer has the entity).
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.snapshot"

# Rolling history of the last boot-pass stats (feeds the status sensor and
# diagnostics: "how well did restore work over the last N restarts").
STORAGE_KEY_RUNS = f"{DOMAIN}.runs"
MAX_RUN_HISTORY = 10

# How often (seconds) to write a periodic snapshot in addition to the one on
# clean shutdown — hedges against crashes/power loss where the shutdown
# event never fires. 0 disables periodic snapshots (shutdown-only).
DEFAULT_SNAPSHOT_INTERVAL = 21600  # 6h
CONF_SNAPSHOT_INTERVAL = "snapshot_interval"

# Repair issue in case a future HA version reworks the state cache.
ISSUE_INCOMPATIBLE = "incompatible_state_cache"

# Default domains whose "last changed" is worth preserving.
DEFAULT_DOMAINS = [
    "light",
    "switch",
    "cover",
    "fan",
    "climate",
    "lock",
    "media_player",
    "input_boolean",
    "humidifier",
    "vacuum",
]

# Only patch entities whose current last_changed is at most this many seconds ago
# (= restart artifact, not really used since boot).
DEFAULT_GRACE = 1800

# Safety margin: only patch if the real time is at least this many seconds before
# the current (restart) last_changed.
MARGIN_SECONDS = 1.5

# Depth of the per-entity fallback query.
HISTORY_DEPTH = 100

# Extra margin (days) added on top of the recorder's own purge_keep_days when
# deciding whether an "unbounded, history exhausted" resolve result (_resolve
# step 4) is trustworthy. Recorder purge runs periodically rather than
# continuously (its own default interval is once a day), so the oldest row
# actually still in the database can lag the nominal now-minus-keep_days
# cutoff by up to about a purge cycle. Without this margin, a same-value run
# that happens to end right at that lag would look like it reached a real
# origin when it may have simply run out of retained rows — indistinguishable
# from a restart artifact whose earlier history was purged (see the
# _near_purge_boundary docstring).
PURGE_BOUNDARY_MARGIN_DAYS = 2

# Time window (days) for the bulk query of all entities at once.
BULK_WINDOW_DAYS = 30

# Per-entity row cap for the bulk query: at most this many of an entity's
# NEWEST genuine value-change rows are fetched per pass. This is what bounds
# a batch's size in rows (BULK_BATCH_SIZE only bounds it in entities): a
# chatty power sensor changing every few seconds has 10^5-10^6 rows in the
# bulk window, and without a per-entity cap a single such entity makes its
# whole batch balloon to hundreds of MB regardless of batch size. The resolve
# walk only ever needs the newest run of same-value rows plus one older,
# differing row to bound it — for entities where even the cap's rows are all
# the same value, the result is depth-capped and treated as unreliable, the
# same rule the per-entity deep query applies via HISTORY_DEPTH.
BULK_PER_ENTITY_LIMIT = 100

# With "track all entities" the candidate list can be thousands of entities;
# one recorder query with an IN(...) list that long gets slow and
# memory-hungry. Split into chunks of this size instead, streamed one batch
# at a time (see _iter_bulk_batches) so only one batch of history is
# resident at once instead of the whole installation's. Together with
# BULK_PER_ENTITY_LIMIT below this bounds a batch to
# BULK_BATCH_SIZE x BULK_PER_ENTITY_LIMIT small rows; the batch size is
# configurable per entry via CONF_BULK_BATCH_SIZE, and the cost of a smaller
# value is only more round-trips (see GitHub issue: OOMs on 3000+ entities).
BULK_BATCH_SIZE = 250

# States that are not real usage (mainly restart artifacts).
INVALID_STATES = ("unavailable", "unknown")

# Seconds after startup for the delayed re-runs (catches devices that return late
# or via a boot sequence unavailable→off→on).
RETRY_DELAYS = (30, 90, 180)
