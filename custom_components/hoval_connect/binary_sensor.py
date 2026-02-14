"""Binary sensor platform for Hoval Connect."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry
from .const import DOMAIN
from .coordinator import HovalDataCoordinator, HovalPlantData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval binary sensor entities."""
    coordinator = entry.runtime_data.coordinator

    entities: list[BinarySensorEntity] = []
    for plant_id, plant_data in coordinator.data.plants.items():
        entities.append(HovalPlantOnline(coordinator, plant_id, plant_data))
        entities.append(HovalPlantError(coordinator, plant_id, plant_data))

    async_add_entities(entities)


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, plant_id)},
            name=f"Hoval {plant_data.name}",
            manufacturer="Hoval",
            model="Plant",
        )

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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, plant_id)},
            name=f"Hoval {plant_data.name}",
            manufacturer="Hoval",
            model="Plant",
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if the plant has an active error."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.has_error
