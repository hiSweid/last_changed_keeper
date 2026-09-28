"""Last Changed Keeper.

Restores the real last change time (`last_changed`) of selected entities after a
Home Assistant restart — directly on the entity.

Sources (in this order):
1. Recorder bulk query (batched over all candidates, streamed one batch at
   a time, and capped per entity to the newest BULK_PER_ENTITY_LIMIT genuine
   value changes, so peak memory stays at one bounded batch) — fast on
   startup.
2. Incremental/periodic store (see async_write_snapshot and
   _on_target_state_changed) — preferred over the bulk result when it holds a
   newer, still-usable timestamp for the same value (e.g. recorder commit
   lag, or a change from between two periodic snapshots).
3. Recorder per-entity query (deeper) as a fallback for ambiguous cases.
4. Snapshot store alone — if the recorder no longer has the entity.

`automation`/`script` entities additionally get their `last_triggered`
attribute restored through a separate path (see _maybe_restore_last_triggered)
— it's an attribute, not the state value, so it needs its own recorder read
and its own apply mechanism.

Entities that get fully re-registered at runtime (a config entry reload, a
Zigbee/Z-Wave device rejoining) — or that merely recover from
unavailable/unknown to a real value during normal runtime, e.g. a brief
network/mesh blip — are caught the same way a restart is, via a persistent
listener (see _setup_reregister_listener) independent of the boot-time
pending/listener machinery. Both trigger conditions are debounce-coalesced
into a single batched drain (see _drain_reregister_burst) so a mass event —
e.g. a hub's config entry reloading with hundreds of entities at once, or a
mesh-wide outage recovering — costs a handful of bulk queries instead of
one recorder query per entity.

Note: setting `last_changed`/`last_triggered` + invalidating the state cache
uses internal HA structures. All accesses are defensively guarded; if the
cache access fails, a repair issue is raised instead of crashing.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_last_state_changes
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    State,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.recorder import session_scope
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
from homeassistant.util.read_only_dict import ReadOnlyDict
from sqlalchemy import func, or_, select

# Internal recorder module (unlike the public helpers above): HA has
# reorganized it before (states_meta split in 2023.4, history package moves).
# Guarded so a future move degrades _bulk_query into the per-batch fallback
# path (empty batch -> snapshot/deep query, see _iter_bulk_batches) instead
# of an ImportError taking the whole integration down.
try:
    from homeassistant.components.recorder.db_schema import States, StatesMeta
except ImportError:  # pragma: no cover - only hit on future HA reorganization
    States = StatesMeta = None  # type: ignore[assignment,misc]

from .const import (
    ATTR_LAST_TRIGGERED,
    BULK_BATCH_SIZE,
    BULK_PER_ENTITY_LIMIT,
    BULK_WINDOW_DAYS,
    CONF_ALL_ENTITIES,
    CONF_AREAS,
    CONF_BULK_BATCH_SIZE,
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_EXCLUDE,
    CONF_GRACE,
    CONF_LABELS,
    CONF_RESTORE_LAST_TRIGGERED,
    CONF_RESTORE_LAST_UPDATED,
    CONF_RETRY_DELAYS,
    CONF_SNAPSHOT_INTERVAL,
    CONF_VERIFY_AFTER_BOOT,
    DEFAULT_ALL_ENTITIES,
    DEFAULT_DOMAINS,
    DEFAULT_GRACE,
    DEFAULT_RESTORE_LAST_TRIGGERED,
    DEFAULT_RESTORE_LAST_UPDATED,
    DEFAULT_SNAPSHOT_INTERVAL,
    DEFAULT_VERIFY_AFTER_BOOT,
    DOMAIN,
    EVENT_RESTORED,
    HISTORY_DEPTH,
    INCREMENTAL_DEBOUNCE_SECONDS,
    INCREMENTAL_MAX_WAIT_SECONDS,
    INVALID_STATES,
    ISSUE_INCOMPATIBLE,
    LAST_TRIGGERED_DOMAINS,
    MARGIN_SECONDS,
    MAX_RUN_HISTORY,
    NEW_TARGET_SCAN_INTERVAL_SECONDS,
    PENDING_RECOVERY_DEBOUNCE_SECONDS,
    PENDING_RECOVERY_MAX_WAIT_SECONDS,
    PURGE_BOUNDARY_MARGIN_DAYS,
    RECORDER_COMMIT_SYNC_MIN_INTERVAL_SECONDS,
    RECORDER_COMMIT_WAIT_TIMEOUT_SECONDS,
    RECORDER_READY_TIMEOUT_SECONDS,
    REREGISTER_DEBOUNCE_SECONDS,
    REREGISTER_MAX_WAIT_SECONDS,
    RETRY_DELAYS,
    SERVICE_RESTORE_NOW,
    SERVICE_VERIFY,
    STORAGE_KEY,
    STORAGE_KEY_RUNS,
    STORAGE_VERSION,
    VERIFY_AFTER_BOOT_DELAY,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]

# Return value of _attempt_reregister_patch meaning "valid candidate, just
# not resolvable yet" — the only outcome that justifies arming a retry
# ladder. Spelled as a name rather than a bare False so the three-way
# result (patched / retry worthwhile / not a candidate) reads explicitly at
# both the return and the call site.
_RETRY_WORTHWHILE = False

# Lazily evaluated (PEP 695), so this forward-references _RestoreJob safely.
type LckConfigEntry = ConfigEntry[_RestoreJob]

# single_config_entry: true, so this integration has no meaningful standalone
# YAML config — but defining async_setup() (to register the service
# independent of any entry) requires a CONFIG_SCHEMA, or hassfest's
# config-schema check flags it.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the restore_now service once, regardless of entry state.

    Registering here (rather than in async_setup_entry) means the service
    still exists — with a clear ServiceValidationError — even if the entry
    is disabled, failed, or momentarily reloading, instead of automations
    hitting a raw "service not found".
    """
    _async_register_service(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LckConfigEntry) -> bool:
    """Set up the job, load the snapshot and run on start (or immediately)."""
    store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    snapshot: dict[str, str] = await store.async_load() or {}

    runs_store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY_RUNS)
    run_history: list[dict] = await runs_store.async_load() or []

    job = _RestoreJob(hass, entry, store, snapshot, runs_store, run_history)
    entry.runtime_data = job

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(job.shutdown)

    # Write the snapshot on shutdown (one write, no ongoing cost).
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, job.async_write_snapshot)
    )

    # Also write it periodically: a clean shutdown isn't guaranteed (power
    # loss, OOM kill, forced container restart), and a snapshot from days
    # ago is a much weaker fallback than one from a few hours ago,
    # especially now that a snapshot is only usable on an exact state match
    # (see async_write_snapshot). 0 disables this (shutdown-only).
    interval = job._snapshot_interval
    if interval > 0:
        entry.async_on_unload(
            async_track_time_interval(
                hass,
                job.async_write_snapshot,
                timedelta(seconds=interval),
                cancel_on_shutdown=True,
            )
        )

    # async_at_started calls the callback once HA is fully started (entities
    # loaded) — or immediately if already started (reload/service). Robust
    # against the "starting" phase where hass.is_running is already True.
    @callback
    def _on_started(_hass: HomeAssistant) -> None:
        hass.async_create_task(job.async_run())

    entry.async_on_unload(async_at_started(hass, _on_started))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: LckConfigEntry) -> bool:
    """Unload the entry.

    Beyond the platforms, nothing to clean up manually: job.shutdown() runs
    via the async_on_unload callback above, and core deletes
    entry.runtime_data itself once this returns True. The service is
    registered in async_setup() and is intentionally NOT torn down here.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: LckConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_register_service(hass: HomeAssistant) -> None:
    """Register the last_changed_keeper.restore_now service (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_RESTORE_NOW):
        return

    async def _handle_restore_now(call: ServiceCall) -> ServiceResponse:
        entries: list[LckConfigEntry] = hass.config_entries.async_loaded_entries(
            DOMAIN
        )
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="entry_not_loaded"
            )
        patched_by_entry: dict[str, int] = {}
        for entry in entries:
            job = entry.runtime_data
            try:
                patched_by_entry[entry.entry_id] = await job._async_run_impl(
                    single_pass=True
                )
            except Exception as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="restore_failed"
                ) from err
        if not call.return_response:
            return None
        # single_config_entry: true, so there is exactly one entry/job.
        job = entries[0].runtime_data
        return {"patched": sum(patched_by_entry.values()), "last_run": job.stats}

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_NOW,
        _handle_restore_now,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _handle_verify(call: ServiceCall) -> ServiceResponse:
        entries: list[LckConfigEntry] = hass.config_entries.async_loaded_entries(
            DOMAIN
        )
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="entry_not_loaded"
            )
        # single_config_entry: true, so there is exactly one entry/job.
        job = entries[0].runtime_data
        try:
            return await job.async_verify()
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="verify_failed"
            ) from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_VERIFY,
        _handle_verify,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )


