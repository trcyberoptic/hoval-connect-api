"""Select platform for Hoval Connect (program selection)."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .const import OPERATION_MODE_REGULAR
from .coordinator import HovalCircuitData, HovalDataCoordinator

_LOGGER = logging.getLogger(__name__)

# Programs available via the API
PROGRAM_OPTIONS = ["week1", "week2", "ecoMode", "standby", "constant"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval select entities."""
    coordinator = entry.runtime_data.coordinator

    entities: list[HovalProgramSelect] = []
    for plant_id, plant_data in coordinator.data.plants.items():
        for path, circuit in plant_data.circuits.items():
            entities.append(
                HovalProgramSelect(coordinator, plant_id, path, circuit)
            )

    async_add_entities(entities)


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
        return circuit.active_program

    async def async_select_option(self, option: str) -> None:
        """Set the active program."""
        _LOGGER.debug(
            "Setting program to %s for %s", option, self._circuit_path,
        )
        async with self.coordinator.control_lock:
            await self.coordinator.api.set_program(
                self._plant_id, self._circuit_path, option,
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
