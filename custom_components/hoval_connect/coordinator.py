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
from .const import (
    CIRCUIT_TYPE_BL,
    CIRCUIT_TYPE_PS,
    CIRCUIT_TYPE_WW,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENTS_CACHE_TTL,
    PROGRAM_CACHE_TTL,
    SUPPORTED_CIRCUIT_TYPES,
    WEATHER_CACHE_TTL,
)

SIGNAL_NEW_CIRCUITS = f"{DOMAIN}_new_circuits"

# Maximum lifetime of an optimistic mode override (seconds). Overrides are
# normally cleared at the end of the next successful poll, but if polls keep
# failing an override must not mask the device's real state indefinitely.
_MODE_OVERRIDE_TTL_S = 120.0

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
    programs: dict[str, Any] | None,
    now: datetime,
    active_program: str | None = None,
) -> tuple[str | None, str | None, float | None]:
    """Resolve the currently active week, day program name, and air volume.

    Picks week1 or week2 from the programs blob based on `active_program`
    (the circuit's `activeProgram` field). Falls back to week1 if the active
    program is not a weekly schedule (ecoMode, standby, constant, manual, …)
    or unset — the resolved day/phase is only meaningful when a weekly program
    is actually running, so callers should treat the values as best-effort
    informational in that case.

    Defensive against schema drift: non-programmable circuits (e.g. BL/boiler)
    may yield None (HTTP 204) or an empty JSON array [] from the programs
    endpoint, and any nested field may have an unexpected shape. Every such
    case degrades to None fields instead of raising — an exception here used
    to propagate out of _fetch_circuit and silently drop the whole circuit
    (including its already-fetched live values).

    Returns (week_name, day_program_name, current_phase_value).
    """
    if not isinstance(programs, dict):
        return None, None, None
    day_programs = programs.get("dayPrograms")
    if not isinstance(day_programs, dict):
        return None, None, None
    day_configs = day_programs.get("dayConfigurations")
    if not isinstance(day_configs, list) or not day_configs:
        return None, None, None

    # Build lookup: id -> day config. Entries that are not dicts or lack an
    # "id" are skipped instead of raising.
    config_by_id: dict[Any, dict] = {
        d["id"]: d for d in day_configs if isinstance(d, dict) and "id" in d
    }

    # Pick week1 or week2 based on what the controller reports as active.
    week_key = "week2" if active_program == "week2" else "week1"
    week = programs.get(week_key)
    if not isinstance(week, dict):
        # Week entry missing or wrong shape — no week/day info resolvable.
        return None, None, None
    week_name = week.get("name")
    day_program_ids = week.get("dayProgramIds")
    if not isinstance(day_program_ids, list):
        day_program_ids = []

    # weekday: 0=Monday in Python, dayProgramIds[0]=Monday in Hoval
    weekday = now.weekday()
    if weekday >= len(day_program_ids):
        return week_name, None, None

    day_prog_id = day_program_ids[weekday]
    day_config = config_by_id.get(day_prog_id)
    if day_config is None:
        return week_name, None, None

    day_name = day_config.get("name")

    # Find active phase based on current time. Malformed phases (non-dict,
    # missing/non-dict start or end, non-numeric times) are skipped, not fatal.
    current_minutes = now.hour * 60 + now.minute
    phases = day_config.get("phases")
    if not isinstance(phases, list):
        phases = []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        start = phase.get("start")
        end = phase.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict):
            continue
        try:
            start_min = int(start["hours"]) * 60 + int(start["minutes"])
            end_min = int(end["hours"]) * 60 + int(end["minutes"])
        except (KeyError, TypeError, ValueError):
            continue
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
    # HV: air-volume percentage; HK: target temperature in °C. Coming from the
    # circuit list endpoint's `targetValue` (renamed from v1 `targetAirVolume`).
    target_value: float | None = None
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


def _is_problem_event(event: HovalEventData | None) -> bool:
    """Return True if event is active and represents a fault (blocking/locking/warning)."""
    return bool(
        event
        and event.is_active
        and event.event_type
        in (
            "blocking",
            "locking",
            "warning",
        )
    )


DEFAULT_FAN_SPEED = 40


