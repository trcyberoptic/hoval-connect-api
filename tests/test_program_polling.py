"""Exercise program polling with the real coordinator and isolated HA stubs.

No Home Assistant installation or HTTP requests are needed. Module aliases keep
these runtime tests independent of the process-wide mocks in older test files.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "hoval_connect"
_PACKAGE = "_program_polling_component"
_PATH = "1.2.3"


class _CoordinatorBase:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, *args, **kwargs):
        self.hass = hass


class _Clock:
    def __init__(self):
        self.elapsed = 1000.0
        self.wall = 1_000_000.0

    def advance(self, seconds):
        self.elapsed += seconds
        self.wall += seconds

    def monotonic(self):
        return self.elapsed

    def time(self):
        return self.wall


def _programs(name):
    return {
        "week1": {"name": name, "dayProgramIds": [1] * 7},
        "dayPrograms": {
            "dayConfigurations": [
                {
                    "id": 1,
                    "name": "All day",
                    "phases": [
                        {
                            "start": {"hours": 0, "minutes": 0},
                            "end": {"hours": 24, "minutes": 0},
                            "value": 21.5,
                        }
                    ],
                }
            ]
        },
    }


class _Api:
    """Return configured outcomes and count actual coordinator API calls."""

    def __init__(self, outcomes, circuit_types=None):
        self.outcomes = outcomes
        self.circuit_types = circuit_types or {}
        self.program_calls = Counter()
        self.live_calls = Counter()

    async def get_plants(self):
        return [
            {"plantExternalId": plant, "isOnline": True}
            for plant in dict.fromkeys(plant for plant, _ in self.outcomes)
        ]

    async def get_circuits(self, plant):
        return [
            {
                "type": self.circuit_types.get((pid, path), "BL"),
                "path": path,
                "selectable": self.circuit_types.get((pid, path), "BL") in {"HK", "HV"},
                "activeProgram": "week1",
            }
            for pid, path in self.outcomes
            if pid == plant
        ]

    async def get_live_values(self, plant, path, circuit_type):
        key = (plant, path)
        self.live_calls[key] += 1
        return [
            {"key": "totalEnergy", "value": str(100 + self.live_calls[key])},
            {"key": "energyElHeater", "value": str(10 + self.live_calls[key])},
        ]

    async def get_programs(self, plant, path):
        key = (plant, path)
        self.program_calls[key] += 1
        outcomes = self.outcomes[key]
        outcome = outcomes[min(self.program_calls[key] - 1, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return deepcopy(outcome)

    async def get_datapoints(self, plant, addresses):
        return {}

    async def get_latest_event(self, plant):
        return None

    async def get_events(self, plant):
        return []

    async def get_weather(self, plant):
        return []


@pytest.fixture
def runtime(monkeypatch):
    """Import original source without integration setup or leaking HA stubs."""
    for name in (
        "homeassistant",
        "homeassistant.config_entries",
        "homeassistant.core",
        "homeassistant.exceptions",
        "homeassistant.helpers",
        "homeassistant.helpers.dispatcher",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.util",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["homeassistant.config_entries"].ConfigEntry = object
    sys.modules["homeassistant.core"].HomeAssistant = object
    sys.modules["homeassistant.exceptions"].ConfigEntryAuthFailed = type(
        "ConfigEntryAuthFailed", (Exception,), {}
    )
    sys.modules["homeassistant.helpers.dispatcher"].async_dispatcher_send = lambda *args: None
    update_module = sys.modules["homeassistant.helpers.update_coordinator"]
    update_module.DataUpdateCoordinator = _CoordinatorBase
    update_module.UpdateFailed = type("UpdateFailed", (Exception,), {})
    sys.modules["homeassistant.util"].dt = SimpleNamespace(now=lambda: datetime(2026, 9, 19, 12, 0))

    package = ModuleType(_PACKAGE)
    package.__path__ = [str(_COMPONENT)]
    monkeypatch.setitem(sys.modules, _PACKAGE, package)
    loaded = {}
    for module_name in ("const", "api", "coordinator"):
        name = f"{_PACKAGE}.{module_name}"
        spec = importlib.util.spec_from_file_location(name, _COMPONENT / f"{module_name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[module_name] = module

    clock = _Clock()
    monkeypatch.setattr(loaded["coordinator"], "time", clock)
    return SimpleNamespace(clock=clock, **loaded)


def _coordinator(runtime, outcomes, circuit_types=None):
    api = _Api(outcomes, circuit_types)
    return runtime.coordinator.HovalDataCoordinator(object(), api), api


def _unsupported(runtime, key):
    plant, path = key
    return runtime.api.HovalApiError(
        "Unsupported programs",
        status=417,
        request_path=f"/v3/plants/{plant}/circuits/{path}/programs",
    )


def _refresh(coordinator, api):
    data = asyncio.run(coordinator._async_update_data())
    # Optional program data must never discard a circuit or its fresh counters.
    for (plant, path), count in api.live_calls.items():
        circuit = data.plants[plant].circuits[path]
        assert circuit.live_values["totalEnergy"] == str(100 + count)
        assert circuit.live_values["energyElHeater"] == str(10 + count)
    return data


def test_successful_programs_refresh_at_cache_deadline(runtime):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [_programs("Winter")]})
    ttl = runtime.const.PROGRAM_CACHE_TTL.total_seconds()
    for advance in (0, 60, ttl - 61):
        runtime.clock.advance(advance)
        data = _refresh(coordinator, api)
        assert data.plants["P1"].circuits[_PATH].active_week_name == "Winter"
        assert api.program_calls[key] == 1
    assert api.live_calls[key] == 3

    runtime.clock.advance(1)
    _refresh(coordinator, api)
    assert api.program_calls[key] == 2


@pytest.mark.parametrize("empty_programs", [None, []], ids=["no-content", "empty-list"])
def test_empty_program_response_uses_normal_cache(runtime, empty_programs):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [empty_programs]})
    ttl = runtime.const.PROGRAM_CACHE_TTL.total_seconds()
    for advance in (0, 60, ttl - 61):
        runtime.clock.advance(advance)
        data = _refresh(coordinator, api)
        assert data.plants["P1"].circuits[_PATH].active_week_name is None
        assert api.program_calls[key] == 1
    assert api.live_calls[key] == 3

    runtime.clock.advance(1)
    _refresh(coordinator, api)
    assert api.program_calls[key] == 2


def test_417_reprobes_after_one_hour_and_recovers(runtime):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(
        runtime,
        {key: [_unsupported(runtime, key), _programs("Back")]},
    )
    retry = runtime.const.PROGRAM_UNAVAILABLE_RETRY_INTERVAL.total_seconds()
    assert retry == 3600
    ttl = runtime.const.PROGRAM_CACHE_TTL.total_seconds()
    for advance in (0, 60, ttl + 1 - 60, retry - ttl - 2):
        runtime.clock.advance(advance)
        data = _refresh(coordinator, api)
        assert data.plants["P1"].circuits[_PATH].active_week_name is None
        assert api.program_calls[key] == 1
    assert api.live_calls[key] == 4

    runtime.clock.advance(1)
    data = _refresh(coordinator, api)
    assert api.program_calls[key] == 2
    assert data.plants["P1"].circuits[_PATH].active_week_name == "Back"

    runtime.clock.advance(ttl - 1)
    _refresh(coordinator, api)
    assert api.program_calls[key] == 2
    runtime.clock.advance(1)
    _refresh(coordinator, api)
    assert api.program_calls[key] == 3


def test_persistent_417_warns_once_per_hourly_probe(runtime, caplog):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [_unsupported(runtime, key)]})
    logger = runtime.coordinator.__name__
    caplog.set_level(logging.WARNING, logger=logger)
    retry = runtime.const.PROGRAM_UNAVAILABLE_RETRY_INTERVAL.total_seconds()
    _refresh(coordinator, api)
    runtime.clock.advance(60)
    _refresh(coordinator, api)
    warnings = [r for r in caplog.records if r.name == logger and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "P1" in warnings[0].getMessage()
    assert _PATH in warnings[0].getMessage()

    runtime.clock.advance(retry - 60)
    _refresh(coordinator, api)
    warnings = [r for r in caplog.records if r.name == logger and r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert api.program_calls[key] == 2


@pytest.mark.parametrize(
    "failure", ["500", "599", "auth", "transport", "gateway-417", "token-417", "unscoped-417"]
)
def test_other_errors_never_mark_programs_unsupported(runtime, failure):
    key = ("P1", _PATH)
    if failure == "auth":
        error = runtime.api.HovalAuthError("Expired token")
    elif failure == "gateway-417":
        error = runtime.api.HovalApiError(
            "Gateway blocked",
            status=417,
            gateway_blocked=True,
            request_path=f"/v3/plants/P1/circuits/{_PATH}/programs",
        )
    elif failure == "token-417":
        error = runtime.api.HovalApiError(
            "Token request failed", status=417, request_path="/v1/plants/P1/settings"
        )
    elif failure == "unscoped-417":
        error = runtime.api.HovalApiError("Unattributed failure", status=417)
    elif failure == "transport":
        error = runtime.api.HovalApiError("Connection lost")
    else:
        error = runtime.api.HovalApiError("Transient server error", status=int(failure))
    coordinator, api = _coordinator(runtime, {key: [error]})
    for count in range(1, 4):
        _refresh(coordinator, api)
        assert api.program_calls[key] == count
        assert api.live_calls[key] == count
        runtime.clock.advance(60)


def test_program_cache_is_scoped_to_plant_and_circuit(runtime):
    first = ("P1", _PATH)
    second = ("P2", _PATH)
    coordinator, api = _coordinator(
        runtime, {first: [_programs("First plant")], second: [_programs("Second plant")]}
    )
    for _ in range(3):
        data = _refresh(coordinator, api)
        assert data.plants["P1"].circuits[_PATH].active_week_name == "First plant"
        assert data.plants["P2"].circuits[_PATH].active_week_name == "Second plant"
        assert api.program_calls == {first: 1, second: 1}
        runtime.clock.advance(60)


def test_unsupported_programs_do_not_suppress_other_plants(runtime):
    first = ("P1", _PATH)
    second = ("P2", _PATH)
    coordinator, api = _coordinator(
        runtime,
        {
            first: [_unsupported(runtime, first)],
            second: [_programs("Second plant")],
        },
    )
    _refresh(coordinator, api)
    runtime.clock.advance(runtime.const.PROGRAM_CACHE_TTL.total_seconds())
    data = _refresh(coordinator, api)
    assert data.plants["P1"].circuits[_PATH].active_week_name is None
    assert data.plants["P2"].circuits[_PATH].active_week_name == "Second plant"
    assert api.program_calls == {first: 1, second: 2}


def test_only_rejecting_circuit_is_paused(runtime):
    boiler = ("P1", _PATH)
    heating = ("P1", "1.3.3")
    ventilation = ("P1", "1.4.3")
    coordinator, api = _coordinator(
        runtime,
        {
            boiler: [_unsupported(runtime, boiler)],
            heating: [_programs("Heating")],
            ventilation: [_programs("Ventilation")],
        },
        {heating: "HK", ventilation: "HV"},
    )
    _refresh(coordinator, api)
    runtime.clock.advance(runtime.const.PROGRAM_CACHE_TTL.total_seconds())
    data = _refresh(coordinator, api)
    circuits = data.plants["P1"].circuits
    assert circuits[heating[1]].active_week_name == "Heating"
    assert circuits[ventilation[1]].active_week_name == "Ventilation"
    assert api.program_calls == {boiler: 1, heating: 2, ventilation: 2}


@pytest.mark.parametrize("wall_jump", [-86400, 86400])
def test_program_cache_deadline_uses_monotonic_time(runtime, wall_jump):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [_programs("Winter")]})
    _refresh(coordinator, api)
    runtime.clock.wall += wall_jump
    _refresh(coordinator, api)
    assert api.program_calls[key] == 1

    runtime.clock.advance(runtime.const.PROGRAM_CACHE_TTL.total_seconds())
    _refresh(coordinator, api)
    assert api.program_calls[key] == 2
