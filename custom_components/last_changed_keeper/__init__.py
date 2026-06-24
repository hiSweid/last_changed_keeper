"""Last Changed Keeper.

Restores the real last change time (`last_changed`) of selected entities after a
Home Assistant restart — directly on the entity.

Sources (in this order):
1. Recorder bulk query (one query for all entities) — fast on startup.
2. Recorder per-entity query (deeper) as a fallback for ambiguous cases.
3. Snapshot store (written on the last shutdown) — if the recorder no longer has
   the entity.

Note: setting `last_changed` + invalidating the state cache uses internal HA
structures. All accesses are defensively guarded; if the cache access fails, a
repair issue is raised instead of crashing.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

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
    State,
    callback,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BULK_WINDOW_DAYS,
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_EXCLUDE,
    CONF_GRACE,
    CONF_RESTORE_LAST_UPDATED,
    CONF_RETRY_DELAYS,
    DEFAULT_DOMAINS,
    DEFAULT_RESTORE_LAST_UPDATED,
    DEFAULT_GRACE,
    DOMAIN,
    HISTORY_DEPTH,
    INVALID_STATES,
    ISSUE_INCOMPATIBLE,
    MARGIN_SECONDS,
    RETRY_DELAYS,
    SERVICE_RESTORE_NOW,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the job, load the snapshot and run on start (or immediately)."""
    store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    snapshot: dict[str, str] = await store.async_load() or {}

    job = _RestoreJob(hass, entry, store, snapshot)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = job

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(job.shutdown)

    # Write the snapshot on shutdown (one write, no ongoing cost).
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, job.async_write_snapshot)
    )

    # async_at_started calls the callback once HA is fully started (entities
    # loaded) — or immediately if already started (reload/service). Robust
    # against the "starting" phase where hass.is_running is already True.
    @callback
    def _on_started(_hass: HomeAssistant) -> None:
        hass.async_create_task(job.async_run())

    entry.async_on_unload(async_at_started(hass, _on_started))

    _async_register_service(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entry; remove the service when no entries remain."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_RESTORE_NOW)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_register_service(hass: HomeAssistant) -> None:
    """Register the last_changed_keeper.restore_now service (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_RESTORE_NOW):
        return

    async def _handle_restore_now(call: ServiceCall) -> None:
        jobs: dict[str, _RestoreJob] = hass.data.get(DOMAIN, {})
        for job in list(jobs.values()):
            await job.async_run(single_pass=True)

    hass.services.async_register(
        DOMAIN, SERVICE_RESTORE_NOW, _handle_restore_now, schema=vol.Schema({})
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
        self.stats: dict[str, object] = {}

    # ----- Configuration -------------------------------------------------

    @property
    def _config(self) -> tuple[list[str], list[str], list[str], float]:
        data = {**self.entry.data, **self.entry.options}
        return (
            data.get(CONF_DOMAINS, DEFAULT_DOMAINS),
            data.get(CONF_ENTITIES, []),
            data.get(CONF_EXCLUDE, []),
            float(data.get(CONF_GRACE, DEFAULT_GRACE)),
        )

    @property
    def _restore_last_updated_enabled(self) -> bool:
        data = {**self.entry.data, **self.entry.options}
        return bool(
            data.get(CONF_RESTORE_LAST_UPDATED, DEFAULT_RESTORE_LAST_UPDATED)
        )

    @property
    def _retry_delays(self) -> tuple[int, ...]:
        data = {**self.entry.data, **self.entry.options}
        return _parse_delays(data.get(CONF_RETRY_DELAYS), RETRY_DELAYS)

    def _targets(
        self,
        domains: list[str],
        entities: list[str],
        exclude: list[str] | None = None,
    ) -> set[str]:
        out: set[str] = set(entities or [])
        if domains:
            for state in self.hass.states.async_all():
                if state.domain in domains:
                    out.add(state.entity_id)
        out -= set(exclude or [])
        return out

    # ----- Lifecycle -----------------------------------------------------

    @callback
    def shutdown(self) -> None:
        """Cancel listener and timers (on unload/reload)."""
        self._stop_listener()
        for cancel in self._unsub_timers:
            cancel()
        self._unsub_timers.clear()

    @callback
    def _stop_listener(self) -> None:
        if self._unsub_listener is not None:
            self._unsub_listener()
            self._unsub_listener = None

    # ----- Snapshot ------------------------------------------------------

    async def async_write_snapshot(self, _event: Event | None = None) -> None:
        """Persist the current last_changed values of all targets."""
        domains, entities, exclude, _ = self._config
        data: dict[str, str] = {}
        for entity_id in self._targets(domains, entities, exclude):
            live = self.hass.states.get(entity_id)
            if live is not None and live.state not in INVALID_STATES:
                data[entity_id] = live.last_changed.isoformat()
        await self._store.async_save(data)
        _LOGGER.debug("Wrote snapshot with %d entries", len(data))

    # ----- Main run ------------------------------------------------------

    async def async_run(self, *, single_pass: bool = False) -> int:
        """Guarded run: an exception must not kill the HA start."""
        try:
            return await self._async_run_impl(single_pass=single_pass)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Last Changed Keeper: async_run failed")
            return 0

    async def _async_run_impl(self, *, single_pass: bool = False) -> int:
        """Initial pass (with bulk query). Sets up listener + re-runs."""
        self.shutdown()
        self._degraded = False
        self._also_updated = self._restore_last_updated_enabled
        domains, entities, exclude, grace = self._config
        targets = self._targets(domains, entities, exclude)
        if not targets:
            return 0

        self._startup = dt_util.utcnow()
        self._pending = set()

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
        for entity_id in candidates:
            live = self.hass.states.get(entity_id)
            if live is None:
                continue
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
            "pending": len(self._pending),
            "snapshot_entries": len(self._snapshot),
            "degraded": self._degraded,
        }
        _LOGGER.info(
            "Last Changed Keeper: pass 0 — %d patched, %d pending "
            "(bulk: %d entities)",
            patched, len(self._pending), len(bulk),
        )

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
        run_patched = 0
        for entity_id in list(self._pending):
            if await self._patch_pending(entity_id, grace):
                run_patched += 1
        self.stats["patched_total"] = (
            int(self.stats.get("patched_total", 0)) + run_patched
        )
        self.stats["pending"] = len(self._pending)
        _LOGGER.info(
            "Last Changed Keeper: re-run +%ds — %d patched, %d pending",
            delay, run_patched, len(self._pending),
        )
        self._cleanup_if_done()

    async def _patch_pending(self, entity_id: str, grace: float) -> bool:
        live = self.hass.states.get(entity_id)
        if live is None or live.state in INVALID_STATES:
            return False
        if (dt_util.utcnow() - live.last_changed).total_seconds() > grace:
            return False
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
            self.shutdown()

    # ----- Resolving the real timestamp ----------------------------------

    async def _resolve(
        self, entity_id: str, live: State, bulk_states: list | None
    ) -> datetime | None:
        """Determine the real last_changed: bulk → snapshot → deep → best-effort."""
        cutoff = live.last_changed

        def _ok(ts: datetime | None) -> bool:
            return ts is not None and (cutoff - ts).total_seconds() > MARGIN_SECONDS

        # 1. Bulk result (only if unambiguously bounded)
        if bulk_states is not None:
            ts, bounded = _real_last_changed(bulk_states, live.state)
            if bounded and _ok(ts):
                return ts

        # 2. Snapshot (free, in memory, authoritative) — before the costly query
        snap_raw = self._snapshot.get(entity_id)
        if snap_raw:
            snap_dt = dt_util.parse_datetime(snap_raw)
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
        if bounded2 and _ok(ts2):
            return ts2

        # 4. Best effort from an unbounded run
        if _ok(ts2):
            return ts2
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
            try:
                state.last_updated_timestamp = ts.timestamp()
            except AttributeError:
                pass  # slot does not exist in this HA version
    except (AttributeError, TypeError) as err:
        _LOGGER.warning("Could not set last_changed (HA version?): %s", err)
        return False
    cache = getattr(state, "_cache", None)
    if isinstance(cache, dict):
        cache.clear()
        return True
    return False
