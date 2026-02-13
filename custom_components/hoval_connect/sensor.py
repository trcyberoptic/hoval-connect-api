"""Sensor platform for Hoval Connect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry
from .const import DOMAIN
from .coordinator import HovalCircuitData, HovalDataCoordinator


@dataclass(frozen=True, kw_only=True)
class HovalSensorEntityDescription(SensorEntityDescription):
    """Describe a Hoval sensor entity."""

    value_fn: Callable[[HovalCircuitData], str | None]


SENSOR_DESCRIPTIONS: tuple[HovalSensorEntityDescription, ...] = (
    HovalSensorEntityDescription(
        key="outside_temperature",
        translation_key="outside_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.live_values.get("outsideTemperature"),
    ),
    HovalSensorEntityDescription(
        key="exhaust_temperature",
        translation_key="exhaust_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.live_values.get("exhaustTemp"),
    ),
    HovalSensorEntityDescription(
        key="air_volume",
        translation_key="air_volume",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan",
        value_fn=lambda c: c.live_values.get("airVolume"),
    ),
    HovalSensorEntityDescription(
        key="humidity_actual",
        translation_key="humidity_actual",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.live_values.get("humidityActual"),
    ),
    HovalSensorEntityDescription(
        key="humidity_target",
        translation_key="humidity_target",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.live_values.get("humidityTarget"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval sensor entities."""
    coordinator = entry.runtime_data.coordinator

    entities: list[HovalSensor] = []
    for plant_id, plant_data in coordinator.data.plants.items():
        for path, circuit in plant_data.circuits.items():
            for description in SENSOR_DESCRIPTIONS:
                entities.append(
                    HovalSensor(coordinator, plant_id, path, circuit, description)
                )

    async_add_entities(entities)


class HovalSensor(CoordinatorEntity[HovalDataCoordinator], SensorEntity):
    """Hoval sensor entity."""

    _attr_has_entity_name = True
    entity_description: HovalSensorEntityDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
        description: HovalSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{plant_id}_{circuit_path}")},
            name=f"Hoval {circuit_data.name}",
            manufacturer="Hoval",
            model=f"HomeVent ({circuit_data.circuit_type})",
            via_device=(DOMAIN, plant_id),
        )

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
    def native_value(self) -> float | None:
        """Return the sensor value."""
        circuit = self._circuit
        if circuit is None:
            return None
        val = self.entity_description.value_fn(circuit)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None
