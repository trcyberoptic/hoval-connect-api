"""The Hoval Connect integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HovalConnectApi
from .const import DOMAIN
from .coordinator import HovalDataCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CLIMATE, Platform.SENSOR]

type HovalConnectConfigEntry = ConfigEntry[HovalRuntimeData]


@dataclass
class HovalRuntimeData:
    """Runtime data for the Hoval Connect integration."""

    coordinator: HovalDataCoordinator
    api: HovalConnectApi


async def async_setup_entry(hass: HomeAssistant, entry: HovalConnectConfigEntry) -> bool:
    """Set up Hoval Connect from a config entry."""
    session = async_get_clientsession(hass)
    api = HovalConnectApi(session, entry.data["email"], entry.data["password"])

    coordinator = HovalDataCoordinator(hass, api)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = HovalRuntimeData(coordinator=coordinator, api=api)

    # Register a parent device for each plant so circuit devices can use via_device
    device_reg = dr.async_get(hass)
    for plant_id, plant_data in coordinator.data.plants.items():
        device_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, plant_id)},
            name=f"Hoval {plant_data.name}",
            manufacturer="Hoval",
            model="Plant",
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: HovalConnectConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
