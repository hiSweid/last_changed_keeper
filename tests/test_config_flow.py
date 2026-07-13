"""Tests for the config, reconfigure and options flow.

Run: `pytest` with `pytest-homeassistant-custom-component` installed
(provides the `hass` fixture used below).
"""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.last_changed_keeper.config_flow import (
    _count_targets,
    _is_empty,
)
from custom_components.last_changed_keeper.const import (
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_EXCLUDE,
    DOMAIN,
)

# ----- Unit tests for the pure helpers --------------------------------------


async def test_count_targets_domain_and_entities(hass: HomeAssistant) -> None:
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("light.hall", "off")
    hass.states.async_set("switch.fan", "on")
    assert _count_targets(hass, ["light"], ["switch.fan"]) == 3


async def test_count_targets_exclude_removes_entity(hass: HomeAssistant) -> None:
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("light.hall", "off")
    assert _count_targets(hass, ["light"], [], ["light.hall"]) == 1


async def test_count_targets_exclude_can_empty_a_domain(hass: HomeAssistant) -> None:
    hass.states.async_set("light.kitchen", "on")
    assert _count_targets(hass, ["light"], [], ["light.kitchen"]) == 0


async def test_is_empty_true_when_no_selection(hass: HomeAssistant) -> None:
    assert _is_empty(hass, {CONF_DOMAINS: [], CONF_ENTITIES: []}) is True


async def test_is_empty_false_when_domain_selected(hass: HomeAssistant) -> None:
    hass.states.async_set("light.kitchen", "on")
    assert _is_empty(hass, {CONF_DOMAINS: ["light"], CONF_ENTITIES: []}) is False


async def test_is_empty_true_when_exclude_covers_whole_domain(
    hass: HomeAssistant,
) -> None:
    """Regression: picking a domain and excluding every entity in it must
    still be treated as an empty selection, not silently accepted."""
    hass.states.async_set("light.kitchen", "on")
    assert (
        _is_empty(
            hass,
            {
                CONF_DOMAINS: ["light"],
                CONF_ENTITIES: [],
                CONF_EXCLUDE: ["light.kitchen"],
            },
        )
        is True
    )


# ----- Integration-style tests through the real flow manager ---------------


async def test_user_flow_shows_form(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_flow_empty_selection_reshows_form_with_error(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DOMAINS: [], CONF_ENTITIES: []}
    )
    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "empty_selection"}


async def test_user_flow_domain_fully_excluded_shows_error(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_DOMAINS: ["light"],
            CONF_ENTITIES: [],
            CONF_EXCLUDE: ["light.kitchen"],
        },
    )
    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "empty_selection"}


async def test_user_flow_creates_entry(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DOMAINS: ["light"], CONF_ENTITIES: []}
    )
    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["data"][CONF_DOMAINS] == ["light"]


async def test_single_config_entry_aborts_second_flow(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(
        first["flow_id"], {CONF_DOMAINS: ["light"], CONF_ENTITIES: []}
    )

    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert second["type"] == FlowResultType.ABORT
    # manifest.json sets single_config_entry: true, which aborts before our
    # own _abort_if_unique_id_configured() unique-id check even runs.
    assert second["reason"] == "single_instance_allowed"


async def test_options_flow_updates_selection(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("switch.fan", "on")
    created = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    entry_result = await hass.config_entries.flow.async_configure(
        created["flow_id"], {CONF_DOMAINS: ["light"], CONF_ENTITIES: []}
    )
    entry = hass.config_entries.async_get_entry(entry_result["result"].entry_id)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DOMAINS: ["switch"], CONF_ENTITIES: []}
    )
    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["data"][CONF_DOMAINS] == ["switch"]


async def test_options_flow_empty_selection_shows_error(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    created = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    entry_result = await hass.config_entries.flow.async_configure(
        created["flow_id"], {CONF_DOMAINS: ["light"], CONF_ENTITIES: []}
    )
    entry = hass.config_entries.async_get_entry(entry_result["result"].entry_id)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DOMAINS: [], CONF_ENTITIES: []}
    )
    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "empty_selection"}
