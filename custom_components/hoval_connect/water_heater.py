"""Water heater platform for Hoval Connect (WW hot water circuits)."""

from __future__ import annotations

import logging

from homeassistant.components.water_heater import (
    STATE_HEAT_PUMP,
    STATE_HIGH_DEMAND,
    STATE_OFF,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .api import HovalApiError
from .const import (
    CIRCUIT_TYPE_WW,
    DURATION_MIDNIGHT,
    OPERATION_MODE_REGULAR,
    OPERATION_MODE_STANDBY,
)
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator

_LOGGER = logging.getLogger(__name__)

# Temperature limits for WW circuits (°C).
# The Hoval app allows 10–65 °C; we use a safe operational range.
WW_MIN_TEMP = 10.0
WW_MAX_TEMP = 65.0
WW_TEMP_STEP = 0.5

# Operation modes exposed to HA
_OP_HEAT_PUMP = STATE_HEAT_PUMP  # "heat_pump"  — normal week-program operation
_OP_HIGH_DEMAND = STATE_HIGH_DEMAND  # "high_demand" — temporary boost override active
_OP_OFF = STATE_OFF  # "off"         — circuit in standby


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval water heater entities for WW circuits."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[HovalWaterHeater] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            for path, circuit in plant_data.circuits.items():
                uid = f"{plant_id}_{path}_water_heater"
                if circuit.circuit_type != CIRCUIT_TYPE_WW or uid in known:
                    continue
                known.add(uid)
                entities.append(HovalWaterHeater(coordinator, entry, plant_id, path, circuit))
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))

    # No per-platform reset service here: the integration-level
    # hoval_connect.reset_temporary_change service already resolves any
    # circuit-bound entity (including water_heater) to its plant/circuit.


class HovalWaterHeater(CoordinatorEntity[HovalDataCoordinator], WaterHeaterEntity):
    """Hoval hot water circuit entity.

    Exposes:
    - current_temperature  — live top-of-tank sensor (tempSf1Actual)
    - target_temperature   — active setpoint (tempTarget from live values)
    - operation_mode       — heat_pump (normal) / high_demand (boost override) / off (standby)
    - set_temperature()    — posts a temporary-change override until midnight
    - set_operation_mode() — switches between heat_pump (reset to week program) and off (standby)
    """

    _attr_has_entity_name = True
    _attr_translation_key = "hot_water"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = WW_MIN_TEMP
    _attr_max_temp = WW_MAX_TEMP
    _attr_target_temperature_step = WW_TEMP_STEP
    _attr_operation_list = [_OP_HEAT_PUMP, _OP_HIGH_DEMAND, _OP_OFF]
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE | WaterHeaterEntityFeature.OPERATION_MODE
    )

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        entry: HovalConnectConfigEntry,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
    ) -> None:
        """Initialize the water heater entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_water_heater"
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
    def current_temperature(self) -> float | None:
        """Return current water temperature (top-of-tank sensor)."""
        circuit = self._circuit
        if circuit is None:
            return None
        val = circuit.live_values.get("tempSf1Actual") or circuit.live_values.get("tempActual")
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                return None
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return target water temperature."""
        circuit = self._circuit
        if circuit is None:
            return None
        val = circuit.live_values.get("tempTarget")
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def current_operation(self) -> str:
        """Return current operation mode."""
        circuit = self._circuit
        if circuit is None:
            return _OP_OFF
        override = self.coordinator.get_mode_override(self._circuit_path)
        mode = override if override is not None else circuit.operation_mode
        if mode == OPERATION_MODE_STANDBY:
            return _OP_OFF
        # If a temporary change is active, show as high_demand
        if circuit.live_values.get("temporaryChangeActive") == "true":
            return _OP_HIGH_DEMAND
        return _OP_HEAT_PUMP

    async def async_set_temperature(self, **kwargs) -> None:
        """Apply a temporary temperature override until midnight.

        DURATION_MIDNIGHT maps to the v4 temporary-change duration that
        automatically expires at 00:00, so the regular week program resumes
        the next day without any cleanup automation.
        """
        temperature = kwargs.get("temperature")
        if temperature is None:
            return
        _LOGGER.debug(
            "WW set_temperature: circuit=%s temp=%s (override until midnight)",
            self._circuit_path,
            temperature,
        )
        try:
            await self.coordinator.async_control_and_refresh(
                self.coordinator.api.set_temporary_change(
                    self._plant_id,
                    self._circuit_path,
                    value=float(temperature),
                    duration=DURATION_MIDNIGHT,
                ),
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set hot water temperature: {err}") from err

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Switch operation mode."""
        try:
            if operation_mode == _OP_OFF:
                await self.coordinator.async_control_and_refresh(
                    self.coordinator.api.set_program(
                        self._plant_id,
                        self._circuit_path,
                        "standby",
                    ),
                    circuit_path=self._circuit_path,
                    mode_override=OPERATION_MODE_STANDBY,
                )
            elif operation_mode in (_OP_HEAT_PUMP, _OP_HIGH_DEMAND):
                # Reset to the normal week program
                await self.coordinator.async_control_and_refresh(
                    self.coordinator.api.reset_circuit(
                        self._plant_id,
                        self._circuit_path,
                    ),
                    circuit_path=self._circuit_path,
                    mode_override=OPERATION_MODE_REGULAR,
                )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set operation mode: {err}") from err
