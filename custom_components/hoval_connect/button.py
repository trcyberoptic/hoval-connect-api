"""Button platform for Hoval Connect (HV acknowledge commands over Modbus)."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .const import CIRCUIT_TYPE_HV, OPERATION_MODE_REGULAR, OPERATION_MODE_STANDBY
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator
from .modbus import HovalModbusError


@dataclass(frozen=True, kw_only=True)
class HovalButtonDescription(ButtonEntityDescription):
    """Acknowledge button writing one value to the `acknowledge` register."""

    unique_suffix: str
    value: int
    action: str


BUTTON_DESCRIPTIONS: tuple[HovalButtonDescription, ...] = (
    HovalButtonDescription(
        key="acknowledge_maintenance",
        translation_key="acknowledge_maintenance",
        unique_suffix="acknowledge_maintenance",
        value=7,
        action="acknowledge maintenance",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:wrench-check",
    ),
    HovalButtonDescription(
        key="acknowledge_filter_cleaning",
        translation_key="acknowledge_filter_cleaning",
        unique_suffix="acknowledge_filter_cleaning",
        value=8,
        action="acknowledge filter cleaning",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:air-filter",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval acknowledge buttons for circuits served over Modbus."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[HovalAcknowledgeButton] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            for path, circuit in plant_data.circuits.items():
                if circuit.circuit_type != CIRCUIT_TYPE_HV or circuit.modbus is None:
                    continue
                for description in BUTTON_DESCRIPTIONS:
                    uid = f"{plant_id}_{path}_{description.unique_suffix}"
                    if uid in known:
                        continue
                    known.add(uid)
                    entities.append(
                        HovalAcknowledgeButton(coordinator, plant_id, path, circuit, description)
                    )
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalAcknowledgeButton(CoordinatorEntity[HovalDataCoordinator], ButtonEntity):
    """Acknowledge a maintenance or filter-cleaning notice on the unit."""

    _attr_has_entity_name = True
    entity_description: HovalButtonDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
        description: HovalButtonDescription,
    ) -> None:
        """Initialize the button."""
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

    async def async_press(self) -> None:
        """Write the acknowledge value and refresh."""
        description = self.entity_description
        circuit = self._circuit
        if circuit is None or circuit.modbus is None:
            raise HomeAssistantError(f"Failed to {description.action}: no Modbus connection")

        async def write() -> None:
            try:
                await self.coordinator.async_hv_write(
                    self._plant_id, self._circuit_path, "acknowledge", description.value
                )
            except (ValueError, HovalModbusError) as err:
                raise HomeAssistantError(
                    f"Failed to {description.action} (value {description.value}): {err}"
                ) from err

        # Acknowledging leaves the running program alone: keep the mode shown now.
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
