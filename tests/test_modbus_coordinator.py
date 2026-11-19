"""Coordinator: Modbus data model, merge helper and local-only mode.

Runs without Home Assistant, reusing the stubbed ``runtime`` fixture of the
program-polling tests (it loads const, api, modbus, modbus_hv and coordinator).
"""

# ruff: noqa: F811  (pytest fixture imported by name, then used as a parameter)

from __future__ import annotations

import asyncio
import sys

import pytest

from .test_program_polling import _PATH, _coordinator, _programs, runtime  # noqa: F401

LIVE_BLOCK = [1, 50, 65, 45, 54, 50, 255, 255, 0, 1, 93, 231, 53]
NO_ERROR = [0, 0, 0, 0, 65535, 255, 255, 255, 65535]


def _reading(runtime, errors=None):
    return runtime.modbus_hv.decode_hv(LIVE_BLOCK, None, errors)


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakePoller:
    def __init__(self, runtime, units=(520,), reading=None):
        self.runtime = runtime
        self.units = list(units)
        self.reading = reading if reading is not None else _reading(runtime)
        self.client = _FakeClient()
        self.discover_calls = 0
        self.poll_error = None
        self.discover_errors = []
        self.writes = []

    async def discover(self):
        self.discover_calls += 1
        if self.discover_errors:
            raise self.discover_errors.pop(0)
        return list(self.units)

    async def poll(self, unit_ids):
        if self.poll_error is not None:
            raise self.poll_error
        return {unit: self.reading for unit in unit_ids}

    async def write(self, unit_id, name, value):
        self.writes.append((unit_id, name, value))


def _local(runtime, poller):
    return runtime.coordinator.HovalDataCoordinator(
        object(), None, modbus=poller, local_plant=("local_E1", "Hoval gw")
    )


def _update(coordinator):
    return asyncio.run(coordinator._async_update_data())


def test_cloud_only_path_unchanged(runtime):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [_programs("Winter")]})
    assert not coordinator.has_modbus and not coordinator.is_local
    data = _update(coordinator)
    circuit = data.plants["P1"].circuits[_PATH]
    assert circuit.active_week_name == "Winter"
    assert circuit.modbus is None
    assert data.cloud_ok is True
    assert api.program_calls[key] == 1
    assert coordinator.cloud_interval == runtime.const.DEFAULT_SCAN_INTERVAL.total_seconds()


def test_apply_reading_overwrites_cloud_fields(runtime):
    c = runtime.coordinator
    circuit = c.HovalCircuitData(
        "HV",
        "520.50.0",
        "HomeVent",
        active_program="constant",
        live_values={"airVolume": "30", "totalEnergy": "5"},
    )
    reading = _reading(runtime)
    c.apply_modbus_reading(circuit, reading)
    assert circuit.modbus is reading
    assert circuit.live_values["airVolume"] == "45"
    assert circuit.live_values["totalEnergy"] == "5"
    assert circuit.live_values["humidityActual"] == "50"
    assert circuit.live_values["humidityTarget"] == "54"
    assert circuit.live_values["outsideTemperature"] == "9.3"
    assert circuit.live_values["exhaustTemp"] == "23.1"
    assert circuit.active_program == "week1"
    assert circuit.constant_value == 50.0
    assert circuit.eco_mode_value == 65.0
    assert circuit.target_value == 45.0
    assert circuit.is_air_quality_guided is False


def test_apply_reading_removes_unknown_values(runtime):
    c = runtime.coordinator
    circuit = c.HovalCircuitData("HV", "520.50.0", "HomeVent", live_values={"airVolume": "30"})
    block = list(LIVE_BLOCK)
    block[3] = 255  # air volume unknown
    block[8] = 1  # air-quality control on
    c.apply_modbus_reading(circuit, runtime.modbus_hv.decode_hv(block, None, None))
    assert "airVolume" not in circuit.live_values
    assert circuit.target_value is None
    assert circuit.is_air_quality_guided is True


