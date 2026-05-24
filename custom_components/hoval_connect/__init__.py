"""The Hoval Connect integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from .api import HovalApiError, HovalConnectApi
from .const import (
    CIRCUIT_TYPE_NAMES,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OPERATION_MODE_REGULAR,
)
from .coordinator import HovalCircuitData, HovalDataCoordinator, HovalPlantData

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.FAN,
    Platform.SELECT,
    Platform.SENSOR,
]

SERVICE_RESET_TEMPORARY_CHANGE = "reset_temporary_change"

_RESET_TEMPORARY_CHANGE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_ENTITY_ID): cv.entity_ids},
)

type HovalConnectConfigEntry = ConfigEntry[HovalRuntimeData]


@dataclass
class HovalRuntimeData:
    """Runtime data for the Hoval Connect integration."""

    coordinator: HovalDataCoordinator
    api: HovalConnectApi


def plant_device_info(plant_data: HovalPlantData) -> DeviceInfo:
    """Build DeviceInfo for a plant device."""
    return DeviceInfo(
        identifiers={(DOMAIN, plant_data.plant_id)},
        name=f"Hoval {plant_data.name}",
        manufacturer="Hoval",
        model="Plant",
    )


def circuit_device_info(
    plant_id: str,
    circuit_data: HovalCircuitData,
) -> DeviceInfo:
    """Build DeviceInfo for a circuit device."""
    model = CIRCUIT_TYPE_NAMES.get(circuit_data.circuit_type, circuit_data.circuit_type)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{plant_id}_{circuit_data.path}")},
        name=f"Hoval {circuit_data.name}",
        manufacturer="Hoval",
        model=model,
        via_device=(DOMAIN, plant_id),
    )


def _get_scan_interval(entry: HovalConnectConfigEntry) -> timedelta:
    """Get the scan interval from options or use default."""
    seconds = entry.options.get(CONF_SCAN_INTERVAL, int(DEFAULT_SCAN_INTERVAL.total_seconds()))
    return timedelta(seconds=seconds)


def _resolve_circuit_from_unique_id(
    coordinator: HovalDataCoordinator,
    unique_id: str,
) -> tuple[str, str] | None:
    """Walk a coordinator snapshot to find the (plant_id, circuit_path) for a unique_id.

    All circuit-level entities use the pattern `{plant_id}_{circuit_path}_<suffix>`.
    Since both plant_id and circuit_path can contain underscores, naive string
    splitting is unreliable; we match against the known plants/circuits instead.
    Returns None if no plant/circuit pair owns this unique_id (e.g. plant-level
    entities like `plant_online`).
    """
    for plant_id, plant_data in coordinator.data.plants.items():
        for circuit_path in plant_data.circuits:
            if unique_id.startswith(f"{plant_id}_{circuit_path}_"):
                return plant_id, circuit_path
    return None


async def _async_handle_reset_temporary_change(
    hass: HomeAssistant,
    call: ServiceCall,
) -> None:
    """Handle the hoval_connect.reset_temporary_change service call."""
    entity_reg = er.async_get(hass)
    # Deduplicate so multiple entities on the same circuit only trigger one reset
    targets: set[tuple[str, str, str]] = set()  # (entry_id, plant_id, circuit_path)

    for entity_id in call.data[ATTR_ENTITY_ID]:
        registry_entry = entity_reg.async_get(entity_id)
        if registry_entry is None:
            raise ServiceValidationError(f"Entity {entity_id} is not in the entity registry")
        if registry_entry.platform != DOMAIN:
            raise ServiceValidationError(f"Entity {entity_id} is not a Hoval Connect entity")
        config_entry = hass.config_entries.async_get_entry(registry_entry.config_entry_id)
        if config_entry is None or not hasattr(config_entry, "runtime_data"):
            raise ServiceValidationError(
                f"Hoval Connect config entry for {entity_id} is not loaded"
            )
        runtime: HovalRuntimeData = config_entry.runtime_data
        resolved = _resolve_circuit_from_unique_id(runtime.coordinator, registry_entry.unique_id)
        if resolved is None:
            raise ServiceValidationError(
                f"Entity {entity_id} is not bound to a Hoval circuit "
                "(temporary-change only applies to fan or climate circuits)"
            )
        plant_id, circuit_path = resolved
        targets.add((config_entry.entry_id, plant_id, circuit_path))

    for entry_id, plant_id, circuit_path in targets:
        config_entry = hass.config_entries.async_get_entry(entry_id)
        if config_entry is None:
            continue
        runtime = config_entry.runtime_data
        try:
            await runtime.coordinator.async_control_and_refresh(
                runtime.api.reset_temporary_change(plant_id, circuit_path),
                circuit_path=circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except HovalApiError as err:
            raise HomeAssistantError(
                f"Failed to reset temporary change on {circuit_path}: {err}"
            ) from err


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration-level services. Safe to call repeatedly."""
    if hass.services.has_service(DOMAIN, SERVICE_RESET_TEMPORARY_CHANGE):
        return

    async def _service_reset(call: ServiceCall) -> None:
        await _async_handle_reset_temporary_change(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_TEMPORARY_CHANGE,
        _service_reset,
        schema=_RESET_TEMPORARY_CHANGE_SCHEMA,
    )


async def async_setup_entry(hass: HomeAssistant, entry: HovalConnectConfigEntry) -> bool:
    """Set up Hoval Connect from a config entry."""
    session = async_get_clientsession(hass)
    api = HovalConnectApi(session, entry.data["email"], entry.data["password"])

    coordinator = HovalDataCoordinator(hass, api)
    coordinator.update_interval = _get_scan_interval(entry)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = HovalRuntimeData(coordinator=coordinator, api=api)

    # Register a parent device for each plant so circuit devices can use via_device
    device_reg = dr.async_get(hass)
    for plant_id, plant_data in coordinator.data.plants.items():
        device_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, plant_id)},
            name=f"Hoval {plant_data.name}",
            manufacturer="Hoval",
            model="Plant",
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _async_register_services(hass)

    # Listen for options changes to update polling interval dynamically
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
) -> None:
    """Handle options update — adjust polling interval without reload."""
    coordinator = entry.runtime_data.coordinator
    coordinator.update_interval = _get_scan_interval(entry)
    _LOGGER.debug("Polling interval updated to %s", coordinator.update_interval)


async def async_unload_entry(hass: HomeAssistant, entry: HovalConnectConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Remove the integration-level service once the last config entry goes away
    remaining = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != entry.entry_id
    ]
    if not remaining and hass.services.has_service(DOMAIN, SERVICE_RESET_TEMPORARY_CHANGE):
        hass.services.async_remove(DOMAIN, SERVICE_RESET_TEMPORARY_CHANGE)
    return unloaded
