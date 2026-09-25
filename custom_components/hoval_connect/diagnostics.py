"""Diagnostics support for Hoval Connect."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any
from urllib.parse import quote, quote_plus

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import HovalConnectConfigEntry

REDACT_CONFIG = {"password", "email"}
REDACT_COORDINATOR = {
    "token",
    "id_token",
    "plant_access_token",
    "plant_id",
    "plantExternalId",
    "name",
    "description",
    "source_path",
}


def _anonymise_coordinator_data(data: Any) -> dict[str, Any]:
    """Remove plant identifiers from mapping keys and embedded text, too.

    HA's field redactor does not redact IDs used as dictionary keys or embedded
    in URLs. Keep technical circuit paths for debugging; they are shared by many
    installations and do not identify an account or plant.
    """
    snapshot = asdict(data)
    identifiers = {
        identifier
        for key, plant in data.plants.items()
        for identifier in (key, plant.plant_id)
        if isinstance(identifier, str) and identifier
    }
    encoded_ids = {
        encode(identifier, safe="") for identifier in identifiers for encode in (quote, quote_plus)
    }
    # Percent escapes are case-insensitive in URLs; raw identifiers are not.
    encoded_ids |= {
        re.sub(r"%[0-9A-F]{2}", lambda match: match.group().lower(), identifier)
        for identifier in encoded_ids
    }
    replacements = sorted(identifiers | encoded_ids, key=len, reverse=True)

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            for identifier in replacements:
                value = value.replace(identifier, "**REDACTED**")
            return value
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact(item) for item in value]
        return value

    snapshot["plants"] = {
        f"plant_{index}": plant for index, plant in enumerate(snapshot["plants"].values(), start=1)
    }
    return redact(async_redact_data(snapshot, REDACT_COORDINATOR))


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HovalConnectConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "config_entry": async_redact_data(dict(entry.data), REDACT_CONFIG),
        "coordinator_data": _anonymise_coordinator_data(coordinator.data),
    }
