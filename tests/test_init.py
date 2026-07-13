"""Tests for the integration lifecycle: setup, unload, and the
service registration / response / error-handling behavior.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.last_changed_keeper.const import (
    CONF_DOMAINS,
    CONF_ENTITIES,
    DOMAIN,
    SERVICE_RESTORE_NOW,
)


async def _add_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_DOMAINS: ["light"], CONF_ENTITIES: []}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_registers_service_and_runtime_data(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)

    assert hass.services.has_service(DOMAIN, SERVICE_RESTORE_NOW)
    assert entry.runtime_data is not None
    assert entry.runtime_data.stats  # the boot pass already ran (has a target)


async def test_unload_clears_runtime_data_but_keeps_service(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """The service is registered in async_setup(), independent of any
    config entry — unloading the (single) entry must not deregister it."""
    entry = await _add_entry(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not hasattr(entry, "runtime_data")
    assert hass.services.has_service(DOMAIN, SERVICE_RESTORE_NOW)


async def test_restore_now_raises_when_no_entry_loaded(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """Regression: calling the service with no loaded entry (e.g. right
    after unload, or before setup) must raise a clear error instead of
    either silently no-op'ing or a raw 'service not found'."""
    entry = await _add_entry(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    try:
        await hass.services.async_call(
            DOMAIN, SERVICE_RESTORE_NOW, {}, blocking=True
        )
    except ServiceValidationError:
        pass
    else:
        raise AssertionError("expected ServiceValidationError")


async def test_restore_now_supports_response(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    await _add_entry(hass)

    result = await hass.services.async_call(
        DOMAIN, SERVICE_RESTORE_NOW, {}, blocking=True, return_response=True
    )

    assert "patched" in result
    assert "last_run" in result


async def test_restore_now_wraps_failure_as_home_assistant_error(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, monkeypatch
) -> None:
    entry = await _add_entry(hass)

    async def _boom(self, *, single_pass=False):
        raise RuntimeError("recorder exploded")

    monkeypatch.setattr(type(entry.runtime_data), "_async_run_impl", _boom)

    try:
        await hass.services.async_call(
            DOMAIN, SERVICE_RESTORE_NOW, {}, blocking=True
        )
    except HomeAssistantError as err:
        assert not isinstance(err, ServiceValidationError)
    else:
        raise AssertionError("expected HomeAssistantError")
