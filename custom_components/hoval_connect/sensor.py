"""Sensor platform for Hoval Connect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info, plant_device_info
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator, HovalPlantData


@dataclass(frozen=True, kw_only=True)
class HovalSensorEntityDescription(SensorEntityDescription):
    """Describe a Hoval sensor entity."""

    value_fn: Callable[[HovalCircuitData], Any | None]


@dataclass(frozen=True, kw_only=True)
class HovalPlantSensorEntityDescription(SensorEntityDescription):
    """Describe a Hoval plant-level sensor entity."""

    value_fn: Callable[[HovalPlantData], Any | None]


CIRCUIT_SENSOR_DESCRIPTIONS: tuple[HovalSensorEntityDescription, ...] = (
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
    HovalSensorEntityDescription(
        key="operation_mode",
        translation_key="operation_mode",
        icon="mdi:cog",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.operation_mode,
    ),
    HovalSensorEntityDescription(
        key="active_week_program",
        translation_key="active_week_program",
        icon="mdi:calendar-week",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.active_week_name,
    ),
    HovalSensorEntityDescription(
        key="active_day_program",
        translation_key="active_day_program",
        icon="mdi:calendar-today",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.active_day_program_name,
    ),
    HovalSensorEntityDescription(
        key="program_air_volume",
        translation_key="program_air_volume",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.program_air_volume,
    ),
)

PLANT_SENSOR_DESCRIPTIONS: tuple[HovalPlantSensorEntityDescription, ...] = (
    HovalPlantSensorEntityDescription(
        key="latest_event_type",
        translation_key="latest_event_type",
        icon="mdi:alert-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.latest_event.event_type if p.latest_event else None,
    ),
    HovalPlantSensorEntityDescription(
        key="latest_event_message",
        translation_key="latest_event_message",
        icon="mdi:message-alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.latest_event.description if p.latest_event else None,
    ),
    HovalPlantSensorEntityDescription(
        key="latest_event_time",
        translation_key="latest_event_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.latest_event.time_occurred if p.latest_event else None,
    ),
    HovalPlantSensorEntityDescription(
        key="active_events",
        translation_key="active_events",
        icon="mdi:alert",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: sum(1 for e in p.events if e.is_active),
    ),
    HovalPlantSensorEntityDescription(
        key="weather_condition",
        translation_key="weather_condition",
        icon="mdi:weather-partly-cloudy",
        value_fn=lambda p: p.weather.weather_type if p.weather else None,
    ),
    HovalPlantSensorEntityDescription(
        key="weather_temperature",
        translation_key="weather_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: p.weather.outside_temperature if p.weather else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval sensor entities."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[SensorEntity] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            # Circuit-level sensors
            for path, circuit in plant_data.circuits.items():
                for description in CIRCUIT_SENSOR_DESCRIPTIONS:
                    uid = f"{plant_id}_{path}_{description.key}"
                    if uid in known:
                        continue
                    known.add(uid)
                    entities.append(
                        HovalCircuitSensor(coordinator, plant_id, path, circuit, description)
                    )

            # Plant-level sensors
            for description in PLANT_SENSOR_DESCRIPTIONS:
                uid = f"{plant_id}_{description.key}"
                if uid in known:
                    continue
                known.add(uid)
                entities.append(HovalPlantSensor(coordinator, plant_id, plant_data, description))

        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalCircuitSensor(CoordinatorEntity[HovalDataCoordinator], SensorEntity):
    """Hoval circuit sensor entity."""

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
    def native_value(self) -> float | str | None:
        """Return the sensor value."""
        circuit = self._circuit
        if circuit is None:
            return None
        val = self.entity_description.value_fn(circuit)
        if val is None:
            return None
        # String sensors (program names, operation mode) return as-is
        if self.entity_description.native_unit_of_measurement is None:
            return str(val)
        try:
            return float(val)
        except (ValueError, TypeError):
            return None


class HovalPlantSensor(CoordinatorEntity[HovalDataCoordinator], SensorEntity):
    """Hoval plant-level sensor entity."""

    _attr_has_entity_name = True
    entity_description: HovalPlantSensorEntityDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_data: HovalPlantData,
        description: HovalPlantSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._plant_id = plant_id
        self._attr_unique_id = f"{plant_id}_{description.key}"
        self._attr_device_info = plant_device_info(plant_data)

    @property
    def _plant(self) -> HovalPlantData | None:
        """Get current plant data from coordinator."""
        return self.coordinator.data.plants.get(self._plant_id)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self._plant is not None

    @property
    def native_value(self) -> float | str | None:
        """Return the sensor value."""
        plant = self._plant
        if plant is None:
            return None
        val = self.entity_description.value_fn(plant)
        if val is None:
            return None
        if self.entity_description.native_unit_of_measurement is None and not isinstance(
            val, (int, float)
        ):
            return str(val)
        try:
            return float(val) if isinstance(val, (int, float)) else val
        except (ValueError, TypeError):
            return None
