"""Tests for the HV register map and decoding (no Home Assistant needed)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "hoval_connect" / "modbus_hv.py"
_SPEC = importlib.util.spec_from_file_location("_hoval_modbus_hv_under_test", _PATH)
modbus_hv = importlib.util.module_from_spec(_SPEC)
# dataclasses resolves string annotations via sys.modules, so register before exec.
sys.modules[_SPEC.name] = modbus_hv
_SPEC.loader.exec_module(modbus_hv)


@pytest.mark.parametrize(
    "unit,block,errors,maint",
    [(513, 23531, 23835, 28843), (520, 23622, 24150, 28934), (528, 23726, 24510, 29038)],
)
def test_bases_match_datapoint_list(unit, block, errors, maint):
    assert modbus_hv.hv_block_base(unit) == block
    assert modbus_hv.hv_error_base(unit) == errors
    assert modbus_hv.hv_maintenance_base(unit) == maint


@pytest.mark.parametrize("unit", [512, 529])
def test_bases_reject_non_hv_units(unit):
    for fn in (modbus_hv.hv_block_base, modbus_hv.hv_error_base, modbus_hv.hv_maintenance_base):
        with pytest.raises(ValueError):
            fn(unit)


LIVE_BLOCK = [1, 50, 65, 45, 54, 50, 255, 255, 0, 1, 93, 231, 53]
LIVE_MAINT = [1, 0, 52, 0, 2, 0, 255, 0, 0, 0, 1, 0, 14]
NO_ERROR = [0, 0, 0, 0, 65535, 255, 255, 255, 65535]


def test_decode_live_values():
    r = modbus_hv.decode_hv(LIVE_BLOCK, LIVE_MAINT, [NO_ERROR] * 5)
    assert (r.program, r.constant_modulation, r.eco_modulation, r.air_volume) == (
        "week1",
        50,
        65,
        45,
    )
    assert (r.humidity_target, r.humidity_actual) == (54, 50)
    assert r.voc_extract is None and r.voc_outside is None  # 255 = not fitted
    assert r.air_quality_control == 0 and r.control_state == "normal"
    assert r.outside_temperature == 9.3 and r.exhaust_temperature == 23.1
    assert r.exhaust_fan_target == 53
    assert (r.maintenance_enabled, r.maintenance_interval_weeks, r.maintenance_weeks_remaining) == (
        True,
        52,
        2,
    )
    assert r.co2_extract is None
    assert (r.filter_cleaning_enabled, r.filter_cleaning_interval_weeks) == (True, 14)
    assert r.active_errors == ()


def test_negative_temperature_and_sentinels():
    block = list(LIVE_BLOCK)
    block[10] = 0xFFF5
    block[11] = 0x8000
    r = modbus_hv.decode_hv(block, None, None)
    assert r.outside_temperature == -1.1 and r.exhaust_temperature is None
    assert r.maintenance_enabled is None and r.active_errors is None


def test_unknown_program_and_state_values_decode_to_none():
    block = list(LIVE_BLOCK)
    block[0] = 3
    block[9] = 42
    r = modbus_hv.decode_hv(block, None, None)
    assert r.program is None and r.control_state is None


def test_active_error_codes_collected():
    active = [100, 1201, 0, 0, 520, 50, 0, 1, 41]  # source 520, code 41
    r = modbus_hv.decode_hv(LIVE_BLOCK, None, [active, NO_ERROR, None, NO_ERROR, NO_ERROR])
    assert r.active_errors == (41,)


def test_program_maps_are_inverse():
    for value, name in modbus_hv.PROGRAM_BY_VALUE.items():
        assert modbus_hv.VALUE_BY_PROGRAM[name] == value


@pytest.mark.parametrize(
    "name,value,address",
    [
        ("program", 0, 23622),
        ("program", 5, 23622),
        ("constant_modulation", 15, 23623),
        ("eco_modulation", 100, 23624),
        ("humidity_target", 30, 23626),
        ("acknowledge", 7, 28939),
        ("acknowledge", 8, 28939),
    ],
)
def test_write_address_for_allowed_values(name, value, address):
    assert modbus_hv.hv_write_address(520, name, value) == address


@pytest.mark.parametrize(
    "name,value",
    [
        ("program", 3),
        ("program", 6),
        ("constant_modulation", 14),
        ("constant_modulation", 101),
        ("eco_modulation", 10),
        ("humidity_target", 29),
        ("humidity_target", 66),
        ("acknowledge", 0),
        ("air_volume", 50),
        ("control_state", 1),
    ],
)
def test_write_rejects_disallowed(name, value):
    with pytest.raises(ValueError):
        modbus_hv.hv_write_address(520, name, value)
