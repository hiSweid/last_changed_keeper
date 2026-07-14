"""Tests for resolve_targets: domains/entities/exclude plus the label/area
cascading (label or area on a device/area applies to every entity in it,
matching HA's built-in label/area target selector semantics).
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.last_changed_keeper import resolve_targets


async def test_domains_and_entities_and_exclude(hass: HomeAssistant) -> None:
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("light.hall", "off")
    hass.states.async_set("switch.fan", "on")
    result = resolve_targets(hass, ["light"], ["switch.fan"], ["light.hall"])
    assert result == {"light.kitchen", "switch.fan"}


async def test_no_selectors_returns_empty(hass: HomeAssistant) -> None:
    hass.states.async_set("light.kitchen", "on")
    assert resolve_targets(hass, [], [], []) == set()


async def test_label_directly_on_entity(hass: HomeAssistant) -> None:
    hass.states.async_set("light.kitchen", "on")
    label = lr.async_get(hass).async_create("keep")
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create("light", "test", "kitchen")
    ent_reg.async_update_entity(entry.entity_id, labels={label.label_id})
    hass.states.async_set(entry.entity_id, "on")

    result = resolve_targets(hass, [], [], [], labels=[label.label_id])
    assert entry.entity_id in result


async def test_label_on_device_cascades_to_its_entities(hass: HomeAssistant) -> None:
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    label = lr.async_get(hass).async_create("keep")
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "device1")},
    )
    dev_reg.async_update_device(device.id, labels={label.label_id})

    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "light", "test", "kitchen", device_id=device.id
    )
    hass.states.async_set(entry.entity_id, "on")

    result = resolve_targets(hass, [], [], [], labels=[label.label_id])
    assert entry.entity_id in result


async def test_label_on_area_cascades_through_devices(hass: HomeAssistant) -> None:
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    label = lr.async_get(hass).async_create("keep")
    area_reg = ar.async_get(hass)
    area = area_reg.async_create("Kitchen", labels={label.label_id})

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "device1")},
    )
    dev_reg.async_update_device(device.id, area_id=area.id)

    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "light", "test", "kitchen", device_id=device.id
    )
    hass.states.async_set(entry.entity_id, "on")

    result = resolve_targets(hass, [], [], [], labels=[label.label_id])
    assert entry.entity_id in result


async def test_area_selection_direct_and_via_device(hass: HomeAssistant) -> None:
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    area_reg = ar.async_get(hass)
    area = area_reg.async_create("Hall")

    ent_reg = er.async_get(hass)
    direct_entry = ent_reg.async_get_or_create("light", "test", "direct")
    ent_reg.async_update_entity(direct_entry.entity_id, area_id=area.id)
    hass.states.async_set(direct_entry.entity_id, "on")

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "device1")},
    )
    dev_reg.async_update_device(device.id, area_id=area.id)
    via_device_entry = ent_reg.async_get_or_create(
        "light", "test", "via_device", device_id=device.id
    )
    hass.states.async_set(via_device_entry.entity_id, "on")

    result = resolve_targets(hass, [], [], [], areas=[area.id])
    assert direct_entry.entity_id in result
    assert via_device_entry.entity_id in result


async def test_exclude_still_applies_to_label_matches(hass: HomeAssistant) -> None:
    label = lr.async_get(hass).async_create("keep")
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create("light", "test", "kitchen")
    ent_reg.async_update_entity(entry.entity_id, labels={label.label_id})
    hass.states.async_set(entry.entity_id, "on")

    result = resolve_targets(
        hass, [], [], [entry.entity_id], labels=[label.label_id]
    )
    assert entry.entity_id not in result
