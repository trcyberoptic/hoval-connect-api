"""Fan platform for Hoval Connect (HV ventilation speed control)."""

from __future__ import annotations

import logging

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry
from .const import DOMAIN, OPERATION_MODE_CONSTANT, OPERATION_MODE_STANDBY
from .coordinator import HovalCircuitData, HovalDataCoordinator

_LOGGER = logging.getLogger(__name__)

SPEED_RANGE = (0, 100)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval fan entities."""
    coordinator = entry.runtime_data.coordinator

    entities: list[HovalFan] = []
    for plant_id, plant_data in coordinator.data.plants.items():
        for path, circuit in plant_data.circuits.items():
            entities.append(
                HovalFan(coordinator, plant_id, path, circuit)
            )

    async_add_entities(entities)


class HovalFan(CoordinatorEntity[HovalDataCoordinator], FanEntity):
    """Hoval ventilation fan entity with percentage speed control."""

    _attr_has_entity_name = True
    _attr_name = "Ventilation"
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_speed_count = 100

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
    ) -> None:
        """Initialize the fan entity."""
        super().__init__(coordinator)
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_fan"
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
    def is_on(self) -> bool | None:
        """Return true if fan is on (not in standby)."""
        circuit = self._circuit
        if circuit is None:
            return None
        return circuit.operation_mode != OPERATION_MODE_STANDBY

    @property
    def percentage(self) -> int | None:
        """Return the current speed percentage (0-100)."""
        circuit = self._circuit
        if circuit is None:
            return None
        val = circuit.live_values.get("airVolume")
        if val is None:
            val = circuit.target_air_volume
        if val is None:
            return None
        return max(0, min(100, int(float(val))))

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed percentage of the fan."""
        _LOGGER.debug("async_set_percentage called: %d%%", percentage)
        if percentage == 0:
            await self.async_turn_off()
            return
        await self.coordinator.api.set_circuit_mode(
            self._plant_id,
            self._circuit_path,
            OPERATION_MODE_CONSTANT,
            value=percentage,
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs,
    ) -> None:
        """Turn on the fan."""
        if percentage is not None:
            await self.async_set_percentage(percentage)
            return
        # Resume with last known speed or default 40%
        circuit = self._circuit
        value = 40
        if circuit:
            val = circuit.live_values.get("airVolume") or circuit.target_air_volume
            if val is not None:
                value = max(1, int(float(val)))
        await self.coordinator.api.set_circuit_mode(
            self._plant_id,
            self._circuit_path,
            OPERATION_MODE_CONSTANT,
            value=value,
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off the fan (standby mode)."""
        await self.coordinator.api.set_circuit_mode(
            self._plant_id,
            self._circuit_path,
            OPERATION_MODE_STANDBY,
        )
        await self.coordinator.async_request_refresh()
