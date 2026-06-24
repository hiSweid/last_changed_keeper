"""Config and options flow (GUI)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_EXCLUDE,
    CONF_GRACE,
    CONF_RESTORE_LAST_UPDATED,
    CONF_RETRY_DELAYS,
    DEFAULT_DOMAINS,
    DEFAULT_GRACE,
    DEFAULT_RESTORE_LAST_UPDATED,
    DOMAIN,
    RETRY_DELAYS,
)

_DEFAULT_RETRY_DELAYS_STR = ", ".join(str(d) for d in RETRY_DELAYS)


def _build_schema(hass: HomeAssistant, defaults: dict[str, Any]) -> vol.Schema:
    domain_options = sorted(
        {s.domain for s in hass.states.async_all()} | set(DEFAULT_DOMAINS)
    )
    return vol.Schema(
        {
            vol.Optional(
                CONF_DOMAINS, default=defaults.get(CONF_DOMAINS, DEFAULT_DOMAINS)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=domain_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_ENTITIES, default=defaults.get(CONF_ENTITIES, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(multiple=True)
            ),
            vol.Optional(
                CONF_EXCLUDE, default=defaults.get(CONF_EXCLUDE, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(multiple=True)
            ),
            vol.Optional(
                CONF_GRACE, default=defaults.get(CONF_GRACE, DEFAULT_GRACE)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=60, max=86400, step=60, unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_RESTORE_LAST_UPDATED,
                default=defaults.get(
                    CONF_RESTORE_LAST_UPDATED, DEFAULT_RESTORE_LAST_UPDATED
                ),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_RETRY_DELAYS,
                default=defaults.get(
                    CONF_RETRY_DELAYS, _DEFAULT_RETRY_DELAYS_STR
                ),
            ): selector.TextSelector(),
        }
    )


def _count_targets(
    hass: HomeAssistant,
    domains: list[str] | None,
    entities: list[str] | None,
    exclude: list[str] | None = None,
) -> int:
    """Number of entities affected by the selection (for the live count)."""
    out: set[str] = set(entities or [])
    selected = set(domains or [])
    if selected:
        for state in hass.states.async_all():
            if state.domain in selected:
                out.add(state.entity_id)
    out -= set(exclude or [])
    return len(out)


def _is_empty(user_input: dict[str, Any]) -> bool:
    return not user_input.get(CONF_DOMAINS) and not user_input.get(CONF_ENTITIES)


class LastChangedKeeperConfigFlow(ConfigFlow, domain=DOMAIN):
    """One-time setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            if _is_empty(user_input):
                errors["base"] = "empty_selection"
            else:
                return self.async_create_entry(
                    title="Last Changed Keeper", data=user_input
                )

        defaults = user_input or {}
        count = _count_targets(
            self.hass,
            defaults.get(CONF_DOMAINS, DEFAULT_DOMAINS),
            defaults.get(CONF_ENTITIES, []),
            defaults.get(CONF_EXCLUDE, []),
        )
        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(self.hass, defaults),
            errors=errors,
            description_placeholders={"count": str(count)},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Reconfigure an existing setup (without deleting / re-adding it)."""
        entry = self._get_reconfigure_entry()

        errors: dict[str, str] = {}
        if user_input is not None:
            if _is_empty(user_input):
                errors["base"] = "empty_selection"
            else:
                return self.async_update_reload_and_abort(entry, data=user_input)
            defaults = user_input
        else:
            defaults = {**entry.data, **entry.options}

        count = _count_targets(
            self.hass,
            defaults.get(CONF_DOMAINS, DEFAULT_DOMAINS),
            defaults.get(CONF_ENTITIES, []),
            defaults.get(CONF_EXCLUDE, []),
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_schema(self.hass, defaults),
            errors=errors,
            description_placeholders={"count": str(count)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return LastChangedKeeperOptionsFlow(config_entry)


class LastChangedKeeperOptionsFlow(OptionsFlow):
    """Change the selection later on."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if _is_empty(user_input):
                errors["base"] = "empty_selection"
            else:
                return self.async_create_entry(title="", data=user_input)
            defaults = user_input
        else:
            defaults = {**self._entry.data, **self._entry.options}

        count = _count_targets(
            self.hass,
            defaults.get(CONF_DOMAINS, DEFAULT_DOMAINS),
            defaults.get(CONF_ENTITIES, []),
            defaults.get(CONF_EXCLUDE, []),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(self.hass, defaults),
            errors=errors,
            description_placeholders={"count": str(count)},
        )