def test_apply_none_blanks_modbus_fields(runtime):
    c = runtime.coordinator
    circuit = c.HovalCircuitData("HV", "520.50.0", "HomeVent", live_values={"totalEnergy": "5"})
    c.apply_modbus_reading(circuit, _reading(runtime))
    c.apply_modbus_reading(circuit, None)
    assert circuit.modbus is None
    assert circuit.live_values == {"totalEnergy": "5"}
    assert circuit.active_program is None
    assert circuit.constant_value is None and circuit.eco_mode_value is None
    assert circuit.target_value is None
    assert circuit.is_air_quality_guided is False


def test_local_poll_builds_plant(runtime):
    poller = _FakePoller(runtime)
    coordinator = _local(runtime, poller)
    assert coordinator.is_local and coordinator.has_modbus
    data = _update(coordinator)
    plant = data.plants["local_E1"]
    circuit = plant.circuits["520.50.0"]
    assert plant.name == "Hoval gw"
    assert circuit.circuit_type == "HV"
    assert circuit.name == runtime.const.CIRCUIT_TYPE_NAMES["HV"]
    assert plant.is_online is True and plant.has_error is False
    assert data.cloud_ok is False
    assert circuit.modbus is poller.reading


def test_local_multiple_units_get_name_suffix(runtime):
    coordinator = _local(runtime, _FakePoller(runtime, units=(520, 521)))
    circuits = _update(coordinator).plants["local_E1"].circuits
    base = runtime.const.CIRCUIT_TYPE_NAMES["HV"]
    assert circuits["520.50.0"].name == f"{base} 520"
    assert circuits["521.50.0"].name == f"{base} 521"


def test_local_unit_without_reading_is_left_out(runtime):
    poller = _FakePoller(runtime, units=(520, 521))

    async def poll(unit_ids):
        return {520: poller.reading, 521: None}

    poller.poll = poll
    circuits = _update(_local(runtime, poller)).plants["local_E1"].circuits
    assert list(circuits) == ["520.50.0"]


def test_local_active_error_sets_plant_error(runtime):
    active = [100, 1201, 0, 0, 520, 50, 0, 1, 41]
    poller = _FakePoller(runtime, reading=_reading(runtime, [active, NO_ERROR]))
    data = _update(_local(runtime, poller))
    assert data.plants["local_E1"].has_error is True


@pytest.mark.parametrize("error", [OSError("down"), TimeoutError("slow"), "modbus"])
def test_local_modbus_failure_raises_update_failed(runtime, error):
    poller = _FakePoller(runtime)
    poller.poll_error = runtime.modbus.HovalModbusError("busy") if error == "modbus" else error
    with pytest.raises(sys_update_failed(runtime)):
        _update(_local(runtime, poller))


def sys_update_failed(runtime):
    return sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed


def test_local_rediscovers_after_failed_discovery(runtime):
    poller = _FakePoller(runtime)
    poller.discover_errors = [runtime.modbus.HovalModbusError("busy")]
    coordinator = _local(runtime, poller)
    with pytest.raises(sys_update_failed(runtime)):
        _update(coordinator)
    data = _update(coordinator)
    assert "520.50.0" in data.plants["local_E1"].circuits
    assert poller.discover_calls == 2
    # Discovery result is cached afterwards.
    _update(coordinator)
    assert poller.discover_calls == 2


def test_local_empty_discovery_raises_and_retries(runtime):
    poller = _FakePoller(runtime, units=())
    coordinator = _local(runtime, poller)
    with pytest.raises(sys_update_failed(runtime)):
        _update(coordinator)
    poller.units = [520]
    assert "520.50.0" in _update(coordinator).plants["local_E1"].circuits


def test_hv_write_delegates_with_unit_from_path(runtime):
    poller = _FakePoller(runtime)
    coordinator = _local(runtime, poller)
    asyncio.run(coordinator.async_hv_write("local_E1", "520.50.0", "constant_modulation", 55))
    assert poller.writes == [(520, "constant_modulation", 55)]


def test_close_closes_modbus_client(runtime):
    poller = _FakePoller(runtime)
    asyncio.run(_local(runtime, poller).async_close())
    assert poller.client.closed
    # No Modbus: closing is a no-op.
    asyncio.run(_coordinator(runtime, {("P1", _PATH): [None]})[0].async_close())


def test_module_exports(runtime):
    assert "hv_active_error" in runtime.const.MODBUS_BACKED_KEYS
    assert len(runtime.const.MODBUS_BACKED_KEYS) == 18
