"""Source contracts: pin live-value keys and port decisions that runtime tests can't reach.

These read the component source as text. Crude, but they run without
homeassistant installed and fail loudly if a port regresses a key decision.
"""

from __future__ import annotations

import pathlib

_BASE = pathlib.Path("custom_components/hoval_connect")


def _read(module_name: str) -> str:
    return (_BASE / module_name).read_text(encoding="utf-8")


class TestClimateLiveValueKeys:
    def test_current_temperature_prefers_room_temp_actual(self):
        src = _read("climate.py")
        assert '"roomTempActual"' in src
        # legacy keys stay as fallbacks
        assert '"actualTemperature"' in src

    def test_target_temperature_prefers_room_temp_target(self):
        src = _read("climate.py")
        assert '"roomTempTarget"' in src
        assert '"targetTemperature"' in src

    def test_hvac_action_uses_status_key(self):
        src = _read("climate.py")
        assert 'live_values.get("status")' in src

    def test_pending_temperature_survived_the_port(self):
        assert "_pending_temperature" in _read("climate.py")


class TestConfigFlowHardening:
    def test_validation_has_outer_timeout(self):
        src = _read("config_flow.py")
        assert "asyncio.timeout(_VALIDATION_TIMEOUT_S)" in src
        assert src.count("asyncio.timeout(_VALIDATION_TIMEOUT_S)") == 2  # user + reauth

    def test_reauth_pins_account(self):
        assert "wrong_account" in _read("config_flow.py")

    def test_wrong_account_translated_everywhere(self):
        import json

        for f in ("strings.json", "translations/en.json", "translations/de.json"):
            data = json.loads(_read(f))
            assert "wrong_account" in data["config"]["error"], f


class TestSensorAdditions:
    def test_new_sensor_keys_exist(self):
        src = _read("sensor.py")
        for key in (
            "room_temp_actual",
            "operating_hours_el_heater",
            "operation_cycles_el_heater",
            "heat_amount_el_heater",
            "energy_el_heater",
            "el_heater_active",
        ):
            assert f'key="{key}"' in src, key

    def test_total_increasing_negative_guard(self):
        src = _read("sensor.py")
        assert "if is_counter and num < 0" in src
        # Unit-less counters (operation cycles) must not escape through the
        # string branch before the guard.
        assert "native_unit_of_measurement is None and not is_counter" in src

    def test_new_keys_translated_everywhere(self):
        import json

        for f in ("strings.json", "translations/en.json", "translations/de.json"):
            sensors = json.loads(_read(f))["entity"]["sensor"]
            for key in ("room_temp_actual", "energy_el_heater", "el_heater_active"):
                assert key in sensors, f"{key} missing in {f}"


class TestOverrideLifecycle:
    def test_override_has_ttl(self):
        src = _read("coordinator.py")
        assert "_MODE_OVERRIDE_TTL_S" in src

    def test_overrides_pruned_at_end_not_cleared(self):
        """Overrides are pruned at the END of a successful poll, and only the
        ones set BEFORE the poll began. An unconditional clear() (at the start
        OR the end) races with control actions: an override set while a poll
        is in flight would be wiped by that poll's pre-change data snapshot.
        """
        src = _read("coordinator.py")
        body = src.split("async def _async_update_data", 1)[1]
        assert "_mode_override.clear()" not in body
        # Poll-start timestamp taken before the fetch, compared during pruning.
        assert "poll_start = time.monotonic()" in body
        assert "entry[1] >= poll_start" in body


class TestWaterHeater:
    def test_platform_registered(self):
        assert "Platform.WATER_HEATER" in _read("__init__.py")

    def test_uses_v4_duration_constant_not_lowercase_midnight(self):
        src = _read("water_heater.py")
        assert "DURATION_MIDNIGHT" in src
        assert (
            '"midnight"' not in src
        )  # fork's v3 literal would silently degrade on our v4 body builder

    def test_no_duplicate_reset_service(self):
        assert "reset_ww_boost" not in _read("water_heater.py")

    def test_translated_everywhere(self):
        import json

        for f in ("strings.json", "translations/en.json", "translations/de.json"):
            data = json.loads(_read(f))
            assert "hot_water" in data["entity"]["water_heater"], f


class TestProgramSelect:
    def test_ww_circuits_get_select(self):
        assert "CIRCUIT_TYPE_WW" in _read("select.py")

    def test_program_key_validated_before_send(self):
        src = _read("select.py")
        assert "VALID_API_PROGRAMS" in src

    def test_disambiguation_survived(self):
        assert 'f"{default} ({api_key})"' in _read("select.py")
