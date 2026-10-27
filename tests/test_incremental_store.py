"""Tests for the debounced incremental runtime store: every genuine value
change of a watched entity gets merged into the same store used for the
periodic/shutdown snapshot, instead of only updating it every
snapshot_interval seconds or at shutdown. Also covers _resolve() preferring
a newer store value over an otherwise-definitive bulk result.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import Event, HomeAssistant, State
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
    CONF_DOMAINS,
    CONF_ENTITIES,
    DOMAIN,
    INCREMENTAL_DEBOUNCE_SECONDS,
    INCREMENTAL_MAX_WAIT_SECONDS,
    STORAGE_KEY,
    STORAGE_VERSION,
)


@dataclass
class FakeRow:
    state: str
    last_changed: object
    last_updated: object


def _make_job(hass: HomeAssistant, snapshot: dict | None = None) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return _RestoreJob(hass, entry, store, snapshot or {})


# ----- End-to-end: persistent listener + debounce ---------------------------


async def test_incremental_change_merges_into_store_after_debounce(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    hass.states.async_set("light.kitchen", "off")
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_DOMAINS: ["light"], CONF_ENTITIES: []}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()

    # Still within the debounce window - not written yet.
    assert "light.kitchen" not in hass_storage.get(STORAGE_KEY, {}).get("data", {})

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=INCREMENTAL_DEBOUNCE_SECONDS + 1)
    )
    await hass.async_block_till_done()

    # _flush_dirty ran, but the actual write is itself now delayed via
    # Store.async_delay_save (see _flush_dirty) - a second advance is needed
    # before it lands in storage.
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=INCREMENTAL_DEBOUNCE_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert hass_storage[STORAGE_KEY]["data"]["light.kitchen"]["s"] == "on"


async def test_attribute_only_change_is_not_merged(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    hass.states.async_set(
        "climate.living_room", "heat", {"current_temperature": 20}
    )
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_DOMAINS: ["climate"], CONF_ENTITIES: []}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set(
        "climate.living_room", "heat", {"current_temperature": 21}
    )
    await hass.async_block_till_done()

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=INCREMENTAL_DEBOUNCE_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert STORAGE_KEY not in hass_storage


async def test_reregistration_event_not_merged_into_incremental_store(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_DOMAINS: ["light"], CONF_ENTITIES: []}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=INCREMENTAL_DEBOUNCE_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert STORAGE_KEY not in hass_storage


async def test_unavailable_recovery_not_merged_into_incremental_store(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    """Regression: recovering from unavailable/unknown resets last_changed
    to "now" the same way a re-registration does (HA treats unavailable as
    a distinct value), so it must be excluded here too - merging it would
    write the raw reset artifact into the snapshot store as if it were a
    real value change, poisoning it for the next boot. It's the
    re-registration listener's job instead (see test_reregistration.py)."""
    hass.states.async_set("light.kitchen", "on")
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_DOMAINS: ["light"], CONF_ENTITIES: []}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("light.kitchen", "unavailable")
    await hass.async_block_till_done()
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=INCREMENTAL_DEBOUNCE_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert STORAGE_KEY not in hass_storage


async def test_unload_cancels_incremental_listener_and_flush_timer(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "off")
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_DOMAINS: ["light"], CONF_ENTITIES: []}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    job = entry.runtime_data

    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    assert job._flush_timer is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert job._flush_timer is None
    assert job._unsub_incremental_listener is None
    assert job._dirty == {}


# ----- Unit-level: _flush_dirty / debounce max-wait -------------------------


async def test_flush_dirty_writes_and_clears_pending(
    recorder_mock, hass: HomeAssistant, hass_storage
) -> None:
    job = _make_job(hass)
    job._dirty = {"light.kitchen": {"s": "on", "t": dt_util.utcnow().isoformat()}}

    job._flush_dirty()
    await hass.async_block_till_done()

    assert job._dirty == {}
    # The write itself is delayed via Store.async_delay_save (see
    # _flush_dirty), so it isn't in storage until that delay elapses.
    assert "light.kitchen" not in hass_storage.get(STORAGE_KEY, {}).get("data", {})
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=INCREMENTAL_DEBOUNCE_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert hass_storage[STORAGE_KEY]["data"]["light.kitchen"]["s"] == "on"


