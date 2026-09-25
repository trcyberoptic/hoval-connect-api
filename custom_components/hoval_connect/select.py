"""Select platform for Hoval Connect (program selection)."""

from __future__ import annotations

import logging
from collections import Counter

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .api import HovalApiError, HovalAuthError
from .const import CIRCUIT_TYPE_HK, CIRCUIT_TYPE_HV, CIRCUIT_TYPE_WW, OPERATION_MODE_REGULAR
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator

_LOGGER = logging.getLogger(__name__)

# API program keys in display order
API_PROGRAMS = ["week1", "week2", "ecoMode", "standby", "constant"]

# Full set of program identifiers the cloud accepts on the programs endpoint.
# A resolved key is validated against this before sending so an unmapped
# display string can't be forwarded verbatim to the API (→ HTTP 400).
VALID_API_PROGRAMS = frozenset(
    {"week1", "week2", "ecoMode", "standby", "constant", "manual", "externalConstant"}
)

# Fallback display names when API doesn't provide custom names
DEFAULT_NAMES: dict[str, str] = {
    "week1": "Week 1",
    "week2": "Week 2",
    "ecoMode": "Eco mode",
    "standby": "Standby",
    "constant": "Constant",
}


def resolve_program_display_names(program_names: dict[str, str]) -> dict[str, str]:
    """Give each selectable program a unique label, preserving unique names.

    Reserve the original names before generating suffixes: a custom name such
    as "Standby (standby)" must not collide with a generated standby label.
    """
    names = {}
    for api_key in API_PROGRAMS:
        name = program_names.get(api_key)
        names[api_key] = name if isinstance(name, str) and name else DEFAULT_NAMES[api_key]
    counts = Counter(names.values())
    reserved = set(names.values())
    labels: dict[str, str] = {}
    for api_key, default in names.items():
        label = default
        if counts[default] > 1:
            label = f"{default} ({api_key})"
            suffix = 2
            while label in reserved or label in labels.values():
                label = f"{default} ({api_key} {suffix})"
                suffix += 1
        labels[api_key] = label
    return labels


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
                if (
                    circuit.circuit_type not in (CIRCUIT_TYPE_HV, CIRCUIT_TYPE_HK, CIRCUIT_TYPE_WW)
                    or uid in known
                ):
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

    def _all_display_names(self) -> dict[str, str]:
        """Resolve names together so collisions can be disambiguated."""
        circuit = self._circuit
        return resolve_program_display_names(circuit.program_names if circuit else {})

    def _api_key_from_display(self, display: str) -> str:
        """Resolve a displayed option, accepting raw API keys for automations."""
        return next(
            (key for key, label in self._all_display_names().items() if label == display),
            display,
        )

    @property
    def options(self) -> list[str]:
        """Return list of program display names."""
        return list(self._all_display_names().values())

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
        return self._all_display_names().get(circuit.active_program)

    async def async_select_option(self, option: str) -> None:
        """Set the active program."""
        api_program = self._api_key_from_display(option)
        if api_program not in VALID_API_PROGRAMS:
            raise HomeAssistantError(
                f"Unknown program '{option}' (resolved to '{api_program}'); "
                f"valid programs: {', '.join(sorted(VALID_API_PROGRAMS))}"
            )
        _LOGGER.debug(
            "Setting program to %s (%s) for %s",
            option,
            api_program,
            self._circuit_path,
        )
        mode = OPERATION_MODE_REGULAR if api_program != "standby" else "standby"
        try:
            await self.coordinator.async_control_and_refresh(
                lambda: self.coordinator.api.set_program(
                    self._plant_id,
                    self._circuit_path,
                    api_program,
                ),
                plant_id=self._plant_id,
                circuit_path=self._circuit_path,
                mode_override=mode,
            )
        except (HovalApiError, HovalAuthError) as err:
            raise HomeAssistantError(f"Failed to set program: {err}") from err
