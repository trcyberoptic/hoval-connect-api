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


@pytest.mark.parametrize("value", [30, 60, 120, 300, "30", "60", "120", "300"])
def test_supported_intervals_survive(options, value):
    assert options.get_scan_interval({"scan_interval": value}) == int(value)


@pytest.mark.parametrize(
    "value", [None, True, False, [], {}, "", "bad", "60.5", 60.5, 0, -1, 45, 10**100]
)
def test_corrupt_intervals_fall_back(options, value):
    assert options.get_scan_interval({"scan_interval": value}) == 60


@pytest.mark.parametrize("value", [None, [], {}, False, 42, "unknown"])
def test_corrupt_behavior_options_fall_back(options, value):
    assert options.get_override_duration({"override_duration": value}) == "endOfPhase"
    assert options.get_turn_on_mode({"turn_on_mode": value}) == "resume"


def test_valid_durations_and_turn_on_modes_survive(options):
    for value in ("endOfPhase", "FOUR", "MIDNIGHT"):
        assert options.get_override_duration({"override_duration": value}) == value
    for value in ("resume", "week1", "week2"):
        assert options.get_turn_on_mode({"turn_on_mode": value}) == value
