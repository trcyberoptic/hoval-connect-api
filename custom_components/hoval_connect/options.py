"""Validated readers for persisted integration options."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .const import (
    CONF_HOST,
    CONF_MODBUS_HOST,
    CONF_MODBUS_PLANT_ID,
    CONF_MODBUS_PORT,
    CONF_MODBUS_SCAN_INTERVAL,
    CONF_MODBUS_UNIT_ID,
    CONF_MODE,
    CONF_OVERRIDE_DURATION,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_TURN_ON_MODE,
    CONF_UNIT_ID,
    DEFAULT_MODBUS_PORT,
    DEFAULT_MODBUS_SCAN_INTERVAL,
    DEFAULT_MODBUS_UNIT_ID,
    DEFAULT_OVERRIDE_DURATION,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TURN_ON_MODE,
    DURATION_END_OF_PHASE,
    DURATION_FOUR_HOURS,
    DURATION_MIDNIGHT,
    MODBUS_SCAN_INTERVAL_OPTIONS,
    MODE_LOCAL,
    SCAN_INTERVAL_OPTIONS,
    TURN_ON_RESUME,
    TURN_ON_WEEK1,
    TURN_ON_WEEK2,
)


def get_scan_interval(options: Mapping[str, Any]) -> int:
    """Return a supported interval, including legacy numeric strings."""
    default = int(DEFAULT_SCAN_INTERVAL.total_seconds())
    value = options.get(CONF_SCAN_INTERVAL, default)
    # Reject floats and booleans rather than silently rounding a corrupt value.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return default
    try:
        seconds = int(value)
    except (ValueError, OverflowError):
        return default
    return seconds if seconds in SCAN_INTERVAL_OPTIONS else default


def get_override_duration(options: Mapping[str, Any]) -> str:
    """Return a valid override duration even when stored options are corrupt."""
    value = options.get(CONF_OVERRIDE_DURATION, DEFAULT_OVERRIDE_DURATION)
    if isinstance(value, str) and value in (
        DURATION_END_OF_PHASE,
        DURATION_FOUR_HOURS,
        DURATION_MIDNIGHT,
    ):
        return value
    return DEFAULT_OVERRIDE_DURATION


def get_turn_on_mode(options: Mapping[str, Any]) -> str:
    """Return the configured turn-on behavior or its documented default."""
    value = options.get(CONF_TURN_ON_MODE, DEFAULT_TURN_ON_MODE)
    if isinstance(value, str) and value in (TURN_ON_RESUME, TURN_ON_WEEK1, TURN_ON_WEEK2):
        return value
    return DEFAULT_TURN_ON_MODE


@dataclass(frozen=True)
class ModbusSettings:
    """Address of the TTE-GW Modbus TCP gateway."""

    host: str
    port: int
    unit_id: int


def is_local_entry(data: Mapping[str, Any]) -> bool:
    """Return True for a local-only entry (no Hoval cloud account)."""
    return data.get(CONF_MODE) == MODE_LOCAL


def _int_in_range(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return default
    try:
        number = int(value)
    except (ValueError, OverflowError):
        return default
    return number if low <= number <= high else default


def get_modbus_settings(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> ModbusSettings | None:
    """Return the gateway address, or None when no Modbus gateway is configured.

    Local entries keep it in `data`; cloud entries (hybrid) in `options`.
    """
    if is_local_entry(data):
        source: Mapping[str, Any] = data
        host_key, port_key, unit_key = CONF_HOST, CONF_PORT, CONF_UNIT_ID
    else:
        source = options
        host_key, port_key, unit_key = CONF_MODBUS_HOST, CONF_MODBUS_PORT, CONF_MODBUS_UNIT_ID
    host = source.get(host_key)
    if not isinstance(host, str) or not host.strip():
        return None
    return ModbusSettings(
        host=host.strip(),
        port=_int_in_range(source.get(port_key), DEFAULT_MODBUS_PORT, 1, 65535),
        unit_id=_int_in_range(source.get(unit_key), DEFAULT_MODBUS_UNIT_ID, 0, 255),
    )


def get_modbus_scan_interval(options: Mapping[str, Any]) -> int:
    """Return a supported Modbus polling interval in seconds."""
    value = options.get(CONF_MODBUS_SCAN_INTERVAL, DEFAULT_MODBUS_SCAN_INTERVAL)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return DEFAULT_MODBUS_SCAN_INTERVAL
    try:
        seconds = int(value)
    except (ValueError, OverflowError):
        return DEFAULT_MODBUS_SCAN_INTERVAL
    return seconds if seconds in MODBUS_SCAN_INTERVAL_OPTIONS else DEFAULT_MODBUS_SCAN_INTERVAL


def get_modbus_plant_id(options: Mapping[str, Any]) -> str | None:
    """Return the plant the Modbus gateway belongs to, if one was chosen."""
    value = options.get(CONF_MODBUS_PLANT_ID)
    return value if isinstance(value, str) and value else None
