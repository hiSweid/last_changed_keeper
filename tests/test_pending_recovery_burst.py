"""Tests for the boot-pending recovery burst: entities that were pending at
boot (unavailable/unknown when the boot pass ran) get a second chance via a
persistent listener for their unavailable→real transition. Individual
transitions are debounce-coalesced into a burst set and drained together
(see _drain_pending_recovery_burst) instead of firing one recorder query per
entity, the same treatment P3.1 gave the re-registration listener.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
    DOMAIN,
    PENDING_RECOVERY_DEBOUNCE_SECONDS,
    STORAGE_KEY,
    STORAGE_VERSION,
)


def _make_job(hass: HomeAssistant, snapshot: dict | None = None) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return _RestoreJob(hass, entry, store, snapshot or {})


async def _flush_pending_recovery_burst(hass: HomeAssistant) -> None:
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(seconds=PENDING_RECOVERY_DEBOUNCE_SECONDS + 1),
    )
    await hass.async_block_till_done()


async def test_pending_recovery_burst_is_coalesced_into_one_batched_drain(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """Regression: several boot-pending entities recovering from unavailable
    close together must be coalesced into a single batched drain instead of
    one recorder query per entity."""
    import custom_components.last_changed_keeper as lck

    entity_ids = [f"light.bulb_{i}" for i in range(5)]
    for entity_id in entity_ids:
        hass.states.async_set(entity_id, "unavailable")
    await hass.async_block_till_done()

    job = _make_job(hass)
    await job.async_run()
    await hass.async_block_till_done()
    assert job._pending == set(entity_ids)

    calls: list[list[str]] = []

    def fake_bulk_query(_hass, _start, chunk):
        calls.append(list(chunk))
        return {}

    monkeypatch.setattr(lck, "_bulk_query", fake_bulk_query)

    for entity_id in entity_ids:
        hass.states.async_set(entity_id, "on")
        await hass.async_block_till_done()

    # All five recovered within the debounce window -> queued together, not
    # yet drained (and no recorder query issued yet).
    assert job._pending_recovery_burst == set(entity_ids)
    assert calls == []

    await _flush_pending_recovery_burst(hass)

    # One batched recorder query covering every entity in the burst, not
    # five separate ones.
    assert len(calls) == 1
    assert sorted(calls[0]) == sorted(entity_ids)
    assert job._pending_recovery_burst == set()

    # None of them were resolvable (fake query returns nothing, no
    # snapshot) -> they stay pending for the scheduled retry pass, same as
    # an unresolvable boot candidate always has.
    assert job._pending == set(entity_ids)
    job.shutdown()


async def test_unresolved_pending_recovery_stays_pending_for_retry_pass(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Unlike the re-registration burst, there is no per-entity retry ladder
    here - an entity the drain can't resolve simply stays in _pending for
    the existing scheduled retry passes to pick up later."""
    hass.states.async_set("light.slow_zigbee", "unavailable")
    await hass.async_block_till_done()

    job = _make_job(hass)
    await job.async_run()
    await hass.async_block_till_done()
    assert "light.slow_zigbee" in job._pending

    hass.states.async_set("light.slow_zigbee", "on")
    await hass.async_block_till_done()
    await _flush_pending_recovery_burst(hass)

    # Nothing resolvable (no recorder history, no snapshot) -> still pending,
    # and the persistent listener/timers are still armed for the next event
    # or scheduled retry.
    assert "light.slow_zigbee" in job._pending
    assert job._unsub_listener is not None
    job.shutdown()


async def test_pending_recovery_resolves_from_snapshot(
    recorder_mock, hass: HomeAssistant
) -> None:
    stale = dt_util.utcnow() - timedelta(days=3)
    hass.states.async_set("light.slow_zigbee", "unavailable")
    await hass.async_block_till_done()

    job = _make_job(
        hass, snapshot={"light.slow_zigbee": {"s": "on", "t": stale.isoformat()}}
    )
    await job.async_run()
    await hass.async_block_till_done()
    assert "light.slow_zigbee" in job._pending

    hass.states.async_set("light.slow_zigbee", "on")
    await hass.async_block_till_done()
    await _flush_pending_recovery_burst(hass)

    live = hass.states.get("light.slow_zigbee")
    assert live.last_changed == stale
    assert "light.slow_zigbee" not in job._pending


async def test_run_after_shutdown_arms_no_listener_or_retry_timers(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Regression: a boot pass still in flight when its entry is unloaded
    (an options save or reload right after start) must not arm the
    pending-recovery listener or the retry timers on the already shut-down
    job - nothing would ever cancel them."""
    hass.states.async_set("light.slow_zigbee", "unavailable")
    await hass.async_block_till_done()

    job = _make_job(hass)
    job.close()
    await job.async_run()
    await hass.async_block_till_done()

    assert job._unsub_listener is None
    assert job._unsub_timers == []
    assert job._unsub_target_discovery_timer is None
