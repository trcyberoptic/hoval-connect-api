"""Data coordinator for Hoval Connect."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from math import isfinite
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import HovalApiError, HovalAuthError, HovalConnectApi, _require_identifier
from .const import (
    CIRCUIT_TYPE_BL,
    CIRCUIT_TYPE_HK,
    CIRCUIT_TYPE_HV,
    CIRCUIT_TYPE_NAMES,
    CIRCUIT_TYPE_PS,
    CIRCUIT_TYPE_WW,
    DATA_TOPOLOGY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENTS_CACHE_TTL,
    PROGRAM_CACHE_TTL,
    PROGRAM_UNAVAILABLE_RETRY_INTERVAL,
    SUPPORTED_CIRCUIT_TYPES,
    WEATHER_CACHE_TTL,
)
from .modbus import HovalModbusError
from .modbus_hv import HV_UNIT_IDS, HovalModbusPoller, HvModbusReading

SIGNAL_NEW_CIRCUITS = f"{DOMAIN}_new_circuits"

# What a Modbus round trip can raise: protocol errors, transport, timeouts.
_MODBUS_ERRORS = (HovalModbusError, OSError, TimeoutError)

# Maximum lifetime of an optimistic mode override (seconds). Overrides are
# normally cleared at the end of the next successful poll, but if polls keep
# failing an override must not mask the device's real state indefinitely.
_MODE_OVERRIDE_TTL_S = 120.0
_MAX_CONCURRENT_CIRCUIT_FETCHES = 8
# The gateway reconnects to Azure IoT Hub about every 48 minutes, and a poll
# that lands inside the reconnect reads isOnline=false once (2026-09-26: ten
# of ten such offline polls were single and on that grid). The first offline
# poll therefore keeps the previous data; this many in a row blank the
# circuit entities and log a warning.
_OFFLINE_POLLS_BEFORE_UNAVAILABLE = 2

_LOGGER = logging.getLogger(__name__)

# v1 API returns different activeProgram values than v3.
# Normalize so entities always see v3 enum keys.
_V1_PROGRAM_MAP: dict[str, str] = {
    "tteControlled": "week1",  # time program active (v1 doesn't say which week)
    "timePrograms": "week1",
    "nightReduction": "week1",
    "dayCooling": "week1",
}


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    """Accept actual API booleans, never truthy strings such as 'false'."""
    return value if isinstance(value, bool) else default


def _coerce_optional_str(value: Any) -> str | None:
    """Ignore malformed optional strings without discarding their circuit."""
    return value if isinstance(value, str) else None


def _coerce_finite_number(value: Any) -> float | None:
    """Normalize numeric DTO fields without exposing NaN or infinity to HA."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


# Circuit types whose entities set a temperature and therefore need the
# controller's temporary-change limits. HV keeps its fixed 15..100 % band,
# which matches the limits the cloud reports for it.
_LIMITS_CIRCUIT_TYPES = frozenset({CIRCUIT_TYPE_WW, CIRCUIT_TYPE_HK})
# Circuit types whose details are fetched at all: the limits above, plus HV for
# the constant and eco program setpoints. Those replaced the raw datapoints
# 40651/40686 when Hoval asked for snapshots/live to be dropped (#24).
_DETAILS_CIRCUIT_TYPES = _LIMITS_CIRCUIT_TYPES | {CIRCUIT_TYPE_HV}


def _parse_temporary_change_limits(details: Any) -> tuple[float | None, float | None]:
    """Return (min, max) from a circuit details DTO, or (None, None) if unusable."""
    limits = details.get("temporaryChangeLimits") if isinstance(details, dict) else None
    if not isinstance(limits, dict):
        return None, None
    low = _coerce_finite_number(limits.get("min"))
    high = _coerce_finite_number(limits.get("max"))
    if low is None or high is None or low > high:
        return None, None
    return low, high


def _parse_program_setpoints(details: Any) -> tuple[float | None, float | None]:
    """Return (constantValue, ecoModeValue) from a circuit details DTO."""
    if not isinstance(details, dict):
        return None, None
    return (
        _coerce_finite_number(details.get("constantValue")),
        _coerce_finite_number(details.get("ecoModeValue")),
    )


