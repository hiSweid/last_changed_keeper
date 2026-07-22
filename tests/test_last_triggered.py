"""Tests for the separate automation.*/script.* `last_triggered` patch
path: it's an attribute, not the state value, so it has its own recorder
read (_resolve_last_triggered) and its own apply mechanism
(_apply_last_triggered), gated by the restore_last_triggered option
(default: on).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.last_changed_keeper as lck
from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_RESTORE_LAST_TRIGGERED,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)


@dataclass
class FakeRow:
    state: str
    last_changed: object
    last_updated: object
    attributes: dict = field(default_factory=dict)


def _make_job(hass: HomeAssistant, *, enabled: bool = True) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    job = _RestoreJob(hass, entry, store, {})
    job._also_restore_triggered = enabled
    return job


async def test_restores_last_triggered_from_history(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    hass.states.async_set("automation.morning", "on")
    await hass.async_block_till_done()

    past = dt_util.utcnow() - timedelta(hours=5)
    rows = [FakeRow("on", past, past, {"last_triggered": past.isoformat()})]

    def fake_get_last_state_changes(_hass, _n, entity_id):
        return {entity_id: rows}

    monkeypatch.setattr(lck, "get_last_state_changes", fake_get_last_state_changes)

    job = _make_job(hass)
    patched = await job._maybe_restore_last_triggered("automation.morning")
    assert patched is True

    live = hass.states.get("automation.morning")
    assert live.attributes["last_triggered"] == past.isoformat()


async def test_noop_when_feature_disabled(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    hass.states.async_set("automation.morning", "on")
    await hass.async_block_till_done()

    past = dt_util.utcnow() - timedelta(hours=5)
    rows = [FakeRow("on", past, past, {"last_triggered": past.isoformat()})]
    monkeypatch.setattr(
        lck, "get_last_state_changes", lambda _h, _n, eid: {eid: rows}
    )

    job = _make_job(hass, enabled=False)
    patched = await job._maybe_restore_last_triggered("automation.morning")
    assert patched is False
    assert "last_triggered" not in hass.states.get("automation.morning").attributes


async def test_noop_for_domain_outside_automation_and_script(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()

    called = False

    def fake_get_last_state_changes(_hass, _n, entity_id):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(lck, "get_last_state_changes", fake_get_last_state_changes)

    job = _make_job(hass)
    patched = await job._maybe_restore_last_triggered("light.kitchen")
    assert patched is False
    assert called is False  # not even queried - wrong domain entirely


async def test_noop_when_live_already_has_last_triggered(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    now = dt_util.utcnow()
    hass.states.async_set(
        "script.greet", "on", {"last_triggered": now.isoformat()}
    )
    await hass.async_block_till_done()

    called = False

    def fake_get_last_state_changes(_hass, _n, entity_id):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(lck, "get_last_state_changes", fake_get_last_state_changes)

    job = _make_job(hass)
    patched = await job._maybe_restore_last_triggered("script.greet")
    assert patched is False
    assert called is False  # already has a value - own restore worked (or never fired)


async def test_noop_when_history_has_no_last_triggered(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    hass.states.async_set("automation.morning", "on")
    await hass.async_block_till_done()

    past = dt_util.utcnow() - timedelta(hours=5)
    rows = [FakeRow("on", past, past, {})]  # never had last_triggered
    monkeypatch.setattr(
        lck, "get_last_state_changes", lambda _h, _n, eid: {eid: rows}
    )

    job = _make_job(hass)
    patched = await job._maybe_restore_last_triggered("automation.morning")
    assert patched is False


async def test_end_to_end_restore_via_boot_pass(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, monkeypatch
) -> None:
    """Full integration: automation domain selected, boot pass restores
    last_triggered alongside (not instead of) the last_changed patch."""
    hass.states.async_set("automation.morning", "on")
    await hass.async_block_till_done()

    past = dt_util.utcnow() - timedelta(hours=5)
    rows = [FakeRow("on", past, past, {"last_triggered": past.isoformat()})]
    monkeypatch.setattr(
        lck, "get_last_state_changes", lambda _h, _n, eid: {eid: rows}
    )

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_DOMAINS: ["automation"], CONF_ENTITIES: []}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    live = hass.states.get("automation.morning")
    assert live.attributes["last_triggered"] == past.isoformat()
    assert entry.runtime_data.stats["patched_last_triggered"] == 1


async def test_option_disables_last_triggered_restore(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, monkeypatch
) -> None:
    hass.states.async_set("automation.morning", "on")
    await hass.async_block_till_done()

    past = dt_util.utcnow() - timedelta(hours=5)
    rows = [FakeRow("on", past, past, {"last_triggered": past.isoformat()})]
    monkeypatch.setattr(
        lck, "get_last_state_changes", lambda _h, _n, eid: {eid: rows}
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DOMAINS: ["automation"],
            CONF_ENTITIES: [],
            CONF_RESTORE_LAST_TRIGGERED: False,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    live = hass.states.get("automation.morning")
    assert "last_triggered" not in live.attributes
