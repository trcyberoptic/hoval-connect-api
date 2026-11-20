"""Sensor platform for Hoval Connect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import HovalConnectConfigEntry, circuit_device_info, plant_device_info
from .const import (
    BOILER_FA_STATES,
    CIRCUIT_CONTROL_TYPES,
    CIRCUIT_STATUSES,
    CIRCUIT_TYPE_BL,
    CIRCUIT_TYPE_HK,
    CIRCUIT_TYPE_HV,
    CIRCUIT_TYPE_PS,
    CIRCUIT_TYPE_WW,
    DOMAIN,
    MODBUS_BACKED_KEYS,
    ROOM_AIR_QUALITIES,
    is_retired_circuit_sensor,
)
from .coordinator import (
    SIGNAL_NEW_CIRCUITS,
    HovalCircuitData,
    HovalDataCoordinator,
    HovalPlantData,
    circuit_value_available,
)
from .modbus_hv import HV_CONTROL_STATES, HvModbusReading


def _no_cloud_value(_circuit: HovalCircuitData) -> None:
    """Default value_fn of sensors that only exist with a Modbus reading."""
    return None


@dataclass(frozen=True, kw_only=True)
class HovalSensorEntityDescription(SensorEntityDescription):
    """Describe a Hoval sensor entity."""

    value_fn: Callable[[HovalCircuitData], Any | None] = _no_cloud_value
    # Value read from the circuit's Modbus reading (keys in MODBUS_BACKED_KEYS
    # that have no cloud counterpart); wins over value_fn when set.
    modbus_value_fn: Callable[[HvModbusReading], Any | None] | None = None
    # Extra state attributes, computed from the Modbus reading.
    modbus_attributes_fn: Callable[[HvModbusReading], dict[str, Any]] | None = None
    circuit_types: frozenset[str] | None = None
    # Create the entity only for circuits this returns True for (e.g. units
    # with an air-quality sensor). Already registered entities are kept.
    exists_fn: Callable[[HovalCircuitData], bool] | None = None


@dataclass(frozen=True, kw_only=True)
class HovalPlantSensorEntityDescription(SensorEntityDescription):
    """Describe a Hoval plant-level sensor entity."""

    value_fn: Callable[[HovalPlantData], Any | None]
    # Value comes from the Modbus gateway: no cloud needed, created only once a
    # value was read (an already registered entity is kept), available while
    # Modbus delivers it.
    modbus_backed: bool = False
    attributes_fn: Callable[[HovalPlantData], dict[str, Any]] | None = None


def _coerce_timestamp(value: Any) -> datetime | None:
    """Return a timezone-aware datetime for timestamp sensors."""
    if isinstance(value, datetime):
        dt_value = value
    elif isinstance(value, str):
        dt_value = dt_util.parse_datetime(value)
        if dt_value is None:
            return None
    else:
        return None

    # Home Assistant timestamp sensors require tz-aware datetimes.
    if dt_value.tzinfo is None:
        return dt_util.as_utc(dt_value)
    return dt_value


def _has_modbus(circuit: HovalCircuitData) -> bool:
    return circuit.modbus is not None


# Modbus program (Betriebswahl) -> the state keys hv_operating_selection had
# in v1.0.18. "ecoMode" is not a valid state key (hassfest: lowercase only).
HV_OPERATING_SELECTIONS: dict[str, str] = {
    "standby": "standby",
    "week1": "week1",
    "week2": "week2",
    "constant": "constant",
    "ecoMode": "eco",
}


CIRCUIT_SENSOR_DESCRIPTIONS: tuple[HovalSensorEntityDescription, ...] = (
    HovalSensorEntityDescription(
        key="outside_temperature",
        translation_key="outside_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HV, CIRCUIT_TYPE_HK}),
        value_fn=lambda c: c.live_values.get("outsideTemperature"),
    ),
    HovalSensorEntityDescription(
        key="exhaust_temperature",
        translation_key="exhaust_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.live_values.get("exhaustTemp"),
    ),
    HovalSensorEntityDescription(
        key="air_volume",
        translation_key="air_volume",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan",
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.live_values.get("airVolume"),
    ),
    HovalSensorEntityDescription(
        key="humidity_actual",
        translation_key="humidity_actual",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.live_values.get("humidityActual"),
    ),
    HovalSensorEntityDescription(
        key="humidity_target",
        translation_key="humidity_target",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.live_values.get("humidityTarget"),
    ),
    HovalSensorEntityDescription(
        key="circuit_status",
        translation_key="circuit_status",
        icon="mdi:information-outline",
        device_class=SensorDeviceClass.ENUM,
        options=list(CIRCUIT_STATUSES),
        value_fn=lambda c: c.circuit_status if c.circuit_status in CIRCUIT_STATUSES else None,
    ),
    # Coarse room air quality from the unit's own sensor: the only cloud
    # stand-in for the CO2/VOC sensors retired in v1.1.0 (#24).
    HovalSensorEntityDescription(
        key="room_air_quality",
        translation_key="room_air_quality",
        icon="mdi:air-filter",
        device_class=SensorDeviceClass.ENUM,
        options=list(ROOM_AIR_QUALITIES),
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        exists_fn=lambda c: c.has_air_quality_sensor or c.room_air_quality is not None,
        value_fn=lambda c: c.room_air_quality if c.room_air_quality in ROOM_AIR_QUALITIES else None,
    ),
    HovalSensorEntityDescription(
        key="holiday_end",
        translation_key="holiday_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:beach",
        circuit_types=frozenset({CIRCUIT_TYPE_HK, CIRCUIT_TYPE_WW}),
        value_fn=lambda c: c.holiday_end,
    ),
    HovalSensorEntityDescription(
        key="circuit_control_type",
        translation_key="circuit_control_type",
        icon="mdi:tune-variant",
        device_class=SensorDeviceClass.ENUM,
        options=list(CIRCUIT_CONTROL_TYPES.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HK}),
        value_fn=lambda c: CIRCUIT_CONTROL_TYPES.get(c.circuit_control_type),
    ),
    # Setpoints of the constant and eco programs, from the circuit details.
    HovalSensorEntityDescription(
        key="hv_constant_modulation",
        translation_key="hv_constant_modulation",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.constant_value,
    ),
    HovalSensorEntityDescription(
        key="hv_eco_modulation",
        translation_key="hv_eco_modulation",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan-minus",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.eco_mode_value,
    ),
    # Sensors that only exist with a Modbus reading. The first six were retired
    # in v1.1.0 (the cloud lost them) and come back under their old keys. Their
    # translation keys, names, units, device and state classes, categories,
    # icons and enum state keys are exactly those of v1.0.18, so entity ids
    # and long-term statistics continue (HA < 2025.7 does not restore a
    # deleted entity id by name).
    HovalSensorEntityDescription(
        key="hv_control_state",
        translation_key="hv_control_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(HV_CONTROL_STATES),
        icon="mdi:state-machine",
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        exists_fn=_has_modbus,
        modbus_value_fn=lambda r: r.control_state,
    ),
    HovalSensorEntityDescription(
        key="hv_operating_selection",
        translation_key="hv_operating_selection",
        device_class=SensorDeviceClass.ENUM,
        options=list(HV_OPERATING_SELECTIONS.values()),
        icon="mdi:tune-variant",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        exists_fn=_has_modbus,
        modbus_value_fn=lambda r: HV_OPERATING_SELECTIONS.get(r.program),
    ),
    HovalSensorEntityDescription(
        key="hv_air_quality_control",
        translation_key="hv_air_quality_control",
        icon="mdi:air-filter",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        exists_fn=_has_modbus,
        # Hoval documents this as a list but ships no labels for it, so the raw
        # code is shown rather than an invented mapping.
        modbus_value_fn=lambda r: r.air_quality_control,
    ),
    # Datapoint 37606 is a U8 percentage (0..100), not ppm.
    HovalSensorEntityDescription(
        key="hv_co2_extract",
        translation_key="hv_co2_extract",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:molecule-co2",
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        exists_fn=lambda c: _has_modbus(c) and c.modbus.co2_extract is not None,
        modbus_value_fn=lambda r: r.co2_extract,
    ),
    HovalSensorEntityDescription(
        key="hv_voc_extract",
        translation_key="hv_voc_extract",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:air-filter",
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        exists_fn=lambda c: _has_modbus(c) and c.modbus.voc_extract is not None,
        modbus_value_fn=lambda r: r.voc_extract,
    ),
    HovalSensorEntityDescription(
        key="hv_voc_outside",
        translation_key="hv_voc_outside",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-windy",
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        exists_fn=lambda c: _has_modbus(c) and c.modbus.voc_outside is not None,
        modbus_value_fn=lambda r: r.voc_outside,
    ),
    HovalSensorEntityDescription(
        key="hv_exhaust_fan_target",
        translation_key="hv_exhaust_fan_target",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan-chevron-up",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        exists_fn=_has_modbus,
        modbus_value_fn=lambda r: r.exhaust_fan_target,
    ),
    HovalSensorEntityDescription(
        key="hv_maintenance_weeks_remaining",
        translation_key="hv_maintenance_weeks_remaining",
        native_unit_of_measurement=UnitOfTime.WEEKS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:wrench-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        exists_fn=_has_modbus,
        modbus_value_fn=lambda r: r.maintenance_weeks_remaining,
        modbus_attributes_fn=lambda r: {
            "enabled": r.maintenance_enabled,
            "interval_weeks": r.maintenance_interval_weeks,
        },
    ),
    HovalSensorEntityDescription(
        key="hv_filter_cleaning_interval",
        translation_key="hv_filter_cleaning_interval",
        native_unit_of_measurement=UnitOfTime.WEEKS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:air-filter",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        exists_fn=_has_modbus,
        modbus_value_fn=lambda r: r.filter_cleaning_interval_weeks,
        modbus_attributes_fn=lambda r: {"enabled": r.filter_cleaning_enabled},
    ),
    # First active error code; 0 = none, None = errors not read yet.
    HovalSensorEntityDescription(
        key="hv_active_error",
        translation_key="hv_active_error",
        icon="mdi:alert-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        exists_fn=_has_modbus,
        modbus_value_fn=lambda r: (
            None if r.active_errors is None else (r.active_errors[0] if r.active_errors else 0)
        ),
        modbus_attributes_fn=lambda r: {"active_errors": list(r.active_errors or ())},
    ),
    HovalSensorEntityDescription(
        key="temporary_change_end",
        translation_key="temporary_change_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:timer-sand",
        value_fn=lambda c: c.temporary_change_end,
    ),
    HovalSensorEntityDescription(
        key="operation_mode",
        translation_key="operation_mode",
        icon="mdi:cog",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.operation_mode,
    ),
    HovalSensorEntityDescription(
        key="active_week_program",
        translation_key="active_week_program",
        icon="mdi:calendar-week",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.active_week_name,
    ),
    HovalSensorEntityDescription(
        key="active_day_program",
        translation_key="active_day_program",
        icon="mdi:calendar-today",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.active_day_program_name,
    ),
    HovalSensorEntityDescription(
        key="program_air_volume",
        translation_key="program_air_volume",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.program_air_volume,
    ),
    # HK additional sensors
    HovalSensorEntityDescription(
        key="room_temp_actual",
        translation_key="room_temp_actual",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HK}),
        value_fn=lambda c: c.live_values.get("roomTempActual"),
    ),
    HovalSensorEntityDescription(
        key="flow_temp_actual",
        translation_key="flow_temp_actual",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HK}),
        value_fn=lambda c: c.live_values.get("outgoingTempActual"),
    ),
    HovalSensorEntityDescription(
        key="flow_temp_target",
        translation_key="flow_temp_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HK}),
        value_fn=lambda c: c.live_values.get("outgoingTempTarget"),
    ),
    HovalSensorEntityDescription(
        key="room_temp_target",
        translation_key="room_temp_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HK}),
        value_fn=lambda c: c.live_values.get("roomTempTarget"),
    ),
    # BL (Boiler/Heat Pump) sensors
    HovalSensorEntityDescription(
        key="boiler_temp_actual",
        translation_key="boiler_temp_actual",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("tempActual"),
    ),
    HovalSensorEntityDescription(
        key="boiler_temp_target",
        translation_key="boiler_temp_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("tempTarget"),
    ),
    HovalSensorEntityDescription(
        key="return_temperature",
        translation_key="return_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("returnTemperature"),
    ),
    HovalSensorEntityDescription(
        key="operating_hours",
        translation_key="operating_hours",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:clock-outline",
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("operatingHours"),
    ),
    HovalSensorEntityDescription(
        key="operating_hours_over_50",
        translation_key="operating_hours_over_50",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:clock-fast",
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("operatingHoursOver50"),
    ),
    HovalSensorEntityDescription(
        key="operating_hours_el_heater",
        translation_key="operating_hours_el_heater",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:clock-outline",
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("operatingHoursElHeater"),
    ),
    HovalSensorEntityDescription(
        key="operation_cycles_el_heater",
        translation_key="operation_cycles_el_heater",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("operationCyclesElHeater"),
    ),
    HovalSensorEntityDescription(
        key="heat_amount_el_heater",
        translation_key="heat_amount_el_heater",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.MEGA_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("heatAmountElHeater"),
    ),
    HovalSensorEntityDescription(
        key="energy_el_heater",
        translation_key="energy_el_heater",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.MEGA_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("energyElHeater"),
    ),
    HovalSensorEntityDescription(
        key="el_heater_active",
        translation_key="el_heater_active",
        icon="mdi:lightning-bolt",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("elHeaterActive"),
    ),
    HovalSensorEntityDescription(
        key="operation_cycles",
        translation_key="operation_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("operationCycles"),
    ),
    HovalSensorEntityDescription(
        key="heat_amount",
        translation_key="heat_amount",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.MEGA_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("heatAmount"),
    ),
    HovalSensorEntityDescription(
        key="total_energy",
        translation_key="total_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.MEGA_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("totalEnergy"),
    ),
    HovalSensorEntityDescription(
        key="current_output_heating",
        translation_key="current_output_heating",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("currentEnergyOutput"),
    ),
    HovalSensorEntityDescription(
        key="modulation",
        translation_key="modulation",
        icon="mdi:speedometer",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("modulation"),
    ),
    HovalSensorEntityDescription(
        key="bl_fa_status",
        translation_key="bl_fa_status",
        icon="mdi:information-outline",
        device_class=SensorDeviceClass.ENUM,
        options=list(BOILER_FA_STATES.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: (
            BOILER_FA_STATES.get(v) if (v := c.live_values.get("faStatus")) is not None else None
        ),
    ),
    # WW (Warm Water) sensors
    HovalSensorEntityDescription(
        key="ww_temp_target",
        translation_key="ww_temp_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_WW}),
        value_fn=lambda c: c.live_values.get("tempTarget"),
    ),
    HovalSensorEntityDescription(
        key="ww_temp_top",
        translation_key="ww_temp_top",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_WW}),
        value_fn=lambda c: c.live_values.get("tempSf1Actual"),
    ),
    HovalSensorEntityDescription(
        key="ww_temp_bottom",
        translation_key="ww_temp_bottom",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_WW}),
        value_fn=lambda c: c.live_values.get("tempSf2Actual"),
    ),
    # PS (Buffer Storage) sensors
    HovalSensorEntityDescription(
        key="ps_buffer_target",
        translation_key="ps_buffer_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_PS}),
        value_fn=lambda c: c.live_values.get("bufferTarget"),
    ),
    HovalSensorEntityDescription(
        key="ps_buffer_actual_pf1",
        translation_key="ps_buffer_actual_pf1",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_PS}),
        value_fn=lambda c: c.live_values.get("bufferActualPF1"),
    ),
    HovalSensorEntityDescription(
        key="ps_buffer_actual_pf2",
        translation_key="ps_buffer_actual_pf2",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_PS}),
        value_fn=lambda c: c.live_values.get("bufferActualPF2"),
    ),
)

PLANT_SENSOR_DESCRIPTIONS: tuple[HovalPlantSensorEntityDescription, ...] = (
    HovalPlantSensorEntityDescription(
        key="latest_event_type",
        translation_key="latest_event_type",
        icon="mdi:alert-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.latest_event.event_type if p.latest_event else None,
    ),
    HovalPlantSensorEntityDescription(
        key="latest_event_message",
        translation_key="latest_event_message",
        icon="mdi:message-alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.latest_event.description if p.latest_event else None,
    ),
    HovalPlantSensorEntityDescription(
        key="latest_event_time",
        translation_key="latest_event_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.latest_event.time_occurred if p.latest_event else None,
    ),
    HovalPlantSensorEntityDescription(
        key="active_events",
        translation_key="active_events",
        icon="mdi:alert",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: sum(1 for e in p.events if e.is_active),
    ),
    HovalPlantSensorEntityDescription(
        key="weather_condition",
        translation_key="weather_condition",
        icon="mdi:weather-partly-cloudy",
        value_fn=lambda p: p.weather.weather_type if p.weather else None,
    ),
    HovalPlantSensorEntityDescription(
        key="weather_temperature",
        translation_key="weather_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: p.weather.outside_temperature if p.weather else None,
    ),
    # Plant-level, from the gateway's own clock (datapoint 20, registers 19802/19803).
    HovalPlantSensorEntityDescription(
        key="gateway_clock_offset",
        translation_key="gateway_clock_offset",
        icon="mdi:clock-alert-outline",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        modbus_backed=True,
        value_fn=lambda p: p.gateway_clock_offset,
        attributes_fn=lambda p: {
            "gateway_time": p.gateway_time.isoformat() if p.gateway_time else None
        },
    ),
)


def _is_modbus_backed(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in MODBUS_BACKED_KEYS)


def _remove_retired_sensors(ent_reg: er.EntityRegistry, entry_id: str, keep_hv: bool) -> None:
    """Delete registry entries of sensors whose data source is gone.

    With `keep_hv` (the entry has Modbus) the retired HV keys that Modbus
    serves again keep their entry, so their history continues.

    Without this, every retired sensor would stay in the UI as "unavailable"
    forever, because nothing creates its entity again.
    """
    for entity in er.async_entries_for_config_entry(ent_reg, entry_id):
        if (
            entity.domain == "sensor"
            and entity.platform == DOMAIN
            and is_retired_circuit_sensor(entity.unique_id)
            and not (keep_hv and _is_modbus_backed(entity.unique_id))
        ):
            ent_reg.async_remove(entity.entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval sensor entities."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    ent_reg = er.async_get(hass)
    _remove_retired_sensors(ent_reg, entry.entry_id, coordinator.has_modbus)

    def _add_new() -> None:
        entities: list[SensorEntity] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            # Circuit-level sensors
            for path, circuit in plant_data.circuits.items():
                for description in CIRCUIT_SENSOR_DESCRIPTIONS:
                    # A local entry has no cloud: only Modbus-backed keys can ever hold a value.
                    if coordinator.is_local and description.key not in MODBUS_BACKED_KEYS:
                        continue
                    uid = f"{plant_id}_{path}_{description.key}"
                    # Grandfather entities that already exist in the registry: when a
                    # circuit_types filter is newly added/tightened (e.g.
                    # outside_temperature → HV/HK only in #5), existing users keep the
                    # entity they already have; only fresh circuits get the filtered set.
                    is_registered = ent_reg.async_get_entity_id("sensor", DOMAIN, uid) is not None
                    if (
                        description.circuit_types is not None
                        and circuit.circuit_type not in description.circuit_types
                        and not is_registered
                    ):
                        continue
                    if (
                        description.exists_fn is not None
                        and not description.exists_fn(circuit)
                        and not is_registered
                    ):
                        continue
                    if uid in known:
                        continue
                    known.add(uid)
                    entities.append(
                        HovalCircuitSensor(coordinator, plant_id, path, circuit, description)
                    )

            # Plant-level sensors (events and weather come from the cloud only,
            # the gateway clock from Modbus)
            for description in PLANT_SENSOR_DESCRIPTIONS:
                if description.modbus_backed:
                    if not coordinator.has_modbus:
                        continue
                elif coordinator.is_local:
                    continue
                uid = f"{plant_id}_{description.key}"
                if (
                    description.modbus_backed
                    and description.value_fn(plant_data) is None
                    and ent_reg.async_get_entity_id("sensor", DOMAIN, uid) is None
                ):
                    continue
                if uid in known:
                    continue
                known.add(uid)
                entities.append(HovalPlantSensor(coordinator, plant_id, plant_data, description))

        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalCircuitSensor(CoordinatorEntity[HovalDataCoordinator], SensorEntity):
    """Hoval circuit sensor entity."""

    _attr_has_entity_name = True
    entity_description: HovalSensorEntityDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
        description: HovalSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_{description.key}"
        self._attr_device_info = circuit_device_info(coordinator, plant_id, circuit_data)

    @property
    def _circuit(self) -> HovalCircuitData | None:
        """Get current circuit data from coordinator."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.circuits.get(self._circuit_path)

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Modbus-backed keys follow the Modbus reading, everything else needs
        cloud data for this circuit (see `circuit_value_available`).
        """
        return super().available and circuit_value_available(
            self.coordinator.data, self._circuit, self.entity_description.key
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Attributes of the Modbus-only sensors."""
        circuit = self._circuit
        fn = self.entity_description.modbus_attributes_fn
        if fn is None or circuit is None or circuit.modbus is None:
            return None
        return fn(circuit.modbus)

    @property
    def native_value(self) -> datetime | float | str | None:
        """Return the sensor value."""
        circuit = self._circuit
        if circuit is None:
            return None
        modbus_fn = self.entity_description.modbus_value_fn
        if modbus_fn is not None:
            val = modbus_fn(circuit.modbus) if circuit.modbus is not None else None
        else:
            val = self.entity_description.value_fn(circuit)
        if val is None:
            return None
        # Must precede the unit-less string branch: HA requires a tz-aware
        # datetime from a TIMESTAMP sensor and raises on a str, which drops the
        # entity at setup instead of just showing a wrong value.
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP:
            return _coerce_timestamp(val)
        # Monotonic counters are numeric even when unit-less (operation
        # cycles): route them past the string branch so the negative guard
        # below always applies to them.
        is_counter = self.entity_description.state_class == SensorStateClass.TOTAL_INCREASING
        # String sensors (program names, operation mode) return as-is
        if self.entity_description.native_unit_of_measurement is None and not is_counter:
            # Modbus integers without a unit (error code, air quality control).
            if isinstance(val, int) and not isinstance(val, bool):
                return val
            return str(val)
        try:
            num = float(val)
        except (ValueError, TypeError):
            return None
        # Guard monotonic counters: a negative reading is never valid for a
        # TOTAL_INCREASING sensor and would be misread by HA's long-term
        # statistics as a meter reset, injecting a spurious spike. Drop it.
        if is_counter and num < 0:
            return None
        return num


