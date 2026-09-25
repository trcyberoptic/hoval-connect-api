"""Validated readers for persisted integration options."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    CONF_OVERRIDE_DURATION,
    CONF_SCAN_INTERVAL,
    CONF_TURN_ON_MODE,
    DEFAULT_OVERRIDE_DURATION,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TURN_ON_MODE,
    DURATION_END_OF_PHASE,
    DURATION_FOUR_HOURS,
    DURATION_MIDNIGHT,
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