def _is_circuit_selectable(circuit: dict[str, Any]) -> bool:
    """Prefer the required v3 field, accepting the legacy field when absent."""
    return _coerce_bool(circuit.get("isSelectable", circuit.get("selectable", False)))


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
    # usable scalar "id" are skipped instead of raising.
    config_by_id: dict[Any, dict] = {
        d["id"]: d
        for d in day_configs
        if isinstance(d, dict)
        and isinstance(d.get("id"), (str, int))
        and not isinstance(d["id"], bool)
    }

    # Pick week1 or week2 based on what the controller reports as active.
    week_key = "week2" if active_program == "week2" else "week1"
    week = programs.get(week_key)
    if not isinstance(week, dict):
        # Week entry missing or wrong shape — no week/day info resolvable.
        return None, None, None
    week_name = _coerce_optional_str(week.get("name"))
    day_program_ids = week.get("dayProgramIds")
    if not isinstance(day_program_ids, list):
        day_program_ids = []

    # weekday: 0=Monday in Python, dayProgramIds[0]=Monday in Hoval
    weekday = now.weekday()
    if weekday >= len(day_program_ids):
        return week_name, None, None

    day_prog_id = day_program_ids[weekday]
    if not isinstance(day_prog_id, (str, int)) or isinstance(day_prog_id, bool):
        return week_name, None, None
    day_config = config_by_id.get(day_prog_id)
    if day_config is None:
        return week_name, None, None

    day_name = _coerce_optional_str(day_config.get("name"))

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
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if start_min <= current_minutes < end_min:
            return week_name, day_name, _coerce_finite_number(phase.get("value"))

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
    # Circuit list field `circuitStatus` (e.g. "active", "heating", "cooling"). Not in the
    # live-values payload — that only carries measurements, no status key.
    circuit_status: str | None = None
    active_program: str | None = None
    # Circuit list field `temporaryChange` — present only while an override is
    # running, e.g. {"type": "away", "value": 100.0, "end": "…+02:00"}. The cloud
    # tracks this itself, so automations do not need their own bookkeeping helpers.
    temporary_change_end: str | None = None
    temporary_change_value: float | None = None
    temporary_change_type: str | None = None
    # `temporaryChangeLimits` from the circuit details endpoint: the static range
    # of the setpoint datapoint. The cloud refuses values outside it with 424;
    # the controller may refuse values inside it too (issue #15). Fetched for WW/HK
    # only (_LIMITS_CIRCUIT_TYPES); None until known or when the cloud omits it.
    temporary_change_min: float | None = None
    temporary_change_max: float | None = None
    # HV: air-volume percentage; HK: target temperature in °C. Coming from the
    # circuit list endpoint's `targetValue` (renamed from v1 `targetAirVolume`).
    target_value: float | None = None
    is_air_quality_guided: bool = False
    # `airQuality` on the circuit list (HV): coarse room air quality from the
    # unit's own sensor ("poor"/"medium"/"good"), null without one. The only
    # cloud stand-in for the CO2/VOC datapoints retired in v1.1.0 (#24).
    room_air_quality: str | None = None
    has_air_quality_sensor: bool = False
    # `holidayEnd` on the circuit list: set while a holiday program runs.
    holiday_end: str | None = None
    # `circuitControlType` on the circuit list: heating/cooling selection of
    # reversible heat pumps (HK); null where it does not apply.
    circuit_control_type: str | None = None
    has_error: bool = False
    # HV: setpoints of the constant and eco programs (`constantValue` and
    # `ecoModeValue` from the circuit details endpoint). None until known.
    constant_value: float | None = None
    eco_mode_value: float | None = None
    live_values: dict[str, str] = field(default_factory=dict)
    active_week_name: str | None = None
    active_day_program_name: str | None = None
    program_air_volume: float | None = None
    # User-defined program names: API key → display name (e.g. "week1" → "Normal")
    program_names: dict[str, str] = field(default_factory=dict)
    # Latest Modbus reading of this circuit (HV with a local gateway), else None.
    modbus: HvModbusReading | None = None


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
    # False when this snapshot has no cloud data (local-only, or the cloud failed).
    cloud_ok: bool = True


_MODBUS_LIVE_KEYS = (
    "airVolume",
    "humidityActual",
    "humidityTarget",
    "outsideTemperature",
    "exhaustTemp",
)


def apply_modbus_reading(circuit: HovalCircuitData, reading: HvModbusReading | None) -> None:
    """Overlay a Modbus reading on a circuit; None blanks the Modbus-owned fields."""
    circuit.modbus = reading
    if reading is None:
        for key in _MODBUS_LIVE_KEYS:
            circuit.live_values.pop(key, None)
        circuit.active_program = None
        circuit.constant_value = None
        circuit.eco_mode_value = None
        circuit.target_value = None
        # A bool field: "unknown" is False, not None.
        circuit.is_air_quality_guided = False
        return
    values = {
        "airVolume": reading.air_volume,
        "humidityActual": reading.humidity_actual,
        "humidityTarget": reading.humidity_target,
        "outsideTemperature": reading.outside_temperature,
        "exhaustTemp": reading.exhaust_temperature,
    }
    for key, value in values.items():
        if value is None:
            circuit.live_values.pop(key, None)
        else:
            circuit.live_values[key] = str(value)
    circuit.active_program = reading.program
    circuit.constant_value = (
        None if reading.constant_modulation is None else float(reading.constant_modulation)
    )
    circuit.eco_mode_value = (
        None if reading.eco_modulation is None else float(reading.eco_modulation)
    )
    circuit.target_value = None if reading.air_volume is None else float(reading.air_volume)
    circuit.is_air_quality_guided = reading.air_quality_control == 1


def _hv_unit(path: str) -> int | None:
    """Return the Modbus unit id of an HV circuit path (`<unit>.50.0`), if it is one."""
    try:
        unit = int(path.split(".", 1)[0])
    except ValueError:  # paths come from the cloud; isdigit() would pass "²"
        return None
    return unit if unit in HV_UNIT_IDS else None


def _valid_topology(topology: Any) -> bool:
    """Check the shape topology_from_data() writes to the config entry."""
    if not isinstance(topology, dict) or not isinstance(topology.get("plant_id"), str):
        return False
    circuits = topology.get("circuits")
    return isinstance(circuits, dict) and all(
        isinstance(path, str) and isinstance(name, str) for path, name in circuits.items()
    )


def topology_from_data(data: HovalData, plant_id: str) -> dict[str, Any] | None:
    """Describe a plant and its HV circuits for a start without the cloud."""
    plant = data.plants.get(plant_id)
    if plant is None:
        return None
    return {
        "plant_id": plant_id,
        "plant_name": plant.name,
        "circuits": {
            path: circuit.name
            for path, circuit in plant.circuits.items()
            if circuit.circuit_type == CIRCUIT_TYPE_HV
        },
    }


