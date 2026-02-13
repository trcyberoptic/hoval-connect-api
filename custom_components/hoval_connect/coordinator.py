"""Data coordinator for Hoval Connect."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HovalAuthError, HovalApiError, HovalConnectApi
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, SUPPORTED_CIRCUIT_TYPES

_LOGGER = logging.getLogger(__name__)


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


@dataclass
class HovalPlantData:
    """Parsed data for a single plant."""

    plant_id: str
    name: str
    circuits: dict[str, HovalCircuitData] = field(default_factory=dict)


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
                plant_id = plant["plantExternalId"]
                plant_name = plant.get("description", plant_id)

                circuits_raw = await self.api.get_circuits(plant_id)
                plant_data = HovalPlantData(plant_id=plant_id, name=plant_name)

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

                    live_values = await self.api.get_live_values(plant_id, path, ctype)
                    circuit_data.live_values = {
                        v["key"]: v["value"] for v in live_values
                    }

                    plant_data.circuits[path] = circuit_data

                data.plants[plant_id] = plant_data

        except HovalAuthError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed — check credentials"
            ) from err
        except HovalApiError as err:
            raise UpdateFailed(f"Error fetching Hoval data: {err}") from err

        return data
