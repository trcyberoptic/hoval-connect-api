"""Runtime regressions for control ordering, validation, and program selection."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from enum import IntFlag, StrEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_program_polling import runtime as coordinator_runtime  # noqa: F401


class _Features(IntFlag):
    SET_SPEED = 1
    TURN_ON = 2
    TURN_OFF = 4
    TARGET_TEMPERATURE = 8
    OPERATION_MODE = 16


class _Mode(StrEnum):
    HEAT = "heat"
    OFF = "off"
    AUTO = "auto"
    DRY = "dry"


class _Action(StrEnum):
    HEATING = "heating"
    COOLING = "cooling"
    IDLE = "idle"
    OFF = "off"


class _CoordinatorEntity:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.hass = coordinator.hass

    @property
    def available(self):
        return True

    async def async_will_remove_from_hass(self):
        pass


@pytest.fixture
def entities(coordinator_runtime, monkeypatch):  # noqa: F811 - imported pytest fixture
    """Extend the isolated coordinator fixture with real entity source files."""
    runtime = coordinator_runtime
    modules = {}
    for name in (
        "homeassistant.components",
        "homeassistant.components.fan",
        "homeassistant.components.climate",
        "homeassistant.components.water_heater",
        "homeassistant.components.select",
        "homeassistant.const",
        "homeassistant.helpers.entity_platform",
    ):
        module = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        modules[name.rsplit(".", 1)[-1]] = module
    modules["fan"].FanEntity = type("FanEntity", (), {})
    modules["fan"].FanEntityFeature = _Features
    modules["climate"].ClimateEntity = type("ClimateEntity", (), {})
    modules["climate"].ClimateEntityFeature = _Features
    modules["climate"].HVACMode = _Mode
    modules["climate"].HVACAction = _Action
    modules["water_heater"].WaterHeaterEntity = type("WaterHeaterEntity", (), {})
    modules["water_heater"].WaterHeaterEntityFeature = _Features
    modules["water_heater"].STATE_HEAT_PUMP = "heat_pump"
    modules["water_heater"].STATE_HIGH_DEMAND = "high_demand"
    modules["water_heater"].STATE_OFF = "off"
    modules["select"].SelectEntity = type("SelectEntity", (), {})
    modules["const"].UnitOfTemperature = SimpleNamespace(CELSIUS="C")
    modules["entity_platform"].AddEntitiesCallback = object
    sys.modules["homeassistant.core"].callback = lambda fn: fn
    error = type("HomeAssistantError", (Exception,), {})
    sys.modules["homeassistant.exceptions"].HomeAssistantError = error
    sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = _CoordinatorEntity
    sys.modules["homeassistant.helpers.dispatcher"].async_dispatcher_connect = lambda *args: None

    package_name = runtime.coordinator.__package__
    package = sys.modules[package_name]
    package.HovalConnectConfigEntry = object
    package.circuit_device_info = lambda *args: {}
    directory = Path(runtime.coordinator.__file__).parent
    loaded = {}
    for basename in ("options", "fan", "climate", "water_heater", "select"):
        name = f"{package_name}.{basename}"
        spec = importlib.util.spec_from_file_location(name, directory / f"{basename}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[basename] = module
    return SimpleNamespace(runtime=runtime, error=error, **loaded)


class _ControlCoordinator:
    """Run lazy entity actions inside a lock without performing cloud I/O."""

    def __init__(self, runtime, circuit_type):
        self.hass = SimpleNamespace(async_create_task=asyncio.create_task)
        self.api = SimpleNamespace(
            set_temporary_change=AsyncMock(),
            set_circuit_mode=AsyncMock(),
            set_program=AsyncMock(),
            reset_circuit=AsyncMock(),
        )
        self.control_lock = asyncio.Lock()
        self.calls = []
        self.resume_program = "week2"
        self.circuit = runtime.coordinator.HovalCircuitData(
            circuit_type=circuit_type,
            path="1.2.3",
            name="Test circuit",
            operation_mode="heating",
            active_program="week2",
            target_value=40,
        )
        self.data = runtime.coordinator.HovalData(
            plants={
                "P1": runtime.coordinator.HovalPlantData(
                    plant_id="P1", name="Plant", circuits={"1.2.3": self.circuit}
                )
            }
        )

    async def async_control_and_refresh(self, action, *, plant_id, circuit_path, mode_override):
        assert callable(action)
        assert (plant_id, circuit_path) == ("P1", "1.2.3")
        self.calls.append(mode_override)
        async with self.control_lock:
            await action()

    def get_mode_override(self, plant_id, circuit_path):
        assert (plant_id, circuit_path) == ("P1", "1.2.3")
        return None

    def resolve_resume_program(self, plant_id, circuit_path):
        assert self.control_lock.locked(), "Resume must be resolved inside the control action"
        assert (plant_id, circuit_path) == ("P1", "1.2.3")
        return self.resume_program


def _make_entity(entities, platform):
    classes = {
        "fan": (entities.fan.HovalFan, "HV"),
        "climate": (entities.climate.HovalClimate, "HK"),
        "water_heater": (entities.water_heater.HovalWaterHeater, "WW"),
        "select": (entities.select.HovalProgramSelect, "HK"),
    }
    cls, circuit_type = classes[platform]
    coordinator = _ControlCoordinator(entities.runtime, circuit_type)
    args = [coordinator]
    if platform != "select":
        args.append(SimpleNamespace(options={}))
    entity = cls(*args, "P1", "1.2.3", coordinator.circuit)
    entity.async_write_ha_state = MagicMock()
    return entity, coordinator


class _DebounceGates:
    def __init__(self):
        self.gates = []

    async def sleep(self, delay):
        gate = asyncio.Event()
        self.gates.append(gate)
        await gate.wait()


def _controlled_debounce(entities, monkeypatch):
    gates = _DebounceGates()
    monkeypatch.setattr(
        entities.fan,
        "asyncio",
        SimpleNamespace(sleep=gates.sleep, current_task=asyncio.current_task),
    )
    return gates


@pytest.mark.parametrize("command", ["async_turn_off", "async_turn_on"])
def test_direct_fan_command_cancels_queued_slider_write(entities, monkeypatch, command):
    _controlled_debounce(entities, monkeypatch)

    async def scenario():
        fan, coordinator = _make_entity(entities, "fan")
        await fan.async_set_percentage(55)
        pending = fan._debounce_task
        await asyncio.sleep(0)
        await getattr(fan, command)()
        await asyncio.gather(pending, return_exceptions=True)
        assert pending.cancelled()
        coordinator.api.set_temporary_change.assert_not_awaited()
        assert len(coordinator.calls) == 1
        assert fan._pending_percentage is None

    asyncio.run(scenario())


def test_fan_off_waits_for_started_write_without_cancelling_it(entities, monkeypatch):
    gates = _controlled_debounce(entities, monkeypatch)

    async def scenario():
        fan, coordinator = _make_entity(entities, "fan")
        started, release = asyncio.Event(), asyncio.Event()
        order = []

        async def write(*args, **kwargs):
            order.append("speed started")
            started.set()
            await release.wait()
            order.append("speed finished")

        async def off(*args, **kwargs):
            order.append("off")

        coordinator.api.set_temporary_change.side_effect = write
        coordinator.api.set_circuit_mode.side_effect = off
        await fan.async_set_percentage(55)
        original = fan._debounce_task
        await asyncio.sleep(0)
        gates.gates[0].set()
        await started.wait()
        assert fan.percentage == 55

        off_task = asyncio.create_task(fan.async_turn_off())
        await asyncio.sleep(0)
        assert not original.cancelled()
        assert not original.done()
        assert fan.percentage == 0
        release.set()
        await asyncio.gather(original, off_task)
        assert order == ["speed started", "speed finished", "off"]
        assert fan._pending_percentage is None

    asyncio.run(scenario())


def test_older_fan_write_keeps_newer_pending_value(entities, monkeypatch):
    gates = _controlled_debounce(entities, monkeypatch)

    async def scenario():
        fan, coordinator = _make_entity(entities, "fan")
        started, release = asyncio.Event(), asyncio.Event()

        async def write(*args, value, **kwargs):
            if value == 30:
                started.set()
                await release.wait()

        coordinator.api.set_temporary_change.side_effect = write
        await fan.async_set_percentage(30)
        older = fan._debounce_task
        await asyncio.sleep(0)
        gates.gates[0].set()
        await started.wait()
        await fan.async_set_percentage(60)
        newer = fan._debounce_task
        await asyncio.sleep(0)
        release.set()
        await older
        assert fan.percentage == 60
        gates.gates[1].set()
        await newer
        assert [
            call.kwargs["value"] for call in coordinator.api.set_temporary_change.await_args_list
        ] == [30, 60]
        assert fan._pending_percentage is None

    asyncio.run(scenario())


def test_fan_repeated_value_keeps_new_request_while_old_refresh_finishes(entities, monkeypatch):
    """30 -> 60 -> 30 must distinguish the two different 30 percent requests."""
    gates = _controlled_debounce(entities, monkeypatch)

    async def scenario():
        fan, coordinator = _make_entity(entities, "fan")
        old_refresh_started, release_old_refresh = asyncio.Event(), asyncio.Event()
        original_control = coordinator.async_control_and_refresh

        async def write(*args, value, **kwargs):
            coordinator.circuit.target_value = value

        async def control(action, **kwargs):
            first = not coordinator.calls
            await original_control(action, **kwargs)
            if first:
                old_refresh_started.set()
                await release_old_refresh.wait()

        coordinator.api.set_temporary_change.side_effect = write
        coordinator.async_control_and_refresh = control
        await fan.async_set_percentage(30)
        first = fan._debounce_task
        await asyncio.sleep(0)
        gates.gates[0].set()
        await old_refresh_started.wait()

        await fan.async_set_percentage(60)
        second = fan._debounce_task
        await asyncio.sleep(0)
        gates.gates[1].set()
        await second
        assert coordinator.circuit.target_value == 60

        await fan.async_set_percentage(30)
        third = fan._debounce_task
        await asyncio.sleep(0)
        try:
            release_old_refresh.set()
            await first
            assert fan._pending_percentage == 30
            assert fan.percentage == 30
        finally:
            gates.gates[2].set()
            await third
        assert fan._pending_percentage is None

    asyncio.run(scenario())


def test_climate_repeated_temperature_keeps_new_request_after_old_refresh(entities):
    """The first 21.5 degree request cannot clear a later 21.5 degree request."""

    async def scenario():
        entity, coordinator = _make_entity(entities, "climate")
        old_refresh_started, release_old_refresh = asyncio.Event(), asyncio.Event()
        third_write_started, release_third_write = asyncio.Event(), asyncio.Event()
        original_control = coordinator.async_control_and_refresh
        writes = 0

        async def write(*args, value, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 3:
                third_write_started.set()
                await release_third_write.wait()
            coordinator.circuit.live_values["roomTempTarget"] = str(value)

        async def control(action, **kwargs):
            first = not coordinator.calls
            await original_control(action, **kwargs)
            if first:
                old_refresh_started.set()
                await release_old_refresh.wait()

        coordinator.api.set_temporary_change.side_effect = write
        coordinator.async_control_and_refresh = control
        first = asyncio.create_task(entity.async_set_temperature(temperature=21.5))
        await old_refresh_started.wait()
        await entity.async_set_temperature(temperature=23)
        third = asyncio.create_task(entity.async_set_temperature(temperature=21.5))
        await third_write_started.wait()
        try:
            release_old_refresh.set()
            await first
            assert entity._pending_temperature == 21.5
            assert entity.target_temperature == 21.5
        finally:
            release_third_write.set()
            await third
        assert entity._pending_temperature is None

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [True, False, -1, 101, 50.5, "50", float("nan"), float("inf")])
def test_invalid_fan_values_never_change_pending_state_or_send(entities, value):
    async def scenario():
        fan, coordinator = _make_entity(entities, "fan")
        with pytest.raises(entities.error, match="Invalid fan percentage"):
            await fan.async_set_percentage(value)
        assert fan._pending_percentage is None
        assert fan._debounce_task is None
        fan.async_write_ha_state.assert_not_called()
        assert coordinator.calls == []

    asyncio.run(scenario())


def test_fan_minimum_is_applied_before_pending_display(entities, monkeypatch):
    gates = _controlled_debounce(entities, monkeypatch)

    async def scenario():
        fan, coordinator = _make_entity(entities, "fan")
        await fan.async_set_percentage(1)
        assert fan.percentage == entities.runtime.const.HV_AIR_VOLUME_MIN
        task = fan._debounce_task
        await asyncio.sleep(0)
        gates.gates[0].set()
        await task
        assert coordinator.api.set_temporary_change.await_args.kwargs == {
            "value": entities.runtime.const.HV_AIR_VOLUME_MIN,
            "duration": entities.runtime.const.DURATION_END_OF_PHASE,
        }

    asyncio.run(scenario())


def test_fan_display_keeps_setpoint_preference(entities):
    fan, coordinator = _make_entity(entities, "fan")
    coordinator.circuit.target_value = 60
    coordinator.circuit.live_values["airVolume"] = "20"
    assert fan.percentage == 60


@pytest.mark.parametrize("platform", ["climate", "water_heater"])
@pytest.mark.parametrize("value", [True, "invalid", [], float("nan"), float("inf"), -float("inf")])
def test_invalid_temperature_never_reaches_control(entities, platform, value):
    entity, coordinator = _make_entity(entities, platform)
    with pytest.raises(entities.error, match="Invalid target temperature"):
        asyncio.run(entity.async_set_temperature(temperature=value))
    assert coordinator.calls == []
    entity.async_write_ha_state.assert_not_called()
    assert getattr(entity, "_pending_temperature", None) is None


@pytest.mark.parametrize(
    ("platform", "value", "expected"),
    [
        ("climate", -20, 5),
        ("climate", 60, 30),
        ("climate", 21.5, 21.5),
        ("water_heater", -20, 10),
        ("water_heater", 90, 65),
        ("water_heater", 55.5, 55.5),
    ],
)
def test_temperature_bounds_and_v4_duration_are_preserved(entities, platform, value, expected):
    entity, coordinator = _make_entity(entities, platform)
    asyncio.run(entity.async_set_temperature(temperature=value))
    args = coordinator.api.set_temporary_change.await_args
    assert args.args == ("P1", "1.2.3")
    assert args.kwargs["value"] == expected
    assert args.kwargs["duration"] == (
        entities.runtime.const.DURATION_END_OF_PHASE
        if platform == "climate"
        else entities.runtime.const.DURATION_MIDNIGHT
    )


@pytest.mark.parametrize("failure", [None, "auth", "api"])
def test_climate_pending_value_survives_request_then_clears(entities, failure):
    async def scenario():
        entity, coordinator = _make_entity(entities, "climate")
        started, release = asyncio.Event(), asyncio.Event()

        async def write(*args, **kwargs):
            started.set()
            await release.wait()
            if failure == "auth":
                raise entities.runtime.api.HovalAuthError("Expired")
            if failure == "api":
                raise entities.runtime.api.HovalApiError("Rejected")

        coordinator.api.set_temporary_change.side_effect = write
        task = asyncio.create_task(entity.async_set_temperature(temperature=21.5))
        await started.wait()
        assert entity.target_temperature == 21.5
        release.set()
        if failure is None:
            await task
        else:
            with pytest.raises(entities.error, match="Failed to set temperature"):
                await task
        assert entity._pending_temperature is None

    asyncio.run(scenario())


def test_heat_is_constant_and_auto_resumes_week_program(entities):
    entity, coordinator = _make_entity(entities, "climate")
    asyncio.run(entity.async_set_hvac_mode(_Mode.HEAT))
    coordinator.api.set_program.assert_awaited_once_with("P1", "1.2.3", "constant")
    coordinator.api.reset_circuit.assert_not_awaited()
    asyncio.run(entity.async_set_hvac_mode(_Mode.AUTO))
    coordinator.api.reset_circuit.assert_awaited_once_with("P1", "1.2.3", program="week2")


def test_invalid_hvac_mode_is_rejected_without_control(entities):
    entity, coordinator = _make_entity(entities, "climate")
    with pytest.raises(entities.error, match="Unsupported HVAC mode"):
        asyncio.run(entity.async_set_hvac_mode(_Mode.DRY))
    assert coordinator.calls == []


@pytest.mark.parametrize("platform", ["fan", "climate", "water_heater"])
def test_resume_is_resolved_after_waiting_for_control_lock(entities, platform):
    async def scenario():
        entity, coordinator = _make_entity(entities, platform)
        coordinator.resume_program = "week1"
        await coordinator.control_lock.acquire()
        if platform == "fan":
            action = entity.async_turn_on()
        elif platform == "climate":
            action = entity.async_set_hvac_mode(_Mode.AUTO)
        else:
            action = entity.async_set_operation_mode("heat_pump")
        task = asyncio.create_task(action)
        await asyncio.sleep(0)
        coordinator.resume_program = "week2"
        coordinator.control_lock.release()
        await task
        coordinator.api.reset_circuit.assert_awaited_once_with("P1", "1.2.3", program="week2")

    asyncio.run(scenario())


@pytest.mark.parametrize("platform", ["fan", "climate", "water_heater"])
def test_missing_operation_mode_is_unknown(entities, platform):
    entity, coordinator = _make_entity(entities, platform)
    coordinator.circuit.operation_mode = None
    attribute = {"fan": "is_on", "climate": "hvac_mode", "water_heater": "current_operation"}[
        platform
    ]
    assert getattr(entity, attribute) is None


@pytest.mark.parametrize(
    "names",
    [
        {"week1": "Summer", "week2": "Summer"},
        {"week1": "Standby", "week2": "Standby (standby)"},
        {"week1": "Eco mode", "week2": "Eco mode (ecoMode)"},
        {"week1": "week2", "week2": "Another schedule"},
        {"week1": "Same", "week2": "Same", "standby": "Same (week1)", "constant": "Same (week1 2)"},
    ],
)
def test_all_select_labels_are_unique_and_roundtrip_to_correct_program(entities, names):
    async def scenario():
        entity, coordinator = _make_entity(entities, "select")
        coordinator.circuit.program_names = names
        labels = entity.options
        assert len(set(labels)) == len(labels) == len(entities.select.API_PROGRAMS)
        for key, label in zip(entities.select.API_PROGRAMS, labels, strict=True):
            coordinator.circuit.active_program = key
            assert entity.current_option == label
            await entity.async_select_option(label)
            assert coordinator.api.set_program.await_args.args == ("P1", "1.2.3", key)

    asyncio.run(scenario())


@pytest.mark.parametrize("program", ["manual", "externalConstant", "unknown", None])
def test_select_only_reports_current_option_from_offered_list(entities, program):
    entity, coordinator = _make_entity(entities, "select")
    coordinator.circuit.active_program = program
    assert entity.current_option is None


def test_select_invalid_option_never_reaches_control(entities):
    entity, coordinator = _make_entity(entities, "select")
    with pytest.raises(entities.error, match="Unknown program"):
        asyncio.run(entity.async_select_option("not a program"))
    assert coordinator.calls == []


@pytest.mark.parametrize("operation", ["high_demand", "unknown"])
def test_water_heater_rejects_unimplemented_modes(entities, operation):
    entity, coordinator = _make_entity(entities, "water_heater")
    with pytest.raises(entities.error, match="Unsupported operation mode"):
        asyncio.run(entity.async_set_operation_mode(operation))
    assert coordinator.calls == []


def test_water_heater_boost_keeps_valid_operation_and_boost_metadata(entities):
    entity, coordinator = _make_entity(entities, "water_heater")
    coordinator.circuit.temporary_change_end = "2026-09-21T00:00:00+02:00"
    coordinator.circuit.temporary_change_value = 55.5
    coordinator.circuit.temporary_change_type = "away"
    assert entity.current_operation == "heat_pump"
    assert entity.current_operation in entity._attr_operation_list
    assert entity._attr_operation_list == ["heat_pump", "off"]
    # Dedicated temporary-change entities can continue reading this same DTO.
    assert coordinator.circuit.temporary_change_end == "2026-09-21T00:00:00+02:00"
    assert coordinator.circuit.temporary_change_value == 55.5
