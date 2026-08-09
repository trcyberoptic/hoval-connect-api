"""Binary sensor platform for Hoval Connect."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info, plant_device_info
from .coordinator import (
    SIGNAL_NEW_CIRCUITS,
    HovalCircuitData,
    HovalDataCoordinator,
    HovalPlantData,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval binary sensor entities."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[BinarySensorEntity] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            uid_online = f"{plant_id}_online"
            uid_error = f"{plant_id}_error"
            if uid_online not in known:
                known.add(uid_online)
                entities.append(HovalPlantOnline(coordinator, plant_id, plant_data))
            if uid_error not in known:
                known.add(uid_error)
                entities.append(HovalPlantError(coordinator, plant_id, plant_data))
            for path, circuit in plant_data.circuits.items():
                uid_tc = f"{plant_id}_{path}_temporary_change"
                if uid_tc not in known:
                    known.add(uid_tc)
                    entities.append(HovalTemporaryChange(coordinator, plant_id, path, circuit))
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalPlantOnline(CoordinatorEntity[HovalDataCoordinator], BinarySensorEntity):
    """Binary sensor for plant online status."""

    _attr_has_entity_name = True
    _attr_translation_key = "plant_online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_data: HovalPlantData,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._plant_id = plant_id
        self._attr_unique_id = f"{plant_id}_online"
        self._attr_device_info = plant_device_info(plant_data)

    @property
    def is_on(self) -> bool | None:
        """Return true if the plant is online."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.is_online


class HovalPlantError(CoordinatorEntity[HovalDataCoordinator], BinarySensorEntity):
    """Binary sensor for plant error status."""

    _attr_has_entity_name = True
    _attr_translation_key = "plant_error"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_data: HovalPlantData,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._plant_id = plant_id
        self._attr_unique_id = f"{plant_id}_error"
        self._attr_device_info = plant_device_info(plant_data)

    @property
    def is_on(self) -> bool | None:
        """Return true if the plant has an active error."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.has_error


class HovalTemporaryChange(CoordinatorEntity[HovalDataCoordinator], BinarySensorEntity):
    """Whether a temporary change (boost / override) is running on this circuit.

    The cloud reports the override itself, so automations that start one do not
    need to remember that they did — this replaces the input_boolean/input_datetime
    bookkeeping the bundled summer-boost Blueprint used to require.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "temporary_change"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_temporary_change"
        self._attr_device_info = circuit_device_info(plant_id, circuit_data)

    @property
    def _circuit(self) -> HovalCircuitData | None:
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.circuits.get(self._circuit_path)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self._circuit is not None

    @property
    def is_on(self) -> bool | None:
        """Return true while an override is active."""
        circuit = self._circuit
        if circuit is None:
            return None
        return circuit.temporary_change_end is not None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose the override's target value and type while one is running."""
        circuit = self._circuit
        if circuit is None or circuit.temporary_change_end is None:
            return None
        return {
            "value": circuit.temporary_change_value,
            "type": circuit.temporary_change_type,
            "end": circuit.temporary_change_end,
        }
