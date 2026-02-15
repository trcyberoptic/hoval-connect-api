"""Select platform for Hoval Connect (program selection)."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .const import CIRCUIT_TYPE_HK, CIRCUIT_TYPE_HV, OPERATION_MODE_REGULAR
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator

_LOGGER = logging.getLogger(__name__)

# Mapping: HA option key (lowercase) → API program name
OPTION_TO_API = {
    "week1": "week1",
    "week2": "week2",
    "eco_mode": "ecoMode",
    "standby": "standby",
    "constant": "constant",
}
API_TO_OPTION = {v: k for k, v in OPTION_TO_API.items()}
PROGRAM_OPTIONS = list(OPTION_TO_API.keys())


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval select entities."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[HovalProgramSelect] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            for path, circuit in plant_data.circuits.items():
                uid = f"{plant_id}_{path}_program"
                if circuit.circuit_type not in (CIRCUIT_TYPE_HV, CIRCUIT_TYPE_HK) or uid in known:
                    continue
                known.add(uid)
                entities.append(
                    HovalProgramSelect(coordinator, plant_id, path, circuit)
                )
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits)
    )


class HovalProgramSelect(CoordinatorEntity[HovalDataCoordinator], SelectEntity):
    """Select entity for choosing the active program on a circuit."""

    _attr_has_entity_name = True
    _attr_translation_key = "program"
    _attr_icon = "mdi:format-list-bulleted"
    _attr_options = PROGRAM_OPTIONS

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
    ) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_program"
        self._attr_device_info = circuit_device_info(plant_id, circuit_data)

    @property
    def _circuit(self) -> HovalCircuitData | None:
        """Get current circuit data from coordinator."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.circuits.get(self._circuit_path)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self._circuit is not None

    @property
    def current_option(self) -> str | None:
        """Return the currently active program."""
        circuit = self._circuit
        if circuit is None:
            return None
        return API_TO_OPTION.get(circuit.active_program, circuit.active_program)

    async def async_select_option(self, option: str) -> None:
        """Set the active program."""
        api_program = OPTION_TO_API.get(option, option)
        _LOGGER.debug(
            "Setting program to %s (%s) for %s", option, api_program, self._circuit_path,
        )
        async with self.coordinator.control_lock:
            await self.coordinator.api.set_program(
                self._plant_id, self._circuit_path, api_program,
            )
            if option != "standby":
                self.coordinator.set_mode_override(
                    self._circuit_path, OPERATION_MODE_REGULAR,
                )
            else:
                self.coordinator.set_mode_override(
                    self._circuit_path, "standby",
                )
            self.async_write_ha_state()
            await asyncio.sleep(2)
            await self.coordinator.async_request_refresh()
