"""Config and options flow (GUI)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from . import resolve_targets
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
    # Union in the already-stored selection too: a domain that was picked
    # earlier but currently has zero live states (integration disabled/
    # temporarily broken) must stay selectable, or re-saving the form after
    # an unrelated change would fail validation / silently drop it.
    domain_options = sorted(
        {s.domain for s in hass.states.async_all()}
        | set(DEFAULT_DOMAINS)
        | set(defaults.get(CONF_DOMAINS) or [])
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
    return len(resolve_targets(hass, domains, entities, exclude))


def _is_empty(hass: HomeAssistant, user_input: dict[str, Any]) -> bool:
    """True if the resulting target set (after exclude) would be empty."""
    return (
        _count_targets(
            hass,
            user_input.get(CONF_DOMAINS),
            user_input.get(CONF_ENTITIES),
            user_input.get(CONF_EXCLUDE),
        )
        == 0
    )


def _invalid_retry_delays(raw: Any) -> bool:
    """True if raw is a non-empty string where not every token is a valid
    integer in 1..3600. Stricter than the runtime _parse_delays() fallback
    (which silently drops bad tokens and falls back to the default) — this
    is the one free-text field in the flow, so bad input should be rejected
    with a visible error rather than silently ignored."""
    if not isinstance(raw, str) or not raw.strip():
        return False
    parts = [p for p in raw.replace(";", ",").split(",") if p.strip()]
    if not parts:
        return False
    for part in parts:
        try:
            value = int(part.strip())
        except ValueError:
            return True
        if not 1 <= value <= 3600:
            return True
    return False


class LastChangedKeeperConfigFlow(ConfigFlow, domain=DOMAIN):
    """One-time setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            if _is_empty(self.hass, user_input):
                errors["base"] = "empty_selection"
            elif _invalid_retry_delays(user_input.get(CONF_RETRY_DELAYS)):
                errors[CONF_RETRY_DELAYS] = "invalid_retry_delays"
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
    ) -> ConfigFlowResult:
        """Reconfigure an existing setup (without deleting / re-adding it)."""
        entry = self._get_reconfigure_entry()

        errors: dict[str, str] = {}
        if user_input is not None:
            if _is_empty(self.hass, user_input):
                errors["base"] = "empty_selection"
            elif _invalid_retry_delays(user_input.get(CONF_RETRY_DELAYS)):
                errors[CONF_RETRY_DELAYS] = "invalid_retry_delays"
            else:
                # Clear options too: the options flow (async_step_init below)
                # writes the FULL form into entry.options, and every runtime
                # read merges {**entry.data, **entry.options} with options
                # last. Without this, a single earlier options-flow save
                # permanently shadows every future reconfigure — the save
                # looks successful but has zero effect.
                return self.async_update_reload_and_abort(
                    entry, data=user_input, options={}
                )
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
        return LastChangedKeeperOptionsFlow()


class LastChangedKeeperOptionsFlow(OptionsFlow):
    """Change the selection later on."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if _is_empty(self.hass, user_input):
                errors["base"] = "empty_selection"
            elif _invalid_retry_delays(user_input.get(CONF_RETRY_DELAYS)):
                errors[CONF_RETRY_DELAYS] = "invalid_retry_delays"
            else:
                return self.async_create_entry(title="", data=user_input)
            defaults = user_input
        else:
            defaults = {**self.config_entry.data, **self.config_entry.options}

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