class _RestoreJob:
    """Encapsulates one restore run incl. listener, re-runs and snapshot."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: Store,
        snapshot: dict[str, str],
        runs_store: Store | None = None,
        run_history: list[dict] | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._store = store
        self._snapshot = snapshot
        self._pending: set[str] = set()
        self._startup = dt_util.utcnow()
        self._unsub_listener: CALLBACK_TYPE | None = None
        self._unsub_timers: list[CALLBACK_TYPE] = []
        # Debounce-coalesced burst of pending entities recovering from
        # unavailable during the boot window, drained together via
        # _drain_pending_recovery_burst instead of one recorder query per
        # entity — see _on_pending_entity_recovered.
        self._pending_recovery_burst: set[str] = set()
        self._pending_recovery_burst_since: datetime | None = None
        self._pending_recovery_flush_timer: CALLBACK_TYPE | None = None
        self._degraded = False
        self._also_updated = False
        self._also_restore_triggered = False
        self._final_fired = False
        self.stats: dict[str, object] = {}

        # Entities whose live last_changed is still an unconfirmed restart
        # artifact (a candidate this session that _resolve() has not yet
        # been able to patch) — see async_write_snapshot, which must not
        # persist these into the snapshot store as if they were real.
        self._unconfirmed: set[str] = set()

        # Entities positively verified this session: either successfully
        # _resolve()-and-_apply()'d, or observed by the incremental listener
        # to undergo a genuine value transition. The ONLY thing
        # async_write_snapshot trusts as ground truth — an entity that was
        # merely never flagged self._unconfirmed (e.g. the boot pass's grace
        # check assumed it was "already genuinely used since boot" without
        # ever actually examining it) is NOT enough; that assumption can be
        # wrong if _async_run_impl itself runs late relative to the true HA
        # restart (recorder migration, startup congestion), and trusting it
        # anyway is exactly how a raw restart artifact gets permanently
        # baked into the snapshot as if it were real. Always mutated via
        # _mark_confirmed/_mark_unconfirmed below, never directly, so this
        # set and self._unconfirmed can never drift out of sync.
        self._confirmed: set[str] = set()

        # ----- Feature: rolling run history + boot self-check --------------
        # runs_store is None in direct unit-test construction; persistence is
        # then simply skipped (run_history stays in-memory only).
        self._runs_store = runs_store
        self.run_history: list[dict] = run_history or []
        self._run_active = False
        self._unsub_verify_timer: CALLBACK_TYPE | None = None

        # ----- Feature: re-patch on runtime re-registration ---------------
        self._unsub_reregister_listener: CALLBACK_TYPE | None = None
        self._reregister_retry_timers: dict[str, list[CALLBACK_TYPE]] = {}
        # Debounce-coalesced burst of pending re-registrations, drained
        # together via _drain_reregister_burst instead of one recorder query
        # per entity — see _on_entity_reregistered.
        self._reregister_burst: set[str] = set()
        self._reregister_burst_since: datetime | None = None
        self._reregister_flush_timer: CALLBACK_TYPE | None = None

        # ----- Feature: incremental runtime store --------------------------
        self._unsub_incremental_listener: CALLBACK_TYPE | None = None
        self._dirty: dict[str, dict[str, str]] = {}
        self._dirty_since: datetime | None = None
        self._flush_timer: CALLBACK_TYPE | None = None

        # ----- Feature: periodic target-discovery sweep --------------------
        # Entities matching the configured criteria that the persistent
        # listeners (above) are currently subscribed to — grows over the
        # entry's lifetime as _async_scan_for_new_targets finds entities
        # that didn't exist yet when _async_run_impl took its one-time boot
        # snapshot. See _async_scan_for_new_targets for why that snapshot
        # alone isn't enough.
        self._known_targets: set[str] = set()
        self._unsub_target_discovery_timer: CALLBACK_TYPE | None = None

        # ----- Feature: recorder commit-wait coalescing --------------------
        # monotonic timestamp of the last time _wait_for_recorder_commit
        # actually checked the recorder (rather than short-circuiting on
        # the cooldown below) — see RECORDER_COMMIT_SYNC_MIN_INTERVAL_SECONDS.
        self._last_commit_sync_monotonic: float = 0.0

    # ----- Configuration -------------------------------------------------

    @property
    def _config(
        self,
    ) -> tuple[list[str], list[str], list[str], float, list[str], list[str]]:
        data = {**self.entry.data, **self.entry.options}
        return (
            data.get(CONF_DOMAINS, DEFAULT_DOMAINS),
            data.get(CONF_ENTITIES, []),
            data.get(CONF_EXCLUDE, []),
            float(data.get(CONF_GRACE, DEFAULT_GRACE)),
            data.get(CONF_LABELS, []),
            data.get(CONF_AREAS, []),
        )

    @property
    def _all_entities_enabled(self) -> bool:
        data = {**self.entry.data, **self.entry.options}
        return bool(data.get(CONF_ALL_ENTITIES, DEFAULT_ALL_ENTITIES))

    @property
    def _restore_last_updated_enabled(self) -> bool:
        data = {**self.entry.data, **self.entry.options}
        return bool(
            data.get(CONF_RESTORE_LAST_UPDATED, DEFAULT_RESTORE_LAST_UPDATED)
        )

    @property
    def _restore_last_triggered_enabled(self) -> bool:
        data = {**self.entry.data, **self.entry.options}
        return bool(
            data.get(CONF_RESTORE_LAST_TRIGGERED, DEFAULT_RESTORE_LAST_TRIGGERED)
        )

    @property
    def _verify_after_boot_enabled(self) -> bool:
        data = {**self.entry.data, **self.entry.options}
        return bool(data.get(CONF_VERIFY_AFTER_BOOT, DEFAULT_VERIFY_AFTER_BOOT))

    @property
    def _retry_delays(self) -> tuple[int, ...]:
        data = {**self.entry.data, **self.entry.options}
        return _parse_delays(data.get(CONF_RETRY_DELAYS), RETRY_DELAYS)

    @property
    def _snapshot_interval(self) -> float:
        data = {**self.entry.data, **self.entry.options}
        try:
            return float(data.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL))
        except (TypeError, ValueError):
            return DEFAULT_SNAPSHOT_INTERVAL

    @property
    def _bulk_batch_size(self) -> int:
        data = {**self.entry.data, **self.entry.options}
        try:
            value = int(data.get(CONF_BULK_BATCH_SIZE, BULK_BATCH_SIZE))
        except (TypeError, ValueError):
            return BULK_BATCH_SIZE
        return value if value > 0 else BULK_BATCH_SIZE

    def _targets(
        self,
        domains: list[str],
        entities: list[str],
        exclude: list[str] | None = None,
        labels: list[str] | None = None,
        areas: list[str] | None = None,
    ) -> set[str]:
        return resolve_targets(
            self.hass,
            domains,
            entities,
            exclude,
            labels,
            areas,
            self._all_entities_enabled,
        )

    # ----- Lifecycle -----------------------------------------------------

    @callback
    def shutdown(self) -> None:
        """Cancel everything (on unload/reload): boot machinery, the
        persistent re-registration listener/retries, the incremental store
        listener/timer, and the target-discovery sweep timer."""
        self._stop_boot_machinery()
        self._stop_reregister_listener()
        self._cancel_all_reregister_retries()
        self._cancel_reregister_flush_timer()
        self._reregister_burst.clear()
        self._reregister_burst_since = None
        self._stop_incremental_listener()
        self._cancel_flush_timer()
        self._dirty.clear()
        self._dirty_since = None
        self._cancel_target_discovery_timer()
        if self._unsub_verify_timer is not None:
            self._unsub_verify_timer()
            self._unsub_verify_timer = None

    @callback
    def _stop_boot_machinery(self) -> None:
        """Cancel only the boot-pass-specific listener/timers.

        Kept separate from shutdown() because _cleanup_if_done() calls this
        once the boot pending set drains — that must not also tear down the
        persistent re-registration/incremental-store listeners, which live
        for the whole entry lifetime, not just the boot pass.
        """
        self._stop_listener()
        for cancel in self._unsub_timers:
            cancel()
        self._unsub_timers.clear()
        self._cancel_pending_recovery_flush_timer()
        self._pending_recovery_burst.clear()
        self._pending_recovery_burst_since = None

    @callback
    def _stop_listener(self) -> None:
        if self._unsub_listener is not None:
            self._unsub_listener()
            self._unsub_listener = None

    @callback
    def _cancel_pending_recovery_flush_timer(self) -> None:
        if self._pending_recovery_flush_timer is not None:
            self._pending_recovery_flush_timer()
            self._pending_recovery_flush_timer = None

    @callback
    def _stop_reregister_listener(self) -> None:
        if self._unsub_reregister_listener is not None:
            self._unsub_reregister_listener()
            self._unsub_reregister_listener = None

    @callback
    def _cancel_all_reregister_retries(self) -> None:
        for timers in self._reregister_retry_timers.values():
            for cancel in timers:
                cancel()
        self._reregister_retry_timers.clear()

    @callback
    def _cancel_reregister_flush_timer(self) -> None:
        if self._reregister_flush_timer is not None:
            self._reregister_flush_timer()
            self._reregister_flush_timer = None

    @callback
    def _stop_incremental_listener(self) -> None:
        if self._unsub_incremental_listener is not None:
            self._unsub_incremental_listener()
            self._unsub_incremental_listener = None

    @callback
    def _cancel_flush_timer(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer()
            self._flush_timer = None

    @callback
    def _cancel_target_discovery_timer(self) -> None:
        if self._unsub_target_discovery_timer is not None:
            self._unsub_target_discovery_timer()
            self._unsub_target_discovery_timer = None

    # ----- Snapshot ------------------------------------------------------

    async def async_write_snapshot(self, _when: Event | datetime | None = None) -> None:
        """Persist the current state + last_changed of all targets.

        The state value is stored alongside the timestamp so a snapshot can
        only be applied if the entity still holds the same value on the next
        boot (see _resolve) — otherwise it could stamp the *previous* value's
        last_changed onto a genuinely new value.

        Trust is an ALLOWLIST, not a blocklist: only an entity in
        self._confirmed (positively verified this session — see its
        declaration comment and _mark_confirmed/_mark_unconfirmed) is
        written from its current live state. Merely being absent from
        self._unconfirmed is NOT enough — an entity the boot pass's grace
        check silently classified as "already genuinely used since boot"
        (__init__.py's candidate-building loop) is never added to either
        set, and if that classification was wrong (_async_run_impl itself
        ran later than expected relative to the true HA restart — a slow
        recorder migration or general startup congestion, e.g. right after
        an upgrade, is plausible), live.last_changed there is still just
        this restart's raw reset value, not a real timestamp. Trusting it
        anyway would poison the snapshot with that artifact, and _resolve's
        step 2 would then confidently reapply it on every future boot
        (comfortably clearing the margin check against each new restart's
        fresher cutoff) without ever reaching the deep/best-effort steps
        that would find the real answer — permanently, since nothing before
        this ever re-checked an already-"confirmed" entity. So anything not
        in self._confirmed has its *existing* stored entry (if any) carried
        forward unchanged instead — no worse than before, and not actively
        made wrong.

        Used both as the EVENT_HOMEASSISTANT_STOP listener (receives an
        Event) and as the periodic snapshot timer (receives a datetime) —
        the argument itself is never used, both just trigger a fresh write.
        """
        domains, entities, exclude, _, labels, areas = self._config
        data: dict[str, dict[str, str]] = {}
        for entity_id in self._targets(domains, entities, exclude, labels, areas):
            live = self.hass.states.get(entity_id)
            if live is None or live.state in INVALID_STATES:
                continue
            if entity_id in self._confirmed:
                data[entity_id] = {"s": live.state, "t": live.last_changed.isoformat()}
                continue
            prior = self._snapshot.get(entity_id)
            if prior is not None:
                data[entity_id] = prior
        await self._store.async_save(data)
        # Keep the in-memory copy in sync with what's on disk: this is also
        # what naturally bounds the incremental store's size (see
        # _flush_dirty) — any entity no longer in current targets is dropped
        # here instead of lingering forever.
        self._snapshot = data
        _LOGGER.debug("Wrote snapshot with %d entries", len(data))

    # ----- Main run ------------------------------------------------------

    async def async_run(self, *, single_pass: bool = False) -> int:
        """Guarded run: an exception must not kill the HA start."""
        try:
            await self._wait_for_recorder_ready()
            return await self._async_run_impl(single_pass=single_pass)
        except Exception:
            _LOGGER.exception("Last Changed Keeper: async_run failed")
            return 0

    async def _wait_for_recorder_ready(self) -> None:
        """Wait for the recorder to be genuinely ready before the boot pass
        runs its own recorder-heavy bulk/deep queries against it.

        manifest.json's "recorder" dependency only guarantees recorder's
        own async_setup() has returned, which is gated on
        Recorder.async_db_ready — resolved once the DB is connected and
        any NON-live migration is done. A *live* migration (the kind
        needed after most HA core upgrades) is deliberately deferred by
        the recorder itself until AFTER HA reports "started" ("we do not
        want it to compete with startup which is also cpu intensive") —
        i.e. exactly the same moment this integration's own boot trigger,
        async_at_started, fires. Without this wait, a live migration in
        progress means our bulk/deep queries compete with it for the same
        executor/DB, both slowing the boot pass down dramatically and
        making a late (>grace) _async_run_impl invocation — and the
        snapshot poisoning that can follow from it (see self._confirmed) —
        a real, common scenario rather than a hypothetical one.

        Recorder.async_recorder_ready (an asyncio.Event, distinct from
        async_db_ready) is only set once migration is genuinely finished.
        It's bounded with a generous timeout rather than awaited
        unconditionally because a failed live migration never sets it at
        all — unconditionally awaiting it would then hang this
        integration's boot pass forever over an unrelated recorder
        failure. On timeout, proceed anyway: querying a possibly-busy
        recorder is still better than never running at all.
        """
        try:
            instance = get_instance(self.hass)
        except Exception:  # noqa: BLE001 - recorder must not kill anything
            return
        try:
            await asyncio.wait_for(
                instance.async_recorder_ready.wait(),
                timeout=RECORDER_READY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _LOGGER.warning(
                "Last Changed Keeper: recorder still not ready after %ds "
                "(migration or startup congestion?) - proceeding anyway",
                RECORDER_READY_TIMEOUT_SECONDS,
            )

    async def _wait_for_recorder_commit(self) -> bool:
        """Flush the recorder's own write queue before querying history, so
        a query can never silently miss a row that's already visible via
        hass.states.get() but not yet committed to the database (recorder
        commit lag).

        _resolve's "bounded" trust (see its docstring) treats the instant a
        query returns any older, differently-valued row as definitive, with
        no protection — unlike the unbounded path's _near_purge_boundary —
        against the query simply having run ahead of the recorder's own
        queue. If the most recent transition genuinely hasn't been
        committed yet, a bounded query confidently returns the *previous*
        boundary instead: wrong, but indistinguishable from a correct
        bounded result at the call site. Field evidence: a real door
        sensor's expected_last_changed matched a transition from the
        previous evening, even though the door had genuinely, physically
        reopened and closed again 3 seconds before the query that produced
        that answer. Most likely right after boot (a burst of restart-time
        writes queued at once) but not boot-exclusive — any query racing a
        very recent write is at risk, which is why this is called from
        every history query site rather than gated to the boot pass the
        way _wait_for_recorder_ready is.

        Recorder.async_get_commit_future returns None immediately when the
        write queue is already empty (the common case, by far), so this
        costs nothing apart from the cases it exists to catch. Bounded with
        a short timeout: if the queue is genuinely stuck rather than just
        busy, every other recorder-backed query this integration makes is
        already broken, and querying anyway is still better than hanging
        this one indefinitely.

        Returns False (checked both before and after the wait, since the
        wait itself takes real time and can straddle the moment shutdown
        begins) if hass is stopping — the caller must then skip its query
        rather than dispatch new recorder work against a recorder that may
        already be disposing its connection. HA's own recorder shutdown
        deliberately closes the connection before forcibly joining any
        still-running query thread (core.py's _shutdown: "we shutdown the
        executor without forcefully joining the threads until after we
        have tried to cleanly close the connection"), trusting a normally-
        brief in-flight query to finish or be interrupted cleanly in that
        window; a query we could have avoided starting in the first place
        by checking here shouldn't be made to rely on that.

        Coalesced to at most one genuine check per
        RECORDER_COMMIT_SYNC_MIN_INTERVAL_SECONDS. A non-empty write queue
        makes async_get_commit_future() enqueue a SynchronizeTask, and
        every RecorderTask defaults to commit_before=True — so a "free
        when idle" check is not free at all on a busy install, it forces
        an early commit. This integration issues its recorder queries
        sequentially (one per bulk batch, one per still-unresolved
        entity's deep/last_triggered query) in a plain loop with no
        pacing of its own, so calling this unconditionally before every
        one of them turns the recorder's normal, efficient batched commit
        behaviour into a forced commit per query. Field-observed on a
        large install: the recorder fell far enough behind under this
        self-reinforcing load (each stalled forced commit adding more
        load in turn) that RECORDER_COMMIT_WAIT_TIMEOUT_SECONDS itself was
        repeatedly hit, and real sensor history stopped being recorded for
        hours — worse than the commit-lag bug this method exists to fix.
        The cooldown caps forced-commit frequency independent of query
        volume while still catching commit lag on the same timescale as
        the original field evidence (a 3-second-old transition).
        """
        if self.hass.is_stopping:
            return False
        now = time.monotonic()
        elapsed = now - self._last_commit_sync_monotonic
        if elapsed < RECORDER_COMMIT_SYNC_MIN_INTERVAL_SECONDS:
            return True
        try:
            instance = get_instance(self.hass)
        except Exception:  # noqa: BLE001 - recorder must not kill anything
            return True
        future = instance.async_get_commit_future()
        if future is not None:
            try:
                await asyncio.wait_for(
                    future, timeout=RECORDER_COMMIT_WAIT_TIMEOUT_SECONDS
                )
            except TimeoutError:
                _LOGGER.debug(
                    "Last Changed Keeper: recorder commit still pending "
                    "after %ds - querying anyway",
                    RECORDER_COMMIT_WAIT_TIMEOUT_SECONDS,
                )
        self._last_commit_sync_monotonic = time.monotonic()
        return not self.hass.is_stopping

    async def _async_run_impl(self, *, single_pass: bool = False) -> int:
        """Initial pass (with bulk query). Sets up listener + re-runs."""
        if single_pass and self._pending:
            # A boot pass is still active (listener + retry timers running
            # for entities that came back late). Do an immediate one-off
            # attempt on the currently pending entities WITHOUT touching
            # that machinery — resetting it here (see below) would silently
            # orphan every entity still pending for the rest of the grace
            # window.
            return await self._immediate_pending_pass()

        self.shutdown()
        self._degraded = False
        self._also_updated = self._restore_last_updated_enabled
        self._also_restore_triggered = self._restore_last_triggered_enabled
        domains, entities, exclude, grace, labels, areas = self._config
        targets = self._targets(domains, entities, exclude, labels, areas)
        self._known_targets = targets

        # Persistent, entry-lifetime listeners (independent of the boot
        # pending/listener machinery below, and not torn down when the boot
        # pass settles — see _stop_boot_machinery vs shutdown()). Set up
        # even when targets is currently empty: the discovery sweep is
        # exactly what covers a domain/label/area-based config where
        # nothing matches *yet* (that domain's integration hasn't finished
        # creating its entities at boot at all) — skipping this setup here
        # on an empty boot-time snapshot would silently defeat the sweep
        # for precisely the installs it exists to help.
        self._setup_reregister_listener()
        self._setup_incremental_listener()
        self._unsub_target_discovery_timer = async_track_time_interval(
            self.hass,
            self._async_scan_for_new_targets,
            timedelta(seconds=NEW_TARGET_SCAN_INTERVAL_SECONDS),
            cancel_on_shutdown=True,
        )

        if not targets:
            return 0

        self._startup = dt_util.utcnow()
        self._pending = set()
        self._final_fired = False

        # Candidates: fresh (restart artifact) and currently valid. Capture
        # each candidate's last_changed as observed here — the streaming
        # resolve loop below re-validates against this snapshot rather than
        # against grace/now a second time (see the comment there for why).
        candidates: list[str] = []
        candidate_last_changed: dict[str, datetime] = {}
        for entity_id in targets:
            live = self.hass.states.get(entity_id)
            if live is None or live.state in INVALID_STATES:
                self._pending.add(entity_id)
                self._mark_unconfirmed(entity_id)
                continue
            if (dt_util.utcnow() - live.last_changed).total_seconds() > grace:
                continue  # already really used since boot
            candidates.append(entity_id)
            candidate_last_changed[entity_id] = live.last_changed

        patched = 0
        patched_triggered = 0
        bulk_entities = 0
        bulk_rows_fetched = 0
        bulk_batches = 0
        counters: dict[str, int] = {}
        # Resolve each batch as it arrives and drop it again, so only one
        # batch of recorder history is resident at a time — see
        # _iter_bulk_batches.
        async for chunk, bulk in self._iter_bulk_batches(candidates):
            bulk_batches += 1
            bulk_entities += len(bulk)
            bulk_rows_fetched += sum(len(rows) for rows in bulk.values())
            for entity_id in chunk:
                # Re-validate: the bulk query above awaited the recorder
                # executor, during which this entity may have gone
                # unavailable or genuinely changed for real — either way the
                # state captured before the await is stale and must not be
                # trusted anymore.
                #
                # "Genuinely changed" is decided against the last_changed
                # snapshotted when the candidate list was built, not by
                # re-checking elapsed time against grace: a streamed pass can
                # itself run long, and an untouched entity's last_changed
                # doesn't move just because our own pass is slow — an
                # elapsed-time re-check would silently drop entities from the
                # tail of a slow run (skipped here, never added to _pending,
                # so nothing ever retries them) purely as a function of pass
                # duration. Comparing last_changed only skips an entity that
                # actually transitioned since it was listed as a candidate.
                live = self.hass.states.get(entity_id)
                if live is None or live.state in INVALID_STATES:
                    self._pending.add(entity_id)
                    self._mark_unconfirmed(entity_id)
                    continue
                if live.last_changed != candidate_last_changed[entity_id]:
                    # Changed for real while we were awaiting the query — this
                    # last_changed reflects genuine usage, not an artifact.
                    self._mark_confirmed(entity_id)
                    continue
                if await self._maybe_restore_last_triggered(entity_id):
                    patched_triggered += 1
                ts = await self._resolve(entity_id, live, bulk.get(entity_id), counters)
                if ts is not None:
                    self._apply(live, ts, entity_id)
                    patched += 1
                else:
                    # _resolve couldn't confirm anything: live.last_changed is
                    # still this restart's raw reset value. Tracked so
                    # async_write_snapshot doesn't persist it as if it were
                    # real (see _unconfirmed).
                    self._mark_unconfirmed(entity_id)
            del bulk  # released before the next batch is fetched

        # Resolve a stale repair issue once the cache patch works again
        # (e.g. after an HA update). Idempotent.
        if patched and not self._degraded:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_INCOMPATIBLE)

        self.stats = {
            "started": self._startup.isoformat(),
            "targets": len(targets),
            "candidates": len(candidates),
            "bulk_entities": bulk_entities,
            "bulk_rows_fetched": bulk_rows_fetched,
            "bulk_batches": bulk_batches,
            "deep_queries": counters.get("deep_queries", 0),
            "patched_immediate": patched,
            "patched_total": patched,
            "patched_last_triggered": patched_triggered,
            "pending": len(self._pending),
            "snapshot_entries": len(self._snapshot),
            "degraded": self._degraded,
        }
        _LOGGER.info(
            "Last Changed Keeper: pass 0 — %d patched, %d pending "
            "(bulk: %d entities / %d rows in %d batches, %d deep queries)",
            patched, len(self._pending), bulk_entities, bulk_rows_fetched,
            bulk_batches, counters.get("deep_queries", 0),
        )
        # Record boot passes (not service-triggered ones) in the rolling run
        # history — before firing the event, so the final-event update below
        # replaces this record instead of appending a second one.
        if not single_pass:
            self._run_active = True
            self._persist_run_stats(new_run=True)
        self._fire_restored_event(final=not self._pending)

        if not single_pass and self._verify_after_boot_enabled:
            self._schedule_boot_verify()

        if single_pass or not self._pending:
            return patched

        self._setup_listener(grace)
        self._schedule_retries(grace)
        return patched

    @callback
    def _setup_listener(self, grace: float) -> None:
        """Listen for unavailable→real transitions of the pending entities.

        Individual transitions are debounce-coalesced into a burst set and
        drained together via _drain_pending_recovery_burst, instead of
        firing one recorder query per entity — a mass unavailable→real
        transition early in boot (a Zigbee/Z-Wave mesh finishing formation
        and announcing many devices within the same few seconds) would
        otherwise be a thundering herd on the recorder executor.
        """

        @callback
        def _on_change(event: Event) -> None:
            old = event.data.get("old_state")
            new = event.data.get("new_state")
            if new is None or new.state in INVALID_STATES:
                return
            if old is not None and old.state not in INVALID_STATES:
                return  # not an unavailable→real transition
            entity_id = new.entity_id
            if entity_id not in self._pending:
                return
            if (dt_util.utcnow() - self._startup).total_seconds() > grace:
                self._pending.discard(entity_id)
                self._cleanup_if_done()
                return
            self._pending_recovery_burst.add(entity_id)
            now = dt_util.utcnow()
            if self._pending_recovery_burst_since is None:
                self._pending_recovery_burst_since = now
            self._cancel_pending_recovery_flush_timer()
            elapsed = (now - self._pending_recovery_burst_since).total_seconds()
            if elapsed >= PENDING_RECOVERY_MAX_WAIT_SECONDS:
                self._flush_pending_recovery_burst()
            else:
                self._pending_recovery_flush_timer = async_call_later(
                    self.hass,
                    PENDING_RECOVERY_DEBOUNCE_SECONDS,
                    self._flush_pending_recovery_burst,
                )

        self._unsub_listener = async_track_state_change_event(
            self.hass, list(self._pending), _on_change
        )

    @callback
    def _flush_pending_recovery_burst(self, _now: datetime | None = None) -> None:
        self._cancel_pending_recovery_flush_timer()
        self._pending_recovery_burst_since = None
        if not self._pending_recovery_burst:
            return
        burst, self._pending_recovery_burst = self._pending_recovery_burst, set()
        self.hass.async_create_task(self._drain_pending_recovery_burst(burst))

    async def _drain_pending_recovery_burst(self, entity_ids: set[str]) -> None:
        """Batched replacement for one recorder query per boot-pending
        entity recovering from unavailable: resolves the whole debounced
        burst together via the same streaming bulk-query machinery the
        boot pass uses (_iter_bulk_batches), instead of each firing its own
        targeted per-entity query (_patch_pending, which — like
        _attempt_reregister_patch — only ever sees the raw, undeduplicated
        deep query since it always passes bulk_states=None).

        Entities that don't resolve here are simply left in _pending:
        unlike the re-registration burst there is no separate per-entity
        retry ladder to arm here — the existing scheduled retry passes
        (_schedule_retries / _patch_all_pending) already re-sweep the whole
        remaining pending set.
        """
        candidates: list[str] = []
        candidate_last_changed: dict[str, datetime] = {}
        for entity_id in entity_ids:
            if entity_id not in self._pending:
                continue  # already resolved by a scheduled retry pass meanwhile
            live = self.hass.states.get(entity_id)
            if live is None or live.state in INVALID_STATES:
                continue
            candidates.append(entity_id)
            candidate_last_changed[entity_id] = live.last_changed

        async for chunk, bulk in self._iter_bulk_batches(candidates):
            for entity_id in chunk:
                # Re-validate against the snapshot taken above: the bulk
                # query just awaited the recorder executor, during which the
                # entity may have gone unavailable again, genuinely changed,
                # or already been resolved by a scheduled retry pass — see
                # the matching comment in _async_run_impl.
                if entity_id not in self._pending:
                    continue
                live = self.hass.states.get(entity_id)
                if live is None or live.state in INVALID_STATES:
                    continue
                if live.last_changed != candidate_last_changed[entity_id]:
                    continue
                await self._maybe_restore_last_triggered(entity_id)
                ts = await self._resolve(entity_id, live, bulk.get(entity_id))
                if ts is not None:
                    self._apply(live, ts, entity_id)
                    self._pending.discard(entity_id)
                    self._cleanup_if_done()
            del bulk  # released before the next batch is fetched

    @callback
    def _schedule_retries(self, grace: float) -> None:
        """Delayed passes for boot sequences (unavail→off→on)."""

        def _make(delay: int) -> CALLBACK_TYPE:
            @callback
            def _fire(_now) -> None:
                self.hass.async_create_task(self._retry_pass(delay, grace))

            return async_call_later(self.hass, delay, _fire)

        self._unsub_timers = [_make(d) for d in self._retry_delays]

    async def _retry_pass(self, delay: int, grace: float) -> None:
        if not self._pending:
            return
        run_patched = await self._patch_all_pending(grace)
        _LOGGER.info(
            "Last Changed Keeper: re-run +%ds — %d patched, %d pending",
            delay, run_patched, len(self._pending),
        )

    async def _immediate_pending_pass(self) -> int:
        """Service-triggered pass over the currently pending set, in place."""
        _, _, _, grace, _, _ = self._config
        run_patched = await self._patch_all_pending(grace)
        _LOGGER.info(
            "Last Changed Keeper: manual pass — %d patched, %d pending",
            run_patched, len(self._pending),
        )
        return run_patched

    async def _patch_all_pending(self, grace: float) -> int:
        """Sweep every currently-pending entity once.

        Snapshots _pending up front since entries are removed while this
        loop runs — but the persistent listener's batched drain
        (_drain_pending_recovery_burst) can also resolve and discard
        entities concurrently (this coroutine yields control at every
        await, including inside _patch_pending itself). Re-checking
        membership right before each call — not just once at snapshot time
        — skips entities a concurrent drain already resolved instead of
        issuing a redundant recorder query for them.
        """
        run_patched = 0
        for entity_id in list(self._pending):
            if entity_id not in self._pending:
                continue
            if await self._patch_pending(entity_id, grace):
                run_patched += 1
        self.stats["patched_total"] = (
            int(self.stats.get("patched_total", 0)) + run_patched
        )
        self.stats["pending"] = len(self._pending)
        return run_patched

    async def _patch_pending(self, entity_id: str, grace: float) -> bool:
        live = self.hass.states.get(entity_id)
        if live is None or live.state in INVALID_STATES:
            return False
        if (dt_util.utcnow() - live.last_changed).total_seconds() > grace:
            return False
        # Independent side effect: does not affect the pending/return-value
        # contract below, which is about last_changed resolution only.
        await self._maybe_restore_last_triggered(entity_id)
        ts = await self._resolve(entity_id, live, None)
        if ts is None:
            return False
        self._apply(live, ts, entity_id)
        self._pending.discard(entity_id)
        self._cleanup_if_done()
        return True

    @callback
    def _cleanup_if_done(self) -> None:
        if not self._pending:
            self._fire_restored_event(final=True)
            self._stop_boot_machinery()

    @callback
    def _fire_restored_event(self, *, final: bool) -> None:
        """Fire EVENT_RESTORED so automations can wait for the pass to
        settle instead of racing it (e.g. an automation computing "unused
        for N days" right after boot, before this integration has patched
        anything yet)."""
        if final:
            if self._final_fired:
                return
            self._final_fired = True
            # Fold the late-pass results (retries/listener) into this run's
            # history record, replacing the pass-0 snapshot of the stats.
            if self._run_active:
                self._persist_run_stats(new_run=False)
        self.hass.bus.async_fire(
            EVENT_RESTORED,
            {
                "entry_id": self.entry.entry_id,
                "patched_total": int(self.stats.get("patched_total", 0)),
                "pending": len(self._pending),
                "final": final,
            },
        )

    # ----- Run history + post-boot self-check ------------------------------

    @callback
    def _persist_run_stats(self, *, new_run: bool) -> None:
        """Record the current stats in the rolling run history (last
        MAX_RUN_HISTORY boot passes). new_run appends; otherwise the latest
        record is replaced in place (final event, verify result). Skips
        disk persistence when no runs store was injected (unit tests)."""
        record = dict(self.stats)
        if new_run or not self.run_history:
            self.run_history.append(record)
        else:
            self.run_history[-1] = record
        del self.run_history[:-MAX_RUN_HISTORY]
        if self._runs_store is not None:
            self._runs_store.async_delay_save(lambda: list(self.run_history), 10)

    @callback
    def _schedule_boot_verify(self) -> None:
        """Optional self-check: run a verify pass a few minutes after the
        boot pass (late enough that the +30/+90/+180s retries are done) and
        log any mismatches — catches "the restore silently did nothing"
        without waiting for a user to notice wrong timestamps."""
        if self._unsub_verify_timer is not None:
            self._unsub_verify_timer()

        @callback
        def _fire(_now) -> None:
            self._unsub_verify_timer = None
            self.hass.async_create_task(self._boot_verify())

        self._unsub_verify_timer = async_call_later(
            self.hass, VERIFY_AFTER_BOOT_DELAY, _fire
        )

    async def _boot_verify(self) -> None:
        try:
            result = await self.async_verify()
        except Exception:
            _LOGGER.exception("Post-boot verify pass failed")
            return
        mismatches = result.get("mismatches") or []
        self.stats["verify_mismatches"] = len(mismatches)
        # Prefixed distinctly from the boot pass's own bulk_rows_fetched/
        # bulk_batches/deep_queries above (this is a second, separate
        # recorder pass over every target, not just the boot candidates —
        # see async_verify's docstring).
        self.stats["verify_bulk_rows_fetched"] = result.get("bulk_rows_fetched", 0)
        self.stats["verify_bulk_batches"] = result.get("bulk_batches", 0)
        self.stats["verify_deep_queries"] = result.get("deep_queries", 0)
        if mismatches:
            _LOGGER.warning(
                "Post-boot verify: %d of %d entities deviate from the "
                "recorder/store-derived value (first: %s)",
                len(mismatches),
                result.get("checked", 0),
                ", ".join(m["entity_id"] for m in mismatches[:10]),
            )
        else:
            _LOGGER.info(
                "Post-boot verify: all %d entities consistent",
                result.get("checked", 0),
            )
        if self._run_active:
            self._persist_run_stats(new_run=False)

    # ----- Resolving the real timestamp ----------------------------------

    async def _resolve(
        self,
        entity_id: str,
        live: State,
        bulk_states: list | None,
        counters: dict[str, int] | None = None,
    ) -> datetime | None:
        """Determine the real last_changed: bulk → snapshot → deep → best-effort.

        counters, when given, is a mutable dict the caller uses to collect
        pass-wide instrumentation — currently just "deep_queries", the count
        of entities that fell through to step 3 below. See _async_run_impl
        and async_verify, the two callers that report it in stats.

        A bounded result (steps 1/3) is trusted unconditionally, even when
        too recent to clear the margin — a genuine differing value proves
        the value really did just change, and no older/staler source may
        override it. A removal (None-state) row can never produce a
        bounded result in the first place — see _real_last_changed's
        docstring — so there is nothing further to guard against here.
        """
        cutoff = live.last_changed

        def _ok(ts: datetime | None) -> bool:
            return ts is not None and (cutoff - ts).total_seconds() > MARGIN_SECONDS

        # 1. Bulk result (only if unambiguously bounded). Keep an unbounded
        # result around as a cheap best-effort fallback for step 4.
        bulk_ts: datetime | None = None
        if bulk_states is not None:
            bulk_ts, bounded = _real_last_changed(bulk_states, live.state)
            if bounded:
                # A bounded run start is definitive: the recorder proves the
                # value genuinely changed at run_start. If that's not old
                # enough to clear the margin, the value just changed for
                # real — no other (older/staler) source may override it.
                if not _ok(bulk_ts):
                    return None
                # The incrementally-updated runtime store (see
                # async_write_snapshot / _on_target_state_changed) can hold a
                # timestamp newer than what the bulk query sees — e.g.
                # recorder commit lag, or a change from between two periodic
                # snapshots. Prefer it over the bulk answer when it is both
                # usable and more recent for the same value.
                newer_snap = self._newer_snapshot_ts(entity_id, live.state, bulk_ts)
                if newer_snap is not None and _ok(newer_snap):
                    return newer_snap
                return bulk_ts

        # 2. Snapshot (free, in memory, authoritative) — before the costly
        # query. Only usable if the entity still holds the SAME value as at
        # the last clean shutdown; otherwise the stored timestamp belongs to
        # a different (often the opposite) value and would be a wrong patch.
        snap = self._snapshot.get(entity_id)
        if isinstance(snap, dict) and snap.get("s") == live.state:
            snap_dt = dt_util.parse_datetime(snap.get("t", ""))
            if _ok(snap_dt):
                return snap_dt

        # 3. Deep per-entity query
        if counters is not None:
            counters["deep_queries"] = counters.get("deep_queries", 0) + 1
        deep_states: list = []
        if await self._wait_for_recorder_commit():
            try:
                deep = await get_instance(self.hass).async_add_executor_job(
                    get_last_state_changes, self.hass, HISTORY_DEPTH, entity_id
                )
                deep_states = deep.get(entity_id, [])
            except Exception as err:  # noqa: BLE001 - recorder must not kill anything
                _LOGGER.debug("Recorder query for %s failed: %s", entity_id, err)
                deep_states = []
        ts2, bounded2 = _real_last_changed(deep_states, live.state)
        if bounded2:
            return ts2 if _ok(ts2) else None

        # 4. Best effort from an unbounded run (deep query first, bulk as a
        # cheap fallback). If the deep query's row window was exhausted by
        # HISTORY_DEPTH rather than by reaching an older value (frequent on
        # attribute-noisy domains like climate/humidifier), run_start is only
        # "oldest of the last N rows", not the true start — too unreliable to
        # use, so it is discarded rather than applied. The bulk result gets
        # the same treatment against its own cap (BULK_PER_ENTITY_LIMIT):
        # a full-length result means the window function truncated the
        # history, not that the run's true start was reached. Likewise, if
        # the run genuinely exhausted history right at (or just past) the
        # recorder's purge boundary, that's indistinguishable from a
        # same-value run whose earlier restarts simply aged out of the
        # database — see _near_purge_boundary — so it is discarded too
        # rather than guessed at as if it were a confirmed origin.
        if (
            _ok(ts2)
            and len(deep_states) < HISTORY_DEPTH
            and not self._near_purge_boundary(ts2)
        ):
            return ts2
        if (
            _ok(bulk_ts)
            and bulk_states is not None
            and len(bulk_states) < BULK_PER_ENTITY_LIMIT
            and not self._near_purge_boundary(bulk_ts)
        ):
            return bulk_ts
        return None

    def _near_purge_boundary(self, ts: datetime) -> bool:
        """True if ts is close enough to the recorder's purge boundary that
        it can't be trusted as a genuine origin (see _resolve step 4).

        Once a same-value run's earlier restarts age out of the recorder
        under purge_keep_days, "history exhausted" no longer means "reached
        the real first occurrence" — it can just as easily mean "the older,
        equally-artificial rows that used to bound this run aren't there
        anymore". The two are indistinguishable from inside a single
        entity's history, so any unbounded result landing at or before the
        boundary (plus PURGE_BOUNDARY_MARGIN_DAYS, to absorb purge running
        only periodically rather than continuously) is treated as unproven
        rather than applied. If auto-purge is off there is no enforced
        retention boundary to compare against, so the check is skipped.
        """
        try:
            instance = get_instance(self.hass)
        except Exception:  # noqa: BLE001 - recorder must not kill anything
            return False
        if not instance.auto_purge:
            return False
        boundary = dt_util.utcnow() - timedelta(
            days=instance.keep_days + PURGE_BOUNDARY_MARGIN_DAYS
        )
        return ts <= boundary

    def _newer_snapshot_ts(
        self, entity_id: str, state: str, than: datetime
    ) -> datetime | None:
        """Store timestamp for entity_id/state if present and newer than
        `than`, else None. Used to let a fresher incremental-store value win
        over an otherwise-definitive bulk result (see _resolve step 1)."""
        snap = self._snapshot.get(entity_id)
        if not isinstance(snap, dict) or snap.get("s") != state:
            return None
        snap_dt = dt_util.parse_datetime(snap.get("t", ""))
        if snap_dt is not None and snap_dt > than:
            return snap_dt
        return None

    async def _iter_bulk_batches(
        self, entity_ids: list[str]
    ) -> AsyncIterator[tuple[list[str], dict[str, list]]]:
        """Yield recorder history for the bulk window one batch at a time.

        Split into batches of the configured bulk batch size (see
        _bulk_batch_size) because with "track all entities" the candidate
        list can be thousands of entities, and a single IN(...) query that
        long gets slow and memory-hungry.

        Yielding per batch rather than returning one merged dict bounds peak
        memory at one batch instead of the entire installation. Within a
        batch, _bulk_query additionally caps rows per entity at
        BULK_PER_ENTITY_LIMIT (see its docstring), so a batch is at most
        batch_size x BULK_PER_ENTITY_LIMIT small rows — without that cap, a
        single chatty entity's 30-day history could balloon one batch to
        hundreds of MB regardless of batch size.

        Both this generator and the caller must drop their reference to a
        batch before the next query runs, or the peak is two batches rather
        than one; hence the explicit release after the yield below and the
        matching `del bulk` in each caller's loop.

        A failed batch yields an empty result but still yields its chunk, so
        those entities fall back to the snapshot/per-entity path in
        `_resolve` exactly as before instead of being skipped.

        The trailing `asyncio.sleep(0)` after each batch paces the stream:
        on a large "track all entities" install this loop can run for many
        batches back to back, each one a synchronous query on the
        recorder's own single-worker executor — without a yield point here,
        our own coroutine is immediately ready again the instant one batch's
        executor job resolves, which can keep crowding out other ready
        callbacks on the main event loop for the whole pass. `sleep(0)` is a
        pure yield with no added delay (unlike `sleep(n>0)`), so pacing this
        way costs nothing in wall-clock time — it only gives other
        already-ready work a fair turn between batches, shared by every
        caller of this generator (the boot pass, verify, and the
        re-registration/pending-recovery burst drains alike).
        """
        start = dt_util.utcnow() - timedelta(days=BULK_WINDOW_DAYS)
        batch_size = self._bulk_batch_size
        for i in range(0, len(entity_ids), batch_size):
            chunk = entity_ids[i : i + batch_size]
            result: dict = {}
            try:
                if await self._wait_for_recorder_commit():
                    result = await get_instance(self.hass).async_add_executor_job(
                        _bulk_query, self.hass, start, chunk
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Bulk recorder query failed (batch %d, %d entities): %s",
                    i // batch_size, len(chunk), err,
                )
                result = {}
            yield chunk, result
            result = {}
            await asyncio.sleep(0)

    # ----- Confirmed/unconfirmed bookkeeping ------------------------------

    @callback
    def _mark_unconfirmed(self, entity_id: str) -> None:
        """entity_id's live last_changed is a suspect/unverified value:
        mark it retry-worthy and revoke any earlier positive verification,
        so async_write_snapshot won't trust it again until it's
        re-verified (see self._confirmed's declaration comment)."""
        self._unconfirmed.add(entity_id)
        self._confirmed.discard(entity_id)

    @callback
    def _mark_confirmed(self, entity_id: str) -> None:
        """entity_id's live last_changed is positively verified this
        session (resolved+applied, or a genuine value transition was
        observed) — the only thing async_write_snapshot trusts."""
        self._unconfirmed.discard(entity_id)
        self._confirmed.add(entity_id)

    # ----- Applying ------------------------------------------------------

    def _apply(self, live: State, ts: datetime, entity_id: str) -> None:
        self._mark_confirmed(entity_id)
        ok = _apply_last_changed(live, ts, self._also_updated)
        if not ok and not self._degraded:
            self._degraded = True
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_INCOMPATIBLE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_INCOMPATIBLE,
            )
        _LOGGER.debug("%s: last_changed -> %s", entity_id, ts.isoformat())

    # ----- last_triggered (automation/script) -----------------------------

    async def _maybe_restore_last_triggered(self, entity_id: str) -> bool:
        """Separate patch path for automation.*/script.* `last_triggered`.

        `last_triggered` is an attribute, not the state value, so it needs
        its own recorder read (_resolve_last_triggered) and its own apply
        mechanism (_apply_last_triggered) instead of the last_changed/
        state-value logic above. automation/script entities normally restore
        this themselves via their own RestoreEntity hook; this is a
        best-effort correction for when that didn't happen (crash, purged
        restore-state cache, long outage, ...). A no-op whenever the entity
        already has a value.
        """
        if not self._also_restore_triggered:
            return False
        if entity_id.split(".", 1)[0] not in LAST_TRIGGERED_DOMAINS:
            return False
        live = self.hass.states.get(entity_id)
        if live is None or live.state in INVALID_STATES:
            return False
        if live.attributes.get(ATTR_LAST_TRIGGERED) is not None:
            return False  # already set (own restore worked, or genuinely unset)
        ts = await self._resolve_last_triggered(entity_id)
        if ts is None:
            return False
        ok = _apply_last_triggered(live, ts)
        if not ok and not self._degraded:
            self._degraded = True
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_INCOMPATIBLE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_INCOMPATIBLE,
            )
        _LOGGER.debug("%s: last_triggered -> %s", entity_id, ts.isoformat())
        return ok

    async def _resolve_last_triggered(self, entity_id: str) -> datetime | None:
        """Newest last_triggered attribute value recorded for entity_id."""
        if not await self._wait_for_recorder_commit():
            return None
        try:
            history = await get_instance(self.hass).async_add_executor_job(
                get_last_state_changes, self.hass, HISTORY_DEPTH, entity_id
            )
        except Exception as err:  # noqa: BLE001 - recorder must not kill anything
            _LOGGER.debug("last_triggered query for %s failed: %s", entity_id, err)
            return None
        states = sorted(
            history.get(entity_id, []), key=lambda s: s.last_updated, reverse=True
        )
        for s in states:
            raw = s.attributes.get(ATTR_LAST_TRIGGERED)
            if not raw:
                continue
            ts = raw if isinstance(raw, datetime) else dt_util.parse_datetime(str(raw))
            if ts is not None:
                return ts
        return None

    # ----- Re-patch on runtime re-registration -----------------------------

    @callback
    def _setup_reregister_listener(self) -> None:
        """Persistent listener (entry lifetime, not just the boot pass) for
        an already-watched entity's last_changed getting reset to "now" by
        something other than a full HA restart: either the entity being
        fully re-created (old_state is None) — e.g. its owning config entry
        reloads, or a Zigbee/Z-Wave device rejoins — or the entity recovering
        from unavailable/unknown to a real value (a brief network/mesh
        blip). HA's own state machine treats unavailable/unknown as a
        distinct value, so recovering from it genuinely bumps last_changed
        the same way a restart does, but unlike a restart nothing else in
        this integration ever revisits that entity afterwards - the boot
        pass only runs once at startup, and this was previously the only
        listener, gated to old_state is None only. Left uncorrected, an
        entity that flaps unavailable periodically during normal runtime
        (common for Zigbee/Z-Wave/network devices) drifts wrong forever,
        self-healing only at the next full restart. Registered after boot
        has already assigned every entity its initial state, so it never
        fires for that initial assignment.

        Subscribes against self._known_targets rather than taking a
        parameter, since _resubscribe_persistent_listeners needs to recreate
        this subscription against an enlarged set as
        _async_scan_for_new_targets discovers entities that didn't exist
        at boot — see that method's docstring."""
        self._unsub_reregister_listener = async_track_state_change_event(
            self.hass, list(self._known_targets), self._on_entity_reregistered
        )

    @callback
    def _on_entity_reregistered(self, event: Event) -> None:
        """Queue a fresh re-registration or unavailable-recovery for the
        batched drain below instead of firing an immediate per-entity task:
        a mass event (a hub's config entry reloading with hundreds of
        entities, a Zigbee/Z-Wave coordinator coming back after an outage)
        fires one such event per entity, and reacting to each with its own
        recorder query is a thundering herd on the recorder executor. See
        _drain_reregister_burst for the batched query itself.
        """
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        if new is None or new.state in INVALID_STATES:
            return
        is_reregistration = old is None
        is_unavailable_recovery = old is not None and old.state in INVALID_STATES
        if not (is_reregistration or is_unavailable_recovery):
            return
        entity_id = new.entity_id
        if entity_id in self._pending:
            return  # already handled by the active boot-time pass
        _, _, _, grace, _, _ = self._config
        if (dt_util.utcnow() - new.last_changed).total_seconds() > grace:
            return
        # Drop any retry ladder still armed from a previous flap of this
        # entity right away, rather than waiting for the drain — a stale
        # timer must not fire mid-debounce-window against an entity that's
        # about to be re-patched anyway.
        self._cancel_reregister_retry(entity_id)
        # Mark unconfirmed immediately, not just on a failed drain attempt:
        # live.last_changed is the raw reset artifact for as long as this
        # entity sits in the debounce window, and a snapshot write racing in
        # during that window (e.g. HA shutting down mid-debounce) must not
        # persist it as if it were real. _apply() clears this on success.
        self._mark_unconfirmed(entity_id)
        self._reregister_burst.add(entity_id)
        now = dt_util.utcnow()
        if self._reregister_burst_since is None:
            self._reregister_burst_since = now
        self._cancel_reregister_flush_timer()
        elapsed = (now - self._reregister_burst_since).total_seconds()
        if elapsed >= REREGISTER_MAX_WAIT_SECONDS:
            self._flush_reregister_burst()
        else:
            self._reregister_flush_timer = async_call_later(
                self.hass, REREGISTER_DEBOUNCE_SECONDS, self._flush_reregister_burst
            )

    @callback
    def _flush_reregister_burst(self, _now: datetime | None = None) -> None:
        self._cancel_reregister_flush_timer()
        self._reregister_burst_since = None
        if not self._reregister_burst:
            return
        burst, self._reregister_burst = self._reregister_burst, set()
        self.hass.async_create_task(self._drain_reregister_burst(burst))

    async def _resolve_candidates(
        self,
        candidates: list[str],
        candidate_last_changed: dict[str, datetime],
        *,
        on_unresolved: Callable[[str], None] | None = None,
    ) -> None:
        """Shared streaming resolve/apply loop for a pre-filtered candidate
        list: resolves the whole list together through the same streaming
        bulk-query machinery the boot pass uses (_iter_bulk_batches), so a
        batch of entities costs a handful of queries instead of one targeted
        query per entity.

        Used by both _drain_reregister_burst (candidates grace-filtered: a
        just-registered/discovered entity's artifact is always fresh) and
        _drain_unconfirmed_burst (candidates NOT grace-filtered: membership
        in self._unconfirmed is itself the trust signal that an entity's
        live last_changed is still fake, regardless of how old that artifact
        now looks by wall-clock time — including entities still sitting in
        self._pending, which this sweep is no longer excluded from: the
        boot-time retry ladder (RETRY_DELAYS, max 180s) always finishes
        before this sweep's own interval, NEW_TARGET_SCAN_INTERVAL_SECONDS
        (300s), can even tick once, so there's no realistic overlap with the
        boot-pending machinery). on_unresolved, when given, lets the caller
        arm its own retry mechanism for anything still unresolved (e.g. the
        reregistration retry ladder) — the unconfirmed sweep has no
        equivalent, since an unresolved entity simply stays in
        self._unconfirmed for the next periodic sweep to retry. A resolved
        entity still sitting in self._pending (i.e. one whose boot-time
        retry ladder gave up before its resolve finally succeeded here) is
        also cleared out of that bookkeeping, since nothing else will.
        """
        async for chunk, bulk in self._iter_bulk_batches(candidates):
            for entity_id in chunk:
                # Re-validate against the snapshot taken above: the bulk
                # query just awaited the recorder executor, during which the
                # entity may have gone unavailable or genuinely changed for
                # real — see the matching comment in _async_run_impl.
                live = self.hass.states.get(entity_id)
                if live is None or live.state in INVALID_STATES:
                    continue
                if live.last_changed != candidate_last_changed[entity_id]:
                    self._mark_confirmed(entity_id)
                    continue
                await self._maybe_restore_last_triggered(entity_id)
                ts = await self._resolve(entity_id, live, bulk.get(entity_id))
                if ts is not None:
                    self._apply(live, ts, entity_id)
                    _LOGGER.debug("%s: re-patched by batched drain", entity_id)
                    if entity_id in self._pending:
                        self._pending.discard(entity_id)
                        self._cleanup_if_done()
                else:
                    # Unresolved: live.last_changed is still a raw reset
                    # artifact — see _unconfirmed.
                    self._mark_unconfirmed(entity_id)
                    if on_unresolved is not None:
                        on_unresolved(entity_id)
            del bulk  # released before the next batch is fetched

    async def _drain_reregister_burst(self, entity_ids: set[str]) -> None:
        """Batched replacement for one recorder query per re-registered
        entity — see _resolve_candidates for the shared streaming loop.

        Candidates are grace-filtered here (unlike _drain_unconfirmed_burst)
        because a re-registration or freshly-discovered entity's artifact
        genuinely is recent; entities that don't resolve get the same retry
        ladder as before (_schedule_reregister_retries).
        """
        _, _, _, grace, _, _ = self._config
        candidates: list[str] = []
        candidate_last_changed: dict[str, datetime] = {}
        for entity_id in entity_ids:
            live = self.hass.states.get(entity_id)
            if live is None or live.state in INVALID_STATES:
                continue
            if (dt_util.utcnow() - live.last_changed).total_seconds() > grace:
                continue
            candidates.append(entity_id)
            candidate_last_changed[entity_id] = live.last_changed

        await self._resolve_candidates(
            candidates,
            candidate_last_changed,
            on_unresolved=lambda eid: self._schedule_reregister_retries(eid, grace),
        )

    async def _drain_unconfirmed_burst(self, entity_ids: set[str]) -> None:
        """Batched re-resolve attempt for entities already known to hold a
        fake last_changed (self._unconfirmed) — see
        _async_scan_for_new_targets. Deliberately not grace-filtered: an
        entity's resolve attempt can fail on a fresh boot (e.g. a recorder
        cold-start race on a large install) even though the true answer
        becomes available minutes later, and without this sweep nothing
        would ever retry it again — its live last_changed stops looking
        "fresh" the moment wall-clock time drifts past grace, even though
        it's still exactly as fake as it was at boot.

        entity_ids may include entities still in self._pending (unlike
        earlier versions, this is no longer excluded) — an entity whose
        boot-time retry ladder exhausted before it recovered to a valid
        state, or whose one recovery-triggered resolve attempt simply
        failed, would otherwise sit in self._pending forever with nothing
        left to retry it. _resolve_candidates clears the matching _pending
        entry on success.

        No fast retry ladder is armed for anything still unresolved here:
        the next periodic sweep will simply pick it up again, since it
        stays in self._unconfirmed until it's genuinely fixed.
        """
        candidates: list[str] = []
        candidate_last_changed: dict[str, datetime] = {}
        for entity_id in entity_ids:
            live = self.hass.states.get(entity_id)
            if live is None or live.state in INVALID_STATES:
                continue
            candidates.append(entity_id)
            candidate_last_changed[entity_id] = live.last_changed

        await self._resolve_candidates(candidates, candidate_last_changed)

    async def _attempt_reregister_patch(
        self, entity_id: str, grace: float
    ) -> bool | None:
        """One targeted, per-entity re-patch attempt (no bulk query — this
        is for a single entity, not a full burst drain). Also attempts the
        last_triggered path. Respects the same grace window as the boot pass.

        Deliberately free of scheduling side effects: it must never arm
        retries itself. It runs as every retry-ladder attempt (see
        _retry_reregister), so scheduling here would let each failing retry
        arm a fresh ladder — the number of attempts would then grow by a
        factor of len(retry_delays) per round instead of being capped at one
        ladder per re-registration. (The initial attempt, by contrast, goes
        through the batched _drain_reregister_burst above, which arms the
        ladder itself exactly once per unresolved entity.)

        Returns True when patched, False (_RETRY_WORTHWHILE) when the entity
        is a valid candidate that simply could not be resolved right now, and
        None when it is not a candidate at all — gone, unavailable, or
        already outside the grace window. Only the middle case justifies a
        retry ladder, which is the distinction the caller acts on.
        """
        live = self.hass.states.get(entity_id)
        if live is None or live.state in INVALID_STATES:
            return None
        if (dt_util.utcnow() - live.last_changed).total_seconds() > grace:
            return None
        await self._maybe_restore_last_triggered(entity_id)
        ts = await self._resolve(entity_id, live, None)
        if ts is None:
            # Unresolved: live.last_changed is still this re-registration's
            # raw reset value — see _unconfirmed.
            self._mark_unconfirmed(entity_id)
            return _RETRY_WORTHWHILE
        self._apply(live, ts, entity_id)
        _LOGGER.debug("%s: re-patched after runtime re-registration", entity_id)
        return True

    @callback
    def _cancel_reregister_retry(self, entity_id: str) -> None:
        for cancel in self._reregister_retry_timers.pop(entity_id, []):
            cancel()

    @callback
    def _schedule_reregister_retries(self, entity_id: str, grace: float) -> None:
        """Arm the fixed retry ladder (one timer per retry delay) for one
        re-registration of one entity.

        Cancels any ladder still armed for this entity first: replacing the
        dict entry without cancelling would leave the previous timers armed
        but untracked, so they could neither be cancelled on unload nor
        counted — the mechanism behind the unbounded retry growth this
        guards against.
        """
        self._cancel_reregister_retry(entity_id)
        delays = self._retry_delays
        remaining = len(delays)

        def _make(delay: int) -> CALLBACK_TYPE:
            @callback
            def _fire(_now) -> None:
                nonlocal remaining
                remaining -= 1
                if remaining <= 0:
                    # Ladder exhausted: drop the (now fully fired) entry so
                    # the dict tracks live timers only, instead of keeping
                    # one stale entry per entity that ever failed a re-patch.
                    self._reregister_retry_timers.pop(entity_id, None)
                self.hass.async_create_task(self._retry_reregister(entity_id, grace))

            return async_call_later(self.hass, delay, _fire)

        self._reregister_retry_timers[entity_id] = [_make(d) for d in delays]

    async def _retry_reregister(self, entity_id: str, grace: float) -> None:
        """One scheduled retry attempt from the ladder above.

        Cancels the rest of the ladder as soon as an attempt succeeds: a
        resolved entity then stops issuing recorder queries instead of
        running its remaining timers out against a state that is already
        patched (and therefore outside the grace window anyway).
        """
        if await self._attempt_reregister_patch(entity_id, grace) is True:
            self._cancel_reregister_retry(entity_id)

    # ----- Periodic target-discovery sweep ----------------------------------

    async def _async_scan_for_new_targets(self, _now: datetime | None = None) -> None:
        """Periodic timer callback (see NEW_TARGET_SCAN_INTERVAL_SECONDS)
        that sweeps two distinct populations sharing one timer.

        1. Newly-existing targets. _async_run_impl resolves targets exactly
        once, at boot, from whatever entities already have a live state at
        that instant. An entity whose owning integration is still mid-setup
        at that moment (a coordinator doing its first data fetch before
        creating entities — common for cloud/hub/mesh integrations
        enumerating many child devices) is invisible to that one-time
        snapshot, and therefore invisible to every patch mechanism this
        integration has — the boot pass, the re-registration listener, and
        the incremental listener all only know about entity_ids that
        existed at that instant. Without this sweep such an entity stays
        wrong for the rest of the session, only getting a chance again at
        the next full restart, where the same race can recur. Newly found
        entities are folded into self._known_targets and driven through
        _drain_reregister_burst — the exact same batched resolve/patch path
        a burst of runtime re-registrations already uses, since "just
        discovered" and "just re-registered" need identical handling (both
        start from a live last_changed that is a raw reset/creation
        artifact, not a real value).

        2. Still-unconfirmed existing targets, INCLUDING ones still in
        self._pending. A candidate that already existed at boot (or was
        already swept in) but whose resolve attempt simply failed — e.g. a
        recorder cold-start race on a large install, where the true answer
        only becomes queryable a few minutes after boot — is otherwise
        abandoned forever: it never got a retry ladder in the first place
        (or, for a boot-pending entity, its one retry ladder/recovery
        listener attempt already exhausted), and nothing else ever revisits
        it unless its value genuinely changes or it's fully re-registered,
        neither of which happens for the largely-static diagnostic/config
        entities this hits hardest. self._unconfirmed is the durable,
        always-correct signal for exactly this: set on every resolve
        failure, reliably cleared the moment an entity is genuinely patched
        or genuinely changes value (_apply, the incremental listener). This
        half of the sweep re-attempts everything still in self._unconfirmed
        via _drain_unconfirmed_burst — no longer excluding self._pending
        members: NEW_TARGET_SCAN_INTERVAL_SECONDS (300s) is longer than the
        boot-time retry ladder's maximum delay (RETRY_DELAYS tops out at
        180s), so this sweep's first tick can never fire while that ladder
        is still active, and _drain_unconfirmed_burst's own per-entity
        live-state check already skips anything genuinely still
        unavailable at zero recorder cost. A resolve that succeeds here for
        a still-_pending entity also clears its _pending bookkeeping (see
        _resolve_candidates) — otherwise that entity's boot-pending job
        could never reach its "final" finalization.

        async_verify doesn't have either gap since it re-resolves every
        current target fresh, with no grace/pending gating, on every call —
        this sweep gives the live listeners that same freshness, just on a
        timer instead of on demand.
        """
        domains, entities, exclude, _, labels, areas = self._config
        current = self._targets(domains, entities, exclude, labels, areas)
        new_targets = current - self._known_targets
        if new_targets:
            self._known_targets |= new_targets
            self._resubscribe_persistent_listeners()
            # Mark unconfirmed immediately, same reasoning as
            # _on_entity_reregistered: a snapshot write racing in before the
            # drain below completes must not persist these entities'
            # current (first-creation-time) last_changed as if it were
            # real. Bulk equivalent of _mark_unconfirmed: new_targets can't
            # already be in self._confirmed (they weren't even in
            # self._known_targets a moment ago), so there's nothing to
            # revoke, just the matching bulk discard for consistency.
            self._unconfirmed |= new_targets
            self._confirmed -= new_targets
            self.hass.async_create_task(self._drain_reregister_burst(new_targets))

        stale = self._unconfirmed - new_targets
        if stale:
            self.hass.async_create_task(self._drain_unconfirmed_burst(stale))

    @callback
    def _resubscribe_persistent_listeners(self) -> None:
        """async_track_state_change_event's subscription is a fixed
        entity_id list with no API to add ids to an existing one — covering
        a newly discovered entity means cancelling and re-registering both
        persistent listeners against the enlarged self._known_targets."""
        self._stop_reregister_listener()
        self._stop_incremental_listener()
        self._setup_reregister_listener()
        self._setup_incremental_listener()

    # ----- Incremental runtime store ---------------------------------------

    @callback
    def _setup_incremental_listener(self) -> None:
        """Persistent listener (entry lifetime) that debounce-merges every
        genuine value change of a watched entity into the same store used
        for the periodic/shutdown snapshot — see _on_target_state_changed.

        Subscribes against self._known_targets rather than taking a
        parameter — see _setup_reregister_listener's docstring for why."""
        self._unsub_incremental_listener = async_track_state_change_event(
            self.hass, list(self._known_targets), self._on_target_state_changed
        )

    @callback
    def _on_target_state_changed(self, event: Event) -> None:
        """Debounced incremental merge into the snapshot store: keeps the
        stored last_changed close to real-time instead of only updating it
        every snapshot_interval / on shutdown — without re-deriving the
        whole store on every single change (see async_write_snapshot).

        Restricted to genuine value transitions (old_state present and
        actually different) — re-registrations (old_state is None) and
        recoveries from unavailable/unknown (old_state.state in
        INVALID_STATES) are both handled by the re-registration listener
        above instead, since HA resets last_changed to "now" for those too
        without the value having genuinely changed; attribute-only "chatter"
        (same state value, e.g. climate current_temperature) is ignored so
        one noisy entity can't perpetually starve the debounce for others.
        """
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        if old is None or new is None:
            return
        if new.state in INVALID_STATES or old.state == new.state:
            return
        if old.state in INVALID_STATES:
            return  # recovery from unavailable/unknown, not a genuine change
        self._mark_confirmed(new.entity_id)
        self._dirty[new.entity_id] = {
            "s": new.state,
            "t": new.last_changed.isoformat(),
        }
        now = dt_util.utcnow()
        if self._dirty_since is None:
            self._dirty_since = now
        self._cancel_flush_timer()
        if (now - self._dirty_since).total_seconds() >= INCREMENTAL_MAX_WAIT_SECONDS:
            self._flush_dirty()
        else:
            self._flush_timer = async_call_later(
                self.hass, INCREMENTAL_DEBOUNCE_SECONDS, self._flush_dirty
            )

    @callback
    def _flush_dirty(self, _now: datetime | None = None) -> None:
        self._cancel_flush_timer()
        self._dirty_since = None
        if not self._dirty:
            return
        dirty, self._dirty = self._dirty, {}
        self._snapshot.update(dirty)
        # async_delay_save (not async_create_task(self._store.async_save(...))):
        # lets Store coalesce flushes that land close together into one
        # write instead of spawning an untracked save task per flush — the
        # same pattern _persist_run_stats already uses for _runs_store.
        # INCREMENTAL_DEBOUNCE_SECONDS is reused as the delay since it's
        # already the calibrated window for this store.
        self._store.async_delay_save(
            lambda: dict(self._snapshot), INCREMENTAL_DEBOUNCE_SECONDS
        )
        _LOGGER.debug("Incremental store: merged %d entities", len(dirty))

    # ----- Verify (diagnostic only, never patches) --------------------------

    async def async_verify(self) -> dict[str, Any]:
        """Compare live last_changed against the recorder/store-derived real
        value for every current target, without patching anything. Returns
        the entities where they deviate — useful for diagnosing "the value
        looks wrong" reports without having to reason through the resolve
        chain by hand.

        Unlike the boot pass this checks every target, not just the fresh
        candidates, so it is the heaviest recorder consumer in the
        integration — it is streamed batch by batch for the same reason (see
        _iter_bulk_batches), and only the mismatch list is accumulated. The
        returned bulk_rows_fetched/bulk_batches/deep_queries counts exist so
        that weight is visible from the service response (and the log line
        below) rather than only inferable from how long the call took.
        """
        domains, entities, exclude, _, labels, areas = self._config
        entity_ids = sorted(self._targets(domains, entities, exclude, labels, areas))

        mismatches: list[dict[str, Any]] = []
        bulk_rows_fetched = 0
        bulk_batches = 0
        counters: dict[str, int] = {}
        async for chunk, bulk in self._iter_bulk_batches(entity_ids):
            bulk_batches += 1
            bulk_rows_fetched += sum(len(rows) for rows in bulk.values())
            for entity_id in chunk:
                live = self.hass.states.get(entity_id)
                if live is None or live.state in INVALID_STATES:
                    continue
                expected = await self._resolve(
                    entity_id, live, bulk.get(entity_id), counters
                )
                if expected is None:
                    continue
                diff = (live.last_changed - expected).total_seconds()
                if abs(diff) > MARGIN_SECONDS:
                    mismatches.append(
                        {
                            "entity_id": entity_id,
                            "live_last_changed": live.last_changed.isoformat(),
                            "expected_last_changed": expected.isoformat(),
                            "diff_seconds": round(diff, 1),
                        }
                    )
            del bulk  # released before the next batch is fetched
        _LOGGER.info(
            "Last Changed Keeper: verify — %d checked, %d mismatches "
            "(bulk: %d rows in %d batches, %d deep queries)",
            len(entity_ids), len(mismatches), bulk_rows_fetched, bulk_batches,
            counters.get("deep_queries", 0),
        )
        return {
            "checked": len(entity_ids),
            "mismatches": mismatches,
            "bulk_rows_fetched": bulk_rows_fetched,
            "bulk_batches": bulk_batches,
            "deep_queries": counters.get("deep_queries", 0),
        }


def resolve_targets(
    hass: HomeAssistant,
    domains: list[str] | None,
    entities: list[str] | None,
    exclude: list[str] | None = None,
    labels: list[str] | None = None,
    areas: list[str] | None = None,
    all_entities: bool = False,
) -> set[str]:
    """Explicit entities, all states of the selected domains, and everything
    reachable via the selected labels/areas — minus exclude. If all_entities
    is set, every live entity is a target regardless of domains/entities/
    labels/areas (exclude still applies).

    Shared between _RestoreJob (what actually gets patched) and config_flow
    (the live count / empty-selection check) so both can never disagree.
    """
    if all_entities:
        out = {state.entity_id for state in hass.states.async_all()}
    else:
        out = set(entities or [])
        if domains:
            for state in hass.states.async_all():
                if state.domain in domains:
                    out.add(state.entity_id)
        if labels or areas:
            out |= _entities_for_labels_and_areas(hass, labels, areas)
    out -= set(exclude or [])
    return out


def _entities_for_labels_and_areas(
    hass: HomeAssistant, labels: list[str] | None, areas: list[str] | None
) -> set[str]:
    """Cascade labels/areas to entities the same way HA's built-in label/area
    target selectors do: a label or area on a device or area applies to
    every entity in/on it, not just entities labeled directly."""
    label_set = set(labels or [])
    area_set = set(areas or [])
    if not label_set and not area_set:
        return set()

    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    if label_set:
        area_set = set(area_set)  # don't mutate the caller's set
        for area in area_reg.async_list_areas():
            if label_set & area.labels:
                area_set.add(area.id)

    device_ids: set[str] = set()
    for device in dev_reg.devices.values():
        if (label_set & device.labels) or (device.area_id in area_set):
            device_ids.add(device.id)

    out: set[str] = set()
    for entry in ent_reg.entities.values():
        if (
            (label_set & entry.labels)
            or (entry.area_id in area_set)
            or (entry.device_id in device_ids)
        ):
            out.add(entry.entity_id)
    return out


@dataclass(slots=True)
class _BulkRow:
    """One genuine value-change row from the capped bulk query — just the
    three fields the resolve walk reads, instead of a full State object."""

    state: str | None
    last_changed: datetime
    last_updated: datetime


def _bulk_query(hass: HomeAssistant, start: datetime, entity_ids: list[str]) -> dict:
    """In the recorder executor: per entity, the newest BULK_PER_ENTITY_LIMIT
    genuine value TRANSITIONS within the bulk window.

    Replaces get_significant_states, which is unbounded per entity in two
    ways that OOM large installations (verified empirically on a real
    recorder DB): a chatty sensor whose VALUE changes every few seconds
    returns every one of its 10^5-10^6 rows in the window
    (significant_changes_only only drops attribute-writes), and entities in
    the recorder's hard-coded SIGNIFICANT_DOMAINS (climate, device_tracker,
    humidifier, thermostat, water_heater) return even attribute-only rows.
    Neither can be capped through that API — it has no per-entity LIMIT.

    Three layers:

    1. candidates: genuine value-change rows for EVERY domain
       (last_changed_ts is NULL when the value changed at that row, i.e.
       equals last_updated; on attribute-only rows it is set and older),
       excluding unavailable/unknown/None (removal) states, plus a LAG
       window function exposing each row's chronologically-preceding
       candidate's state.
    2. transitions: keeps only rows from (1) whose value actually differs
       from their predecessor's (or have no predecessor within the
       window) - i.e. deduplicates consecutive same-value rows - then
       ranks the SURVIVORS newest-first with row_number().
    3. the final SELECT, capping on that rank so a batch is bounded by
       BULK_BATCH_SIZE x BULK_PER_ENTITY_LIMIT small rows.

    Dropping attribute-only, unavailable/unknown, and None (removal) rows
    never changes the resolve walk's outcome (_real_last_changed already
    treats all three as transparent - see its docstring) - it only avoids
    wasting cap slots on rows that could never bound a run or move its
    start. The same logic extends to same-value rows in general via the
    transitions layer: a device flapping availability, or an entity torn
    down and recreated with an unchanged value (an ordinary restart, a
    config-entry reload, or - field-diagnosed - a cascade from an
    unrelated integration's own instability repeatedly reloading entities
    it doesn't even own) all produce a row that is indistinguishable from
    a genuine transition at the SQL layer without this dedup step. Left
    uncapped-on-transitions, an install where this happens every few
    hours indefinitely can have an affected entity's cap slots
    continuously consumed by these non-transitions, silently shrinking
    how far back a query can actually see and making the computed answer
    drift between calls instead of settling on the true, stable origin.
    Deduplicating before ranking means the cap always represents up to
    BULK_PER_ENTITY_LIMIT genuine transitions, regardless of how much
    teardown/recreate noise happened in between.
    Window functions need SQLite >= 3.25 / MariaDB >= 10.2 / MySQL 8 / any
    PostgreSQL — all far below Home Assistant's own database minimums;
    LAG was introduced in the same SQL:2003 batch as row_number, so this
    adds no new floor.
    """
    if States is None or StatesMeta is None:
        # Import-time fallback (see the guarded db_schema import): raising
        # here lands in _iter_bulk_batches' per-batch catch, which yields an
        # empty result so every entity resolves via snapshot/deep instead.
        raise RuntimeError("recorder db_schema models unavailable")
    prev_state = (
        func.lag(States.state)
        .over(
            partition_by=States.metadata_id,
            order_by=States.last_updated_ts.asc(),
        )
        .label("prev_state")
    )
    candidates = (
        select(
            StatesMeta.entity_id,
            States.state,
            States.last_updated_ts,
            States.last_changed_ts,
            prev_state,
        )
        .join(StatesMeta, States.metadata_id == StatesMeta.metadata_id)
        .where(
            StatesMeta.entity_id.in_(entity_ids),
            States.last_updated_ts > start.timestamp(),
            or_(
                States.last_changed_ts.is_(None),
                States.last_changed_ts == States.last_updated_ts,
            ),
            # A NULL state (entity removal) is dropped here too, the same
            # as unavailable/unknown: _real_last_changed no longer lets a
            # removal row bound a run (it only proves the entity briefly
            # didn't exist, not that its value changed — see that
            # function's docstring), so keeping it would only waste a cap
            # slot. NULL NOT IN (...) already evaluates to NULL, not TRUE,
            # so plain not_in() excludes it for free without an explicit
            # is_(None) check.
            States.state.not_in(INVALID_STATES),
        )
        .subquery()
    )
    rn = (
        func.row_number()
        .over(
            partition_by=candidates.c.entity_id,
            order_by=candidates.c.last_updated_ts.desc(),
        )
        .label("rn")
    )
    transitions = (
        select(
            candidates.c.entity_id,
            candidates.c.state,
            candidates.c.last_updated_ts,
            candidates.c.last_changed_ts,
            rn,
        )
        .where(
            or_(
                candidates.c.prev_state.is_(None),
                candidates.c.state != candidates.c.prev_state,
            )
        )
        .subquery()
    )
    stmt = select(
        transitions.c.entity_id,
        transitions.c.state,
        transitions.c.last_updated_ts,
        transitions.c.last_changed_ts,
    ).where(transitions.c.rn <= BULK_PER_ENTITY_LIMIT)

    result: dict[str, list[_BulkRow]] = {}
    with session_scope(hass=hass, read_only=True) as session:
        for entity_id, state, last_updated_ts, last_changed_ts in session.execute(
            stmt
        ):
            if last_updated_ts is None:
                continue
            last_updated = dt_util.utc_from_timestamp(last_updated_ts)
            last_changed = (
                dt_util.utc_from_timestamp(last_changed_ts)
                if last_changed_ts is not None
                else last_updated
            )
            result.setdefault(entity_id, []).append(
                _BulkRow(state, last_changed, last_updated)
            )
    return result


def _real_last_changed(
    history: Iterable, current_state: str
) -> tuple[datetime | None, bool]:
    """Determine when the current real value run began.

    Walks the valid states from newest to oldest while the value equals
    current_state. The oldest entry of that contiguous run is the real time.
    Restart recoveries (only via unavailable in between) are skipped this
    way, and so is a removal (state=None) row — written by
    Entity.async_remove() on every graceful entity teardown, which includes
    an ordinary graceful HA restart and a config-entry reload, not just the
    rare case of an entity_id being deliberately reused for a genuinely
    different device. A removal row only proves the entity briefly didn't
    exist; it says nothing about whether its value changed, so — like
    unavailable/unknown — it neither extends the run nor bounds it, no
    matter how old it is: an entity that has survived several restarts
    without a genuine value change accumulates one removal row per
    restart, and treating any of them (beyond the very latest) as a real
    boundary is exactly how an entity ends up confidently, permanently
    patched to an intermediate restart's own artifact instead of its true
    origin — field-diagnosed via a cluster of entities stuck at a stale
    restart timestamp for days, never revisited again since a "bounded"
    resolve gets _apply()'d and the entity marked _confirmed. See
    _bulk_query's docstring: since a removal row can no longer bound
    anything, that query no longer bothers fetching it either.

    Returns: (timestamp | None, bounded). bounded=True means the run was
    bounded by a row with a genuinely different value → the timestamp is
    certain. With bounded=False the history was exhausted (or only
    contained removal/invalid rows) → best effort only.
    """
    valid = sorted(
        (
            s
            for s in history
            if getattr(s, "state", None) not in INVALID_STATES
            and getattr(s, "state", None) is not None
            and getattr(s, "last_changed", None) is not None
        ),
        key=lambda s: s.last_updated,
        reverse=True,  # newest first
    )
    run_start: datetime | None = None
    bounded = False
    for s in valid:
        if s.state != current_state:
            bounded = True
            break
        run_start = s.last_changed
    return run_start, bounded


def _parse_delays(raw, default: tuple[int, ...]) -> tuple[int, ...]:
    """'30, 90, 180' → (30, 90, 180). On invalid input: default.

    Also accepts a list/tuple already. Values are clamped to 1..3600 s.
    """
    if raw is None or raw == "":
        return default
    try:
        if isinstance(raw, (list, tuple)):
            parts = list(raw)
        else:
            parts = [p for p in str(raw).replace(";", ",").split(",") if p.strip()]
        vals = [int(str(p).strip()) for p in parts]
        vals = [v for v in vals if 1 <= v <= 3600]
        return tuple(vals) if vals else default
    except (ValueError, TypeError):
        return default


@callback
def _apply_last_changed(
    state: State, ts: datetime, also_updated: bool = False
) -> bool:
    """Set last_changed on the live state and invalidate the cache.

    If also_updated is True, last_updated (incl. the last_updated_timestamp slot)
    is set to the same time as well.

    Returns True when fully applied (incl. cache invalidation). False = degraded
    (value set, but cache structure unknown) or error.
    """
    try:
        state.last_changed = ts
        if also_updated:
            state.last_updated = ts
            with contextlib.suppress(AttributeError):
                # slot does not exist in this HA version
                state.last_updated_timestamp = ts.timestamp()
    except (AttributeError, TypeError) as err:
        _LOGGER.warning("Could not set last_changed (HA version?): %s", err)
        return False
    cache = getattr(state, "_cache", None)
    if isinstance(cache, dict):
        cache.clear()
        return True
    return False


@callback
def _apply_last_triggered(state: State, ts: datetime) -> bool:
    """Patch the last_triggered attribute directly on the live state.

    Unlike _apply_last_changed (a dedicated State slot), this touches
    `attributes` (a ReadOnlyDict) — automation/script entities normally
    manage last_triggered themselves and usually restore it fine via their
    own RestoreEntity hook; this is a best-effort correction for when that
    didn't happen. A later genuine trigger (or the entity's own restore path
    succeeding on a subsequent reload) simply overwrites it again, same as
    any other attribute.

    Returns True when fully applied (incl. cache invalidation). False =
    degraded (value set, but cache structure unknown) or error.
    """
    try:
        state.attributes = ReadOnlyDict(
            {**state.attributes, ATTR_LAST_TRIGGERED: ts.isoformat()}
        )
    except (AttributeError, TypeError) as err:
        _LOGGER.warning("Could not set last_triggered (HA version?): %s", err)
        return False
    cache = getattr(state, "_cache", None)
    if isinstance(cache, dict):
        cache.clear()
        return True
    return False
