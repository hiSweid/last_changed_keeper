"""Tests for _RestoreJob._resolve: snapshot/bulk/deep priority, the
bounded-but-not-ok short-circuit, and the exhausted-deep-window guard.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.last_changed_keeper as lck
from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
    DOMAIN,
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


async def test_snapshot_ignored_when_state_value_differs(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Regression: a snapshot taken while the entity held a different value
    must not be applied — the stored timestamp belongs to that other value."""
    hass.states.async_set("light.kitchen", "off")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    stale = dt_util.utcnow() - timedelta(days=3)
    job = _make_job(
        hass, snapshot={"light.kitchen": {"s": "on", "t": stale.isoformat()}}
    )

    result = await job._resolve("light.kitchen", live, None)
    assert result is None


async def test_snapshot_used_when_state_value_matches(
    recorder_mock, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    stale = dt_util.utcnow() - timedelta(days=3)
    job = _make_job(
        hass, snapshot={"light.kitchen": {"s": "on", "t": stale.isoformat()}}
    )

    result = await job._resolve("light.kitchen", live, None)
    assert result == stale


async def test_old_plain_string_snapshot_format_is_discarded(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Pre-0.5.9 snapshots stored a bare isoformat string per entity, with no
    state value. That format must be ignored, not crash or misapply."""
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    stale = dt_util.utcnow() - timedelta(days=3)
    job = _make_job(hass, snapshot={"light.kitchen": stale.isoformat()})

    result = await job._resolve("light.kitchen", live, None)
    assert result is None


async def test_bounded_bulk_result_short_circuits_without_ok(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A bounded bulk result that fails the margin check means the value
    genuinely just changed — no snapshot/deep fallback may override it."""
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    now = live.last_changed
    bulk_states = [
        FakeRow("off", now - timedelta(minutes=5), now - timedelta(minutes=5)),
        FakeRow("on", now, now),
    ]

    stale = now - timedelta(days=3)
    job = _make_job(
        hass, snapshot={"light.kitchen": {"s": "on", "t": stale.isoformat()}}
    )

    result = await job._resolve("light.kitchen", live, bulk_states)
    assert result is None  # not the stale snapshot value either


async def test_deep_query_best_effort_discarded_when_window_exhausted_by_count(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """HISTORY_DEPTH rows all showing the same value (attribute noise, e.g.
    climate current_temperature updates) must not be treated as a reliable
    'oldest of window' answer — the true change could be far older."""
    hass.states.async_set("climate.living_room", "heat")
    await hass.async_block_till_done()
    live = hass.states.get("climate.living_room")

    now = live.last_changed
    rows = [
        FakeRow("heat", now - timedelta(minutes=i), now - timedelta(minutes=i))
        for i in range(1, lck.HISTORY_DEPTH + 1)
    ]

    def fake_get_last_state_changes(_hass, _number_of_states, entity_id):
        return {entity_id: rows}

    monkeypatch.setattr(lck, "get_last_state_changes", fake_get_last_state_changes)

    job = _make_job(hass)
    result = await job._resolve("climate.living_room", live, None)
    assert result is None


async def test_deep_query_best_effort_used_when_window_not_exhausted(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """Fewer rows than HISTORY_DEPTH with no older differing value found is a
    genuinely unbounded-but-short history — still usable as best effort."""
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    now = live.last_changed
    oldest = now - timedelta(hours=2)
    rows = [FakeRow("on", oldest, oldest), FakeRow("on", now, now)]

    def fake_get_last_state_changes(_hass, _number_of_states, entity_id):
        return {entity_id: rows}

    monkeypatch.setattr(lck, "get_last_state_changes", fake_get_last_state_changes)

    job = _make_job(hass)
    result = await job._resolve("light.kitchen", live, None)
    assert result == oldest
