"""Last Changed Keeper.

Restores the real last change time (`last_changed`) of selected entities after a
Home Assistant restart — directly on the entity.

Sources (in this order):
1. Recorder bulk query (one query for all entities) — fast on startup.
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
Zigbee/Z-Wave device rejoining) are caught the same way a restart is, via a
persistent listener (see _setup_reregister_listener) independent of the
boot-time pending/listener machinery.

Note: setting `last_changed`/`last_triggered` + invalidating the state cache
uses internal HA structures. All accesses are defensively guarded; if the
cache access fails, a repair issue is raised instead of crashing.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import (
    get_last_state_changes,
    get_significant_states,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
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
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
from homeassistant.util.read_only_dict import ReadOnlyDict

from .const import (
    ATTR_LAST_TRIGGERED,
    BULK_WINDOW_DAYS,
    CONF_AREAS,
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_EXCLUDE,
    CONF_GRACE,
    CONF_LABELS,
    CONF_RESTORE_LAST_TRIGGERED,
    CONF_RESTORE_LAST_UPDATED,
    CONF_RETRY_DELAYS,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_DOMAINS,
    DEFAULT_GRACE,
    DEFAULT_RESTORE_LAST_TRIGGERED,
    DEFAULT_RESTORE_LAST_UPDATED,
    DEFAULT_SNAPSHOT_INTERVAL,
    DOMAIN,
    EVENT_RESTORED,
    HISTORY_DEPTH,
    INCREMENTAL_DEBOUNCE_SECONDS,
    INCREMENTAL_MAX_WAIT_SECONDS,
    INVALID_STATES,
    ISSUE_INCOMPATIBLE,
    LAST_TRIGGERED_DOMAINS,
    MARGIN_SECONDS,
    RETRY_DELAYS,
    SERVICE_RESTORE_NOW,
    SERVICE_VERIFY,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

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

    job = _RestoreJob(hass, entry, store, snapshot)
    entry.runtime_data = job

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

    Nothing to clean up manually: job.shutdown() runs via the
    async_on_unload callback above, and core deletes entry.runtime_data
    itself once this returns True. The service is registered in
    async_setup() and is intentionally NOT torn down here.
    """
    return True


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
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._store = store
        self._snapshot = snapshot
        self._pending: set[str] = set()
        self._startup = dt_util.utcnow()
        self._unsub_listener: CALLBACK_TYPE | None = None
        self._unsub_timers: list[CALLBACK_TYPE] = []
        self._degraded = False
        self._also_updated = False
        self._also_restore_triggered = False
        self._final_fired = False
        self.stats: dict[str, object] = {}

        # ----- Feature: re-patch on runtime re-registration ---------------
        self._unsub_reregister_listener: CALLBACK_TYPE | None = None
        self._reregister_retry_timers: dict[str, list[CALLBACK_TYPE]] = {}

        # ----- Feature: incremental runtime store --------------------------
        self._unsub_incremental_listener: CALLBACK_TYPE | None = None
        self._dirty: dict[str, dict[str, str]] = {}
        self._dirty_since: datetime | None = None
        self._flush_timer: CALLBACK_TYPE | None = None

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

    def _targets(
        self,
        domains: list[str],
        entities: list[str],
        exclude: list[str] | None = None,
        labels: list[str] | None = None,
        areas: list[str] | None = None,
    ) -> set[str]:
        return resolve_targets(self.hass, domains, entities, exclude, labels, areas)

    # ----- Lifecycle -----------------------------------------------------

    @callback
    def shutdown(self) -> None:
        """Cancel everything (on unload/reload): boot machinery, the
        persistent re-registration listener/retries, and the incremental
        store listener/timer."""
        self._stop_boot_machinery()
        self._stop_reregister_listener()
        self._cancel_all_reregister_retries()
        self._stop_incremental_listener()
        self._cancel_flush_timer()
        self._dirty.clear()
        self._dirty_since = None

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

    @callback
    def _stop_listener(self) -> None:
        if self._unsub_listener is not None:
            self._unsub_listener()
            self._unsub_listener = None

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
    def _stop_incremental_listener(self) -> None:
        if self._unsub_incremental_listener is not None:
            self._unsub_incremental_listener()
            self._unsub_incremental_listener = None

    @callback
    def _cancel_flush_timer(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer()
            self._flush_timer = None

    # ----- Snapshot ------------------------------------------------------

    async def async_write_snapshot(self, _when: Event | datetime | None = None) -> None:
        """Persist the current state + last_changed of all targets.

        The state value is stored alongside the timestamp so a snapshot can
        only be applied if the entity still holds the same value on the next
        boot (see _resolve) — otherwise it could stamp the *previous* value's
        last_changed onto a genuinely new value.

        Used both as the EVENT_HOMEASSISTANT_STOP listener (receives an
        Event) and as the periodic snapshot timer (receives a datetime) —
        the argument itself is never used, both just trigger a fresh write.
        """
        domains, entities, exclude, _, labels, areas = self._config
        data: dict[str, dict[str, str]] = {}
        for entity_id in self._targets(domains, entities, exclude, labels, areas):
            live = self.hass.states.get(entity_id)
            if live is not None and live.state not in INVALID_STATES:
                data[entity_id] = {"s": live.state, "t": live.last_changed.isoformat()}
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
            return await self._async_run_impl(single_pass=single_pass)
        except Exception:
            _LOGGER.exception("Last Changed Keeper: async_run failed")
            return 0

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
        if not targets:
            return 0

        # Persistent, entry-lifetime listeners (independent of the boot
        # pending/listener machinery below, and not torn down when the boot
        # pass settles — see _stop_boot_machinery vs shutdown()).
        self._setup_reregister_listener(targets)
        self._setup_incremental_listener(targets)

        self._startup = dt_util.utcnow()
        self._pending = set()
        self._final_fired = False

        # Candidates: fresh (restart artifact) and currently valid.
        candidates: list[str] = []
        for entity_id in targets:
            live = self.hass.states.get(entity_id)
            if live is None or live.state in INVALID_STATES:
                self._pending.add(entity_id)
                continue
            if (dt_util.utcnow() - live.last_changed).total_seconds() > grace:
                continue  # already really used since boot
            candidates.append(entity_id)

        bulk = await self._bulk_fetch(candidates) if candidates else {}

        patched = 0
        patched_triggered = 0
        for entity_id in candidates:
            # Re-validate: the bulk query above awaited the recorder executor,
            # during which this entity may have gone unavailable or genuinely
            # changed for real — either way the state captured before the
            # await is stale and must not be trusted anymore.
            live = self.hass.states.get(entity_id)
            if live is None or live.state in INVALID_STATES:
                self._pending.add(entity_id)
                continue
            if (dt_util.utcnow() - live.last_changed).total_seconds() > grace:
                continue  # changed for real while we were awaiting the query
            if await self._maybe_restore_last_triggered(entity_id):
                patched_triggered += 1
            ts = await self._resolve(entity_id, live, bulk.get(entity_id))
            if ts is not None:
                self._apply(live, ts, entity_id)
                patched += 1

        # Resolve a stale repair issue once the cache patch works again
        # (e.g. after an HA update). Idempotent.
        if patched and not self._degraded:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_INCOMPATIBLE)

        self.stats = {
            "started": self._startup.isoformat(),
            "targets": len(targets),
            "candidates": len(candidates),
            "bulk_entities": len(bulk),
            "patched_immediate": patched,
            "patched_total": patched,
            "patched_last_triggered": patched_triggered,
            "pending": len(self._pending),
            "snapshot_entries": len(self._snapshot),
            "degraded": self._degraded,
        }
        _LOGGER.info(
            "Last Changed Keeper: pass 0 — %d patched, %d pending "
            "(bulk: %d entities)",
            patched, len(self._pending), len(bulk),
        )
        self._fire_restored_event(final=not self._pending)

        if single_pass or not self._pending:
            return patched

        self._setup_listener(grace)
        self._schedule_retries(grace)
        return patched

    @callback
    def _setup_listener(self, grace: float) -> None:
        """Listen for unavailable→real transitions of the pending entities."""

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
            self.hass.async_create_task(self._patch_pending(entity_id, grace))

        self._unsub_listener = async_track_state_change_event(
            self.hass, list(self._pending), _on_change
        )

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
        run_patched = 0
        for entity_id in list(self._pending):
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
        self.hass.bus.async_fire(
            EVENT_RESTORED,
            {
                "entry_id": self.entry.entry_id,
                "patched_total": int(self.stats.get("patched_total", 0)),
                "pending": len(self._pending),
                "final": final,
            },
        )

    # ----- Resolving the real timestamp ----------------------------------

    async def _resolve(
        self, entity_id: str, live: State, bulk_states: list | None
    ) -> datetime | None:
        """Determine the real last_changed: bulk → snapshot → deep → best-effort."""
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
        # use, so it is discarded rather than applied.
        if _ok(ts2) and len(deep_states) < HISTORY_DEPTH:
            return ts2
        if _ok(bulk_ts):
            return bulk_ts
        return None

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

    async def _bulk_fetch(self, entity_ids: list[str]) -> dict[str, list]:
        """One recorder query for all candidates over the bulk window."""
        start = dt_util.utcnow() - timedelta(days=BULK_WINDOW_DAYS)
        try:
            return await get_instance(self.hass).async_add_executor_job(
                _bulk_query, self.hass, start, entity_ids
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Bulk recorder query failed: %s", err)
            return {}

    # ----- Applying ------------------------------------------------------

    def _apply(self, live: State, ts: datetime, entity_id: str) -> None:
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
    def _setup_reregister_listener(self, targets: set[str]) -> None:
        """Persistent listener (entry lifetime, not just the boot pass) for
        an already-watched entity being fully re-created (old_state is
        None) after boot — e.g. its owning config entry reloads, or a
        Zigbee/Z-Wave device rejoins — which resets last_changed to "now"
        the same way a full HA restart does. Registered after boot has
        already assigned every entity its initial state, so it never fires
        for that initial assignment."""
        self._unsub_reregister_listener = async_track_state_change_event(
            self.hass, list(targets), self._on_entity_reregistered
        )

    @callback
    def _on_entity_reregistered(self, event: Event) -> None:
        if event.data.get("old_state") is not None:
            return  # not a full re-registration
        new = event.data.get("new_state")
        if new is None or new.state in INVALID_STATES:
            return
        entity_id = new.entity_id
        if entity_id in self._pending:
            return  # already handled by the active boot-time pass
        _, _, _, grace, _, _ = self._config
        if (dt_util.utcnow() - new.last_changed).total_seconds() > grace:
            return
        self.hass.async_create_task(self._patch_reregistered(entity_id, grace))

    async def _patch_reregistered(self, entity_id: str, grace: float) -> bool:
        """Entry point for a fresh re-registration event: drops any retries
        still scheduled from a previous flap of the same entity, then makes
        one attempt right away."""
        self._cancel_reregister_retry(entity_id)
        return await self._attempt_reregister_patch(entity_id, grace)

    async def _attempt_reregister_patch(self, entity_id: str, grace: float) -> bool:
        """One targeted, per-entity re-patch attempt (no bulk query — this
        is for a single entity, not a full boot pass). Also attempts the
        last_triggered path. On failure, schedules retries using the same
        retry_delays as the boot pass, respecting the same grace window."""
        live = self.hass.states.get(entity_id)
        if live is None or live.state in INVALID_STATES:
            return False
        if (dt_util.utcnow() - live.last_changed).total_seconds() > grace:
            return False
        await self._maybe_restore_last_triggered(entity_id)
        ts = await self._resolve(entity_id, live, None)
        if ts is None:
            self._schedule_reregister_retries(entity_id, grace)
            return False
        self._apply(live, ts, entity_id)
        _LOGGER.debug("%s: re-patched after runtime re-registration", entity_id)
        return True

    @callback
    def _cancel_reregister_retry(self, entity_id: str) -> None:
        for cancel in self._reregister_retry_timers.pop(entity_id, []):
            cancel()

    @callback
    def _schedule_reregister_retries(self, entity_id: str, grace: float) -> None:
        def _make(delay: int) -> CALLBACK_TYPE:
            @callback
            def _fire(_now) -> None:
                self.hass.async_create_task(
                    self._attempt_reregister_patch(entity_id, grace)
                )

            return async_call_later(self.hass, delay, _fire)

        self._reregister_retry_timers[entity_id] = [
            _make(d) for d in self._retry_delays
        ]

    # ----- Incremental runtime store ---------------------------------------

    @callback
    def _setup_incremental_listener(self, targets: set[str]) -> None:
        """Persistent listener (entry lifetime) that debounce-merges every
        genuine value change of a watched entity into the same store used
        for the periodic/shutdown snapshot — see _on_target_state_changed."""
        self._unsub_incremental_listener = async_track_state_change_event(
            self.hass, list(targets), self._on_target_state_changed
        )

    @callback
    def _on_target_state_changed(self, event: Event) -> None:
        """Debounced incremental merge into the snapshot store: keeps the
        stored last_changed close to real-time instead of only updating it
        every snapshot_interval / on shutdown — without re-deriving the
        whole store on every single change (see async_write_snapshot).

        Restricted to genuine value transitions (old_state present and
        actually different) — re-registrations (old_state is None, handled
        by the re-registration listener above) and attribute-only "chatter"
        (same state value, e.g. climate current_temperature) are ignored, so
        one noisy entity can't perpetually starve the debounce for others.
        """
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        if old is None or new is None:
            return
        if new.state in INVALID_STATES or old.state == new.state:
            return
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
        self.hass.async_create_task(self._store.async_save(dict(self._snapshot)))
        _LOGGER.debug("Incremental store: merged %d entities", len(dirty))

    # ----- Verify (diagnostic only, never patches) --------------------------

    async def async_verify(self) -> dict[str, Any]:
        """Compare live last_changed against the recorder/store-derived real
        value for every current target, without patching anything. Returns
        the entities where they deviate — useful for diagnosing "the value
        looks wrong" reports without having to reason through the resolve
        chain by hand."""
        domains, entities, exclude, _, labels, areas = self._config
        entity_ids = sorted(self._targets(domains, entities, exclude, labels, areas))
        bulk = await self._bulk_fetch(entity_ids) if entity_ids else {}

        mismatches: list[dict[str, Any]] = []
        for entity_id in entity_ids:
            live = self.hass.states.get(entity_id)
            if live is None or live.state in INVALID_STATES:
                continue
            expected = await self._resolve(entity_id, live, bulk.get(entity_id))
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
        return {"checked": len(entity_ids), "mismatches": mismatches}


def resolve_targets(
    hass: HomeAssistant,
    domains: list[str] | None,
    entities: list[str] | None,
    exclude: list[str] | None = None,
    labels: list[str] | None = None,
    areas: list[str] | None = None,
) -> set[str]:
    """Explicit entities, all states of the selected domains, and everything
    reachable via the selected labels/areas — minus exclude.

    Shared between _RestoreJob (what actually gets patched) and config_flow
    (the live count / empty-selection check) so both can never disagree.
    """
    out: set[str] = set(entities or [])
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


def _bulk_query(hass: HomeAssistant, start: datetime, entity_ids: list[str]) -> dict:
    """In the recorder executor: fetch significant states for many entities."""
    return get_significant_states(
        hass,
        start,
        None,
        entity_ids,
        include_start_time_state=False,
        significant_changes_only=True,
        no_attributes=True,
    )


def _real_last_changed(
    history: Iterable, current_state: str
) -> tuple[datetime | None, bool]:
    """Determine when the current real value run began.

    Walks the valid states from newest to oldest while the value equals
    current_state. The oldest entry of that contiguous run is the real time.
    Restart recoveries (only via unavailable in between) are skipped this way.

    Returns: (timestamp | None, bounded). bounded=True means the run was bounded
    by a different valid value → the timestamp is certain. With bounded=False the
    history was exhausted → best effort only.
    """
    valid = sorted(
        (
            s
            for s in history
            if getattr(s, "state", None) not in INVALID_STATES
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
