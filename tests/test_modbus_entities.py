"""Sensors and binary sensors with a Modbus source: availability and revived keys."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from tests.test_modbus_coordinator import _HV, _hybrid, _tick
from tests.test_program_polling import runtime  # noqa: F401


class _CoordinatorEntity:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def available(self):
        return True


class _Device(StrEnum):
    ENUM = "enum"
    CO2 = "carbon_dioxide"
    HUMIDITY = "humidity"
    TEMPERATURE = "temperature"
    TIMESTAMP = "timestamp"
    ENERGY = "energy"
    POWER = "power"
    CONNECTIVITY = "connectivity"
    PROBLEM = "problem"
    RUNNING = "running"


class _StateClass(StrEnum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


@dataclass(frozen=True, kw_only=True)
class _EntityDescription:
    key: str
    translation_key: str | None = None
    device_class: object = None
    native_unit_of_measurement: str | None = None
    state_class: object = None
    icon: str | None = None
    entity_category: object = None
    options: list | None = None


class _Registry:
    def __init__(self, unique_ids):
        self.entries = {
            f"sensor.e{i}": SimpleNamespace(
                entity_id=f"sensor.e{i}", domain="sensor", platform="hoval_connect", unique_id=uid
            )
            for i, uid in enumerate(unique_ids)
        }
        self.removed = []

    def async_remove(self, entity_id):
        self.removed.append(self.entries[entity_id].unique_id)

    def async_get_entity_id(self, domain, platform, uid):
        return next((e.entity_id for e in self.entries.values() if e.unique_id == uid), None)


@pytest.fixture
def ent(runtime, monkeypatch):  # noqa: F811 - imported pytest fixture
    names = (
        "homeassistant.components",
        "homeassistant.components.sensor",
        "homeassistant.components.binary_sensor",
        "homeassistant.const",
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.entity_registry",
    )
    mods = {}
    for name in names:
        mod = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, mod)
        mods[name] = mod
    sensor = mods["homeassistant.components.sensor"]
    sensor.SensorDeviceClass = _Device
    sensor.SensorStateClass = _StateClass
    sensor.SensorEntityDescription = _EntityDescription
    sensor.SensorEntity = type("SensorEntity", (), {})
    binary = mods["homeassistant.components.binary_sensor"]
    binary.BinarySensorDeviceClass = _Device
    binary.BinarySensorEntity = type("BinarySensorEntity", (), {})
    const = mods["homeassistant.const"]
    const.PERCENTAGE = "%"
    const.CONCENTRATION_PARTS_PER_MILLION = "ppm"
    const.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic")
    const.UnitOfEnergy = SimpleNamespace(MEGA_WATT_HOUR="MWh")
    const.UnitOfPower = SimpleNamespace(KILO_WATT="kW")
    const.UnitOfTemperature = SimpleNamespace(CELSIUS="C")
    const.UnitOfTime = SimpleNamespace(HOURS="h", WEEKS="w", SECONDS="s")
    mods["homeassistant.helpers.entity_platform"].AddEntitiesCallback = object
    sys.modules["homeassistant.helpers"].entity_registry = mods[
        "homeassistant.helpers.entity_registry"
    ]
    mods["homeassistant.helpers.entity_registry"].EntityRegistry = object
    mods["homeassistant.helpers.entity_registry"].async_entries_for_config_entry = (
        lambda reg, entry_id: list(reg.entries.values())
    )
    sys.modules["homeassistant.core"].callback = lambda fn: fn
    sys.modules["homeassistant.helpers.dispatcher"].async_dispatcher_connect = lambda *a: None
    sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = _CoordinatorEntity

    package_name = runtime.coordinator.__package__
    package = sys.modules[package_name]
    package.HovalConnectConfigEntry = object
    package.circuit_device_info = lambda *args: {}
    package.plant_device_info = lambda *args: {}
    directory = Path(runtime.coordinator.__file__).parent
    loaded = {}
    for basename in ("sensor", "binary_sensor"):
        name = f"{package_name}.{basename}"
        spec = importlib.util.spec_from_file_location(name, directory / f"{basename}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[basename] = module
    return SimpleNamespace(runtime=runtime, **loaded)


def _reading(rt, **overrides):
    fields = dict(
        program="week1",
        constant_modulation=60,
        eco_modulation=30,
        air_volume=45,
        humidity_target=50,
        humidity_actual=48,
        voc_extract=None,
        voc_outside=None,
        air_quality_control=2,
        control_state="normal",
        outside_temperature=7.5,
        exhaust_temperature=20.5,
        exhaust_fan_target=44,
        maintenance_enabled=True,
        maintenance_interval_weeks=26,
        maintenance_weeks_remaining=12,
        co2_extract=None,
        filter_cleaning_enabled=False,
        filter_cleaning_interval_weeks=8,
        active_errors=(),
    )
    fields.update(overrides)
    return rt.modbus_hv.HvModbusReading(**fields)


def _setup(rt, *, cloud_ok=True, reading=None, restored=False, local=False, modbus=False):
    c = rt.coordinator
    circuit = c.HovalCircuitData("HV", "1.50.0", "Ventilation", restored=restored)
    circuit.active_week_name = "Normal"
    circuit.live_values["airVolume"] = "45"
    if reading is not None:
        c.apply_modbus_reading(circuit, reading)
    plant = c.HovalPlantData("P1", "Plant", circuits={"1.50.0": circuit})
    data = c.HovalData(plants={"P1": plant}, cloud_ok=cloud_ok)
    coordinator = SimpleNamespace(
        hass=None, data=data, is_local=local, has_modbus=modbus or reading is not None
    )
    return coordinator, circuit


def _sensor(ent, coordinator, circuit, key):
    description = next(d for d in ent.sensor.CIRCUIT_SENSOR_DESCRIPTIONS if d.key == key)
    return ent.sensor.HovalCircuitSensor(coordinator, "P1", "1.50.0", circuit, description)


def test_modbus_sensor_available_while_cloud_down(ent):
    coordinator, circuit = _setup(ent.runtime, cloud_ok=False, reading=_reading(ent.runtime))
    assert _sensor(ent, coordinator, circuit, "air_volume").available is True


def test_cloud_sensor_unavailable_while_cloud_down(ent):
    coordinator, circuit = _setup(ent.runtime, cloud_ok=False, reading=_reading(ent.runtime))
    assert _sensor(ent, coordinator, circuit, "active_week_program").available is False


def test_cloud_only_entry_availability_unchanged(ent):
    coordinator, circuit = _setup(ent.runtime)
    assert circuit.modbus is None
    for key in ("air_volume", "active_week_program", "circuit_status"):
        assert _sensor(ent, coordinator, circuit, key).available is True


def test_restored_circuit_cloud_fields_unavailable_even_with_cloud_ok(ent):
    coordinator, circuit = _setup(ent.runtime, restored=True, reading=_reading(ent.runtime))
    assert _sensor(ent, coordinator, circuit, "active_week_program").available is False
    assert _sensor(ent, coordinator, circuit, "air_volume").available is True


def test_modbus_failed_and_cloud_down_makes_backed_key_unavailable(ent):
    coordinator, circuit = _setup(ent.runtime, cloud_ok=False)
    assert _sensor(ent, coordinator, circuit, "air_volume").available is False


def test_topology_and_offline_restore_mark_circuits_restored(ent):
    c = ent.runtime.coordinator
    data = c.data_from_topology(
        {"plant_id": "P1", "plant_name": "Plant", "circuits": {"1.50.0": "Ventilation"}}
    )
    assert data.plants["P1"].circuits["1.50.0"].restored is True
    assert c.HovalCircuitData("HV", "1.50.0", "x").restored is False


def test_revived_sensors_keep_old_unique_ids(ent):
    coordinator, circuit = _setup(ent.runtime, reading=_reading(ent.runtime))
    for key in (
        "hv_control_state",
        "hv_operating_selection",
        "hv_air_quality_control",
    ):
        assert _sensor(ent, coordinator, circuit, key)._attr_unique_id == f"P1_1.50.0_{key}"
    assert (
        _sensor(ent, coordinator, circuit, "hv_control_state")._attr_unique_id
        == "P1_1.50.0_hv_control_state"
    )


def test_retired_cleanup_skips_hv_keys_with_modbus(ent):
    ids = [
        "P1_1.50.0_hv_control_state",
        "P1_1.50.0_hv_operating_selection",
        "P1_1.50.0_hv_air_quality_control",
        "P1_1.50.0_hv_co2_extract",
        "P1_1.50.0_hv_voc_extract",
        "P1_1.50.0_hv_voc_outside",
        "P1_2.10.0_hk_control_state",
        "P1_1.50.0_air_volume",
    ]
    reg = _Registry(ids)
    ent.sensor._remove_retired_sensors(reg, "entry", True)
    assert reg.removed == ["P1_2.10.0_hk_control_state"]


def test_retired_cleanup_unchanged_without_modbus(ent):
    ids = [
        "P1_1.50.0_hv_control_state",
        "P1_1.50.0_hv_co2_extract",
        "P1_2.10.0_hk_control_state",
        "P1_1.50.0_air_volume",
    ]
    reg = _Registry(ids)
    ent.sensor._remove_retired_sensors(reg, "entry", False)
    assert reg.removed == [
        "P1_1.50.0_hv_control_state",
        "P1_1.50.0_hv_co2_extract",
        "P1_2.10.0_hk_control_state",
    ]


def _created_keys(ent, coordinator):
    added = []
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator),
        entry_id="entry",
        async_on_unload=lambda fn: None,
    )
    hass = object()
    reg = _Registry([])
    ent.sensor.er.async_get = lambda hass: reg
    import asyncio

    asyncio.run(ent.sensor.async_setup_entry(hass, entry, added.extend))
    return {e._attr_unique_id for e in added}


def test_co2_sensor_only_when_fitted(ent):
    coordinator, _ = _setup(ent.runtime, reading=_reading(ent.runtime, co2_extract=None))
    keys = _created_keys(ent, coordinator)
    assert "P1_1.50.0_hv_co2_extract" not in keys
    assert "P1_1.50.0_hv_voc_extract" not in keys
    assert "P1_1.50.0_hv_active_error" in keys
    coordinator, _ = _setup(
        ent.runtime, reading=_reading(ent.runtime, co2_extract=650, voc_extract=12, voc_outside=5)
    )
    keys = _created_keys(ent, coordinator)
    assert {"P1_1.50.0_hv_co2_extract", "P1_1.50.0_hv_voc_extract"} <= keys
    assert "P1_1.50.0_hv_voc_outside" in keys


def test_no_modbus_sensors_without_modbus(ent):
    coordinator, _ = _setup(ent.runtime)
    keys = _created_keys(ent, coordinator)
    assert not [k for k in keys if "_hv_control_state" in k or "_hv_active_error" in k]
    assert "P1_1.50.0_air_volume" in keys


def test_control_state_values(ent):
    coordinator, circuit = _setup(
        ent.runtime, reading=_reading(ent.runtime, control_state="coolvent")
    )
    assert _sensor(ent, coordinator, circuit, "hv_control_state").native_value == "coolvent"
    description = next(
        d for d in ent.sensor.CIRCUIT_SENSOR_DESCRIPTIONS if d.key == "hv_control_state"
    )
    assert description.options == list(ent.runtime.modbus_hv.HV_CONTROL_STATES)
    selection = next(
        d for d in ent.sensor.CIRCUIT_SENSOR_DESCRIPTIONS if d.key == "hv_operating_selection"
    )
    assert selection.options == ["standby", "week1", "week2", "constant", "eco"]


@pytest.mark.parametrize(("errors", "value"), [((), 0), ((41, 7), 41), (None, None)])
def test_active_error_value(ent, errors, value):
    coordinator, circuit = _setup(ent.runtime, reading=_reading(ent.runtime, active_errors=errors))
    assert _sensor(ent, coordinator, circuit, "hv_active_error").native_value == value


def test_maintenance_attributes(ent):
    coordinator, circuit = _setup(ent.runtime, reading=_reading(ent.runtime))
    sensor = _sensor(ent, coordinator, circuit, "hv_maintenance_weeks_remaining")
    assert sensor.native_value == 12.0
    assert sensor.extra_state_attributes == {"enabled": True, "interval_weeks": 26}
    filt = _sensor(ent, coordinator, circuit, "hv_filter_cleaning_interval")
    assert filt.native_value == 8.0
    assert filt.extra_state_attributes == {"enabled": False}


def test_local_entry_has_no_weather_or_event_sensors(ent):
    coordinator, _ = _setup(ent.runtime, cloud_ok=False, reading=_reading(ent.runtime), local=True)
    keys = _created_keys(ent, coordinator)
    assert not [k for k in keys if k.startswith("P1_") and k.count("_") == 1]
    assert "P1_1.50.0_air_volume" in keys
    coordinator.is_local = False
    keys = _created_keys(ent, coordinator)
    assert "P1_weather_condition" in keys


def test_hybrid_plant_sensors_follow_cloud(ent):
    coordinator, _ = _setup(ent.runtime, cloud_ok=False, reading=_reading(ent.runtime))
    plant = coordinator.data.plants["P1"]
    description = ent.sensor.PLANT_SENSOR_DESCRIPTIONS[0]
    sensor = ent.sensor.HovalPlantSensor(coordinator, "P1", plant, description)
    assert sensor.available is False
    coordinator.data.cloud_ok = True
    assert sensor.available is True


def test_local_plant_online_and_error_available(ent):
    coordinator, _ = _setup(ent.runtime, cloud_ok=False, reading=_reading(ent.runtime), local=True)
    plant = coordinator.data.plants["P1"]
    assert ent.binary_sensor.HovalPlantOnline(coordinator, "P1", plant).available is True
    assert ent.binary_sensor.HovalPlantError(coordinator, "P1", plant).available is True


def test_hybrid_plant_binary_sensors_unavailable_while_cloud_down(ent):
    coordinator, _ = _setup(ent.runtime, cloud_ok=False, reading=_reading(ent.runtime))
    plant = coordinator.data.plants["P1"]
    assert ent.binary_sensor.HovalPlantOnline(coordinator, "P1", plant).available is False
    assert ent.binary_sensor.HovalPlantError(coordinator, "P1", plant).available is False
    coordinator.data.cloud_ok = True
    assert ent.binary_sensor.HovalPlantOnline(coordinator, "P1", plant).available is True


def test_circuit_binary_sensor_availability(ent):
    coordinator, circuit = _setup(ent.runtime, cloud_ok=False, reading=_reading(ent.runtime))
    aq = ent.binary_sensor.HovalAirQualityGuided(coordinator, "P1", "1.50.0", circuit)
    tc = ent.binary_sensor.HovalTemporaryChange(coordinator, "P1", "1.50.0", circuit)
    assert aq.available is True
    assert tc.available is False
    coordinator, circuit = _setup(ent.runtime)
    assert (
        ent.binary_sensor.HovalTemporaryChange(coordinator, "P1", "1.50.0", circuit).available
        is True
    )


def _run_binary_setup(ent, coordinator):
    import asyncio

    added = []
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator), async_on_unload=lambda fn: None
    )
    asyncio.run(ent.binary_sensor.async_setup_entry(object(), entry, added.extend))
    return {e._attr_unique_id for e in added}


def test_local_entry_creates_only_modbus_backed_circuit_entities(ent):
    MODBUS_BACKED_KEYS = ent.runtime.const.MODBUS_BACKED_KEYS
    coordinator, _ = _setup(
        ent.runtime,
        cloud_ok=False,
        local=True,
        reading=_reading(ent.runtime, co2_extract=600, voc_extract=3, voc_outside=2),
    )
    sensor_keys = {k.removeprefix("P1_1.50.0_") for k in _created_keys(ent, coordinator)}
    assert sensor_keys
    assert sensor_keys <= MODBUS_BACKED_KEYS
    binary_ids = _run_binary_setup(ent, coordinator)
    assert binary_ids == {"P1_online", "P1_error", "P1_1.50.0_air_quality_guided"}


def test_cloud_entry_still_creates_cloud_circuit_entities(ent):
    coordinator, _ = _setup(ent.runtime)
    assert "P1_1.50.0_temporary_change" in _run_binary_setup(ent, coordinator)
    assert "P1_1.50.0_active_week_program" in _created_keys(ent, coordinator)


def test_modbus_only_sensors_appear_when_reading_arrives_later(ent, monkeypatch):
    import asyncio

    callbacks = []
    monkeypatch.setattr(
        ent.sensor, "async_dispatcher_connect", lambda hass, sig, cb: callbacks.append(cb)
    )
    coordinator, circuit = _setup(ent.runtime, cloud_ok=False, modbus=True)
    added = []
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator),
        entry_id="entry",
        async_on_unload=lambda fn: None,
    )
    reg = _Registry([])
    ent.sensor.er.async_get = lambda hass: reg
    asyncio.run(ent.sensor.async_setup_entry(object(), entry, added.extend))
    assert not [e for e in added if e._attr_unique_id.endswith("_hv_control_state")]
    ent.runtime.coordinator.apply_modbus_reading(circuit, _reading(ent.runtime))
    callbacks[0]()
    assert [e for e in added if e._attr_unique_id.endswith("_hv_control_state")]


def test_gateway_failure_in_hybrid_makes_modbus_backed_entities_unavailable(ent):
    """I2: with the cloud fine and the gateway down, Modbus keys show unavailable.

    Before, they fell back to the cloud rule and showed "unknown", and
    air_quality_guided showed a made-up "off".
    """
    rt = ent.runtime
    coordinator, _, poller, _ = _hybrid(rt)
    hv = _tick(coordinator).plants["P1"].circuits[_HV]

    def sensor(key):
        description = next(d for d in ent.sensor.CIRCUIT_SENSOR_DESCRIPTIONS if d.key == key)
        return ent.sensor.HovalCircuitSensor(coordinator, "P1", _HV, hv, description)

    modbus_entities = [
        sensor("air_volume"),
        sensor("hv_constant_modulation"),
        sensor("hv_control_state"),
        ent.binary_sensor.HovalAirQualityGuided(coordinator, "P1", _HV, hv),
    ]
    cloud_entities = [
        sensor("active_week_program"),
        ent.binary_sensor.HovalTemporaryChange(coordinator, "P1", _HV, hv),
    ]
    assert all(e.available for e in modbus_entities + cloud_entities)

    poller.poll_error = OSError("gateway down")
    rt.clock.advance(30)
    data = _tick(coordinator)

    assert data.cloud_ok is True
    assert [e.available for e in modbus_entities] == [False] * len(modbus_entities)
    assert all(e.available for e in cloud_entities)


def test_modbus_served_circuit_without_reading_unavailable_for_backed_keys(ent):
    coordinator, circuit = _setup(ent.runtime)
    ent.runtime.coordinator.apply_modbus_reading(circuit, None)  # served, gateway down
    assert _sensor(ent, coordinator, circuit, "air_volume").available is False
    assert _sensor(ent, coordinator, circuit, "active_week_program").available is True
    aq = ent.binary_sensor.HovalAirQualityGuided(coordinator, "P1", "1.50.0", circuit)
    assert aq.available is False


# --- Revived sensors keep their v1.0.18 definitions (R-REVIVE: I3, I4) ----------

# The last definitions before v1.1.0 retired these sensors, copied from
# `git show v1.0.18:custom_components/hoval_connect/sensor.py` and const.py.
# Entity ids and long-term statistics continue only if nothing here changes.
_V1_0_18 = {
    "hv_control_state": dict(
        device_class="enum",
        native_unit_of_measurement=None,
        state_class=None,
        entity_category=None,
        icon="mdi:state-machine",
        options=[
            "off",
            "normal",
            "voc",
            "humidity",
            "frost_protection",
            "coolvent",
            "fault",
            "summer_humidity",
            "switch_off_stop",
        ],
    ),
    "hv_operating_selection": dict(
        device_class="enum",
        native_unit_of_measurement=None,
        state_class=None,
        entity_category="diagnostic",
        icon="mdi:tune-variant",
        options=["standby", "week1", "week2", "constant", "eco"],
    ),
    "hv_air_quality_control": dict(
        device_class=None,
        native_unit_of_measurement=None,
        state_class=None,
        entity_category="diagnostic",
        icon="mdi:air-filter",
        options=None,
    ),
    "hv_co2_extract": dict(
        device_class=None,
        native_unit_of_measurement="%",
        state_class="measurement",
        entity_category=None,
        icon="mdi:molecule-co2",
        options=None,
    ),
    "hv_voc_extract": dict(
        device_class=None,
        native_unit_of_measurement="%",
        state_class="measurement",
        entity_category=None,
        icon="mdi:air-filter",
        options=None,
    ),
    "hv_voc_outside": dict(
        device_class=None,
        native_unit_of_measurement="%",
        state_class="measurement",
        entity_category=None,
        icon="mdi:weather-windy",
        options=None,
    ),
}


@pytest.mark.parametrize("key", sorted(_V1_0_18))
def test_revived_sensor_matches_v1_0_18(ent, key):
    description = next(d for d in ent.sensor.CIRCUIT_SENSOR_DESCRIPTIONS if d.key == key)
    assert description.translation_key == key
    actual = {field: getattr(description, field) for field in _V1_0_18[key]}
    assert actual == _V1_0_18[key]


@pytest.mark.parametrize(
    ("program", "state"),
    [
        ("standby", "standby"),
        ("week1", "week1"),
        ("week2", "week2"),
        ("constant", "constant"),
        ("ecoMode", "eco"),
        (None, None),
    ],
)
def test_operating_selection_maps_modbus_program_to_old_state_keys(ent, program, state):
    coordinator, circuit = _setup(ent.runtime, reading=_reading(ent.runtime, program=program))
    assert _sensor(ent, coordinator, circuit, "hv_operating_selection").native_value == state


def test_every_modbus_program_has_an_operating_selection_state(ent):
    programs = set(ent.runtime.modbus_hv.PROGRAM_BY_VALUE.values())
    assert set(ent.sensor.HV_OPERATING_SELECTIONS) == programs


def test_co2_extract_is_a_percentage(ent):
    coordinator, circuit = _setup(ent.runtime, reading=_reading(ent.runtime, co2_extract=42))
    sensor = _sensor(ent, coordinator, circuit, "hv_co2_extract")
    assert sensor.native_value == 42.0
    assert sensor.entity_description.native_unit_of_measurement == "%"
    assert sensor.entity_description.device_class is None


_TRANSLATION_FILES = ("strings.json", "translations/en.json", "translations/de.json")


def _translations(name):
    import json

    path = Path(__file__).resolve().parents[1] / "custom_components" / "hoval_connect" / name
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", _TRANSLATION_FILES)
def test_entity_state_keys_pass_hassfest(name):
    """hassfest's translation schema (script/hassfest/translations.py) only
    accepts lowercase snake_case state keys; "ecoMode" failed it (I4)."""
    import re

    pattern = re.compile(r"^(?!.+[_-]{2})(?![_-])[a-z0-9-_]+(?<![_-])$")
    bad = [
        f"{platform}.{key}.{state}"
        for platform, entities in _translations(name)["entity"].items()
        for key, entity in entities.items()
        for state in entity.get("state", {})
        if not pattern.match(state)
    ]
    assert not bad


@pytest.mark.parametrize("name", _TRANSLATION_FILES)
def test_enum_sensor_options_equal_translated_state_keys(ent, name):
    sensors = _translations(name)["entity"]["sensor"]
    descriptions = (*ent.sensor.CIRCUIT_SENSOR_DESCRIPTIONS, *ent.sensor.PLANT_SENSOR_DESCRIPTIONS)
    enums = [d for d in descriptions if d.device_class == "enum"]
    assert enums
    for description in enums:
        states = sensors[description.translation_key].get("state", {})
        assert sorted(description.options) == sorted(states), description.key


# v1.0.18 names; entity ids derive from them when an entity is (re)created.
_V1_0_18_NAMES = {
    "translations/en.json": {
        "hv_control_state": "Control status",
        "hv_operating_selection": "Operating selection",
        "hv_air_quality_control": "Air quality control (code)",
        "hv_co2_extract": "CO₂ extract air",
        "hv_voc_extract": "VOC extract air",
        "hv_voc_outside": "VOC outside air",
    },
    "translations/de.json": {
        "hv_control_state": "Regelungsstatus",
        "hv_operating_selection": "Betriebswahl",
        "hv_air_quality_control": "Luftqualitätsregelung (Code)",
        "hv_co2_extract": "CO₂ Abluft",
        "hv_voc_extract": "VOC Abluft",
        "hv_voc_outside": "VOC Aussenluft",
    },
}


@pytest.mark.parametrize("name", sorted(_V1_0_18_NAMES))
def test_revived_sensor_names_match_v1_0_18(name):
    sensors = _translations(name)["entity"]["sensor"]
    assert {key: sensors[key]["name"] for key in _V1_0_18_NAMES[name]} == _V1_0_18_NAMES[name]


def test_strings_json_matches_english_for_revived_sensors():
    english = _translations("translations/en.json")["entity"]["sensor"]
    source = _translations("strings.json")["entity"]["sensor"]
    for key in _V1_0_18_NAMES["translations/en.json"]:
        assert source[key] == english[key], key


# --- Gateway clock offset (plant-level, Modbus-backed diagnostic sensor) ----------


def _with_clock(coordinator, offset=9):
    from datetime import datetime, timedelta, timezone

    plant = coordinator.data.plants["P1"]
    plant.gateway_clock_offset = offset
    plant.gateway_time = datetime(2026, 11, 20, 0, 46, 57, tzinfo=timezone(timedelta(hours=1)))


def _clock_sensor(ent, coordinator):
    description = next(
        d for d in ent.sensor.PLANT_SENSOR_DESCRIPTIONS if d.key == "gateway_clock_offset"
    )
    return ent.sensor.HovalPlantSensor(
        coordinator, "P1", coordinator.data.plants["P1"], description
    )


def test_gateway_clock_sensor_description(ent):
    description = next(
        d for d in ent.sensor.PLANT_SENSOR_DESCRIPTIONS if d.key == "gateway_clock_offset"
    )
    assert description.translation_key == "gateway_clock_offset"
    assert description.native_unit_of_measurement == "s"
    assert description.state_class == "measurement"
    assert description.entity_category == "diagnostic"
    assert description.icon == "mdi:clock-alert-outline"


def test_gateway_clock_sensor_value_and_attribute(ent):
    coordinator, _ = _setup(ent.runtime, reading=_reading(ent.runtime))
    _with_clock(coordinator)
    sensor = _clock_sensor(ent, coordinator)
    assert sensor._attr_unique_id == "P1_gateway_clock_offset"
    assert sensor.native_value == 9
    assert sensor.extra_state_attributes == {"gateway_time": "2026-11-20T00:46:57+01:00"}
    assert sensor.available is True


def test_gateway_clock_sensor_available_while_cloud_down(ent):
    coordinator, _ = _setup(ent.runtime, cloud_ok=False, reading=_reading(ent.runtime))
    _with_clock(coordinator)
    assert _clock_sensor(ent, coordinator).available is True


def test_gateway_clock_sensor_unavailable_without_value(ent):
    coordinator, _ = _setup(ent.runtime, reading=_reading(ent.runtime))
    assert _clock_sensor(ent, coordinator).available is False


def test_gateway_clock_sensor_created_only_with_a_value(ent):
    coordinator, _ = _setup(ent.runtime, reading=_reading(ent.runtime))
    assert "P1_gateway_clock_offset" not in _created_keys(ent, coordinator)
    _with_clock(coordinator)
    assert "P1_gateway_clock_offset" in _created_keys(ent, coordinator)


def test_gateway_clock_sensor_in_local_entry_but_not_without_modbus(ent):
    coordinator, _ = _setup(ent.runtime, cloud_ok=False, reading=_reading(ent.runtime), local=True)
    _with_clock(coordinator)
    assert "P1_gateway_clock_offset" in _created_keys(ent, coordinator)
    cloud_only, _ = _setup(ent.runtime)
    _with_clock(cloud_only)
    assert "P1_gateway_clock_offset" not in _created_keys(ent, cloud_only)
