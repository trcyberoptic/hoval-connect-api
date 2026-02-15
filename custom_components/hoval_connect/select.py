"""Select platform for Hoval Connect (program selection)."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .api import HovalApiError
from .const import CIRCUIT_TYPE_HK, CIRCUIT_TYPE_HV, OPERATION_MODE_REGULAR
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator

_LOGGER = logging.getLogger(__name__)

# API program keys in display order
API_PROGRAMS = ["week1", "week2", "ecoMode", "standby", "constant"]

# Fallback display names when API doesn't provide custom names
DEFAULT_NAMES: dict[str, str] = {
    "week1": "Week 1",
    "week2": "Week 2",
    "ecoMode": "Eco mode",
    "standby": "Standby",
    "constant": "Constant",
}


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
                entities.append(HovalProgramSelect(coordinator, plant_id, path, circuit))
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalProgramSelect(CoordinatorEntity[HovalDataCoordinator], SelectEntity):
    """Select entity for choosing the active program on a circuit."""

    _attr_has_entity_name = True
    _attr_translation_key = "program"
    _attr_icon = "mdi:format-list-bulleted"

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

    def _display_name(self, api_key: str) -> str:
        """Get display name for an API program key."""
        circuit = self._circuit
        if circuit and api_key in circuit.program_names:
            return circuit.program_names[api_key]
        return DEFAULT_NAMES.get(api_key, api_key)

    def _api_key_from_display(self, display: str) -> str:
        """Reverse-lookup: display name → API key."""
        circuit = self._circuit
        if circuit:
            for key, name in circuit.program_names.items():
                if name == display:
                    return key
        for key, name in DEFAULT_NAMES.items():
            if name == display:
                return key
        return display

    @property
    def options(self) -> list[str]:
        """Return list of program display names."""
        return [self._display_name(k) for k in API_PROGRAMS]

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self._circuit is not None

    @property
    def current_option(self) -> str | None:
        """Return the currently active program's display name."""
        circuit = self._circuit
        if circuit is None or circuit.active_program is None:
            return None
        return self._display_name(circuit.active_program)

    async def async_select_option(self, option: str) -> None:
        """Set the active program."""
        api_program = self._api_key_from_display(option)
        _LOGGER.debug(
            "Setting program to %s (%s) for %s",
            option,
            api_program,
            self._circuit_path,
        )
        mode = OPERATION_MODE_REGULAR if api_program != "standby" else "standby"
        try:
            await self.coordinator.async_control_and_refresh(
                self.coordinator.api.set_program(
                    self._plant_id,
                    self._circuit_path,
                    api_program,
                ),
                circuit_path=self._circuit_path,
                mode_override=mode,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set program: {err}") from err
