"""Runtime regressions for control ordering, validation, and program selection."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import dataclass
from enum import IntFlag, StrEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_modbus_coordinator import _HK, _HV, _hybrid, _tick
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


@dataclass(frozen=True, kw_only=True)
class _Description:
    key: str
    translation_key: str | None = None
    icon: str | None = None
    entity_category: object = None
    native_min_value: float | None = None
    native_max_value: float | None = None
    native_step: float | None = None
    native_unit_of_measurement: str | None = None


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
        "homeassistant.components.number",
        "homeassistant.components.button",
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
    modules["number"].NumberEntity = type("NumberEntity", (), {})
    modules["number"].NumberEntityDescription = _Description
    modules["button"].ButtonEntity = type("ButtonEntity", (), {})
    modules["button"].ButtonEntityDescription = _Description
    modules["const"].UnitOfTemperature = SimpleNamespace(CELSIUS="C")
    modules["const"].PERCENTAGE = "%"
    modules["const"].EntityCategory = SimpleNamespace(CONFIG="config")
    modules["entity_platform"].AddEntitiesCallback = object
    sys.modules["homeassistant.core"].callback = lambda fn: fn
    error = type("HomeAssistantError", (Exception,), {})
    sys.modules["homeassistant.exceptions"].HomeAssistantError = error
    validation_error = type("ServiceValidationError", (error,), {})
    sys.modules["homeassistant.exceptions"].ServiceValidationError = validation_error
    sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = _CoordinatorEntity
    sys.modules["homeassistant.helpers.dispatcher"].async_dispatcher_connect = lambda *args: None

    package_name = runtime.coordinator.__package__
    package = sys.modules[package_name]
    package.HovalConnectConfigEntry = object
    package.circuit_device_info = lambda *args: {}
    directory = Path(runtime.coordinator.__file__).parent
    loaded = {}
    for basename in ("options", "fan", "climate", "water_heater", "select", "number", "button"):
        name = f"{package_name}.{basename}"
        spec = importlib.util.spec_from_file_location(name, directory / f"{basename}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[basename] = module
    return SimpleNamespace(
        runtime=runtime, error=error, validation_error=validation_error, **loaded
    )


class _ControlCoordinator:
    """Run lazy entity actions inside a lock without performing cloud I/O."""

    def __init__(self, runtime, circuit_type, *, modbus=False, local=False, cloud_ok=True):
        self.hass = SimpleNamespace(async_create_task=asyncio.create_task)
        self.is_local = local
        self.hv_writes = []
        self.hv_error = None
        self.cloud_flags = []
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
        if modbus:
            self.circuit.modbus = SimpleNamespace(
                constant_modulation=60, eco_modulation=30, humidity_target=50
            )
        self.data = runtime.coordinator.HovalData(
            plants={
                "P1": runtime.coordinator.HovalPlantData(
                    plant_id="P1", name="Plant", circuits={"1.2.3": self.circuit}
                )
            },
            cloud_ok=cloud_ok,
        )

    async def async_hv_write(self, plant_id, circuit_path, name, value):
        assert (plant_id, circuit_path) == ("P1", "1.2.3")
        if self.hv_error is not None:
            raise self.hv_error
        self.hv_writes.append((name, value))

    async def async_control_and_refresh(
        self, action, *, plant_id, circuit_path, mode_override, cloud_action=True
    ):
        assert callable(action)
        assert (plant_id, circuit_path) == ("P1", "1.2.3")
        self.calls.append(mode_override)
        self.cloud_flags.append(cloud_action)
        async with self.control_lock:
            await action()

    def get_mode_override(self, plant_id, circuit_path):
        assert (plant_id, circuit_path) == ("P1", "1.2.3")
        return None

    def resolve_resume_program(self, plant_id, circuit_path):
        assert self.control_lock.locked(), "Resume must be resolved inside the control action"
        assert (plant_id, circuit_path) == ("P1", "1.2.3")
        return self.resume_program


def _make_entity(entities, platform, **kwargs):
    classes = {
        "fan": (entities.fan.HovalFan, "HV"),
        "climate": (entities.climate.HovalClimate, "HK"),
        "water_heater": (entities.water_heater.HovalWaterHeater, "WW"),
        "select": (entities.select.HovalProgramSelect, "HK"),
    }
    cls, circuit_type = classes[platform]
    options = kwargs.pop("options", {})
    if kwargs.get("modbus"):
        circuit_type = "HV"
    coordinator = _ControlCoordinator(entities.runtime, circuit_type, **kwargs)
    args = [coordinator]
    if platform != "select":
        args.append(SimpleNamespace(options=options))
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
        ("water_heater", 10, 10),
        ("water_heater", 65, 65),
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


@pytest.mark.parametrize("value", [9.5, 65.5, -20, 90])
def test_water_heater_rejects_values_outside_fallback_range(entities, value):
    entity, coordinator = _make_entity(entities, "water_heater")
    with pytest.raises(entities.validation_error, match="10–65 °C"):
        asyncio.run(entity.async_set_temperature(temperature=value))
    assert coordinator.calls == []


def test_water_heater_follows_controller_limits(entities):
    # The range comes from the cloud's temporaryChangeLimits (see coordinator).
    entity, coordinator = _make_entity(entities, "water_heater")
    coordinator.circuit.temporary_change_min = 10.0
    coordinator.circuit.temporary_change_max = 49.0
    assert (entity.min_temp, entity.max_temp) == (10.0, 49.0)
    with pytest.raises(entities.validation_error, match="55 °C is outside .*10–49 °C"):
        asyncio.run(entity.async_set_temperature(temperature=55))
    assert coordinator.calls == []
    asyncio.run(entity.async_set_temperature(temperature=49))
    assert coordinator.api.set_temporary_change.await_args.kwargs["value"] == 49


def test_climate_clamps_to_controller_limits(entities):
    entity, coordinator = _make_entity(entities, "climate")
    coordinator.circuit.temporary_change_min = 16.0
    coordinator.circuit.temporary_change_max = 24.0
    assert (entity.min_temp, entity.max_temp) == (16.0, 24.0)
    asyncio.run(entity.async_set_temperature(temperature=28))
    assert coordinator.api.set_temporary_change.await_args.kwargs["value"] == 24.0


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


# --- Modbus routing, setpoint numbers and acknowledge buttons ------------------


def _hv(entities, kind, **kwargs):
    """Build a number or button entity on a Modbus-served HV circuit."""
    coordinator = _ControlCoordinator(entities.runtime, "HV", modbus=True, **kwargs)
    if kind in ("constant", "eco", "humidity"):
        register = {
            "constant": "constant_modulation",
            "eco": "eco_modulation",
            "humidity": "humidity_target",
        }[kind]
        description = next(d for d in entities.number.NUMBER_DESCRIPTIONS if d.register == register)
        entity = entities.number.HovalSetpointNumber(
            coordinator, "P1", "1.2.3", coordinator.circuit, description
        )
    else:
        description = next(d for d in entities.button.BUTTON_DESCRIPTIONS if d.key == kind)
        entity = entities.button.HovalAcknowledgeButton(
            coordinator, "P1", "1.2.3", coordinator.circuit, description
        )
    entity.async_write_ha_state = MagicMock()
    return entity, coordinator


def _no_debounce(entities, monkeypatch):
    async def instant(delay):
        return None

    monkeypatch.setattr(
        entities.fan,
        "asyncio",
        SimpleNamespace(sleep=instant, current_task=asyncio.current_task),
    )


def test_select_writes_program_over_modbus(entities):
    async def scenario():
        select, coordinator = _make_entity(entities, "select", modbus=True)
        await select.async_select_option("Standby")
        assert coordinator.hv_writes == [("program", 0)]
        assert coordinator.cloud_flags == [False]
        coordinator.api.set_program.assert_not_called()

    asyncio.run(scenario())


def test_select_without_modbus_uses_cloud(entities):
    async def scenario():
        select, coordinator = _make_entity(entities, "select")
        await select.async_select_option("Standby")
        coordinator.api.set_program.assert_awaited_once_with("P1", "1.2.3", "standby")
        assert coordinator.hv_writes == []
        assert coordinator.cloud_flags == [True]

    asyncio.run(scenario())


def test_select_modbus_failure_becomes_home_assistant_error(entities):
    async def scenario():
        select, coordinator = _make_entity(entities, "select", modbus=True)
        coordinator.hv_error = entities.runtime.modbus.HovalModbusError("read-back mismatch")
        with pytest.raises(entities.error, match="program=1.*read-back mismatch"):
            await select.async_select_option("Week 1")

    asyncio.run(scenario())


def test_fan_off_and_on_write_program(entities):
    async def scenario():
        fan, coordinator = _make_entity(entities, "fan", modbus=True)
        await fan.async_turn_off()
        assert coordinator.hv_writes == [("program", 0)]
        coordinator.hv_writes.clear()
        coordinator.resume_program = "week2"
        await fan.async_turn_on()
        assert coordinator.hv_writes == [("program", 2)]
        assert coordinator.cloud_flags == [False, False]
        coordinator.api.set_circuit_mode.assert_not_called()
        coordinator.api.reset_circuit.assert_not_called()

    asyncio.run(scenario())


def test_fan_on_with_fixed_week_option_writes_that_week(entities):
    async def scenario():
        fan, coordinator = _make_entity(
            entities, "fan", modbus=True, options={"turn_on_mode": "week1"}
        )
        await fan.async_turn_on()
        assert coordinator.hv_writes == [("program", 1)]

    asyncio.run(scenario())


def test_fan_is_on_from_modbus_program(entities):
    fan, coordinator = _make_entity(entities, "fan", modbus=True)
    coordinator.circuit.operation_mode = "standby"  # cloud field must not matter
    coordinator.circuit.active_program = "week1"
    assert fan.is_on is True
    coordinator.circuit.active_program = "standby"
    assert fan.is_on is False
    coordinator.circuit.active_program = None
    assert fan.is_on is None


def test_modbus_controls_available_without_cloud_but_local_needs_a_reading(entities):
    fan, coordinator = _make_entity(entities, "fan", modbus=True, cloud_ok=False)
    select, _ = _make_entity(entities, "select", modbus=True, cloud_ok=False)
    assert fan.available is True
    assert select.available is True
    local_fan, local = _make_entity(entities, "fan", local=True)
    assert local.circuit.modbus is None
    assert local_fan.available is False


def test_hybrid_fan_percentage_uses_cloud_temporary_change(entities, monkeypatch):
    _no_debounce(entities, monkeypatch)

    async def scenario():
        fan, coordinator = _make_entity(entities, "fan", modbus=True)
        await fan.async_set_percentage(60)
        await fan._debounce_task
        coordinator.api.set_temporary_change.assert_awaited_once()
        assert coordinator.hv_writes == []
        assert coordinator.cloud_flags == [True]

    asyncio.run(scenario())


def test_hybrid_fan_percentage_cloud_failure_raises_without_fallback(entities, monkeypatch):
    _no_debounce(entities, monkeypatch)

    async def scenario():
        # Cloud already known to be down: raises at once, nothing is queued.
        fan, coordinator = _make_entity(entities, "fan", modbus=True, cloud_ok=False)
        with pytest.raises(entities.error, match="cloud"):
            await fan.async_set_percentage(60)
        assert fan._debounce_task is None
        assert coordinator.hv_writes == []
        coordinator.api.set_temporary_change.assert_not_called()
        # Cloud call fails after being sent: the error surfaces, still no Modbus write.
        fan, coordinator = _make_entity(entities, "fan", modbus=True)
        coordinator.api.set_temporary_change.side_effect = entities.runtime.api.HovalApiError("x")
        with pytest.raises(entities.error, match="Failed to set fan speed"):
            await fan._send_percentage(60, object())
        assert coordinator.hv_writes == []

    asyncio.run(scenario())


def test_hybrid_fan_off_still_works_with_cloud_down(entities):
    async def scenario():
        fan, coordinator = _make_entity(entities, "fan", modbus=True, cloud_ok=False)
        await fan.async_turn_off()
        assert coordinator.hv_writes == [("program", 0)]

    asyncio.run(scenario())


def test_local_fan_percentage_sets_constant(entities, monkeypatch):
    _no_debounce(entities, monkeypatch)

    async def scenario():
        fan, coordinator = _make_entity(entities, "fan", modbus=True, local=True)
        coordinator.api = None
        await fan.async_set_percentage(60)
        await fan._debounce_task
        assert coordinator.hv_writes == [("constant_modulation", 60), ("program", 4)]
        assert coordinator.cloud_flags == [False]

    asyncio.run(scenario())


def test_local_fan_percentage_is_clamped(entities, monkeypatch):
    _no_debounce(entities, monkeypatch)

    async def scenario():
        fan, coordinator = _make_entity(entities, "fan", modbus=True, local=True)
        await fan.async_set_percentage(5)
        await fan._debounce_task
        assert coordinator.hv_writes[0] == ("constant_modulation", 15)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kind", "value"),
    [("constant", 10), ("constant", 101), ("eco", 14), ("humidity", 29), ("humidity", 66)],
)
def test_number_rejects_out_of_range_before_write(entities, kind, value):
    async def scenario():
        number, coordinator = _hv(entities, kind)
        with pytest.raises(entities.error, match="outside"):
            await number.async_set_native_value(value)
        assert coordinator.hv_writes == []
        assert coordinator.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "50"])
def test_number_rejects_non_numbers(entities, value):
    async def scenario():
        number, coordinator = _hv(entities, "humidity")
        with pytest.raises(entities.error):
            await number.async_set_native_value(value)
        assert coordinator.hv_writes == []

    asyncio.run(scenario())


def test_number_writes_and_refreshes(entities):
    async def scenario():
        number, coordinator = _hv(entities, "humidity")
        await number.async_set_native_value(55.0)
        assert coordinator.hv_writes == [("humidity_target", 55)]
        assert isinstance(coordinator.hv_writes[0][1], int)
        assert coordinator.cloud_flags == [False]
        number, coordinator = _hv(entities, "constant")
        await number.async_set_native_value(44.6)
        assert coordinator.hv_writes == [("constant_modulation", 45)]
        number, coordinator = _hv(entities, "eco")
        await number.async_set_native_value(20)
        assert coordinator.hv_writes == [("eco_modulation", 20)]

    asyncio.run(scenario())


def test_number_metadata_and_value(entities):
    number, coordinator = _hv(entities, "humidity")
    assert number.native_value == 50
    assert number._attr_unique_id == "P1_1.2.3_humidity_setpoint"
    description = number.entity_description
    assert (description.native_min_value, description.native_max_value) == (30, 65)
    assert _hv(entities, "constant")[0]._attr_unique_id == "P1_1.2.3_constant_modulation_setpoint"
    assert _hv(entities, "eco")[0]._attr_unique_id == "P1_1.2.3_eco_modulation_setpoint"
    coordinator.circuit.modbus = None
    assert number.available is False
    assert number.native_value is None


def test_number_modbus_failure_names_value(entities):
    async def scenario():
        number, coordinator = _hv(entities, "humidity")
        coordinator.hv_error = ValueError("not allowed")
        with pytest.raises(entities.error, match="humidity_target to 55.*not allowed"):
            await number.async_set_native_value(55)

    asyncio.run(scenario())


def test_buttons_write_acknowledge_values(entities):
    async def scenario():
        button, coordinator = _hv(entities, "acknowledge_maintenance")
        await button.async_press()
        assert coordinator.hv_writes == [("acknowledge", 7)]
        assert coordinator.cloud_flags == [False]
        assert button._attr_unique_id == "P1_1.2.3_acknowledge_maintenance"
        button, coordinator = _hv(entities, "acknowledge_filter_cleaning")
        await button.async_press()
        assert coordinator.hv_writes == [("acknowledge", 8)]
        assert button._attr_unique_id == "P1_1.2.3_acknowledge_filter_cleaning"
        assert button.entity_description.entity_category == "config"

    asyncio.run(scenario())


def test_button_reports_gateway_exception(entities):
    async def scenario():
        button, coordinator = _hv(entities, "acknowledge_maintenance")
        coordinator.hv_error = entities.runtime.modbus.HovalModbusError(
            "gateway exception 2", exception_code=2
        )
        with pytest.raises(entities.error, match="acknowledge maintenance.*gateway exception 2"):
            await button.async_press()

    asyncio.run(scenario())


def test_local_fan_percentage_modbus_failure_reverts_pending_value(entities):
    async def scenario():
        fan, coordinator = _make_entity(entities, "fan", modbus=True, local=True)
        coordinator.hv_error = entities.runtime.modbus.HovalModbusError("down")
        fan._pending_percentage = 60
        request = fan._pending_request = object()
        with pytest.raises(entities.error, match="constant_modulation=60"):
            await fan._send_percentage(60, request)
        assert fan._pending_percentage is None

    asyncio.run(scenario())


# --- Availability during a hybrid cloud outage (C2) -----------------------------


def test_hybrid_cloud_outage_takes_cloud_fed_controls_unavailable(entities):
    """C2: HK/WW circuits stay in the snapshot with frozen cloud values; their
    entities must go unavailable, while Modbus-served HV controls keep working."""
    rt = entities.runtime
    coordinator, api, _, _ = _hybrid(rt)
    data = _tick(coordinator)
    entry = SimpleNamespace(options={})
    hk = data.plants["P1"].circuits[_HK]
    hv = data.plants["P1"].circuits[_HV]
    climate = entities.climate.HovalClimate(coordinator, entry, "P1", _HK, hk)
    hk_select = entities.select.HovalProgramSelect(coordinator, "P1", _HK, hk)
    hv_select = entities.select.HovalProgramSelect(coordinator, "P1", _HV, hv)
    fan = entities.fan.HovalFan(coordinator, entry, "P1", _HV, hv)
    controls = (climate, hk_select, hv_select, fan)
    assert all(e.available for e in controls)

    rt.clock.advance(900)
    api.plants_error = rt.api.HovalApiError("blocked")
    for _ in range(4):  # one cloud attempt, then three Modbus-only updates
        data = _tick(coordinator)
        rt.clock.advance(30)

    assert data.cloud_ok is False and _HK in data.plants["P1"].circuits
    assert climate.available is False
    assert hk_select.available is False
    assert hv_select.available is True
    assert fan.available is True


@pytest.mark.parametrize("platform", ["climate", "water_heater", "select", "fan"])
def test_cloud_fed_entities_unavailable_while_cloud_down(entities, platform):
    entity, _ = _make_entity(entities, platform, cloud_ok=False)
    assert entity.available is False


@pytest.mark.parametrize("platform", ["climate", "water_heater", "select", "fan"])
def test_cloud_only_entity_availability_unchanged(entities, platform):
    entity, coordinator = _make_entity(entities, platform)
    assert coordinator.data.cloud_ok is True and coordinator.circuit.modbus is None
    assert entity.available is True


@pytest.mark.parametrize("platform", ["select", "fan"])
def test_restored_circuit_without_reading_unavailable(entities, platform):
    entity, coordinator = _make_entity(entities, platform, cloud_ok=False)
    coordinator.circuit.circuit_type = "HV"
    coordinator.circuit.restored = True
    assert entity.available is False


@pytest.mark.parametrize("platform", ["select", "fan"])
def test_modbus_served_control_follows_the_reading_not_the_cloud(entities, platform):
    entity, coordinator = _make_entity(entities, platform)
    entities.runtime.coordinator.apply_modbus_reading(coordinator.circuit, None)  # gateway down
    assert coordinator.data.cloud_ok is True
    assert entity.available is False
    coordinator.circuit.modbus = SimpleNamespace()
    coordinator.data.cloud_ok = False
    assert entity.available is True


@pytest.mark.parametrize(
    ("module", "class_name"), [("climate", "HovalClimate"), ("water_heater", "HovalWaterHeater")]
)
def test_temperature_members_carry_native_and_old_names(entities, module, class_name):
    """HA 2026.11 renamed three members to native_* and warns about a class that
    provides only the old names; HA 2024.11 to 2026.10 reads only the old names.

    HA decides by the class's own namespace (migrate_deprecated_entity_members in
    helpers/deprecation.py): a class that provides the native name is left alone.
    So both names must be defined on the class itself, the old ones as aliases.
    """
    own = vars(getattr(getattr(entities, module), class_name))
    pairs = (
        ("_attr_native_temperature_unit", "_attr_temperature_unit"),
        ("native_current_temperature", "current_temperature"),
        ("native_target_temperature", "target_temperature"),
    )
    for native, old in pairs:
        assert native in own, native
        assert old in own, old
    assert own["_attr_temperature_unit"] == own["_attr_native_temperature_unit"]
    assert own["current_temperature"] is own["native_current_temperature"]
    assert own["target_temperature"] is own["native_target_temperature"]
