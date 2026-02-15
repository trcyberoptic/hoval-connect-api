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

from . import HovalConnectConfigEntry, plant_device_info
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalDataCoordinator, HovalPlantData


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
