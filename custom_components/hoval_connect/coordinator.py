"""Data coordinator for Hoval Connect."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import HovalAuthError, HovalApiError, HovalConnectApi
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, SUPPORTED_CIRCUIT_TYPES

_LOGGER = logging.getLogger(__name__)


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
    message: str | None = None
    timestamp: str | None = None
    circuit_path: str | None = None
    is_active: bool = False


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


@dataclass
class HovalData:
    """Top-level data returned by the coordinator."""

    plants: dict[str, HovalPlantData] = field(default_factory=dict)


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

    async def _async_update_data(self) -> HovalData:
        """Fetch data from the API."""
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
                    data.plants[plant_id] = plant_data
                    continue

                # Fetch circuits
                try:
                    circuits_raw = await self.api.get_circuits(plant_id)
                except HovalApiError:
                    _LOGGER.debug("Circuits endpoint not available for plant")
                    circuits_raw = []

                for circuit in circuits_raw:
                    ctype = circuit.get("type", "")
                    if ctype not in SUPPORTED_CIRCUIT_TYPES:
                        continue
                    if not circuit.get("selectable", False):
                        continue

                    path = circuit["path"]
                    circuit_data = HovalCircuitData(
                        circuit_type=ctype,
                        path=path,
                        name=circuit.get("name") or ctype,
                        operation_mode=circuit.get("operationMode"),
                        active_program=circuit.get("activeProgram"),
                        target_air_volume=circuit.get("targetAirVolume"),
                        target_air_humidity=circuit.get("targetAirHumidity"),
                        is_air_quality_guided=circuit.get("isAirQualityGuided", False),
                        has_error=circuit.get("hasError", False),
                    )

                    try:
                        live_values = await self.api.get_live_values(
                            plant_id, path, ctype
                        )
                        circuit_data.live_values = {
                            v["key"]: v["value"] for v in live_values
                        }
                    except HovalApiError:
                        _LOGGER.debug("Live values not available for %s", path)

                    # Fetch time programs to resolve currently active phase
                    try:
                        programs = await self.api.get_programs(plant_id, path)
                        now = dt_util.now()
                        week_name, day_name, phase_value = (
                            _resolve_active_program_value(programs, now)
                        )
                        circuit_data.active_week_name = week_name
                        circuit_data.active_day_program_name = day_name
                        circuit_data.program_air_volume = phase_value
                    except HovalApiError:
                        _LOGGER.debug("Programs endpoint not available for %s", path)

                    if circuit_data.has_error:
                        plant_data.has_error = True

                    plant_data.circuits[path] = circuit_data

                # Fetch plant events
                try:
                    latest_raw = await self.api.get_latest_event(plant_id)
                    if latest_raw:
                        plant_data.latest_event = HovalEventData(
                            event_type=latest_raw.get("eventType"),
                            message=latest_raw.get("message"),
                            timestamp=latest_raw.get("timestamp"),
                            circuit_path=latest_raw.get("circuitPath"),
                            is_active=latest_raw.get("isActive", False),
                        )
                        _LOGGER.debug(
                            "Latest event: type=%s active=%s",
                            latest_raw.get("eventType"),
                            latest_raw.get("isActive"),
                        )
                except HovalApiError:
                    _LOGGER.debug("Events endpoint not available for %s", plant_id)

                try:
                    events_raw = await self.api.get_events(plant_id)
                    if events_raw:
                        # Keep only the most recent 10 events
                        for ev in events_raw[:10]:
                            plant_data.events.append(HovalEventData(
                                event_type=ev.get("eventType"),
                                message=ev.get("message"),
                                timestamp=ev.get("timestamp"),
                                circuit_path=ev.get("circuitPath"),
                                is_active=ev.get("isActive", False),
                            ))
                        # Set has_error if any active blocking/locking event
                        for ev in plant_data.events:
                            if ev.is_active and ev.event_type in (
                                "blocking", "locking"
                            ):
                                plant_data.has_error = True
                                break
                except HovalApiError:
                    _LOGGER.debug(
                        "Events list endpoint not available for %s", plant_id
                    )

                data.plants[plant_id] = plant_data

        except HovalAuthError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed — check credentials"
            ) from err
        except HovalApiError as err:
            raise UpdateFailed("Error fetching Hoval data") from err

        return data
