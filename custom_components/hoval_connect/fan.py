"""Fan platform for Hoval Connect (HV ventilation speed control)."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry
from .const import (
    CONF_OVERRIDE_DURATION,
    CONF_TURN_ON_MODE,
    DEFAULT_OVERRIDE_DURATION,
    DEFAULT_TURN_ON_MODE,
    DOMAIN,
    OPERATION_MODE_STANDBY,
    TURN_ON_RESUME,
)
from .coordinator import HovalCircuitData, HovalDataCoordinator

_LOGGER = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.5


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
                HovalFan(coordinator, entry, plant_id, path, circuit)
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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{plant_id}_{circuit_path}")},
            name=f"Hoval {circuit_data.name}",
            manufacturer="Hoval",
            model=f"HomeVent ({circuit_data.circuit_type})",
            via_device=(DOMAIN, plant_id),
        )
        self._debounce_task: asyncio.Task | None = None
        self._pending_percentage: int | None = None

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
        async with self.coordinator.control_lock:
            await self.coordinator.api.set_temporary_change(
                self._plant_id,
                self._circuit_path,
                value=percentage,
                duration=self._override_duration,
            )
            # Clear standby override — fan is now running
            self.coordinator.set_mode_override(self._circuit_path, "REGULAR")
            self.async_write_ha_state()
            await asyncio.sleep(2)
            await self.coordinator.async_request_refresh()

    async def _debounced_set(self, percentage: int) -> None:
        """Wait for debounce period, then send the latest percentage."""
        await asyncio.sleep(DEBOUNCE_SECONDS)
        _LOGGER.debug("Debounce complete, sending %d%%", percentage)
        await self._send_percentage(percentage)

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed percentage of the fan (debounced)."""
        _LOGGER.debug("async_set_percentage called: %d%%", percentage)
        if percentage == 0:
            # Cancel any pending debounce
            if self._debounce_task and not self._debounce_task.done():
                self._debounce_task.cancel()
                self._debounce_task = None
                self._pending_percentage = None
            await self.async_turn_off()
            return
        # Store pending value and update UI immediately
        self._pending_percentage = percentage
        self.async_write_ha_state()
        # Cancel previous debounce timer
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        # Start new debounce timer
        self._debounce_task = asyncio.ensure_future(
            self._debounced_set(percentage)
        )

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
        async with self.coordinator.control_lock:
            mode = self._turn_on_mode
            if mode == TURN_ON_RESUME:
                await self.coordinator.api.reset_circuit(
                    self._plant_id, self._circuit_path,
                )
            else:
                # week1 or week2 — activate specific program
                await self.coordinator.api.set_program(
                    self._plant_id, self._circuit_path, mode,
                )
            # Clear standby override — fan is now running
            self.coordinator.set_mode_override(self._circuit_path, "REGULAR")
            self.async_write_ha_state()
            await asyncio.sleep(2)
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off the fan (standby mode)."""
        async with self.coordinator.control_lock:
            await self.coordinator.api.set_circuit_mode(
                self._plant_id,
                self._circuit_path,
                OPERATION_MODE_STANDBY,
            )
            self.coordinator.set_mode_override(
                self._circuit_path, OPERATION_MODE_STANDBY
            )
            self.async_write_ha_state()
            await asyncio.sleep(2)
            await self.coordinator.async_request_refresh()
