"""Climate platform for Hoval Connect (HV ventilation)."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry
from .const import (
    DOMAIN,
    OPERATION_MODE_CONSTANT,
    OPERATION_MODE_MANUAL,
    OPERATION_MODE_REGULAR,
    OPERATION_MODE_STANDBY,
)
from .coordinator import HovalCircuitData, HovalDataCoordinator, resolve_fan_speed

_LOGGER = logging.getLogger(__name__)

# Map Hoval operation modes to HA HVAC modes
HOVAL_TO_HVAC_MODE: dict[str | None, HVACMode] = {
    OPERATION_MODE_REGULAR: HVACMode.AUTO,
    OPERATION_MODE_CONSTANT: HVACMode.FAN_ONLY,
    OPERATION_MODE_MANUAL: HVACMode.FAN_ONLY,
    OPERATION_MODE_STANDBY: HVACMode.OFF,
    None: HVACMode.AUTO,
}

# Map HA HVAC modes back to Hoval API mode endpoints
HVAC_MODE_TO_HOVAL: dict[HVACMode, str] = {
    HVACMode.AUTO: "reset",
    HVACMode.FAN_ONLY: OPERATION_MODE_CONSTANT,
    HVACMode.OFF: OPERATION_MODE_STANDBY,
}

async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval climate entities."""
    coordinator = entry.runtime_data.coordinator

    entities: list[HovalClimate] = []
    for plant_id, plant_data in coordinator.data.plants.items():
        for path, circuit in plant_data.circuits.items():
            entities.append(
                HovalClimate(coordinator, plant_id, path, circuit)
            )

    async_add_entities(entities)


class HovalClimate(CoordinatorEntity[HovalDataCoordinator], ClimateEntity):
    """Hoval ventilation climate entity."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.FAN_ONLY]
    _attr_supported_features = ClimateEntityFeature(0)

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_climate"
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
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC mode."""
        circuit = self._circuit
        if circuit is None:
            return HVACMode.OFF
        mapped = HOVAL_TO_HVAC_MODE.get(circuit.operation_mode)
        if mapped is None:
            _LOGGER.warning(
                "Unknown operationMode %r, defaulting to AUTO", circuit.operation_mode
            )
            return HVACMode.AUTO
        return mapped

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return current HVAC action."""
        circuit = self._circuit
        if circuit is None:
            return None
        if circuit.operation_mode == OPERATION_MODE_STANDBY:
            return HVACAction.OFF
        return HVACAction.FAN

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature (exhaust air)."""
        circuit = self._circuit
        if circuit is None:
            return None
        val = circuit.live_values.get("exhaustTemp")
        return float(val) if val is not None else None

    @property
    def current_humidity(self) -> int | None:
        """Return the current humidity."""
        circuit = self._circuit
        if circuit is None:
            return None
        val = circuit.live_values.get("humidityActual")
        return int(float(val)) if val is not None else None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode."""
        _LOGGER.debug("async_set_hvac_mode called: %s", hvac_mode)
        mode = HVAC_MODE_TO_HOVAL.get(hvac_mode)
        if mode is None:
            _LOGGER.warning("Unsupported HVAC mode: %s", hvac_mode)
            return
        async with self.coordinator.control_lock:
            # 'constant' mode requires the current air volume as value (min 1%)
            value = None
            if mode == OPERATION_MODE_CONSTANT:
                circuit = self._circuit
                value = resolve_fan_speed(circuit)
            await self.coordinator.api.set_circuit_mode(
                self._plant_id, self._circuit_path, mode, value=value
            )
            await asyncio.sleep(2)
            await self.coordinator.async_request_refresh()


