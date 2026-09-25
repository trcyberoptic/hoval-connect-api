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
from .api import HovalApiError, HovalAuthError
from .const import (
    CIRCUIT_TYPE_HV,
    HV_AIR_VOLUME_MAX,
    HV_AIR_VOLUME_MIN,
    OPERATION_MODE_REGULAR,
    OPERATION_MODE_STANDBY,
    TURN_ON_RESUME,
    clamp_hv_air_volume,
)
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator
from .options import get_override_duration, get_turn_on_mode

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
        self._committed_task: asyncio.Task | None = None
        self._pending_percentage: int | None = None
        self._pending_request: object | None = None

    def _clear_pending_percentage(self, request: object) -> None:
        """Only the request that owns the displayed value may clear it."""
        if self._pending_request is request:
            self._pending_percentage = None
            self._pending_request = None
            self.async_write_ha_state()

    def _cancel_debounce(self) -> None:
        """Cancel the timer, keeping already-started writes in command order."""
        task = self._debounce_task
        if task is not None and not task.done() and task is not self._committed_task:
            task.cancel()
        self._debounce_task = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending debounce task on removal."""
        self._cancel_debounce()
        await super().async_will_remove_from_hass()

    @property
    def _override_duration(self) -> str:
        """Get override duration enum from options."""
        return get_override_duration(self._entry.options)

    @property
    def _turn_on_mode(self) -> str:
        """Get turn-on mode from options (resume, week1, week2)."""
        return get_turn_on_mode(self._entry.options)

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
        override = self.coordinator.get_mode_override(self._plant_id, self._circuit_path)
        mode = override if override is not None else circuit.operation_mode
        if mode is None:
            return None
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
        # Prefer the setpoint (target_value) over the live measurement
        # (airVolume): the slider reflects what the user has set; live
        # airVolume lags as the fan physically ramps to the new target.
        val = circuit.target_value
        if val is None:
            val = circuit.live_values.get("airVolume")
        if val is None:
            return None
        return max(0, min(100, int(float(val))))

    async def _send_percentage(self, percentage: int, request: object) -> None:
        """Actually send the percentage to the API (called after debounce).

        Keeps `_pending_percentage` set across the whole API call + refresh
        so the slider does not snap back to the stale, ~30-second-old
        coordinator data during the in-flight request. Clears it only after
        the refresh has fetched the new setpoint from Hoval.

        The value is clamped into the HV device band before sending: HA allows
        1-14 %, which the cloud rejects or handles undefined. 0 never reaches
        here (handled as turn_off in async_set_percentage).
        """
        clamped = clamp_hv_air_volume(percentage)
        if clamped != percentage:
            _LOGGER.debug(
                "Clamped requested air volume %d%% to device band %d-%d%% → %d%%",
                percentage,
                HV_AIR_VOLUME_MIN,
                HV_AIR_VOLUME_MAX,
                clamped,
            )
        try:
            await self.coordinator.async_control_and_refresh(
                lambda: self.coordinator.api.set_temporary_change(
                    self._plant_id,
                    self._circuit_path,
                    value=clamped,
                    duration=self._override_duration,
                ),
                plant_id=self._plant_id,
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except (HovalApiError, HovalAuthError) as err:
            # HovalAuthError is not a HovalApiError subclass — without it here
            # an auth failure would escape the fire-and-forget debounce task
            # unwrapped and leave the slider frozen at the pending value.
            # Revert pending so UI shows actual circuit state on failure,
            # but only if the user has not queued a newer value in between.
            self._clear_pending_percentage(request)
            raise HomeAssistantError(f"Failed to set fan speed: {err}") from err
        # A later request may ask for the same value (30 -> 60 -> 30), so
        # comparing percentages cannot establish ownership of pending state.
        self._clear_pending_percentage(request)

    async def _debounced_set(self, percentage: int, request: object) -> None:
        """Wait for debounce period, then send the latest percentage.

        Runs as a fire-and-forget task, so a raised HomeAssistantError would
        only reach the event loop's unhandled-task logger. Log it at WARNING
        instead — _send_percentage has already reverted the pending state.
        """
        await asyncio.sleep(DEBOUNCE_SECONDS)
        _LOGGER.debug("Debounce complete, sending %d%%", percentage)
        # Cancelling after this point could release the control lock while
        # the cloud is still processing this write. New commands queue behind it.
        self._committed_task = asyncio.current_task()
        try:
            await self._send_percentage(percentage, request)
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Setting fan speed to %d%% failed for circuit %s: %s — "
                "the slider reverts to the device's actual value",
                percentage,
                self._circuit_path,
                err,
            )
        finally:
            if self._committed_task is asyncio.current_task():
                self._committed_task = None

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed percentage of the fan (debounced)."""
        if (
            not isinstance(percentage, int)
            or isinstance(percentage, bool)
            or not 0 <= percentage <= 100
        ):
            raise HomeAssistantError(
                f"Invalid fan percentage: {percentage!r}; expected an integer from 0 to 100"
            )
        _LOGGER.debug("async_set_percentage called: %d%%", percentage)
        if percentage == 0:
            await self.async_turn_off()
            return
        percentage = clamp_hv_air_volume(percentage)
        # Store pending value and update UI immediately
        request = self._pending_request = object()
        self._pending_percentage = percentage
        self.async_write_ha_state()
        # Cancel previous debounce timer
        self._cancel_debounce()
        # Start new debounce timer
        self._debounce_task = self.hass.async_create_task(self._debounced_set(percentage, request))

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
        self._cancel_debounce()
        self._pending_percentage = None
        self._pending_request = None
        self.async_write_ha_state()
        mode = self._turn_on_mode

        def action():
            if mode == TURN_ON_RESUME:
                return self.coordinator.api.reset_circuit(
                    self._plant_id,
                    self._circuit_path,
                    program=self.coordinator.resolve_resume_program(
                        self._plant_id, self._circuit_path
                    ),
                )
            return self.coordinator.api.set_program(self._plant_id, self._circuit_path, mode)

        try:
            await self.coordinator.async_control_and_refresh(
                action,
                plant_id=self._plant_id,
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except (HovalApiError, HovalAuthError) as err:
            raise HomeAssistantError(f"Failed to turn on fan: {err}") from err

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off the fan (standby mode)."""
        self._cancel_debounce()
        request = self._pending_request = object()
        self._pending_percentage = 0
        self.async_write_ha_state()
        try:
            await self.coordinator.async_control_and_refresh(
                lambda: self.coordinator.api.set_circuit_mode(
                    self._plant_id,
                    self._circuit_path,
                    OPERATION_MODE_STANDBY,
                ),
                plant_id=self._plant_id,
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_STANDBY,
            )
        except (HovalApiError, HovalAuthError) as err:
            raise HomeAssistantError(f"Failed to turn off fan: {err}") from err
        finally:
            self._clear_pending_percentage(request)
