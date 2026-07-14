"""Tests for the last_changed_keeper_restored event: fired once a pass
settles (no more pending entities), so automations can wait for it instead
of racing the restore pass right after boot.
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
    EVENT_RESTORED,
    STORAGE_KEY,
    STORAGE_VERSION,
)


def _make_job(hass: HomeAssistant, snapshot: dict | None = None) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return _RestoreJob(hass, entry, store, snapshot or {})


async def test_fires_final_true_when_nothing_pending(
    recorder_mock, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()

    events = []
    hass.bus.async_listen(EVENT_RESTORED, lambda e: events.append(e.data))

    job = _make_job(hass)
    await job.async_run()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0]["final"] is True
    assert events[0]["pending"] == 0


async def test_fires_not_final_then_final_once_pending_drains(
    recorder_mock, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.slow_zigbee", "unavailable")
    await hass.async_block_till_done()

    events = []
    hass.bus.async_listen(EVENT_RESTORED, lambda e: events.append(e.data))

    stale = dt_util.utcnow() - timedelta(days=3)
    job = _make_job(
        hass, snapshot={"light.slow_zigbee": {"s": "on", "t": stale.isoformat()}}
    )
    await job.async_run()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0]["final"] is False
    assert events[0]["pending"] == 1

    # entity recovers -> listener patches it -> pending drains to zero
    hass.states.async_set("light.slow_zigbee", "on")
    await hass.async_block_till_done()

    assert len(events) == 2
    assert events[1]["final"] is True
    assert events[1]["pending"] == 0


async def test_final_event_not_fired_twice_for_same_pass(
    recorder_mock, hass: HomeAssistant
) -> None:
    """_cleanup_if_done() can legitimately be invoked more than once around
    the same drain-to-zero transition; the final event must fire only
    once per pass."""
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()

    events = []
    hass.bus.async_listen(EVENT_RESTORED, lambda e: events.append(e.data))

    job = _make_job(hass)
    await job.async_run()
    await hass.async_block_till_done()
    assert len(events) == 1

    # Defensive extra call — must be a no-op for the event.
    job._cleanup_if_done()
    await hass.async_block_till_done()
    assert len(events) == 1