class HovalPlantSensor(CoordinatorEntity[HovalDataCoordinator], SensorEntity):
    """Hoval plant-level sensor entity."""

    _attr_has_entity_name = True
    entity_description: HovalPlantSensorEntityDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_data: HovalPlantData,
        description: HovalPlantSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._plant_id = plant_id
        self._attr_unique_id = f"{plant_id}_{description.key}"
        self._attr_device_info = plant_device_info(plant_data)

    @property
    def _plant(self) -> HovalPlantData | None:
        """Get current plant data from coordinator."""
        return self.coordinator.data.plants.get(self._plant_id)

    @property
    def available(self) -> bool:
        """Return if entity is available (events and weather need the cloud)."""
        plant = self._plant
        if not super().available or plant is None:
            return False
        if self.entity_description.modbus_backed:
            return self.entity_description.value_fn(plant) is not None
        return self.coordinator.data.cloud_ok

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the extra attributes of a Modbus-backed sensor."""
        fn = self.entity_description.attributes_fn
        plant = self._plant
        return None if fn is None or plant is None else fn(plant)

    @property
    def native_value(self) -> datetime | float | str | None:
        """Return the sensor value."""
        plant = self._plant
        if plant is None:
            return None
        val = self.entity_description.value_fn(plant)
        if val is None:
            return None
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP:
            return _coerce_timestamp(val)
        if self.entity_description.native_unit_of_measurement is None and not isinstance(
            val, (int, float)
        ):
            return str(val)
        try:
            return float(val) if isinstance(val, (int, float)) else val
        except (ValueError, TypeError):
            return None
