"""The Hoval Connect integration."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

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
from .compat import DEVICE_INFO_HAS_VIA_DEVICE_ID, OPTIONS_FLOW_RELOADS
from .const import (
    CIRCUIT_TYPE_HV,
    CIRCUIT_TYPE_NAMES,
    DATA_TOPOLOGY,
    DOMAIN,
    OPERATION_MODE_REGULAR,
)
from .coordinator import HovalCircuitData, HovalDataCoordinator, HovalPlantData
from .modbus import HovalModbusClient
from .modbus_hv import HovalModbusPoller
from .options import (
    ModbusSettings,
    get_modbus_plant_id,
    get_modbus_scan_interval,
    get_modbus_settings,
    get_scan_interval,
    is_local_entry,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.WATER_HEATER,
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
    api: HovalConnectApi | None
    # Gateway address and plant binding the coordinator was built with. The
    # legacy-HA options listener reloads the entry when either changes.
    modbus_settings: ModbusSettings | None = None
    modbus_plant_id: str | None = None


def plant_device_info(plant_data: HovalPlantData) -> DeviceInfo:
    """Build DeviceInfo for a plant device."""
    return DeviceInfo(
        identifiers={(DOMAIN, plant_data.plant_id)},
        name=f"Hoval {plant_data.name}",
        manufacturer="Hoval",
        model="Plant",
    )


def circuit_device_info(
    coordinator: HovalDataCoordinator,
    plant_id: str,
    circuit_data: HovalCircuitData,
) -> DeviceInfo:
    """Build DeviceInfo for a circuit device, linked to its plant device."""
    model = CIRCUIT_TYPE_NAMES.get(circuit_data.circuit_type, circuit_data.circuit_type)
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{plant_id}_{circuit_data.path}")},
        name=f"Hoval {circuit_data.name}",
        manufacturer="Hoval",
        model=model,
    )
    if not DEVICE_INFO_HAS_VIA_DEVICE_ID:
        info["via_device"] = (DOMAIN, plant_id)
    elif (plant_device_id := coordinator.plant_device_ids.get(plant_id)) is not None:
        info["via_device_id"] = plant_device_id
    return info


def _apply_intervals(coordinator: HovalDataCoordinator, entry: HovalConnectConfigEntry) -> None:
    """Set the polling cadence: Modbus interval with a gateway, else the cloud interval.

    Shared by setup and the legacy-HA update listener, which also fires when the
    coordinator writes the topology into entry.data and must not reset the cadence.
    The choice follows the coordinator, never the options alone: a gateway added
    in the options on legacy HA reaches a coordinator built without a poller,
    whose every update is a full cloud poll (C1, Hoval allows one per 15 min).
    """
    cloud_seconds = get_scan_interval(entry.options)
    coordinator.cloud_interval = cloud_seconds
    if coordinator.has_modbus:
        coordinator.update_interval = timedelta(seconds=get_modbus_scan_interval(entry.options))
    else:
        coordinator.update_interval = timedelta(seconds=cloud_seconds)


# Unique id of an HV circuit's fan entity; plant ids may contain underscores.
_FAN_UNIQUE_ID = re.compile(r"^(?P<plant>.+)_(?P<path>\d+\.\d+\.\d+)_fan$")


def _seed_topology(
    hass: HomeAssistant, entry: HovalConnectConfigEntry, modbus_plant_id: str | None
) -> dict[str, Any] | None:
    """Rebuild a hybrid entry's topology from its own registry entries (I6).

    The topology is stored only after a successful cloud poll in hybrid mode,
    so an entry switched to hybrid while Hoval already refuses it could not
    start at all. Its HV circuits are known anyway: they are the fan entities
    `{plant_id}_{path}_fan` this entry registered as a cloud entry, and the
    device registry holds their names (with the "Hoval " prefix that
    plant_device_info/circuit_device_info add). Only one plant is seeded: the
    plant chosen in the options, else the only plant found; otherwise None,
    and the first start needs the cloud as before. The coordinator uses the
    seed like a stored topology; the next successful cloud poll stores it.
    """
    circuits: dict[str, list[str]] = {}
    for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if entity.domain != Platform.FAN or entity.platform != DOMAIN:
            continue
        if (match := _FAN_UNIQUE_ID.match(entity.unique_id)) is not None:
            circuits.setdefault(match["plant"], []).append(match["path"])
    if modbus_plant_id:
        plant_id = modbus_plant_id if modbus_plant_id in circuits else None
    else:
        plant_id = next(iter(circuits)) if len(circuits) == 1 else None
    if plant_id is None:
        return None

    device_reg = dr.async_get(hass)

    def name(identifier: str, fallback: str) -> str:
        device = device_reg.async_get_device(identifiers={(DOMAIN, identifier)})
        registered = device.name if device is not None else None
        return (registered or "").removeprefix("Hoval ") or fallback

    topology = {
        "plant_id": plant_id,
        "plant_name": name(plant_id, plant_id),
        # The cloud names a circuit without a name after its type.
        "circuits": {
            path: name(f"{plant_id}_{path}", CIRCUIT_TYPE_HV) for path in sorted(circuits[plant_id])
        },
    }
    _LOGGER.debug(
        "No stored plant topology; using %d HV circuit(s) from the entity registry",
        len(topology["circuits"]),
    )
    return topology


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
        if runtime.api is None:
            raise ServiceValidationError(
                f"Entity {entity_id} belongs to a local-only entry; resetting a temporary "
                "change needs the Hoval account (cloud)"
            )
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
                lambda api=runtime.api, pid=plant_id, path=circuit_path: api.reset_temporary_change(
                    pid, path
                ),
                plant_id=plant_id,
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
    local = is_local_entry(entry.data)
    api: HovalConnectApi | None = None
    if not local:
        api = HovalConnectApi(
            async_get_clientsession(hass), entry.data["email"], entry.data["password"]
        )

    settings = get_modbus_settings(entry.data, entry.options)
    poller = (
        HovalModbusPoller(HovalModbusClient(settings.host, settings.port, settings.unit_id))
        if settings is not None
        else None
    )
    modbus_plant_id = None if local else get_modbus_plant_id(entry.options)
    topology = entry.data.get(DATA_TOPOLOGY)
    if topology is None and poller is not None and not local:
        topology = _seed_topology(hass, entry, modbus_plant_id)

    coordinator = HovalDataCoordinator(
        hass,
        api,
        modbus=poller,
        modbus_plant_id=modbus_plant_id,
        local_plant=(f"local_{entry.entry_id}", entry.title) if local else None,
        topology=topology,
    )
    _apply_intervals(coordinator, entry)

    # HA does not call async_unload_entry when setup fails, so release the
    # (lazily connected) Modbus client here, or retries would pile up connections.
    try:
        await coordinator.async_config_entry_first_refresh()

        entry.runtime_data = HovalRuntimeData(
            coordinator=coordinator,
            api=api,
            modbus_settings=settings,
            modbus_plant_id=modbus_plant_id,
        )

        # Register a parent device for each plant before the platforms set up, so
        # circuit devices can link to it by registry id (see circuit_device_info).
        device_reg = dr.async_get(hass)
        plant_device_ids: dict[str, str] = {}
        for plant_id, plant_data in coordinator.data.plants.items():
            plant_device = device_reg.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, plant_id)},
                name=f"Hoval {plant_data.name}",
                manufacturer="Hoval",
                model="Plant",
            )
            plant_device_ids[plant_id] = plant_device.id
        coordinator.plant_device_ids = plant_device_ids

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        _async_register_services(hass)

        # New HA reloads via the options flow. Combining that with an update
        # listener is rejected; old HA keeps the existing in-place interval update.
        if not OPTIONS_FLOW_RELOADS:
            entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    except BaseException:
        await coordinator.async_close()
        raise

    return True


async def _async_options_updated(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
) -> None:
    """Apply an options change on HA without OptionsFlowWithReload.

    Intervals change in place. The Modbus poller and the plant binding are
    fixed when the coordinator is built, so a changed gateway address or
    binding reloads the entry; until then the cadence still follows the
    coordinator. Also fires when the coordinator stores its topology in
    entry.data, which changes neither and so never reloads. Local entries
    change their gateway through reconfigure, which reloads on its own.
    """
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    _apply_intervals(coordinator, entry)
    if not is_local_entry(entry.data):
        settings = get_modbus_settings(entry.data, entry.options)
        # Without a gateway before and after, the binding is unused: an entry
        # without Modbus keeps the old behaviour and is never reloaded here.
        if settings != runtime.modbus_settings or (
            settings is not None and get_modbus_plant_id(entry.options) != runtime.modbus_plant_id
        ):
            _LOGGER.debug("Modbus gateway or plant binding changed; reloading the entry")
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return
    _LOGGER.debug("Polling interval updated to %s", coordinator.update_interval)


async def async_unload_entry(hass: HomeAssistant, entry: HovalConnectConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False
    await entry.runtime_data.coordinator.async_close()
    # Remove the integration-level service once the last config entry goes away
    remaining = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != entry.entry_id
    ]
    if not remaining and hass.services.has_service(DOMAIN, SERVICE_RESET_TEMPORARY_CHANGE):
        hass.services.async_remove(DOMAIN, SERVICE_RESET_TEMPORARY_CHANGE)
    return unloaded
