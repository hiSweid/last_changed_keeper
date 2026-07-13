"""Diagnostics for Last Changed Keeper (zero cost, on demand only)."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import LckConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LckConfigEntry
) -> dict[str, Any]:
    # Full merged config — nothing here is sensitive, and every key (incl.
    # exclude/retry_delays/restore_last_updated) is relevant when debugging
    # "why wasn't entity X restored".
    return {
        "config": {**entry.data, **entry.options},
        "last_run": entry.runtime_data.stats,
    }