def data_from_topology(topology: dict[str, Any]) -> HovalData:
    """Rebuild the plant and its HV circuits (no values) from a stored topology."""
    plant_id = topology["plant_id"]
    plant_name = _coerce_optional_str(topology.get("plant_name")) or plant_id
    plant = HovalPlantData(plant_id, plant_name, is_online=False)
    for path, name in topology["circuits"].items():
        plant.circuits[path] = HovalCircuitData(CIRCUIT_TYPE_HV, path, name)
    return HovalData(plants={plant_id: plant}, cloud_ok=False)


async def _no_cloud_live_values() -> list[dict[str, Any]]:
    """Stand in for get_live_values() on a circuit whose values come from Modbus."""
    return []


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
        api: HovalConnectApi | None,
        *,
        modbus: HovalModbusPoller | None = None,
        modbus_plant_id: str | None = None,
        local_plant: tuple[str, str] | None = None,
        topology: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the coordinator.

        api None = local-only (Modbus, no Hoval account); api and modbus both
        set = hybrid.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.api = api
        self.modbus = modbus
        self.modbus_plant_id = modbus_plant_id
        if topology is not None and not _valid_topology(topology):
            _LOGGER.warning("Ignoring a malformed stored plant topology")
            topology = None
        # Bound plant and HV circuits of the last successful cloud fetch
        # (entry.data[DATA_TOPOLOGY]); lets a hybrid entry start without the cloud.
        self.topology = topology
        self._local_plant = local_plant
        self.cloud_interval: float = DEFAULT_SCAN_INTERVAL.total_seconds()
        # Unit ids found by discovery; None = not discovered yet (retried each update).
        self._local_units: list[int] | None = None
        # --- Hybrid state (cloud account + Modbus gateway) ---
        # The plant the gateway belongs to. Before any cloud fetch: the chosen
        # plant, else the stored topology's (it is only ever stored for the bound plant).
        self.bound_plant_id: str | None = modbus_plant_id or (topology or {}).get("plant_id")
        self._binding_warned = False
        # Monotonic time of the next cloud fetch; 0 = fetch at the next update.
        self._cloud_deadline = 0.0
        # Last successful cloud snapshot, never mutated (the merge works on copies).
        self._cloud_data: HovalData | None = None
        # Outcome of the latest cloud attempt, and whether a failure streak runs.
        self._cloud_ok = False
        self._cloud_failing = False
        # (plant_id, path) of HV circuits Modbus serves: the cloud fetch skips
        # their live values and circuit details (G6). Empty in cloud-only mode.
        self._modbus_paths: set[tuple[str, str]] = set()
        # (plant_id, HV paths) the last successful discovery ran against.
        self._discovered_for: tuple[str, frozenset[str]] | None = None
        self._modbus_failing = False
        self._circuit_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._circuit_fetch_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CIRCUIT_FETCHES)
        # Optimistic mode override per circuit (set by control actions,
        # cleared at the END of the next successful poll or after
        # _MODE_OVERRIDE_TTL_S). Key: (plant_id, circuit_path),
        # value: (operation mode string, monotonic timestamp).
        self._mode_override: dict[tuple[str, str], tuple[str, float]] = {}
        self._last_week_program: dict[tuple[str, str], str] = {}
        # Circuit paths repeat across plants. Cache both data and empty
        # results per plant, with a monotonic deadline for the next probe.
        self._program_cache: dict[tuple[str, str], tuple[Any, float]] = {}
        self._program_cache_ttl = PROGRAM_CACHE_TTL.total_seconds()
        # Circuits whose 417 on /programs was already logged at WARNING.
        self._programs_417_logged: set[tuple[str, str]] = set()
        # Parsed circuit details per circuit: (min, max) temporary-change limits,
        # (constant, eco) program setpoints and the deadline of the next fetch.
        # Refreshed on the program cadence so a changed setting shows up.
        self._details_cache: dict[
            tuple[str, str],
            tuple[
                tuple[float | None, float | None],
                tuple[float | None, float | None],
                float,
            ],
        ] = {}
        # Plant-level caches: (parsed value(s), monotonic timestamp)
        self._weather_cache: dict[str, tuple[HovalWeatherData | None, float]] = {}
        self._weather_cache_ttl = WEATHER_CACHE_TTL.total_seconds()
        self._events_cache: dict[
            str, tuple[HovalEventData | None, list[HovalEventData], float]
        ] = {}
        self._events_cache_ttl = EVENTS_CACHE_TTL.total_seconds()
        # Track known circuits for dynamic entity discovery
        self._known_circuits: set[str] = set()
        # Consecutive polls per plant that reported isOnline=false.
        self._offline_polls: dict[str, int] = {}
        # Device-registry ids of the plant devices, filled by async_setup_entry.
        self.plant_device_ids: dict[str, str] = {}

    @property
    def has_modbus(self) -> bool:
        """True when a Modbus poller is configured."""
        return self.modbus is not None

    @property
    def is_local(self) -> bool:
        """True in local-only mode (no cloud API)."""
        return self.api is None

    async def async_hv_write(self, plant_id: str, circuit_path: str, name: str, value: int) -> None:
        """Write one HV register over Modbus (unit = first path segment)."""
        if self.modbus is None:
            raise HovalModbusError("Modbus is not configured")
        await self.modbus.write(int(circuit_path.split(".")[0]), name, value)

    async def async_close(self) -> None:
        """Close the Modbus connection, if any."""
        if self.modbus is not None:
            await self.modbus.client.close()

    def set_mode_override(self, plant_id: str, circuit_path: str, mode: str) -> None:
        """Set optimistic mode override after a control action."""
        self._mode_override[(plant_id, circuit_path)] = (mode, time.monotonic())

    def get_mode_override(self, plant_id: str, circuit_path: str) -> str | None:
        """Get the optimistic mode override for a circuit.

        Returns None once the override exceeds _MODE_OVERRIDE_TTL_S so a stale
        optimistic value cannot mask the device's real state when polls fail.
        """
        key = (plant_id, circuit_path)
        entry = self._mode_override.get(key)
        if entry is None:
            return None
        mode, ts = entry
        if time.monotonic() - ts > _MODE_OVERRIDE_TTL_S:
            self._mode_override.pop(key, None)
            return None
        return mode

    def resolve_resume_program(self, plant_id: str, circuit_path: str) -> str:
        """Resume the last observed weekly schedule for this specific circuit."""
        data = self.data
        plant = data.plants.get(plant_id) if data is not None else None
        circuit = plant.circuits.get(circuit_path) if plant is not None else None
        if circuit is not None and circuit.active_program in {"week1", "week2"}:
            return circuit.active_program
        return self._last_week_program.get((plant_id, circuit_path), "week1")

    def _get_circuit_lock(self, plant_id: str, circuit_path: str) -> asyncio.Lock:
        """Serialize writes to one circuit while leaving other circuits free."""
        key = (plant_id, circuit_path)
        if key not in self._circuit_locks:
            self._circuit_locks[key] = asyncio.Lock()
        return self._circuit_locks[key]

    async def async_control_and_refresh(
        self,
        action_factory: Callable[[], Awaitable[Any]],
        *,
        plant_id: str,
        circuit_path: str,
        mode_override: str,
        cloud_action: bool = True,
    ) -> None:
        """Execute a control command with lock, optimistic state, and refresh.

        The API call and optimistic override are serialised inside
        the circuit's lock; the 2 s settle delay and the refresh run OUTSIDE the
        lock so a slow refresh cannot starve concurrent control actions.

        The refresh is deliberately AWAITED, not fired-and-forgotten:
        entities keep optimistic ``_pending_*`` state exactly until this
        method returns, so returning early hands the UI stale coordinator
        data for the settle window. v1.0.0 did exactly that — after every
        control action the fan slider dipped back to the pre-change value
        for ~2-4 s, which the bundled summer-boost Blueprint interpreted as
        a manual override and turned into a notification loop.
        A failed refresh is swallowed — the coordinator retries on its
        normal poll schedule and entities stay on their optimistic state.

        ``cloud_action`` False marks a Modbus write: the gateway has already
        confirmed it by read-back, so the refresh runs at once and, in hybrid
        mode, leaves the cloud on its own interval. A cloud action (temporary
        change) settles 2 s and then forces a cloud fetch, as before.
        """
        async with self._get_circuit_lock(plant_id, circuit_path):
            # A cancelled lock waiter must never leave an unawaited coroutine.
            await action_factory()
            self.set_mode_override(plant_id, circuit_path, mode_override)

        if cloud_action:
            # Give the cloud time to commit the change before fetching.
            await asyncio.sleep(2)
            if self.modbus is not None:
                # Hybrid fetches the cloud only once per cloud interval; this
                # refresh must see the action's result. Set after the settle
                # delay so a scheduled tick during it cannot use up the fetch.
                self._cloud_deadline = 0.0
        try:
            await self.async_request_refresh()
        except Exception:  # noqa: BLE001 — see docstring
            _LOGGER.debug(
                "Post-control refresh failed for %s; coordinator will retry on next poll",
                circuit_path,
            )

    async def _async_update_data(self) -> HovalData:
        """Dispatch to the cloud-only, hybrid or local-only path."""
        if self.api is None:
            return await self._async_fetch_local()
        if self.modbus is None:
            return await self._async_fetch_cloud()
        return await self._async_update_hybrid()

    async def _async_update_hybrid(self) -> HovalData:
        """Modbus on every update, the cloud only once per cloud interval.

        The update interval is the Modbus interval (30 s by default). Hoval
        allows one cloud poll per 15 minutes (#24), so between cloud fetches
        the last cloud snapshot is reused, and a failing cloud leaves the
        Modbus values working (G3).
        """
        modbus_down = False
        if time.monotonic() >= self._cloud_deadline:
            # Rediscover once per cloud fetch, and before it while the HV paths
            # are known (previous fetch or stored topology): the fetch then
            # already skips what Modbus serves (G6), right after an HA restart too.
            modbus_down = not await self._async_discover(
                self._hv_paths(self._bound_plant(self._base_snapshot()))
            )
            await self._async_refresh_cloud()
        data = self._base_snapshot()
        if data is None:
            raise UpdateFailed("Hoval cloud unavailable and no stored plant topology to start from")
        modbus_fresh = await self._async_merge_modbus(data, modbus_down=modbus_down)
        if not data.cloud_ok and not modbus_fresh:
            # Nothing in this snapshot is current; fail like a cloud-only
            # entry does, so HA keeps the old data and marks entities unavailable.
            raise UpdateFailed("Neither the Hoval cloud nor the Modbus gateway delivered data")
        return data

    async def _async_refresh_cloud(self) -> None:
        """Fetch the cloud once; a failure is recorded, never raised."""
        # Hoval may block again without notice, so whatever the outcome, the
        # next attempt waits a full cloud interval: a failing cloud must never
        # be retried at the Modbus interval.
        self._cloud_deadline = time.monotonic() + self.cloud_interval
        try:
            data = await self._async_fetch_cloud()
        except ConfigEntryAuthFailed as err:
            self._cloud_failed(err)
            # Raising would fail the update and take the Modbus values with it,
            # so start the reauth flow HA would otherwise have started.
            self.config_entry.async_start_reauth(self.hass)
            return
        except (UpdateFailed, HovalApiError) as err:
            self._cloud_failed(err)
            return
        if self._cloud_failing:
            _LOGGER.info("Hoval cloud is reachable again")
        self._cloud_failing = False
        self._cloud_ok = True
        self._cloud_data = data
        self._resolve_binding(data)
        self._persist_topology(data)

    def _cloud_failed(self, err: Exception) -> None:
        """Mark the cloud failed; log once per failure streak."""
        self._cloud_ok = False
        if self._cloud_failing:
            _LOGGER.debug("Hoval cloud still failing: %s", err)
            return
        self._cloud_failing = True
        _LOGGER.warning(
            "Hoval cloud request failed (%s); Modbus values keep updating, cloud "
            "values are unavailable. Next cloud attempt in %d minutes",
            err,
            self.cloud_interval / 60,
        )

    def _resolve_binding(self, data: HovalData) -> None:
        """Bind the gateway to one plant of a fresh cloud snapshot."""
        stored = self.topology["plant_id"] if self.topology is not None else None
        if self.modbus_plant_id:
            bound = self.modbus_plant_id
        elif stored in data.plants:
            # The topology is only stored for the bound plant, so it keeps an
            # implicit binding when the account later gains a second plant.
            bound = stored
        elif len(data.plants) == 1:
            bound = next(iter(data.plants))
        else:
            bound = None
        if (bound is None or bound not in data.plants) and not self._binding_warned:
            self._binding_warned = True
            if bound is None:
                _LOGGER.warning(
                    "The Hoval account has %d plants and none is chosen for the Modbus "
                    "gateway; choose the plant in the integration options. Modbus is "
                    "not used until then",
                    len(data.plants),
                )
            else:
                _LOGGER.warning(
                    "Plant %s chosen for the Modbus gateway is not in the Hoval account; "
                    "Modbus values are not used",
                    bound,
                )
        if bound != self.bound_plant_id:
            self.bound_plant_id = bound
            self._discovered_for = None
            self._modbus_paths = set()

    def _persist_topology(self, data: HovalData) -> None:
        """Store the bound plant's topology in the entry when it changed."""
        plant = self._bound_plant(data)
        # An offline plant comes without its circuit list (or with the previous
        # poll's); storing that would drop the HV circuits a start without the
        # cloud needs.
        if plant is None or not plant.is_online:
            return
        topology = topology_from_data(data, plant.plant_id)
        self.topology = topology
        entry = self.config_entry
        if entry.data.get(DATA_TOPOLOGY) != topology:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, DATA_TOPOLOGY: topology}
            )

    def _base_snapshot(self) -> HovalData | None:
        """This update's starting point: the last cloud data, else the topology."""
        if self._cloud_data is not None:
            # A deep copy per update: the Modbus merge mutates circuits, while
            # the cached cloud snapshot must stay as fetched and HA still holds
            # the previous update's object as self.data.
            data = deepcopy(self._cloud_data)
            data.cloud_ok = self._cloud_ok
        elif self.topology is not None:
            # HA started while the cloud is down: rebuild the bound plant and
            # its HV circuits from the last successful fetch.
            data = data_from_topology(self.topology)
        else:
            return None
        plant = self._bound_plant(data)
        topology = self.topology
        if (
            plant is not None
            and not plant.is_online
            and topology is not None
            and topology["plant_id"] == plant.plant_id
        ):
            # The cloud lists no circuits for a plant it reports offline, but
            # the gateway may still answer on the LAN (G3): give Modbus the
            # stored HV circuits to merge into.
            for path, name in topology["circuits"].items():
                plant.circuits.setdefault(path, HovalCircuitData(CIRCUIT_TYPE_HV, path, name))
        return data

    def _bound_plant(self, data: HovalData | None) -> HovalPlantData | None:
        """Return the bound plant of a snapshot, if both exist."""
        if data is None or self.bound_plant_id is None:
            return None
        return data.plants.get(self.bound_plant_id)

    @staticmethod
    def _hv_paths(plant: HovalPlantData | None) -> dict[str, int]:
        """Map the plant's HV circuit paths to their Modbus unit ids."""
        if plant is None:
            return {}
        paths = {}
        for path, circuit in plant.circuits.items():
            unit = _hv_unit(path) if circuit.circuit_type == CIRCUIT_TYPE_HV else None
            if unit is not None:
                paths[path] = unit
        return paths

    async def _async_discover(self, paths: dict[str, int]) -> bool:
        """Find which HV paths of the bound plant Modbus serves.

        Returns False when the gateway failed; the previous result then stays,
        so a gateway outage never turns cloud live values back on (spec: no
        automatic fallback to the cloud's live values).
        """
        if self.bound_plant_id is None or not paths:
            return True
        try:
            served = set(await self.modbus.discover(sorted(set(paths.values()))))
        except _MODBUS_ERRORS as err:
            self._modbus_failed(err)
            return False
        self._modbus_recovered()
        self._discovered_for = (self.bound_plant_id, frozenset(paths))
        self._modbus_paths = {
            (self.bound_plant_id, path) for path, unit in paths.items() if unit in served
        }
        return True

    async def _async_merge_modbus(self, data: HovalData, *, modbus_down: bool) -> bool:
        """Overlay Modbus readings on the bound plant; True if any reading arrived."""
        plant = self._bound_plant(data)
        if plant is None:
            return False
        paths = self._hv_paths(plant)
        if not modbus_down and (plant.plant_id, frozenset(paths)) != self._discovered_for:
            # First fetch without a stored topology, a new HV circuit, or a
            # binding resolved just now.
            modbus_down = not await self._async_discover(paths)
        served = {
            path: paths[path]
            for pid, path in self._modbus_paths
            if pid == plant.plant_id and path in paths
        }
        if not served:
            return False
        readings: dict[int, HvModbusReading | None] = {}
        if not modbus_down:
            try:
                readings = await self.modbus.poll(sorted(set(served.values())))
            except _MODBUS_ERRORS as err:
                self._modbus_failed(err)
            else:
                self._modbus_recovered()
        fresh = False
        for path, unit in served.items():
            # None blanks the Modbus-owned fields: no fallback to cloud values.
            reading = readings.get(unit)
            apply_modbus_reading(plant.circuits[path], reading)
            fresh = fresh or reading is not None
        return fresh

    def _modbus_failed(self, err: Exception) -> None:
        """Log a Modbus failure once per outage, like the offline handling."""
        if self._modbus_failing:
            _LOGGER.debug("Modbus gateway still failing: %s", err)
            return
        self._modbus_failing = True
        _LOGGER.warning(
            "Modbus gateway is not answering (%s); its values are unavailable until it answers",
            err,
        )

    def _modbus_recovered(self) -> None:
        if self._modbus_failing:
            self._modbus_failing = False
            _LOGGER.info("Modbus gateway is answering again")

    async def _async_fetch_local(self) -> HovalData:
        """Build the whole data model from Modbus (no cloud account)."""
        if self.modbus is None:
            raise UpdateFailed("Neither cloud nor Modbus is configured")
        plant_id, plant_name = self._local_plant or ("local", "Hoval")
        try:
            if self._local_units is None:
                units = await self.modbus.discover()
                if not units:
                    raise UpdateFailed("No HV unit answered on the Modbus gateway")
                self._local_units = units
            readings = await self.modbus.poll(self._local_units)
        except _MODBUS_ERRORS as err:
            raise UpdateFailed(f"Modbus error: {err}") from err
        plant = HovalPlantData(plant_id, plant_name, is_online=True)
        multiple = len(self._local_units) > 1
        for unit in self._local_units:
            reading = readings.get(unit)
            if reading is None:
                continue
            path = f"{unit}.50.0"
            name = CIRCUIT_TYPE_NAMES[CIRCUIT_TYPE_HV]
            circuit = HovalCircuitData(
                CIRCUIT_TYPE_HV, path, f"{name} {unit}" if multiple else name
            )
            apply_modbus_reading(circuit, reading)
            plant.circuits[path] = circuit
            if reading.active_errors:
                plant.has_error = True
        return HovalData(plants={plant_id: plant}, cloud_ok=False)

    async def _async_fetch_cloud(self) -> HovalData:
        """Fetch data from the API.

        This is the former _async_update_data body, moved so the cloud-only
        behaviour stays identical. Its one hybrid hook: circuits in
        `_modbus_paths` (always empty without Modbus) skip live values and
        circuit details.
        """
        if self.api is None:  # only reachable by calling this directly
            raise UpdateFailed("Cloud API is not configured")
        # Timestamp BEFORE any fetch: overrides set after this instant belong
        # to control actions this poll's data snapshot cannot reflect yet, so
        # the pruning at the end must leave them alone.
        poll_start = time.monotonic()
        data = HovalData()
        # Plants served from the previous poll's data; nothing was fetched.
        stale_plants: set[str] = set()

        try:
            plants = await self.api.get_plants()

            if not isinstance(plants, list):
                raise HovalApiError("Plant topology must be a list")
            for plant in plants:
                if not isinstance(plant, dict):
                    raise HovalApiError("Plant topology contains a non-object plant")
                plant_id = _require_identifier(plant.get("plantExternalId"), "plant ID")
                if plant_id in data.plants:
                    _LOGGER.warning("Ignoring duplicate plant in topology: %s", plant_id)
                    continue

                plant_name = _coerce_optional_str(plant.get("description")) or plant_id

                plant_data = HovalPlantData(
                    plant_id=plant_id,
                    name=plant_name,
                    is_online=_coerce_bool(plant.get("isOnline", True)),
                )

                previous_offline_polls = self._offline_polls.pop(plant_id, 0)
                if plant_data.is_online:
                    if previous_offline_polls >= _OFFLINE_POLLS_BEFORE_UNAVAILABLE:
                        _LOGGER.info("Hoval cloud reports plant %s online again", plant_id)
                else:
                    # Skip all API calls while the plant is offline.
                    offline_polls = previous_offline_polls + 1
                    self._offline_polls[plant_id] = offline_polls
                    # Invalidate cached PAT so we get a fresh token when back
                    self.api.invalidate_plant_token(plant_id)
                    previous = self.data.plants.get(plant_id) if self.data is not None else None
                    if offline_polls < _OFFLINE_POLLS_BEFORE_UNAVAILABLE and previous is not None:
                        _LOGGER.debug(
                            "Plant %s reported offline for one poll; keeping its previous data",
                            plant_id,
                        )
                        data.plants[plant_id] = replace(previous, name=plant_name, is_online=False)
                        stale_plants.add(plant_id)
                        continue
                    # Offline blanks every circuit entity without any request
                    # failing, so nothing else would reach the log.
                    if offline_polls == _OFFLINE_POLLS_BEFORE_UNAVAILABLE:
                        _LOGGER.warning(
                            "Hoval cloud has reported plant %s offline for %d consecutive "
                            "polls; its circuit entities are unavailable until the gateway "
                            "reconnects to the cloud",
                            plant_id,
                            offline_polls,
                        )
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

                if not isinstance(circuits_raw, list):
                    raise HovalApiError("Circuit topology must be a list")

                # Build list of supported circuits
                supported_circuits: list[tuple[str, str, dict]] = []
                seen_paths: set[str] = set()
                for circuit in circuits_raw:
                    if not isinstance(circuit, dict):
                        _LOGGER.warning("Ignoring non-object circuit for plant %s", plant_id)
                        continue
                    ctype = _coerce_optional_str(circuit.get("type"))
                    if ctype not in SUPPORTED_CIRCUIT_TYPES:
                        continue
                    if not _is_circuit_selectable(circuit) and ctype not in _non_selectable_types:
                        continue
                    try:
                        path = _require_identifier(circuit.get("path"), "circuit path")
                    except HovalApiError:
                        _LOGGER.warning("Ignoring circuit with invalid path for plant %s", plant_id)
                        continue
                    if path in seen_paths:
                        _LOGGER.warning(
                            "Ignoring duplicate circuit %s for plant %s", path, plant_id
                        )
                        continue
                    seen_paths.add(path)
                    _LOGGER.debug(
                        "Circuit %s raw: %s",
                        path,
                        {k: v for k, v in circuit.items() if k != "name"},
                    )
                    supported_circuits.append((path, ctype, circuit))

                _LOGGER.debug(
                    "Fetched %d circuits (%d supported)",
                    len(circuits_raw),
                    len(supported_circuits),
                )

                # Fetch live values + programs for all circuits in parallel
                async def _fetch_circuit(
                    path: str,
                    ctype: str,
                    circuit: dict,
                    _plant_id: str = plant_id,
                ) -> HovalCircuitData:
                    raw_program = _coerce_optional_str(circuit.get("activeProgram"))
                    air_quality = circuit.get("airQuality")
                    if not isinstance(air_quality, dict):
                        air_quality = {}
                    # Absent (or null) whenever no override is running.
                    temporary_change = circuit.get("temporaryChange")
                    if not isinstance(temporary_change, dict):
                        temporary_change = {}
                    circuit_data = HovalCircuitData(
                        circuit_type=ctype,
                        path=path,
                        name=_coerce_optional_str(circuit.get("name")) or ctype,
                        operation_mode=_coerce_optional_str(circuit.get("operationMode")),
                        circuit_status=_coerce_optional_str(circuit.get("circuitStatus")),
                        temporary_change_end=_coerce_optional_str(temporary_change.get("end")),
                        temporary_change_value=_coerce_finite_number(temporary_change.get("value")),
                        temporary_change_type=_coerce_optional_str(temporary_change.get("type")),
                        active_program=_V1_PROGRAM_MAP.get(raw_program, raw_program),
                        target_value=_coerce_finite_number(circuit.get("targetValue")),
                        is_air_quality_guided=_coerce_bool(air_quality.get("isAirQualityGuided")),
                        room_air_quality=_coerce_optional_str(
                            air_quality.get("actualRoomAirQuality")
                        ),
                        has_air_quality_sensor=_coerce_bool(air_quality.get("hasAirQualitySensor")),
                        holiday_end=_coerce_optional_str(circuit.get("holidayEnd")),
                        circuit_control_type=_coerce_optional_str(
                            circuit.get("circuitControlType")
                        ),
                        has_error=_coerce_bool(circuit.get("hasError")),
                    )
                    if circuit_data.active_program in {"week1", "week2"}:
                        self._last_week_program[(_plant_id, path)] = circuit_data.active_program

                    # Check program cache
                    program_key = (_plant_id, path)
                    cached_prog = self._program_cache.get(program_key)
                    need_programs = cached_prog is None or time.monotonic() >= cached_prog[1]

                    # Fetch live values (always) + programs (only if cache expired)
                    # + circuit details (limits, HV setpoints) on the same cadence.
                    # G6: an HV circuit Modbus serves gets its live values and
                    # setpoints from Modbus, so neither is asked from the cloud.
                    served = program_key in self._modbus_paths
                    live_task = (
                        _no_cloud_live_values()
                        if served
                        else self.api.get_live_values(_plant_id, path, ctype)
                    )
                    cached_details = self._details_cache.get(program_key)
                    need_details = (
                        not served
                        and ctype in _DETAILS_CIRCUIT_TYPES
                        and (cached_details is None or time.monotonic() >= cached_details[2])
                    )
                    details = (
                        [self.api.get_circuit_details(_plant_id, path)] if need_details else []
                    )
                    if need_programs:
                        prog_task = self.api.get_programs(_plant_id, path)
                        results = await asyncio.gather(
                            live_task,
                            prog_task,
                            *details,
                            return_exceptions=True,
                        )
                    else:
                        gathered = await asyncio.gather(
                            live_task,
                            *details,
                            return_exceptions=True,
                        )
                        # Keep index 0 = live values and 1 = programs in both
                        # branches so the readers below stay branch-agnostic.
                        results = [gathered[0], cached_prog[0], *gathered[1:]]

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
                            if isinstance(v, dict)
                            and isinstance(v.get("key"), str)
                            and "value" in v
                        }
                        _LOGGER.debug("Circuit %s live_values: %s", path, circuit_data.live_values)
                    else:
                        _LOGGER.debug("Live values not available for %s", path)

                    limits, setpoints = (
                        cached_details[:2] if cached_details else ((None, None), (None, None))
                    )
                    if need_details:
                        details_result = results[2]
                        if isinstance(details_result, dict):
                            limits = _parse_temporary_change_limits(details_result)
                            setpoints = _parse_program_setpoints(details_result)
                        else:
                            # Keep the last known values: falling back to the
                            # entity's static range would widen it again.
                            _LOGGER.debug(
                                "Circuit details not available for %s: %s", path, details_result
                            )
                        self._details_cache[program_key] = (
                            limits,
                            setpoints,
                            time.monotonic() + self._program_cache_ttl,
                        )
                    if ctype in _LIMITS_CIRCUIT_TYPES:
                        circuit_data.temporary_change_min, circuit_data.temporary_change_max = (
                            limits
                        )
                    if ctype == CIRCUIT_TYPE_HV:
                        circuit_data.constant_value, circuit_data.eco_mode_value = setpoints

                    programs = results[1]
                    if need_programs:
                        if (
                            isinstance(programs, HovalApiError)
                            and programs.status == 417
                            and not programs.gateway_blocked
                            and programs.request_path
                            == f"/v3/plants/{_plant_id}/circuits/{path}/programs"
                        ):
                            retry_interval = PROGRAM_UNAVAILABLE_RETRY_INTERVAL.total_seconds()
                            self._program_cache[program_key] = (
                                None,
                                time.monotonic() + retry_interval,
                            )
                            # BL/PS circuits answer 417 on every re-probe
                            # (issue #16): warn once, not once an hour.
                            first_417 = program_key not in self._programs_417_logged
                            self._programs_417_logged.add(program_key)
                            _LOGGER.log(
                                logging.WARNING if first_417 else logging.DEBUG,
                                "Programs unavailable for plant %s circuit %s (HTTP 417); "
                                "checking again every %d minutes, repeats are logged at "
                                "debug level. Live values continue to update.",
                                _plant_id,
                                path,
                                retry_interval / 60,
                            )
                            programs = None
                        elif (
                            isinstance(programs, dict)
                            or programs is None
                            or (isinstance(programs, list) and not programs)
                        ):
                            # Empty successful responses also need a cache;
                            # otherwise circuits without schedules poll forever.
                            # A later 417 after a success is news again.
                            self._programs_417_logged.discard(program_key)
                            self._program_cache[program_key] = (
                                programs,
                                time.monotonic() + self._program_cache_ttl,
                            )
                    if isinstance(programs, dict):
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
                async def _fetch_circuit_bounded(path: str, ctype: str, circ: dict):
                    async with self._circuit_fetch_semaphore:
                        return await _fetch_circuit(path, ctype, circ)

                all_tasks = [
                    _fetch_circuit_bounded(path, ctype, circ)
                    for path, ctype, circ in supported_circuits
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
                    latest_ok = not isinstance(latest_result, BaseException)
                    events_ok = not isinstance(events_result, BaseException)
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
                        if not latest_ok:
                            _LOGGER.debug("Events endpoint not available for %s", plant_id)
                        elif isinstance(latest_result, dict) and latest_result:
                            parsed_latest = _parse_event(latest_result)
                            _LOGGER.debug(
                                "Latest event: type=%s active=%s desc=%s",
                                parsed_latest.event_type,
                                parsed_latest.is_active,
                                parsed_latest.description,
                            )
                        if not events_ok:
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
                        latest_ok = events_ok = False
                        parsed_latest = None
                        parsed_events = []
                    # Per-endpoint fallback: a failed half reuses its cached
                    # value instead of wiping it; a successful half is cached
                    # even when EMPTY — otherwise a healthy zero-event plant
                    # would re-fetch both endpoints on every poll and the
                    # cache would never save a single request.
                    if not latest_ok and events_cached is not None:
                        parsed_latest = events_cached[0]
                    if not events_ok and events_cached is not None:
                        parsed_events = events_cached[1]
                    if latest_ok or events_ok:
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
                    weather_ok = not isinstance(weather_result, BaseException)
                    parsed_weather = None
                    if (
                        weather_ok
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
                    elif not weather_ok:
                        _LOGGER.debug("Weather not available for %s", plant_id)
                    if weather_ok:
                        # Cache even a None result: a plant without forecast
                        # data must not re-fetch weather on every poll.
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
            raise UpdateFailed(f"Error fetching Hoval data: {err}") from err

        # Clear optimistic overrides only after a SUCCESSFUL fetch — fresh data
        # replaces them. Clearing at the start meant a failed refresh snapped
        # entities back to stale pre-override data. Prune ONLY overrides set
        # before this poll began: an unconditional clear() would also wipe an
        # override set by a control action while the poll was in flight, and
        # the poll's pre-change data snapshot would snap the entity back to
        # its old state. Mid-poll overrides survive until a poll that STARTED
        # after them succeeds (or the TTL in get_mode_override expires).
        # A plant served from previous data got no fresh snapshot either.
        self._mode_override = {
            key: entry
            for key, entry in self._mode_override.items()
            if entry[1] >= poll_start or key[0] in stale_plants
        }
        return data

    def _async_refresh_finished(self) -> None:
        """Announce circuits that are new in the data HA has just published.

        Dynamic entity discovery runs here, not at the end of
        _async_update_data: HA calls this hook only after it has assigned
        `self.data`, and it runs dispatcher @callback receivers synchronously.
        Sent from _async_update_data, the signal reached every platform's
        _add_new() while `coordinator.data` still held the previous snapshot.
        After an HA start with the plant offline, that snapshot had no
        circuits, and _known_circuits then suppressed the signal for good.
        Fire on any newly seen circuit, the first one included; each platform
        deduplicates via its `known` set.
        """
        if not self.last_update_success or self.data is None:
            return
        current_circuits = {
            f"{pid}_{path}" for pid, plant in self.data.plants.items() for path in plant.circuits
        }
        new_circuits = current_circuits - self._known_circuits
        self._known_circuits = current_circuits
        if new_circuits:
            _LOGGER.info("New circuits discovered: %s", new_circuits)
            async_dispatcher_send(self.hass, SIGNAL_NEW_CIRCUITS)
