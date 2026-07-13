"""Tests for _RestoreJob lifecycle behavior: the single_pass service call
must not tear down an active boot-time retry/listener pass.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)


def _make_job(hass: HomeAssistant, snapshot: dict | None = None) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return _RestoreJob(hass, entry, store, snapshot or {})


async def test_single_pass_does_not_tear_down_active_retry_machinery(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Regression: calling the restore_now service while a boot pass is
    still waiting on late-booting devices must not cancel that listener and
    the retry timers — doing so permanently orphans everything still
    pending for the rest of the grace window."""
    hass.states.async_set("light.slow_zigbee", "unavailable")
    await hass.async_block_till_done()

    job = _make_job(hass)
    torn_down: list[str] = []
    job._unsub_listener = lambda: torn_down.append("listener")
    job._unsub_timers = [lambda: torn_down.append("timer")]
    job._pending = {"light.slow_zigbee"}
    job._startup = dt_util.utcnow() - timedelta(seconds=5)

    await job.async_run(single_pass=True)

    assert torn_down == []
    assert job._unsub_listener is not None
    assert job._unsub_timers
    # Still unavailable -> not patched, still pending (untouched by the pass).
    assert "light.slow_zigbee" in job._pending


async def test_single_pass_patches_pending_entity_that_recovered(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A manual restore_now during an active pass still attempts to patch
    entities that already recovered, without resetting the machinery."""
    hass.states.async_set("light.slow_zigbee", "on")
    await hass.async_block_till_done()

    stale = dt_util.utcnow() - timedelta(days=3)
    job = _make_job(
        hass, snapshot={"light.slow_zigbee": {"s": "on", "t": stale.isoformat()}}
    )
    torn_down: list[str] = []
    job._unsub_listener = lambda: torn_down.append("listener")
    job._unsub_timers = []
    job._pending = {"light.slow_zigbee"}
    job._startup = dt_util.utcnow() - timedelta(seconds=5)

    patched = await job.async_run(single_pass=True)

    assert patched == 1
    assert "light.slow_zigbee" not in job._pending
    # pending is now empty -> _cleanup_if_done() legitimately tore it down.
    assert torn_down == ["listener"]
