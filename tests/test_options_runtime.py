"""Persisted options always produce values supported by the integration."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def options(monkeypatch):
    component = Path(__file__).resolve().parents[1] / "custom_components" / "hoval_connect"
    package_name = "_options_component"
    package = ModuleType(package_name)
    package.__path__ = [str(component)]
    monkeypatch.setitem(sys.modules, package_name, package)
    loaded = {}
    for module_name in ("const", "options"):
        name = f"{package_name}.{module_name}"
        spec = importlib.util.spec_from_file_location(name, component / f"{module_name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[module_name] = module
    return loaded["options"]


@pytest.mark.parametrize("value", [900, 1800, 3600, "900", "1800", "3600"])
def test_supported_intervals_survive(options, value):
    assert options.get_scan_interval({"scan_interval": value}) == int(value)


@pytest.mark.parametrize(
    "value", [None, True, False, [], {}, "", "bad", "60.5", 60.5, 0, -1, 45, 10**100]
)
def test_corrupt_intervals_fall_back(options, value):
    assert options.get_scan_interval({"scan_interval": value}) == 900


# 30..300 s were selectable until Hoval's 15-minute floor (#24). A stored
# legacy value must fall back to the floor, not keep polling faster.
@pytest.mark.parametrize("value", [30, 60, 120, 300, "30", "60", "120", "300"])
def test_legacy_fast_intervals_fall_back_to_floor(options, value):
    assert options.get_scan_interval({"scan_interval": value}) == 900


@pytest.mark.parametrize("value", [None, [], {}, False, 42, "unknown"])
def test_corrupt_behavior_options_fall_back(options, value):
    assert options.get_override_duration({"override_duration": value}) == "endOfPhase"
    assert options.get_turn_on_mode({"turn_on_mode": value}) == "resume"


def test_valid_durations_and_turn_on_modes_survive(options):
    for value in ("endOfPhase", "FOUR", "MIDNIGHT"):
        assert options.get_override_duration({"override_duration": value}) == value
    for value in ("resume", "week1", "week2"):
        assert options.get_turn_on_mode({"turn_on_mode": value}) == value


def test_get_modbus_settings_cloud_and_local(options):
    local = {"mode": "local", "host": " gw.local ", "port": 1502, "unit_id": 2}
    assert options.is_local_entry(local)
    assert options.get_modbus_settings(local, {}) == options.ModbusSettings("gw.local", 1502, 2)

    cloud = {"email": "a@b.c", "password": "x"}
    assert not options.is_local_entry(cloud)
    assert options.get_modbus_settings(cloud, {}) is None
    assert options.get_modbus_settings(cloud, {"modbus_host": ""}) is None
    assert options.get_modbus_settings(cloud, {"modbus_host": "  "}) is None
    opts = {"modbus_host": "192.0.2.10", "modbus_port": "503", "modbus_unit_id": 3}
    assert options.get_modbus_settings(cloud, opts) == options.ModbusSettings("192.0.2.10", 503, 3)
    # Defaults and corrupt values.
    assert options.get_modbus_settings(cloud, {"modbus_host": "h", "modbus_port": True}) == (
        options.ModbusSettings("h", 502, 1)
    )
    # A cloud entry never reads the local keys from its options.
    assert options.get_modbus_settings(cloud, {"host": "h"}) is None


@pytest.mark.parametrize("value", [45, "x", True, None, [], 10**100, 0])
def test_get_modbus_scan_interval_rejects_unknown(options, value):
    assert options.get_modbus_scan_interval({"modbus_scan_interval": value}) == 30


@pytest.mark.parametrize("value", [10, 30, 60, 120, "60"])
def test_get_modbus_scan_interval_keeps_allowed(options, value):
    assert options.get_modbus_scan_interval({"modbus_scan_interval": value}) == int(value)
    assert options.get_modbus_scan_interval({}) == 30


def test_get_modbus_plant_id(options):
    assert options.get_modbus_plant_id({"modbus_plant_id": "p1"}) == "p1"
    assert options.get_modbus_plant_id({"modbus_plant_id": ""}) is None
    assert options.get_modbus_plant_id({}) is None
