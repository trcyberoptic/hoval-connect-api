"""Tests for config-flow schema logic (no Home Assistant required)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# test_api.py/test_coordinator.py stub voluptuous with a MagicMock via
# sys.modules. This file needs the REAL voluptuous (it is installed as a test
# dep), so purge a stale mock before importing — order-independent either way.
if isinstance(sys.modules.get("voluptuous"), MagicMock):
    del sys.modules["voluptuous"]
import voluptuous as vol  # noqa: E402

# Mock homeassistant modules so we can import without HA installed
ha_mock = MagicMock()
sys.modules.setdefault("homeassistant", ha_mock)
sys.modules.setdefault("homeassistant.config_entries", ha_mock)
sys.modules.setdefault("homeassistant.const", ha_mock)
sys.modules.setdefault("homeassistant.core", ha_mock)
sys.modules.setdefault("homeassistant.exceptions", ha_mock)
sys.modules.setdefault("homeassistant.helpers", ha_mock)
sys.modules.setdefault("homeassistant.helpers.update_coordinator", ha_mock)
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", ha_mock)
sys.modules.setdefault("homeassistant.helpers.device_registry", ha_mock)
sys.modules.setdefault("homeassistant.helpers.dispatcher", ha_mock)
sys.modules.setdefault("homeassistant.util", ha_mock)
sys.modules.setdefault("homeassistant.util.dt", ha_mock)

from custom_components.hoval_connect.const import SCAN_INTERVAL_OPTIONS  # noqa: E402


def _scan_interval_schema() -> vol.Schema:
    """Mirror of the options-flow scan_interval validator (config_flow.py)."""
    return vol.Schema(
        {vol.Required("scan_interval"): vol.All(vol.Coerce(int), vol.In(SCAN_INTERVAL_OPTIONS))}
    )


class TestScanIntervalSchema:
    """The HA frontend submits dict-select keys as STRINGS ("60"), not ints."""

    def test_string_from_frontend_is_coerced_and_accepted(self):
        result = _scan_interval_schema()({"scan_interval": "60"})
        assert result["scan_interval"] == 60
        assert isinstance(result["scan_interval"], int)

    def test_int_value_accepted(self):
        assert _scan_interval_schema()({"scan_interval": 120})["scan_interval"] == 120

    def test_unknown_value_rejected(self):
        with pytest_raises_invalid():
            _scan_interval_schema()({"scan_interval": "45"})


def pytest_raises_invalid():
    import pytest

    return pytest.raises(vol.Invalid)


class TestOptionsFlowSourceContract:
    """config_flow.py must coerce the frontend string before vol.In."""

    def test_scan_interval_uses_coerce(self):
        import pathlib

        src = pathlib.Path("custom_components/hoval_connect/config_flow.py").read_text(
            encoding="utf-8"
        )
        assert "vol.All(vol.Coerce(int), vol.In(SCAN_INTERVAL_OPTIONS))" in src
