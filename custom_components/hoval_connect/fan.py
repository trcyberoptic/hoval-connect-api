"""Fan platform for Hoval Connect (HV ventilation speed control)."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .api import HovalApiError
from .const import (
    CIRCUIT_TYPE_HV,
    CONF_OVERRIDE_DURATION,
    CONF_TURN_ON_MODE,
    DEFAULT_OVERRIDE_DURATION,
    DEFAULT_TURN_ON_MODE,
    OPERATION_MODE_REGULAR,
    OPERATION_MODE_STANDBY,
    TURN_ON_RESUME,
)
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator

_LOGGER = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval fan entities."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[HovalFan] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            for path, circuit in plant_data.circuits.items():
                uid = f"{plant_id}_{path}_fan"
                if circuit.circuit_type != CIRCUIT_TYPE_HV or uid in known:
                    continue
                known.add(uid)
                entities.append(HovalFan(coordinator, entry, plant_id, path, circuit))
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalFan(CoordinatorEntity[HovalDataCoordinator], FanEntity):
    """Hoval ventilation fan entity with percentage speed control."""

    _attr_has_entity_name = True
    _attr_translation_key = "ventilation"
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    )
    _attr_speed_count = 100

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        entry: HovalConnectConfigEntry,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
    ) -> None:
        """Initialize the fan entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_fan"
        self._attr_device_info = circuit_device_info(plant_id, circuit_data)
        self._debounce_task: asyncio.Task | None = None
        self._pending_percentage: int | None = None

    def _cancel_debounce(self) -> None:
        """Cancel any pending debounce task safely."""
        task = self._debounce_task
        if task is not None and not task.done():
            task.cancel()
        self._debounce_task = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending debounce task on removal."""
        self._cancel_debounce()
        await super().async_will_remove_from_hass()

    @property
    def _override_duration(self) -> str:
        """Get override duration enum from options (FOUR or MIDNIGHT)."""
        return self._entry.options.get(CONF_OVERRIDE_DURATION, DEFAULT_OVERRIDE_DURATION)

    @property
    def _turn_on_mode(self) -> str:
        """Get turn-on mode from options (resume, week1, week2)."""
        return self._entry.options.get(CONF_TURN_ON_MODE, DEFAULT_TURN_ON_MODE)

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
        override = self.coordinator.get_mode_override(self._circuit_path)
        mode = override if override is not None else circuit.operation_mode
        return mode != OPERATION_MODE_STANDBY

    @property
    def percentage(self) -> int | None:
        """Return the current speed percentage (0-100)."""
        # Show pending value immediately for responsive UI
        if self._pending_percentage is not None:
            return self._pending_percentage
        circuit = self._circuit
        if circuit is None:
            return None
        val = circuit.live_values.get("airVolume")
        if val is None:
            val = circuit.target_air_volume
        if val is None:
            return None
        return max(0, min(100, int(float(val))))

    async def _send_percentage(self, percentage: int) -> None:
        """Actually send the percentage to the API (called after debounce)."""
        self._pending_percentage = None
        try:
            await self.coordinator.async_control_and_refresh(
                self.coordinator.api.set_temporary_change(
                    self._plant_id,
                    self._circuit_path,
                    value=percentage,
                    duration=self._override_duration,
                ),
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set fan speed: {err}") from err

    async def _debounced_set(self, percentage: int) -> None:
        """Wait for debounce period, then send the latest percentage."""
        await asyncio.sleep(DEBOUNCE_SECONDS)
        _LOGGER.debug("Debounce complete, sending %d%%", percentage)
        await self._send_percentage(percentage)

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed percentage of the fan (debounced)."""
        _LOGGER.debug("async_set_percentage called: %d%%", percentage)
        if percentage == 0:
            self._cancel_debounce()
            self._pending_percentage = None
            await self.async_turn_off()
            return
        # Store pending value and update UI immediately
        self._pending_percentage = percentage
        self.async_write_ha_state()
        # Cancel previous debounce timer
        self._cancel_debounce()
        # Start new debounce timer
        self._debounce_task = self.hass.async_create_task(self._debounced_set(percentage))

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
        mode = self._turn_on_mode
        if mode == TURN_ON_RESUME:
            coro = self.coordinator.api.reset_circuit(
                self._plant_id,
                self._circuit_path,
            )
        else:
            coro = self.coordinator.api.set_program(
                self._plant_id,
                self._circuit_path,
                mode,
            )
        try:
            await self.coordinator.async_control_and_refresh(
                coro,
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to turn on fan: {err}") from err

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off the fan (standby mode)."""
        try:
            await self.coordinator.async_control_and_refresh(
                self.coordinator.api.set_circuit_mode(
                    self._plant_id,
                    self._circuit_path,
                    OPERATION_MODE_STANDBY,
                ),
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_STANDBY,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to turn off fan: {err}") from err