def resolve_fan_speed(circuit: HovalCircuitData | None) -> int:
    """Resolve the best fan speed value for constant mode.

    Fallback chain: live airVolume → targetValue → program air volume → default.
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
    if circuit.target_value is not None:
        speed = int(circuit.target_value)
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
        # cleared at the END of the next successful poll or after
        # _MODE_OVERRIDE_TTL_S). Key: circuit_path,
        # value: (operation mode string, monotonic timestamp).
        self._mode_override: dict[str, tuple[str, float]] = {}
        # Program cache: key=circuit_path, value=(programs_data, timestamp)
        self._program_cache: dict[str, tuple[Any, float]] = {}
        self._program_cache_ttl = PROGRAM_CACHE_TTL.total_seconds()
        # Plant-level caches: (parsed value(s), monotonic timestamp)
        self._weather_cache: dict[str, tuple[HovalWeatherData | None, float]] = {}
        self._weather_cache_ttl = WEATHER_CACHE_TTL.total_seconds()
        self._events_cache: dict[
            str, tuple[HovalEventData | None, list[HovalEventData], float]
        ] = {}
        self._events_cache_ttl = EVENTS_CACHE_TTL.total_seconds()
        # Track known circuits for dynamic entity discovery
        self._known_circuits: set[str] = set()

    def set_mode_override(self, circuit_path: str, mode: str) -> None:
        """Set optimistic mode override after a control action."""
        self._mode_override[circuit_path] = (mode, time.monotonic())

    def get_mode_override(self, circuit_path: str) -> str | None:
        """Get the optimistic mode override for a circuit.

        Returns None once the override exceeds _MODE_OVERRIDE_TTL_S so a stale
        optimistic value cannot mask the device's real state when polls fail.
        """
        entry = self._mode_override.get(circuit_path)
        if entry is None:
            return None
        mode, ts = entry
        if time.monotonic() - ts > _MODE_OVERRIDE_TTL_S:
            self._mode_override.pop(circuit_path, None)
            return None
        return mode

    async def async_control_and_refresh(
        self,
        coro: Any,
        circuit_path: str,
        mode_override: str,
    ) -> None:
        """Execute a control command with lock, optimistic state, and refresh.

        The API call and optimistic override are serialised inside
        control_lock; the refresh runs as a fire-and-forget background task
        OUTSIDE the lock (with a 2 s settle delay) so the calling entity
        method returns promptly and a slow refresh cannot starve the lock.
        A failed background refresh is dropped — the coordinator retries on
        its normal poll schedule and entities stay on their optimistic state.
        """
        async with self.control_lock:
            await coro
            self.set_mode_override(circuit_path, mode_override)

        async def _do_refresh() -> None:
            await asyncio.sleep(2)
            try:
                await self.async_request_refresh()
            except Exception:  # noqa: BLE001 — see docstring
                _LOGGER.debug(
                    "Post-control refresh failed for %s; coordinator will retry on next poll",
                    circuit_path,
                )

        self.hass.async_create_task(_do_refresh())

    async def _async_update_data(self) -> HovalData:
        """Fetch data from the API."""
        # Timestamp BEFORE any fetch: overrides set after this instant belong
        # to control actions this poll's data snapshot cannot reflect yet, so
        # the pruning at the end must leave them alone.
        poll_start = time.monotonic()
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

                # Fetch circuits. A persistent failure here is the most common
                # symptom of an upstream API change (the v1 endpoint removal in
                # April 2026 was masked for days because we used to swallow this
                # error). Log loudly and let DataUpdateCoordinator surface the
                # failure to the user as `unavailable` entities.
                try:
                    circuits_raw = await self.api.get_circuits(plant_id)
                except HovalApiError as err:
                    _LOGGER.error(
                        "Circuits endpoint failed for plant %s: %s — entities will go "
                        "unavailable until the cloud API recovers or the integration is "
                        "updated.",
                        plant_id,
                        err,
                    )
                    raise

                # BL/WW/PS circuits have selectable=False but still provide live values
                _non_selectable_types = {CIRCUIT_TYPE_BL, CIRCUIT_TYPE_WW, CIRCUIT_TYPE_PS}

                _LOGGER.debug(
                    "Fetched %d circuits (%d supported)",
                    len(circuits_raw),
                    sum(
                        1
                        for c in circuits_raw
                        if c.get("type") in SUPPORTED_CIRCUIT_TYPES
                        and (c.get("selectable") or c.get("type") in _non_selectable_types)
                    ),
                )

                # Build list of supported circuits
                supported_circuits: list[tuple[str, str, dict]] = []
                for circuit in circuits_raw:
                    ctype = circuit.get("type", "")
                    if ctype not in SUPPORTED_CIRCUIT_TYPES:
                        continue
                    if not circuit.get("selectable", False) and ctype not in _non_selectable_types:
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
                    air_quality = circuit.get("airQuality") or {}
                    circuit_data = HovalCircuitData(
                        circuit_type=ctype,
                        path=path,
                        name=circuit.get("name") or ctype,
                        operation_mode=circuit.get("operationMode"),
                        active_program=_V1_PROGRAM_MAP.get(raw_program, raw_program),
                        target_value=circuit.get("targetValue"),
                        is_air_quality_guided=bool(air_quality.get("isAirQualityGuided")),
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
                        lv_raw = results[0]
                        # api.get_live_values() already normalises the wrapper;
                        # one lightweight guard against future shape regressions.
                        if not isinstance(lv_raw, list):
                            _LOGGER.warning(
                                "Live-values for %s returned unexpected type %s; treating as empty",
                                path,
                                type(lv_raw).__name__,
                            )
                            lv_raw = []
                        circuit_data.live_values = {
                            v["key"]: v["value"]
                            for v in lv_raw
                            if isinstance(v, dict) and "key" in v and "value" in v
                        }
                        _LOGGER.debug("Circuit %s live_values: %s", path, circuit_data.live_values)
                    else:
                        _LOGGER.debug("Live values not available for %s", path)

                    programs = results[1]
                    if isinstance(programs, dict):
                        if need_programs:
                            self._program_cache[path] = (programs, time.time())
                        # Isolation barrier: any residual exception here must
                        # degrade the program fields only — never propagate out
                        # of _fetch_circuit, which would discard the whole
                        # circuit (incl. its live values) via
                        # gather(return_exceptions=True).
                        try:
                            now = dt_util.now()
                            week_name, day_name, phase_value = _resolve_active_program_value(
                                programs, now, circuit_data.active_program
                            )
                            circuit_data.active_week_name = week_name
                            circuit_data.active_day_program_name = day_name
                            circuit_data.program_air_volume = phase_value
                            # Extract user-defined program names
                            w1 = programs.get("week1")
                            w2 = programs.get("week2")
                            if isinstance(w1, dict) and w1.get("name"):
                                circuit_data.program_names["week1"] = w1["name"]
                            if isinstance(w2, dict) and w2.get("name"):
                                circuit_data.program_names["week2"] = w2["name"]
                        except Exception:  # noqa: BLE001 — see isolation note
                            _LOGGER.warning(
                                "Program data for circuit %s could not be parsed; "
                                "program sensors will be unknown this cycle "
                                "(live values are unaffected)",
                                path,
                                exc_info=True,
                            )
                    elif isinstance(programs, BaseException):
                        _LOGGER.debug("Programs not available for %s: %s", path, programs)
                    else:
                        _LOGGER.debug(
                            "Programs endpoint for %s returned %r (type=%s); skipping",
                            path,
                            programs,
                            type(programs).__name__,
                        )

                    return circuit_data

                # Run circuits in parallel. Plant-level events/weather are only
                # appended when their cache is stale (they are slow-changing and
                # plant-scoped, so fetching them every poll wastes round-trips).
                all_tasks = [
                    _fetch_circuit(path, ctype, circ) for path, ctype, circ in supported_circuits
                ]
                num_circuits = len(all_tasks)
                now_mono = time.monotonic()

                events_cached = self._events_cache.get(plant_id)
                need_events = (
                    events_cached is None or now_mono - events_cached[2] > self._events_cache_ttl
                )
                latest_idx = events_idx = None
                if need_events:
                    latest_idx = len(all_tasks)
                    all_tasks.append(self.api.get_latest_event(plant_id))
                    events_idx = len(all_tasks)
                    all_tasks.append(self.api.get_events(plant_id))

                weather_cached = self._weather_cache.get(plant_id)
                need_weather = (
                    weather_cached is None or now_mono - weather_cached[1] > self._weather_cache_ttl
                )
                weather_idx = None
                if need_weather:
                    weather_idx = len(all_tasks)
                    all_tasks.append(self.api.get_weather(plant_id))

                all_results = await asyncio.gather(
                    *all_tasks,
                    return_exceptions=True,
                )

                # Process circuit results
                for result in all_results[:num_circuits]:
                    if isinstance(result, BaseException):
                        _LOGGER.debug("Circuit fetch failed: %s", result)
                        continue
                    if result.has_error:
                        plant_data.has_error = True
                    plant_data.circuits[result.path] = result

                # --- Events (latest + list), cached together ---
                if need_events:
                    latest_result = all_results[latest_idx]
                    events_result = all_results[events_idx]
                    parsed_latest = None
                    parsed_events: list[HovalEventData] = []
                    # Isolation barrier: this block runs OUTSIDE the per-circuit
                    # gather's exception isolation, so a shape surprise here
                    # (e.g. a pagination wrapper reaching the list slice) would
                    # fail the ENTIRE poll and take every entity unavailable.
                    # The API client now normalises both event endpoints; the
                    # isinstance guards and try/except below are defence in
                    # depth for anything it hasn't seen yet.
                    try:
                        if isinstance(latest_result, BaseException):
                            _LOGGER.debug("Events endpoint not available for %s", plant_id)
                        elif isinstance(latest_result, dict) and latest_result:
                            parsed_latest = _parse_event(latest_result)
                            _LOGGER.debug(
                                "Latest event: type=%s active=%s desc=%s",
                                parsed_latest.event_type,
                                parsed_latest.is_active,
                                parsed_latest.description,
                            )
                        if isinstance(events_result, BaseException):
                            _LOGGER.debug("Events list not available for %s", plant_id)
                        elif isinstance(events_result, list) and events_result:
                            parsed_events = [
                                _parse_event(ev)
                                for ev in events_result[:10]
                                if isinstance(ev, dict)
                            ]
                    except Exception:  # noqa: BLE001 — events must never fail the poll
                        _LOGGER.warning(
                            "Event data for plant %s could not be parsed; "
                            "reusing cached events for this cycle",
                            plant_id,
                            exc_info=True,
                        )
                        parsed_latest = None
                        parsed_events = []
                    # Refresh the cache only when we got something; on a total
                    # miss reuse the previous cache (if any) rather than wiping
                    # good data.
                    if parsed_latest is not None or parsed_events:
                        self._events_cache[plant_id] = (parsed_latest, parsed_events, now_mono)
                    elif events_cached is not None:
                        parsed_latest, parsed_events, _ = events_cached
                else:
                    parsed_latest, parsed_events, _ = events_cached

                plant_data.latest_event = parsed_latest
                plant_data.events = list(parsed_events)
                if parsed_latest is not None and _is_problem_event(parsed_latest):
                    plant_data.has_error = True
                else:
                    for ev in parsed_events:
                        if _is_problem_event(ev):
                            plant_data.has_error = True
                            break

                # --- Weather forecast, cached ---
                if need_weather:
                    weather_result = all_results[weather_idx]
                    parsed_weather = None
                    if (
                        not isinstance(weather_result, BaseException)
                        and isinstance(weather_result, list)
                        and weather_result
                        # First forecast element must be a dict (defence in depth)
                        and isinstance(weather_result[0], dict)
                    ):
                        w = weather_result[0]
                        parsed_weather = HovalWeatherData(
                            weather_type=w.get("weatherType"),
                            outside_temperature=w.get("outsideTemperature"),
                            outside_temperature_min=w.get("outsideTemperatureMin"),
                        )
                    elif isinstance(weather_result, BaseException):
                        _LOGGER.debug("Weather not available for %s", plant_id)
                    if parsed_weather is not None:
                        self._weather_cache[plant_id] = (parsed_weather, now_mono)
                    elif weather_cached is not None:
                        parsed_weather = weather_cached[0]
                else:
                    parsed_weather = weather_cached[0]
                plant_data.weather = parsed_weather

                data.plants[plant_id] = plant_data

        except HovalAuthError as err:
            raise ConfigEntryAuthFailed("Authentication failed — check credentials") from err
        except HovalApiError as err:
            raise UpdateFailed("Error fetching Hoval data") from err

        # Detect new circuits for dynamic entity discovery.
        # Fire on any newly seen circuit, including the first one. Skipping the
        # initial set (when `_known_circuits` was still empty) used to leave
        # circuits stranded if the very first refresh came back without them
        # — async_setup_entry's _add_new() ran against an empty circuits dict
        # and the dispatcher then suppressed the catch-up signal. Each platform
        # already deduplicates via its `known` set, so firing on the first
        # discovery is a no-op when entities are already present.
        current_circuits = {
            f"{pid}_{path}" for pid, plant in data.plants.items() for path in plant.circuits
        }
        new_circuits = current_circuits - self._known_circuits
        if new_circuits:
            _LOGGER.info("New circuits discovered: %s", new_circuits)
            async_dispatcher_send(self.hass, SIGNAL_NEW_CIRCUITS)
        self._known_circuits = current_circuits

        # Clear optimistic overrides only after a SUCCESSFUL fetch — fresh data
        # replaces them. Clearing at the start meant a failed refresh snapped
        # entities back to stale pre-override data. Prune ONLY overrides set
        # before this poll began: an unconditional clear() would also wipe an
        # override set by a control action while the poll was in flight, and
        # the poll's pre-change data snapshot would snap the entity back to
        # its old state. Mid-poll overrides survive until a poll that STARTED
        # after them succeeds (or the TTL in get_mode_override expires).
        self._mode_override = {
            path: entry for path, entry in self._mode_override.items() if entry[1] >= poll_start
        }
        return data
