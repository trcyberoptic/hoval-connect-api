"""Number platform for Hoval Connect (HV setpoints written over Modbus)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .const import CIRCUIT_TYPE_HV, OPERATION_MODE_REGULAR, OPERATION_MODE_STANDBY
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator
from .modbus import HovalModbusError
from .modbus_hv import HvModbusReading


@dataclass(frozen=True, kw_only=True)
class HovalNumberDescription(NumberEntityDescription):
    """Setpoint number backed by one writable HV register."""

    register: str
    unique_suffix: str
    value_fn: Callable[[HvModbusReading], int | None]


# Ranges match the write validation in modbus_hv (the poller re-checks them).
NUMBER_DESCRIPTIONS: tuple[HovalNumberDescription, ...] = (
    HovalNumberDescription(
        key="constant_modulation_setpoint",
        translation_key="constant_modulation_setpoint",
        register="constant_modulation",
        unique_suffix="constant_modulation_setpoint",
        value_fn=lambda r: r.constant_modulation,
        native_min_value=15,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
        icon="mdi:fan",
    ),
    HovalNumberDescription(
        key="eco_modulation_setpoint",
        translation_key="eco_modulation_setpoint",
        register="eco_modulation",
        unique_suffix="eco_modulation_setpoint",
        value_fn=lambda r: r.eco_modulation,
        native_min_value=15,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
        icon="mdi:fan-chevron-down",
    ),
    HovalNumberDescription(
        key="humidity_setpoint",
        translation_key="humidity_setpoint",
        register="humidity_target",
        unique_suffix="humidity_setpoint",
        value_fn=lambda r: r.humidity_target,
        native_min_value=30,
        native_max_value=65,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
        icon="mdi:water-percent",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval setpoint numbers for circuits served over Modbus."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[HovalSetpointNumber] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            for path, circuit in plant_data.circuits.items():
                if circuit.circuit_type != CIRCUIT_TYPE_HV or circuit.modbus is None:
                    continue
                for description in NUMBER_DESCRIPTIONS:
                    uid = f"{plant_id}_{path}_{description.unique_suffix}"
                    if uid in known:
                        continue
                    known.add(uid)
                    entities.append(
                        HovalSetpointNumber(coordinator, plant_id, path, circuit, description)
                    )
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalSetpointNumber(CoordinatorEntity[HovalDataCoordinator], NumberEntity):
    """Writable setpoint of an HV circuit (percent, over Modbus)."""

    _attr_has_entity_name = True
    entity_description: HovalNumberDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
        description: HovalNumberDescription,
    ) -> None:
        """Initialize the setpoint number."""
        super().__init__(coordinator)
        self.entity_description = description
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_{description.unique_suffix}"
        self._attr_device_info = circuit_device_info(coordinator, plant_id, circuit_data)

    @property
    def _circuit(self) -> HovalCircuitData | None:
        plant = self.coordinator.data.plants.get(self._plant_id)
        return None if plant is None else plant.circuits.get(self._circuit_path)

    @property
    def available(self) -> bool:
        """Available while a Modbus reading exists for the circuit."""
        circuit = self._circuit
        return super().available and circuit is not None and circuit.modbus is not None

    @property
    def native_value(self) -> float | None:
        """Current setpoint from the Modbus reading."""
        circuit = self._circuit
        if circuit is None or circuit.modbus is None:
            return None
        return self.entity_description.value_fn(circuit.modbus)

    async def async_set_native_value(self, value: float) -> None:
        """Validate, write over Modbus and refresh."""
        description = self.entity_description
        low, high = description.native_min_value, description.native_max_value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise HomeAssistantError(f"Invalid value {value!r} for {description.register}")
        number = int(round(value))
        if not low <= number <= high:
            raise HomeAssistantError(
                f"Value {value} for {description.register} is outside {low:g}-{high:g} %"
            )
        circuit = self._circuit
        if circuit is None or circuit.modbus is None:
            raise HomeAssistantError(f"Failed to set {description.register}: no Modbus connection")

        async def write() -> None:
            try:
                await self.coordinator.async_hv_write(
                    self._plant_id, self._circuit_path, description.register, number
                )
            except (ValueError, HovalModbusError) as err:
                raise HomeAssistantError(
                    f"Failed to set {description.register} to {number}: {err}"
                ) from err

        # A setpoint does not change the running program: keep the mode shown now.
        mode = (
            OPERATION_MODE_STANDBY
            if circuit.active_program == "standby"
            else OPERATION_MODE_REGULAR
        )
        await self.coordinator.async_control_and_refresh(
            write,
            plant_id=self._plant_id,
            circuit_path=self._circuit_path,
            mode_override=mode,
            cloud_action=False,
        )
