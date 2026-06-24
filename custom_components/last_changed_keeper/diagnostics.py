"""Diagnostics for Last Changed Keeper (zero cost, on demand only)."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DOMAINS, CONF_ENTITIES, CONF_GRACE, DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    data = {**entry.data, **entry.options}
    job = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    return {
        "config": {
            CONF_DOMAINS: data.get(CONF_DOMAINS),
            CONF_ENTITIES: data.get(CONF_ENTITIES),
            CONF_GRACE: data.get(CONF_GRACE),
        },
        "last_run": getattr(job, "stats", None),
    }
