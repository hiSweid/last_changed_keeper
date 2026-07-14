"""Constants for Last Changed Keeper."""

DOMAIN = "last_changed_keeper"

CONF_DOMAINS = "domains"
CONF_ENTITIES = "entities"
CONF_LABELS = "labels"
CONF_AREAS = "areas"
CONF_EXCLUDE = "exclude"
CONF_GRACE = "grace_seconds"
CONF_RESTORE_LAST_UPDATED = "restore_last_updated"
CONF_RETRY_DELAYS = "retry_delays"

DEFAULT_RESTORE_LAST_UPDATED = False

SERVICE_RESTORE_NOW = "restore_now"

EVENT_RESTORED = f"{DOMAIN}_restored"

# Snapshot store (fallback when the recorder no longer has the entity).
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.snapshot"

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

# Time window (days) for the bulk query of all entities at once.
BULK_WINDOW_DAYS = 30

# States that are not real usage (mainly restart artifacts).
INVALID_STATES = ("unavailable", "unknown")

# Seconds after startup for the delayed re-runs (catches devices that return late
# or via a boot sequence unavailable→off→on).
RETRY_DELAYS = (30, 90, 180)
