"""Diagnostics for Last Changed Keeper (zero cost, on demand only)."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    # Full merged config — nothing here is sensitive, and every key (incl.
    # exclude/retry_delays/restore_last_updated) is relevant when debugging
    # "why wasn't entity X restored".
    job = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    return {
        "config": {**entry.data, **entry.options},
        "last_run": getattr(job, "stats", None),
    }
