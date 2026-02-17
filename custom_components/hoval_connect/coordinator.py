"""Data coordinator for Hoval Connect."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import HovalApiError, HovalAuthError, HovalConnectApi
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, PROGRAM_CACHE_TTL, SUPPORTED_CIRCUIT_TYPES

SIGNAL_NEW_CIRCUITS = f"{DOMAIN}_new_circuits"

_LOGGER = logging.getLogger(__name__)

# v1 API returns different activeProgram values than v3.
# Normalize so entities always see v3 enum keys.
_V1_PROGRAM_MAP: dict[str, str] = {
    "tteControlled": "week1",  # time program active (v1 doesn't say which week)
    "timePrograms": "week1",
    "nightReduction": "week1",
    "dayCooling": "week1",
}


def _resolve_active_program_value(
    programs: dict[str, Any], now: datetime
) -> tuple[str | None, str | None, float | None]:
    """Resolve the currently active week, day program name, and air volume.

    Returns (week_name, day_program_name, current_phase_value).
    """
    day_programs = programs.get("dayPrograms", {})
    day_configs = day_programs.get("dayConfigurations", [])
    if not day_configs:
        return None, None, None

    # Build lookup: id -> day config
    config_by_id: dict[int, dict] = {d["id"]: d for d in day_configs}

    # Determine which week is active (week1 by default)
    week = programs.get("week1", {})
    week_name = week.get("name")
    day_program_ids = week.get("dayProgramIds", [])

    # weekday: 0=Monday in Python, dayProgramIds[0]=Monday in Hoval
    weekday = now.weekday()
    if weekday >= len(day_program_ids):
        return week_name, None, None

    day_prog_id = day_program_ids[weekday]
    day_config = config_by_id.get(day_prog_id)
    if day_config is None:
        return week_name, None, None

    day_name = day_config.get("name")

    # Find active phase based on current time
    current_minutes = now.hour * 60 + now.minute
    for phase in day_config.get("phases", []):
        start = phase["start"]
        end = phase["end"]
        start_min = start["hours"] * 60 + start["minutes"]
        end_min = end["hours"] * 60 + end["minutes"]
        if start_min <= current_minutes < end_min:
            return week_name, day_name, phase.get("value")

    return week_name, day_name, None


@dataclass
class HovalEventData:
    """Parsed data for a plant event."""

    event_type: str | None = None
    description: str | None = None
    time_occurred: str | None = None
    time_resolved: str | None = None
    source_path: str | None = None
    code: int | None = None

    @property
    def is_active(self) -> bool:
        """Event is active when it has not been resolved."""
        return self.time_resolved is None


@dataclass
class HovalCircuitData:
    """Parsed data for a single circuit."""

    circuit_type: str
    path: str
    name: str
    operation_mode: str | None = None
    active_program: str | None = None
    target_air_volume: int | None = None
    target_air_humidity: int | None = None
    is_air_quality_guided: bool = False
    has_error: bool = False
    live_values: dict[str, str] = field(default_factory=dict)
    active_week_name: str | None = None
    active_day_program_name: str | None = None
    program_air_volume: float | None = None
    # User-defined program names: API key → display name (e.g. "week1" → "Normal")
    program_names: dict[str, str] = field(default_factory=dict)


@dataclass
class HovalWeatherData:
    """Parsed weather forecast data for a plant."""

    weather_type: str | None = None
    outside_temperature: float | None = None
    outside_temperature_min: float | None = None


@dataclass
class HovalPlantData:
    """Parsed data for a single plant."""

    plant_id: str
    name: str
    is_online: bool = True
    has_error: bool = False
    circuits: dict[str, HovalCircuitData] = field(default_factory=dict)
    latest_event: HovalEventData | None = None
    events: list[HovalEventData] = field(default_factory=list)
    weather: HovalWeatherData | None = None


@dataclass
class HovalData:
    """Top-level data returned by the coordinator."""

    plants: dict[str, HovalPlantData] = field(default_factory=dict)


def _parse_event(raw: dict) -> HovalEventData:
    """Parse a PlantEventDTO dict into HovalEventData."""
    return HovalEventData(
        event_type=raw.get("eventType"),
        description=raw.get("description"),
        time_occurred=raw.get("timeOccurred"),
        time_resolved=raw.get("timeResolved"),
        source_path=raw.get("sourcePath"),
        code=raw.get("code"),
    )


DEFAULT_FAN_SPEED = 40


def resolve_fan_speed(circuit: HovalCircuitData | None) -> int:
    """Resolve the best fan speed value for constant mode.

    Fallback chain: live airVolume → targetAirVolume → program air volume → default.
    Always returns at least 1 (API rejects value=0).
    """
    if circuit is None:
        return DEFAULT_FAN_SPEED
    # Try live sensor value first
    val = circuit.live_values.get("airVolume")
    if val is not None:
        speed = int(float(val))
        if speed >= 1:
            return speed
    # Try target from circuit config
    if circuit.target_air_volume is not None:
        speed = int(circuit.target_air_volume)
        if speed >= 1:
            return speed
    # Try the currently active time program phase value
    if circuit.program_air_volume is not None:
        speed = int(circuit.program_air_volume)
        if speed >= 1:
            return speed
    return DEFAULT_FAN_SPEED


class HovalDataCoordinator(DataUpdateCoordinator[HovalData]):
    """Coordinator to fetch data from Hoval Connect API."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: HovalConnectApi,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.api = api
        self.control_lock = asyncio.Lock()
        # Optimistic mode override per circuit (set by control actions,
        # cleared on next poll). Key: circuit_path, value: operation mode string.
        self._mode_override: dict[str, str] = {}
        # Program cache: key=circuit_path, value=(programs_data, timestamp)
        self._program_cache: dict[str, tuple[Any, float]] = {}
        self._program_cache_ttl = PROGRAM_CACHE_TTL.total_seconds()
        # Track known circuits for dynamic entity discovery
        self._known_circuits: set[str] = set()

    def set_mode_override(self, circuit_path: str, mode: str) -> None:
        """Set optimistic mode override after a control action."""
        self._mode_override[circuit_path] = mode

    def get_mode_override(self, circuit_path: str) -> str | None:
        """Get the optimistic mode override for a circuit."""
        return self._mode_override.get(circuit_path)

    async def async_control_and_refresh(
        self,
        coro: Any,
        circuit_path: str,
        mode_override: str,
    ) -> None:
        """Execute a control command with lock, optimistic state, and refresh.

        Serializes the API call via control_lock, sets the optimistic mode
        override, and triggers a coordinator refresh after a short delay.
        """
        async with self.control_lock:
            await coro
            self.set_mode_override(circuit_path, mode_override)
            await asyncio.sleep(2)
            await self.async_request_refresh()

    async def _async_update_data(self) -> HovalData:
        """Fetch data from the API."""
        # Clear optimistic overrides — fresh data replaces them
        self._mode_override.clear()
        data = HovalData()

        try:
            plants = await self.api.get_plants()

            for plant in plants:
                plant_id = plant.get("plantExternalId")
                if not plant_id:
                    _LOGGER.debug("Skipping plant with missing plantExternalId")
                    continue

                plant_name = plant.get("description", plant_id)

                plant_data = HovalPlantData(
                    plant_id=plant_id,
                    name=plant_name,
                    is_online=plant.get("isOnline", True),
                )

                # Skip all API calls when plant is offline
                if not plant_data.is_online:
                    # Invalidate cached PAT so we get a fresh token when back
                    self.api.invalidate_plant_token(plant_id)
                    data.plants[plant_id] = plant_data
                    continue

                # Fetch circuits
                try:
                    circuits_raw = await self.api.get_circuits(plant_id)
                except HovalApiError:
                    _LOGGER.warning("Circuits endpoint not available for plant %s", plant_id)
                    circuits_raw = []

                _LOGGER.debug(
                    "Fetched %d circuits (%d supported)",
                    len(circuits_raw),
                    sum(
                        1
                        for c in circuits_raw
                        if c.get("type") in SUPPORTED_CIRCUIT_TYPES and c.get("selectable")
                    ),
                )

                # Build list of supported circuits
                supported_circuits: list[tuple[str, str, dict]] = []
                for circuit in circuits_raw:
                    ctype = circuit.get("type", "")
                    if ctype not in SUPPORTED_CIRCUIT_TYPES:
                        continue
                    if not circuit.get("selectable", False):
                        continue
                    path = circuit["path"]
                    _LOGGER.debug(
                        "Circuit %s raw: %s",
                        path,
                        {k: v for k, v in circuit.items() if k != "name"},
                    )
                    supported_circuits.append((path, ctype, circuit))

                # Fetch live values + programs for all circuits in parallel
                async def _fetch_circuit(
                    path: str,
                    ctype: str,
                    circuit: dict,
                    _plant_id: str = plant_id,
                ) -> HovalCircuitData:
                    raw_program = circuit.get("activeProgram")
                    circuit_data = HovalCircuitData(
                        circuit_type=ctype,
                        path=path,
                        name=circuit.get("name") or ctype,
                        operation_mode=circuit.get("operationMode"),
                        active_program=_V1_PROGRAM_MAP.get(raw_program, raw_program),
                        target_air_volume=circuit.get("targetAirVolume"),
                        target_air_humidity=circuit.get("targetAirHumidity"),
                        is_air_quality_guided=circuit.get("isAirQualityGuided", False),
                        has_error=circuit.get("hasError", False),
                    )

                    # Check program cache
                    cached_prog = self._program_cache.get(path)
                    need_programs = (
                        cached_prog is None
                        or time.time() - cached_prog[1] > self._program_cache_ttl
                    )

                    # Fetch live values (always) + programs (only if cache expired)
                    live_task = self.api.get_live_values(_plant_id, path, ctype)
                    if need_programs:
                        prog_task = self.api.get_programs(_plant_id, path)
                        results = await asyncio.gather(
                            live_task,
                            prog_task,
                            return_exceptions=True,
                        )
                    else:
                        live_result = await asyncio.gather(
                            live_task,
                            return_exceptions=True,
                        )
                        results = [live_result[0], cached_prog[0]]

                    if not isinstance(results[0], BaseException):
                        circuit_data.live_values = {v["key"]: v["value"] for v in results[0]}
                        _LOGGER.debug("Circuit %s live_values: %s", path, circuit_data.live_values)
                    else:
                        _LOGGER.debug("Live values not available for %s", path)

                    programs = results[1]
                    if not isinstance(programs, BaseException):
                        if need_programs:
                            self._program_cache[path] = (programs, time.time())
                        now = dt_util.now()
                        week_name, day_name, phase_value = _resolve_active_program_value(
                            programs, now
                        )
                        circuit_data.active_week_name = week_name
                        circuit_data.active_day_program_name = day_name
                        circuit_data.program_air_volume = phase_value
                        # Extract user-defined program names
                        w1 = programs.get("week1", {})
                        w2 = programs.get("week2", {})
                        if w1.get("name"):
                            circuit_data.program_names["week1"] = w1["name"]
                        if w2.get("name"):
                            circuit_data.program_names["week2"] = w2["name"]
                    else:
                        _LOGGER.debug("Programs not available for %s", path)

                    return circuit_data

                # Run circuits, events, and weather all in parallel
                all_tasks = [
                    _fetch_circuit(path, ctype, circ) for path, ctype, circ in supported_circuits
                ]
                # Append plant-level tasks (events + weather)
                latest_idx = len(all_tasks)
                all_tasks.append(self.api.get_latest_event(plant_id))
                events_idx = len(all_tasks)
                all_tasks.append(self.api.get_events(plant_id))
                weather_idx = len(all_tasks)
                all_tasks.append(self.api.get_weather(plant_id))

                all_results = await asyncio.gather(
                    *all_tasks,
                    return_exceptions=True,
                )

                # Process circuit results
                for result in all_results[:latest_idx]:
                    if isinstance(result, BaseException):
                        _LOGGER.debug("Circuit fetch failed: %s", result)
                        continue
                    if result.has_error:
                        plant_data.has_error = True
                    plant_data.circuits[result.path] = result

                # Process latest event
                latest_result = all_results[latest_idx]
                if not isinstance(latest_result, BaseException) and latest_result:
                    plant_data.latest_event = _parse_event(latest_result)
                    _LOGGER.debug(
                        "Latest event: type=%s active=%s desc=%s",
                        plant_data.latest_event.event_type,
                        plant_data.latest_event.is_active,
                        plant_data.latest_event.description,
                    )
                elif isinstance(latest_result, BaseException):
                    _LOGGER.debug("Events endpoint not available for %s", plant_id)

                # Process events list
                events_result = all_results[events_idx]
                if not isinstance(events_result, BaseException) and events_result:
                    for ev in events_result[:10]:
                        plant_data.events.append(_parse_event(ev))
                    for ev in plant_data.events:
                        if ev.is_active and ev.event_type in (
                            "blocking",
                            "locking",
                            "warning",
                        ):
                            plant_data.has_error = True
                            break
                elif isinstance(events_result, BaseException):
                    _LOGGER.debug("Events list not available for %s", plant_id)

                # Process weather forecast
                weather_result = all_results[weather_idx]
                if not isinstance(weather_result, BaseException) and weather_result:
                    if isinstance(weather_result, list) and weather_result:
                        w = weather_result[0]
                        plant_data.weather = HovalWeatherData(
                            weather_type=w.get("weatherType"),
                            outside_temperature=w.get("outsideTemperature"),
                            outside_temperature_min=w.get("outsideTemperatureMin"),
                        )
                elif isinstance(weather_result, BaseException):
                    _LOGGER.debug("Weather not available for %s", plant_id)

                data.plants[plant_id] = plant_data

        except HovalAuthError as err:
            raise ConfigEntryAuthFailed("Authentication failed — check credentials") from err
        except HovalApiError as err:
            raise UpdateFailed("Error fetching Hoval data") from err

        # Detect new circuits for dynamic entity discovery
        current_circuits = {
            f"{pid}_{path}" for pid, plant in data.plants.items() for path in plant.circuits
        }
        new_circuits = current_circuits - self._known_circuits
        if self._known_circuits and new_circuits:
            _LOGGER.info("New circuits discovered: %s", new_circuits)
            async_dispatcher_send(self.hass, SIGNAL_NEW_CIRCUITS)
        self._known_circuits = current_circuits

        return data
