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
from custom_components.hoval_connect.const import (  # noqa: E402
    HV_AIR_VOLUME_MAX,
    HV_AIR_VOLUME_MIN,
    clamp_hv_air_volume,
)
from custom_components.hoval_connect.coordinator import (  # noqa: E402
    _V1_PROGRAM_MAP,
    HovalCircuitData,
    HovalEventData,
    _is_problem_event,
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
            target_value=50,
        )
        assert resolve_fan_speed(circuit) == 50

    def test_target_value_fallback(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            target_value=80,
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
            target_value=0,
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
        """Build a minimal programs structure with both week1 and week2."""
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
            "week2": {
                "name": "Sommer",
                "dayProgramIds": [3, 3, 3, 3, 3, 3, 3],  # Früh+Abend all week
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
                    {
                        "id": 3,
                        "name": "Früh+Abend",
                        "phases": [
                            {
                                "start": {"hours": 0, "minutes": 0},
                                "end": {"hours": 9, "minutes": 0},
                                "value": 40,
                            },
                            {
                                "start": {"hours": 9, "minutes": 0},
                                "end": {"hours": 19, "minutes": 0},
                                "value": 15,
                            },
                            {
                                "start": {"hours": 19, "minutes": 0},
                                "end": {"hours": 24, "minutes": 0},
                                "value": 40,
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

    def test_active_program_week2_picks_week2(self):
        """User has activeProgram=week2 → must read week2's day config, not week1's."""
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)  # Monday 10:00
        week, day, value = _resolve_active_program_value(programs, now, "week2")
        assert week == "Sommer"
        assert day == "Früh+Abend"
        assert value == 15  # 09:00–19:00 phase

    def test_active_program_week1_explicit(self):
        """Explicit week1 must behave identically to default."""
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)  # Monday
        week, day, value = _resolve_active_program_value(programs, now, "week1")
        assert week == "Woche 1"
        assert day == "Normal"
        assert value == 60

    def test_active_program_ecomode_falls_back_to_week1(self):
        """Non-weekly active programs (ecoMode, standby, …) fall back to week1."""
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)
        week, day, value = _resolve_active_program_value(programs, now, "ecoMode")
        assert week == "Woche 1"  # fallback
        assert day == "Normal"
        assert value == 60

    def test_default_active_program_none_falls_back_to_week1(self):
        """Backwards-compat: callers passing no active_program still get week1."""
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)
        week, day, value = _resolve_active_program_value(programs, now)
        assert week == "Woche 1"


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


class TestIsProblemEvent:
    """Tests for problem event classification."""

    def test_active_blocking_is_problem(self):
        assert _is_problem_event(HovalEventData(event_type="blocking")) is True

    def test_active_locking_is_problem(self):
        assert _is_problem_event(HovalEventData(event_type="locking")) is True

    def test_active_warning_is_problem(self):
        assert _is_problem_event(HovalEventData(event_type="warning")) is True

    def test_resolved_warning_is_not_problem(self):
        ev = HovalEventData(event_type="warning", time_resolved="2026-02-17T12:00:00Z")
        assert _is_problem_event(ev) is False

    def test_info_and_offline_are_not_problem(self):
        assert _is_problem_event(HovalEventData(event_type="info")) is False
        assert _is_problem_event(HovalEventData(event_type="offline")) is False

    def test_none_is_not_problem(self):
        assert _is_problem_event(None) is False

    def test_none_event_type_is_not_problem(self):
        assert _is_problem_event(HovalEventData(event_type=None)) is False


class TestClampHvAirVolume:
    """HA allows 1-14 %, the HV firmware band starts at 15 %."""

    def test_below_minimum_clamps_up(self):
        assert clamp_hv_air_volume(5) == HV_AIR_VOLUME_MIN
        assert clamp_hv_air_volume(14) == HV_AIR_VOLUME_MIN

    def test_zero_clamps_to_minimum(self):
        # 0 never reaches the clamp in fan.py (handled as turn_off first),
        # but the pure helper still has defined behavior.
        assert clamp_hv_air_volume(0) == HV_AIR_VOLUME_MIN

    def test_above_maximum_clamps_down(self):
        assert clamp_hv_air_volume(120) == HV_AIR_VOLUME_MAX

    def test_band_values_pass_through(self):
        for v in (15, 55, 100):
            assert clamp_hv_air_volume(v) == v

    def test_float_truncates_to_int(self):
        assert clamp_hv_air_volume(54.9) == 54


class TestResolveActiveProgramRobustness:
    """Nested schema drift must degrade to None fields, never raise.

    Ported from the GMH224 fork (their v0.21.1 audit finding F1): each case
    below crashed the old resolver (KeyError/AttributeError inside
    _fetch_circuit → gather(return_exceptions=True) silently discarded the
    whole circuit, including its already-fetched live values).
    """

    NOW = datetime(2026, 7, 20, 10, 0)  # a Monday

    def test_none_programs_returns_all_none(self):
        """programs=None (HTTP 204 / empty body) must not raise AttributeError."""
        assert _resolve_active_program_value(None, self.NOW) == (None, None, None)

    def test_empty_list_programs_returns_all_none(self):
        """programs=[] (HTTP 200 with body []) must not raise AttributeError.

        Hoval's May 2026 change made the programs endpoint return [] for
        non-programmable circuits (e.g. BL/boiler).
        """
        assert _resolve_active_program_value([], self.NOW) == (None, None, None)

    def test_int_programs_returns_all_none(self):
        """Any non-dict value must be handled gracefully (defensive)."""
        assert _resolve_active_program_value(42, self.NOW) == (None, None, None)

    def test_string_programs_returns_all_none(self):
        assert _resolve_active_program_value("programs", self.NOW) == (None, None, None)

    def test_day_config_missing_id(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"name": "no-id"}]},
            "week1": {"name": "W1", "dayProgramIds": [1]},
        }
        # Config unusable → week resolves, day/value do not.
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", None, None)

    def test_week_entry_is_a_list(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"id": 1, "name": "x", "phases": []}]},
            "week1": ["oops"],
        }
        assert _resolve_active_program_value(programs, self.NOW) == (None, None, None)

    def test_week_entry_missing(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"id": 1, "name": "x", "phases": []}]},
        }
        assert _resolve_active_program_value(programs, self.NOW) == (None, None, None)

    def test_day_program_ids_not_a_list(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"id": 1, "name": "x", "phases": []}]},
            "week1": {"name": "W1", "dayProgramIds": "1,2,3"},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", None, None)

    def test_phase_missing_start(self):
        programs = {
            "dayPrograms": {
                "dayConfigurations": [{"id": 1, "name": "Day", "phases": [{"value": 40}]}]
            },
            "week1": {"name": "W1", "dayProgramIds": [1] * 7},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", "Day", None)

    def test_phase_not_a_dict(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"id": 1, "name": "Day", "phases": ["oops"]}]},
            "week1": {"name": "W1", "dayProgramIds": [1] * 7},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", "Day", None)

    def test_phases_not_a_list(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"id": 1, "name": "Day", "phases": {"bad": 1}}]},
            "week1": {"name": "W1", "dayProgramIds": [1] * 7},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", "Day", None)

    def test_phase_time_values_not_numeric(self):
        programs = {
            "dayPrograms": {
                "dayConfigurations": [
                    {
                        "id": 1,
                        "name": "Day",
                        "phases": [
                            {
                                "start": {"hours": "x", "minutes": 0},
                                "end": {"hours": 22, "minutes": 0},
                                "value": 60,
                            }
                        ],
                    }
                ]
            },
            "week1": {"name": "W1", "dayProgramIds": [1] * 7},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", "Day", None)

    def test_day_configurations_not_a_list(self):
        programs = {
            "dayPrograms": {"dayConfigurations": {"bad": "shape"}},
            "week1": {"name": "W1", "dayProgramIds": [1]},
        }
        assert _resolve_active_program_value(programs, self.NOW) == (None, None, None)

    def test_day_config_entry_not_a_dict(self):
        programs = {
            "dayPrograms": {"dayConfigurations": ["oops", 42]},
            "week1": {"name": "W1", "dayProgramIds": [1]},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", None, None)

    def test_day_programs_not_a_dict(self):
        programs = {"dayPrograms": ["oops"], "week1": {"name": "W1"}}
        assert _resolve_active_program_value(programs, self.NOW) == (None, None, None)

    def test_mixed_valid_and_invalid_day_configs(self):
        """Valid configs are still resolved when malformed siblings exist."""
        programs = {
            "dayPrograms": {
                "dayConfigurations": [
                    {"name": "no-id"},
                    {
                        "id": 1,
                        "name": "Good",
                        "phases": [
                            {
                                "start": {"hours": 6, "minutes": 0},
                                "end": {"hours": 22, "minutes": 0},
                                "value": 55,
                            }
                        ],
                    },
                ]
            },
            "week1": {"name": "W1", "dayProgramIds": [1] * 7},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", "Good", 55)


from datetime import timedelta as _timedelta  # noqa: E402

from custom_components.hoval_connect.const import (  # noqa: E402
    EVENTS_CACHE_TTL,
    PROGRAM_CACHE_TTL,
    WEATHER_CACHE_TTL,
)


class TestCacheTtls:
    def test_events_ttl_longer_than_fastest_poll(self):
        assert _timedelta(minutes=1) <= EVENTS_CACHE_TTL

    def test_weather_ttl_longer_than_events(self):
        assert WEATHER_CACHE_TTL > EVENTS_CACHE_TTL

    def test_program_ttl_unchanged(self):
        assert _timedelta(minutes=5) == PROGRAM_CACHE_TTL


class TestRawDatapointConstants:
    """The HV operating state (CoolVent, Sommerfeuchte, …) is only readable as a
    raw controller datapoint, addressed `<circuitPath>.<DatapointId>`."""

    def test_status_datapoint_requested_for_hv(self):
        from custom_components.hoval_connect.const import CIRCUIT_DATAPOINT_IDS

        assert "39652" in CIRCUIT_DATAPOINT_IDS["HV"]

    def test_modbus_register_is_never_used_as_an_identifier(self):
        """23631 is the register for CAN unit 520 only — it shifts per unit and
        the cloud silently answers {} for it."""
        from custom_components.hoval_connect.const import CIRCUIT_DATAPOINT_IDS

        every_id = {i for ids in CIRCUIT_DATAPOINT_IDS.values() for i in ids}
        assert not every_id & {"23631", "23540", "23553", "23566"}

    def test_control_state_enum_covers_0_to_8(self):
        from custom_components.hoval_connect.const import HV_CONTROL_STATES

        assert [*HV_CONTROL_STATES] == [str(i) for i in range(9)]
        assert HV_CONTROL_STATES["5"] == "coolvent"
        assert HV_CONTROL_STATES["7"] == "summer_humidity"

    def test_operating_selection_matches_active_program_names(self):
        """40650 = 2 was observed together with activeProgram "week2"."""
        from custom_components.hoval_connect.const import HV_OPERATING_SELECTIONS

        assert HV_OPERATING_SELECTIONS["2"] == "week2"

    def test_circuit_data_defaults_to_no_datapoints(self):
        circuit = HovalCircuitData(circuit_type="HK", path="1.2.3", name="Test")
        assert circuit.datapoints == {}
