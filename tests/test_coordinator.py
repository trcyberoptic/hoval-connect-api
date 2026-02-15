"""Tests for the Hoval Connect coordinator logic (pure functions).

These tests cover the pure utility functions that don't depend on Home Assistant.
They can be run without homeassistant installed by using sys.path manipulation.
"""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import MagicMock

# Mock homeassistant modules so we can import the coordinator's pure functions
ha_mock = MagicMock()
sys.modules["homeassistant"] = ha_mock
sys.modules["homeassistant.config_entries"] = ha_mock
sys.modules["homeassistant.const"] = ha_mock
sys.modules["homeassistant.core"] = ha_mock
sys.modules["homeassistant.exceptions"] = ha_mock
sys.modules["homeassistant.helpers"] = ha_mock
sys.modules["homeassistant.helpers.update_coordinator"] = ha_mock
sys.modules["homeassistant.helpers.aiohttp_client"] = ha_mock
sys.modules["homeassistant.helpers.device_registry"] = ha_mock
sys.modules["homeassistant.util"] = ha_mock
sys.modules["homeassistant.util.dt"] = ha_mock
sys.modules["aiohttp"] = ha_mock
sys.modules["voluptuous"] = ha_mock

# Now we can import the pure functions and dataclasses
from custom_components.hoval_connect.coordinator import (  # noqa: E402
    HovalCircuitData,
    _resolve_active_program_value,
    resolve_fan_speed,
)


class TestResolveFanSpeed:
    """Tests for resolve_fan_speed()."""

    def test_none_circuit_returns_default(self):
        assert resolve_fan_speed(None) == 40

    def test_live_air_volume(self):
        circuit = HovalCircuitData(
            circuit_type="HV", path="1.2.3", name="Test",
            live_values={"airVolume": "65"},
        )
        assert resolve_fan_speed(circuit) == 65

    def test_live_air_volume_float(self):
        circuit = HovalCircuitData(
            circuit_type="HV", path="1.2.3", name="Test",
            live_values={"airVolume": "72.5"},
        )
        assert resolve_fan_speed(circuit) == 72

    def test_live_zero_falls_through(self):
        circuit = HovalCircuitData(
            circuit_type="HV", path="1.2.3", name="Test",
            live_values={"airVolume": "0"},
            target_air_volume=50,
        )
        assert resolve_fan_speed(circuit) == 50

    def test_target_air_volume_fallback(self):
        circuit = HovalCircuitData(
            circuit_type="HV", path="1.2.3", name="Test",
            target_air_volume=80,
        )
        assert resolve_fan_speed(circuit) == 80

    def test_program_air_volume_fallback(self):
        circuit = HovalCircuitData(
            circuit_type="HV", path="1.2.3", name="Test",
            program_air_volume=55.0,
        )
        assert resolve_fan_speed(circuit) == 55

    def test_all_none_returns_default(self):
        circuit = HovalCircuitData(
            circuit_type="HV", path="1.2.3", name="Test",
        )
        assert resolve_fan_speed(circuit) == 40

    def test_minimum_is_one(self):
        circuit = HovalCircuitData(
            circuit_type="HV", path="1.2.3", name="Test",
            live_values={"airVolume": "0"},
            target_air_volume=0,
            program_air_volume=0.0,
        )
        assert resolve_fan_speed(circuit) == 40  # falls through to default


class TestResolveActiveProgramValue:
    """Tests for _resolve_active_program_value()."""

    def _make_programs(
        self, phases: list[dict] | None = None, day_name: str = "Normal",
    ) -> dict:
        """Build a minimal programs structure."""
        if phases is None:
            phases = [
                {"start": {"hours": 6, "minutes": 0}, "end": {"hours": 22, "minutes": 0}, "value": 60},
                {"start": {"hours": 22, "minutes": 0}, "end": {"hours": 23, "minutes": 59}, "value": 30},
            ]
        return {
            "week1": {
                "name": "Woche 1",
                "dayProgramIds": [1, 1, 1, 1, 1, 2, 2],  # Mon-Fri=1, Sat-Sun=2
            },
            "dayPrograms": {
                "dayConfigurations": [
                    {"id": 1, "name": day_name, "phases": phases},
                    {"id": 2, "name": "Weekend", "phases": [
                        {"start": {"hours": 8, "minutes": 0}, "end": {"hours": 22, "minutes": 0}, "value": 50},
                    ]},
                ],
            },
        }

    def test_monday_morning(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)  # Monday
        week, day, value = _resolve_active_program_value(programs, now)
        assert week == "Woche 1"
        assert day == "Normal"
        assert value == 60

    def test_monday_night(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 23, 30)  # Monday
        week, day, value = _resolve_active_program_value(programs, now)
        assert week == "Woche 1"
        assert day == "Normal"
        assert value == 30

    def test_saturday(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 13, 12, 0)  # Saturday
        week, day, value = _resolve_active_program_value(programs, now)
        assert week == "Woche 1"
        assert day == "Weekend"
        assert value == 50

    def test_no_matching_phase(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 4, 0)  # Monday 4 AM
        week, day, value = _resolve_active_program_value(programs, now)
        assert week == "Woche 1"
        assert day == "Normal"
        assert value is None

    def test_empty_programs(self):
        programs = {}
        now = datetime(2024, 1, 8, 10, 0)
        week, day, value = _resolve_active_program_value(programs, now)
        assert week is None
        assert day is None
        assert value is None

    def test_empty_day_configurations(self):
        programs = {"dayPrograms": {"dayConfigurations": []}}
        now = datetime(2024, 1, 8, 10, 0)
        week, day, value = _resolve_active_program_value(programs, now)
        assert week is None
        assert day is None
        assert value is None

    def test_phase_boundary_start(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 6, 0)  # Exactly at phase start
        week, day, value = _resolve_active_program_value(programs, now)
        assert value == 60

    def test_phase_boundary_end(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 22, 0)  # Exactly at phase end/next start
        week, day, value = _resolve_active_program_value(programs, now)
        assert value == 30
