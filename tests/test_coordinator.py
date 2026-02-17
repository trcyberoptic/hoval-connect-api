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
sys.modules["homeassistant.helpers.dispatcher"] = ha_mock
sys.modules["homeassistant.util"] = ha_mock
sys.modules["homeassistant.util.dt"] = ha_mock
sys.modules["aiohttp"] = ha_mock
sys.modules["voluptuous"] = ha_mock

# Now we can import the pure functions and dataclasses
from custom_components.hoval_connect.coordinator import (  # noqa: E402
    _V1_PROGRAM_MAP,
    HovalCircuitData,
    HovalEventData,
    _parse_event,
    _resolve_active_program_value,
    resolve_fan_speed,
)


class TestResolveFanSpeed:
    """Tests for resolve_fan_speed()."""

    def test_none_circuit_returns_default(self):
        assert resolve_fan_speed(None) == 40

    def test_live_air_volume(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            live_values={"airVolume": "65"},
        )
        assert resolve_fan_speed(circuit) == 65

    def test_live_air_volume_float(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            live_values={"airVolume": "72.5"},
        )
        assert resolve_fan_speed(circuit) == 72

    def test_live_zero_falls_through(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            live_values={"airVolume": "0"},
            target_air_volume=50,
        )
        assert resolve_fan_speed(circuit) == 50

    def test_target_air_volume_fallback(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            target_air_volume=80,
        )
        assert resolve_fan_speed(circuit) == 80

    def test_program_air_volume_fallback(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            program_air_volume=55.0,
        )
        assert resolve_fan_speed(circuit) == 55

    def test_all_none_returns_default(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
        )
        assert resolve_fan_speed(circuit) == 40

    def test_minimum_is_one(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            live_values={"airVolume": "0"},
            target_air_volume=0,
            program_air_volume=0.0,
        )
        assert resolve_fan_speed(circuit) == 40  # falls through to default


class TestResolveActiveProgramValue:
    """Tests for _resolve_active_program_value()."""

    def _make_programs(
        self,
        phases: list[dict] | None = None,
        day_name: str = "Normal",
    ) -> dict:
        """Build a minimal programs structure."""
        if phases is None:
            phases = [
                {
                    "start": {"hours": 6, "minutes": 0},
                    "end": {"hours": 22, "minutes": 0},
                    "value": 60,
                },
                {
                    "start": {"hours": 22, "minutes": 0},
                    "end": {"hours": 23, "minutes": 59},
                    "value": 30,
                },
            ]
        return {
            "week1": {
                "name": "Woche 1",
                "dayProgramIds": [1, 1, 1, 1, 1, 2, 2],  # Mon-Fri=1, Sat-Sun=2
            },
            "dayPrograms": {
                "dayConfigurations": [
                    {"id": 1, "name": day_name, "phases": phases},
                    {
                        "id": 2,
                        "name": "Weekend",
                        "phases": [
                            {
                                "start": {"hours": 8, "minutes": 0},
                                "end": {"hours": 22, "minutes": 0},
                                "value": 50,
                            },
                        ],
                    },
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


class TestV1ProgramMap:
    """Tests for _V1_PROGRAM_MAP normalization."""

    def test_tte_controlled_maps_to_week1(self):
        assert _V1_PROGRAM_MAP.get("tteControlled", "tteControlled") == "week1"

    def test_time_programs_maps_to_week1(self):
        assert _V1_PROGRAM_MAP.get("timePrograms", "timePrograms") == "week1"

    def test_v3_values_pass_through(self):
        for v3_key in ("week1", "week2", "ecoMode", "standby", "constant"):
            assert _V1_PROGRAM_MAP.get(v3_key, v3_key) == v3_key

    def test_none_passes_through(self):
        assert _V1_PROGRAM_MAP.get(None, None) is None


class TestParseEvent:
    """Tests for _parse_event() and HovalEventData."""

    def test_parse_full_event(self):
        raw = {
            "eventType": "warning",
            "description": "Filterwechsel erforderlich",
            "timeOccurred": "2026-02-17T10:30:00Z",
            "timeResolved": None,
            "sourcePath": "520.50.0",
            "code": 12345,
        }
        ev = _parse_event(raw)
        assert ev.event_type == "warning"
        assert ev.description == "Filterwechsel erforderlich"
        assert ev.time_occurred == "2026-02-17T10:30:00Z"
        assert ev.time_resolved is None
        assert ev.source_path == "520.50.0"
        assert ev.code == 12345

    def test_active_when_not_resolved(self):
        ev = _parse_event({"eventType": "warning", "timeResolved": None})
        assert ev.is_active is True

    def test_inactive_when_resolved(self):
        ev = _parse_event({"eventType": "warning", "timeResolved": "2026-02-17T12:00:00Z"})
        assert ev.is_active is False

    def test_active_when_time_resolved_missing(self):
        """If API doesn't return timeResolved at all, event is active."""
        ev = _parse_event({"eventType": "blocking"})
        assert ev.is_active is True

    def test_parse_empty_dict(self):
        ev = _parse_event({})
        assert ev.event_type is None
        assert ev.description is None
        assert ev.time_occurred is None
        assert ev.time_resolved is None
        assert ev.source_path is None
        assert ev.code is None
        assert ev.is_active is True  # no timeResolved → active

    def test_default_event_data_is_active(self):
        """Default HovalEventData has no timeResolved so is active."""
        ev = HovalEventData()
        assert ev.is_active is True

    def test_resolved_event_data(self):
        ev = HovalEventData(time_resolved="2026-02-17T12:00:00Z")
        assert ev.is_active is False
