"""Tests for the config, reconfigure and options flow.

Run: `pytest` with `pytest-homeassistant-custom-component` installed
(provides the `hass` fixture used below).
"""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.last_changed_keeper.config_flow import (
    _build_schema,
    _count_targets,
    _invalid_retry_delays,
    _is_empty,
)
from custom_components.last_changed_keeper.const import (
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_EXCLUDE,
    CONF_GRACE,
    CONF_LABELS,
    CONF_RETRY_DELAYS,
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


# ----- Regression: reconfigure must not be shadowed by stale options -------


async def test_reconfigure_after_options_flow_actually_takes_effect(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """Regression: the options flow writes the full form into entry.options;
    every runtime read merges {**entry.data, **entry.options} with options
    last. Reconfigure must clear stale options, or it becomes a silent
    no-op after the first options-flow save."""
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("switch.fan", "on")
    created = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    entry_result = await hass.config_entries.flow.async_configure(
        created["flow_id"], {CONF_DOMAINS: ["light"], CONF_ENTITIES: []}
    )
    entry = hass.config_entries.async_get_entry(entry_result["result"].entry_id)

    # Save via the options flow -> entry.options now holds every key.
    opt_init = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        opt_init["flow_id"],
        {CONF_DOMAINS: ["light"], CONF_ENTITIES: [], CONF_GRACE: 900},
    )
    assert entry.options[CONF_GRACE] == 900

    # Reconfigure with a different value.
    reconf = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        reconf["flow_id"],
        {CONF_DOMAINS: ["switch"], CONF_ENTITIES: [], CONF_GRACE: 300},
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    merged = {**entry.data, **entry.options}
    assert merged[CONF_GRACE] == 300
    assert merged[CONF_DOMAINS] == ["switch"]
    assert entry.options == {}


# ----- Regression: a stored domain without live states stays selectable ----


async def test_build_schema_includes_stored_domain_without_live_states(
    hass: HomeAssistant,
) -> None:
    """A domain saved earlier but with zero current live states (e.g. its
    integration is temporarily disabled) must remain a valid dropdown
    option, or resubmitting the form fails validation / drops it."""
    schema = _build_schema(hass, {CONF_DOMAINS: ["valve"]})
    for key, validator in schema.schema.items():
        if str(key) == CONF_DOMAINS:
            assert "valve" in validator.config["options"]
            return
    raise AssertionError("domains field not found in schema")


# ----- Unit tests for _invalid_retry_delays ---------------------------------


def test_invalid_retry_delays_accepts_empty_and_none():
    assert _invalid_retry_delays("") is False
    assert _invalid_retry_delays(None) is False
    assert _invalid_retry_delays("   ") is False


def test_invalid_retry_delays_accepts_valid_input():
    assert _invalid_retry_delays("30, 90, 180") is False
    assert _invalid_retry_delays("10;20;30") is False
    assert _invalid_retry_delays("1") is False
    assert _invalid_retry_delays("3600") is False


def test_invalid_retry_delays_rejects_garbage():
    assert _invalid_retry_delays("abc") is True
    assert _invalid_retry_delays("30, abc") is True  # partial garbage too


def test_invalid_retry_delays_rejects_out_of_range():
    assert _invalid_retry_delays("0") is True
    assert _invalid_retry_delays("3601") is True
    assert _invalid_retry_delays("30, 99999") is True  # partial out-of-range too


# ----- Regression: retry_delays validated in the flow -----------------------


async def test_user_flow_invalid_retry_delays_shows_error(
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
            CONF_RETRY_DELAYS: "abc",
        },
    )
    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {CONF_RETRY_DELAYS: "invalid_retry_delays"}


# ----- Labels/areas: schema fields + not-empty via labels/areas alone ------


def test_build_schema_includes_labels_areas_and_snapshot_interval(
    hass: HomeAssistant,
) -> None:
    schema = _build_schema(hass, {})
    field_names = {str(key) for key in schema.schema}
    assert {"labels", "areas", "snapshot_interval"} <= field_names


async def test_count_targets_zero_for_empty_selection_incl_labels_areas(
    hass: HomeAssistant,
) -> None:
    assert _count_targets(hass, [], [], [], [], []) == 0


async def test_is_empty_false_when_a_labeled_entity_exists(
    hass: HomeAssistant,
) -> None:
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import label_registry as lr

    label = lr.async_get(hass).async_create("keep-present")
    entry = er.async_get(hass).async_get_or_create("light", "test", "kitchen")
    er.async_get(hass).async_update_entity(entry.entity_id, labels={label.label_id})
    hass.states.async_set(entry.entity_id, "on")

    assert (
        _is_empty(
            hass,
            {CONF_DOMAINS: [], CONF_ENTITIES: [], CONF_LABELS: [label.label_id]},
        )
        is False
    )


async def test_is_empty_true_when_label_matches_nothing(
    hass: HomeAssistant,
) -> None:
    from homeassistant.helpers import label_registry as lr

    label = lr.async_get(hass).async_create("keep-unused")
    assert (
        _is_empty(
            hass,
            {CONF_DOMAINS: [], CONF_ENTITIES: [], CONF_LABELS: [label.label_id]},
        )
        is True
    )
