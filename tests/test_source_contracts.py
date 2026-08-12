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

    def test_hvac_action_reads_circuit_dto_status(self):
        # Status comes from the circuit list's `circuitStatus`, never from
        # live-values — that payload has no status key at all (verified live).
        src = _read("climate.py")
        assert "circuit.circuit_status" in src
        assert 'live_values.get("status")' not in src

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


class TestTemporaryChangeExposed:
    """The cloud reports a running override in the circuit DTO's `temporaryChange`.

    It went unparsed until 2026-08-09, which is why the bundled Blueprint had to
    track a boost with its own input_boolean/input_datetime helpers.
    """

    def test_coordinator_parses_the_field(self):
        src = _read("coordinator.py")
        assert 'circuit.get("temporaryChange")' in src
        assert "temporary_change_end" in src

    def test_binary_sensor_reports_it(self):
        src = _read("binary_sensor.py")
        assert "circuit.temporary_change_end is not None" in src

    def test_end_time_is_a_timestamp_sensor(self):
        src = _read("sensor.py")
        assert 'key="temporary_change_end"' in src
        assert "SensorDeviceClass.TIMESTAMP" in src

    def test_both_sensor_classes_coerce_timestamps(self):
        """A TIMESTAMP sensor returning a str raises in HA and the entity is dropped.

        v1.0.2 shipped the circuit-level timestamp description while only the
        plant-level class coerced, so the entity died at setup with
        `'str' object has no attribute 'tzinfo'`.
        """
        src = _read("sensor.py")
        assert src.count("return _coerce_timestamp(val)") == 2

    def test_translated_everywhere(self):
        import json

        for f in ("strings.json", "translations/en.json", "translations/de.json"):
            data = json.loads(_read(f))
            assert "temporary_change" in data["entity"]["binary_sensor"], f
            assert "temporary_change_end" in data["entity"]["sensor"], f


class TestRawDatapoints:
    """The ventilation operating state is only readable as a raw datapoint.

    Addresses are `<circuitPath>.<DatapointId>`; the Modbus register number is
    NOT accepted and returns an empty object instead of an error, which is what
    made this endpoint look dead for a whole session.
    """

    def test_address_is_built_from_circuit_path_not_register(self):
        src = _read("coordinator.py")
        assert 'f"{path}.{i}" for i in dp_ids' in src

    def test_not_fitted_marker_is_dropped(self):
        src = _read("sensor.py")
        assert "def _u8_percent" in src
        # The CO2/VOC sensors must route through it, or they report 255 %.
        for dp in ("37606", "37608", "37611"):
            assert f'_u8_percent(c.datapoints.get("{dp}"))' in src

    def test_enum_states_translated_everywhere(self):
        import json

        for f in ("strings.json", "translations/en.json", "translations/de.json"):
            sensors = json.loads(_read(f))["entity"]["sensor"]
            for key in ("hv_control_state", "hv_operating_selection"):
                assert "state" in sensors[key], f"{key} has no state labels in {f}"
            assert "coolvent" in sensors["hv_control_state"]["state"], f


class TestRetryCoversNonStandardStatuses:
    def test_retry_is_a_range_not_an_enumeration(self):
        """Hoval's gateway emits 599; enumerating codes reintroduced the outage bug."""
        src = _read("api.py")
        assert "def _is_retryable_status" in src
        assert "_RETRYABLE_STATUS_CODES" not in src


class TestControlRefreshBlocksUntilFresh:
    """async_control_and_refresh must not return before fresh data arrived.

    v1.0.0 made the post-control refresh a fire-and-forget task; entities
    clear their _pending_* optimistic state when the call returns, so the
    UI snapped back to stale data for ~2-4 s after every control action.
    The bundled summer-boost Blueprint read that dip as a manual override,
    released its latch, re-boosted, and spammed a notification every few
    seconds. The lock must still be released before the settle+refresh.
    """

    def test_refresh_awaited_not_fire_and_forget(self):
        src = _read("coordinator.py")
        body = src.split("async def async_control_and_refresh", 1)[1].split(
            "async def _async_update_data", 1
        )[0]
        assert "async_create_task" not in body
        assert "await self.async_request_refresh()" in body


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