async def test_on_target_state_changed_forces_flush_after_max_wait(
    recorder_mock, hass: HomeAssistant, hass_storage
) -> None:
    """A continuously chatty entity must not postpone the incremental write
    forever - once INCREMENTAL_MAX_WAIT_SECONDS of accumulated dirty time
    has passed, the next change flushes immediately."""
    job = _make_job(hass)
    job._dirty_since = dt_util.utcnow() - timedelta(
        seconds=INCREMENTAL_MAX_WAIT_SECONDS + 1
    )

    old = State("light.kitchen", "off")
    new = State("light.kitchen", "on")
    event = Event(
        "state_changed",
        {"entity_id": "light.kitchen", "old_state": old, "new_state": new},
    )
    job._on_target_state_changed(event)
    await hass.async_block_till_done()

    assert job._dirty == {}  # already flushed, not left sitting around
    assert job._flush_timer is None
    # The write itself is delayed via Store.async_delay_save (see
    # _flush_dirty), so it isn't in storage until that delay elapses.
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=INCREMENTAL_DEBOUNCE_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert hass_storage[STORAGE_KEY]["data"]["light.kitchen"]["s"] == "on"


# ----- _resolve(): store precedence over bulk when newer --------------------


async def test_resolve_prefers_newer_store_value_over_bulk_when_bounded(
    recorder_mock, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    now = live.last_changed
    bulk_ts = now - timedelta(hours=2)
    bulk_states = [
        FakeRow("off", now - timedelta(hours=3), now - timedelta(hours=3)),
        FakeRow("on", bulk_ts, bulk_ts),
    ]
    newer_store_ts = now - timedelta(minutes=10)  # more recent than bulk's answer

    job = _make_job(
        hass, snapshot={"light.kitchen": {"s": "on", "t": newer_store_ts.isoformat()}}
    )
    result = await job._resolve("light.kitchen", live, bulk_states)
    assert result == newer_store_ts



# ----- async_write_snapshot(): trust is an ALLOWLIST (self._confirmed), --
# ----- not a blocklist (see self._confirmed's declaration in __init__.py) -
#
# Merely being absent from self._unconfirmed is not enough to be trusted:
# an entity the boot pass's grace check silently classified as "already
# genuinely used since boot" is never added to EITHER set, and if that
# assumption was wrong (_async_run_impl itself ran later than expected
# relative to the true HA restart), its live.last_changed is still just a
# raw restart artifact. Only self._confirmed - populated exclusively via
# _mark_confirmed - is trusted.


async def test_snapshot_write_preserves_prior_entry_for_unconfirmed_entity(
    recorder_mock, hass: HomeAssistant
) -> None:
    """An entity still in _unconfirmed (a boot candidate _resolve() never
    managed to patch) must not have its raw, unresolved live.last_changed
    overwrite whatever the store already held for it - that would poison
    the store with a restart artifact, which _resolve()'s snapshot step
    would then confidently reapply on every future boot."""
    hass.states.async_set("light.kitchen", "on")
    good_ts = dt_util.utcnow() - timedelta(days=3)
    job = _make_job(
        hass, snapshot={"light.kitchen": {"s": "on", "t": good_ts.isoformat()}}
    )
    job._mark_unconfirmed("light.kitchen")

    await job.async_write_snapshot()

    assert job._snapshot["light.kitchen"] == {"s": "on", "t": good_ts.isoformat()}


async def test_snapshot_write_omits_unconfirmed_entity_with_no_prior_entry(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Same as above, but with nothing previously stored - there is no
    better answer to fall back to, so the entity is simply left out rather
    than seeded with an unresolved value."""
    hass.states.async_set("light.kitchen", "on")
    job = _make_job(hass)
    job._mark_unconfirmed("light.kitchen")

    await job.async_write_snapshot()

    assert "light.kitchen" not in job._snapshot


async def test_snapshot_write_omits_never_examined_entity_with_prior_entry(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The direct regression test for the snapshot-poisoning bug: an entity
    that was never touched at all this session - not in _unconfirmed (the
    boot pass's grace check silently classified it as "already genuinely
    used since boot" without ever examining it) and not in _confirmed
    either - is exactly what a grace-skipped candidate looks like. It must
    NOT be trusted just because it's absent from _unconfirmed; its existing
    stored entry is carried forward unchanged instead."""
    hass.states.async_set("light.kitchen", "on")
    good_ts = dt_util.utcnow() - timedelta(days=3)
    job = _make_job(
        hass, snapshot={"light.kitchen": {"s": "on", "t": good_ts.isoformat()}}
    )
    assert "light.kitchen" not in job._unconfirmed
    assert "light.kitchen" not in job._confirmed

    await job.async_write_snapshot()

    assert job._snapshot["light.kitchen"] == {"s": "on", "t": good_ts.isoformat()}


async def test_snapshot_write_omits_never_examined_entity_with_no_prior_entry(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Same as above, but with nothing previously stored - a grace-skipped
    entity with no history to fall back to is simply left out, never seeded
    from its unverified live state."""
    hass.states.async_set("light.kitchen", "on")
    job = _make_job(hass)

    await job.async_write_snapshot()

    assert "light.kitchen" not in job._snapshot


async def test_snapshot_write_uses_live_value_for_confirmed_entity(
    recorder_mock, hass: HomeAssistant
) -> None:
    """An entity explicitly marked confirmed (positively verified this
    session) is written from its current live state."""
    hass.states.async_set("light.kitchen", "on")
    live = hass.states.get("light.kitchen")
    job = _make_job(hass)
    job._mark_confirmed("light.kitchen")

    await job.async_write_snapshot()

    assert job._snapshot["light.kitchen"] == {
        "s": "on",
        "t": live.last_changed.isoformat(),
    }


async def test_snapshot_write_uses_live_value_after_real_apply(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Same as above, but exercising _mark_confirmed via a real _apply()
    call (the boot-pass/drain success path) rather than poking the set
    directly."""
    hass.states.async_set("light.kitchen", "on")
    live = hass.states.get("light.kitchen")
    ts = dt_util.utcnow() - timedelta(days=2)
    job = _make_job(hass)

    job._apply(live, ts, "light.kitchen")
    await job.async_write_snapshot()

    assert job._snapshot["light.kitchen"] == {
        "s": "on",
        "t": live.last_changed.isoformat(),
    }


async def test_reregistration_revokes_confirmed_trust_before_drain_completes(
    recorder_mock, hass: HomeAssistant
) -> None:
    """An entity confirmed earlier this session that then re-registers
    (old_state is None - a device rejoin, a config entry reload) must not
    be snapshot-written with its fresh, unresolved re-registration
    artifact just because it was trusted a moment ago - _mark_unconfirmed
    must revoke that trust immediately, before the reregister drain even
    runs, since a snapshot write could race in during the debounce
    window."""
    hass.states.async_set("light.kitchen", "on")
    job = _make_job(hass)
    job._known_targets = {"light.kitchen"}
    job._mark_confirmed("light.kitchen")
    assert "light.kitchen" in job._confirmed

    new_state = hass.states.get("light.kitchen")
    job._on_entity_reregistered(
        Event("state_changed", {"old_state": None, "new_state": new_state})
    )
    assert "light.kitchen" not in job._confirmed

    await job.async_write_snapshot()

    assert "light.kitchen" not in job._snapshot
    job.shutdown()


async def test_resolve_ignores_older_store_value_when_bulk_bounded(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Regression: an older/stale store entry must not override an
    otherwise-definitive, more recent bulk result."""
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    now = live.last_changed
    bulk_ts = now - timedelta(minutes=10)
    bulk_states = [
        FakeRow("off", now - timedelta(hours=3), now - timedelta(hours=3)),
        FakeRow("on", bulk_ts, bulk_ts),
    ]
    older_store_ts = now - timedelta(days=3)

    job = _make_job(
        hass, snapshot={"light.kitchen": {"s": "on", "t": older_store_ts.isoformat()}}
    )
    result = await job._resolve("light.kitchen", live, bulk_states)
    assert result == bulk_ts
