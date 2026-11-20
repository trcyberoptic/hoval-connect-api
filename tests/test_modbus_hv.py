"""Tests for the HV register map and decoding (no Home Assistant needed)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "hoval_connect"
_PACKAGE = "_hoval_modbus_hv_pkg"

# modbus_hv imports .modbus, so load both under one synthetic package (no HA import).
_package = ModuleType(_PACKAGE)
_package.__path__ = [str(_COMPONENT)]
sys.modules[_PACKAGE] = _package
_loaded = {}
for _name in ("modbus", "modbus_hv"):
    _spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE}.{_name}", _COMPONENT / f"{_name}.py"
    )
    _module = importlib.util.module_from_spec(_spec)
    # dataclasses resolves string annotations via sys.modules, so register before exec.
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)
    _loaded[_name] = _module
modbus = _loaded["modbus"]
modbus_hv = _loaded["modbus_hv"]


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


# --- HovalModbusPoller ---------------------------------------------------------

HovalModbusError = modbus.HovalModbusError


class FakeClient:
    """Records calls; `answers` maps (address, count) -> words / None / Exception."""

    def __init__(self, answers=None, default=None):
        self.answers = answers or {}
        self.default = default
        self.reads = []
        self.writes = []

    async def read_holding(self, address, count):
        self.reads.append((address, count))
        result = self.answers.get((address, count), self.default)
        if isinstance(result, Exception):
            raise result
        return result

    async def write_register(self, address, value):
        self.writes.append((address, value))


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def _unit_answers(unit):
    return {
        (modbus_hv.hv_block_base(unit), 13): LIVE_BLOCK,
        (modbus_hv.hv_maintenance_base(unit), 13): LIVE_MAINT,
        **{
            (modbus_hv.hv_error_base(unit) + 9 * i, 9): NO_ERROR
            for i in range(modbus_hv.HV_ERROR_ENTRIES)
        },
    }


async def test_discover_keeps_answering_units():
    client = FakeClient({(modbus_hv.hv_block_base(520), 13): LIVE_BLOCK})
    assert await modbus_hv.HovalModbusPoller(client).discover() == [520]
    assert len(client.reads) == 16


async def test_discover_does_not_drop_a_busy_unit():
    busy = HovalModbusError("busy", exception_code=modbus.EXC_BUSY)
    client = FakeClient({(modbus_hv.hv_block_base(520), 13): busy})
    with pytest.raises(HovalModbusError):
        await modbus_hv.HovalModbusPoller(client).discover()


async def test_poll_reads_slow_blocks_only_after_ttl(monkeypatch):
    clock = FakeTime()
    monkeypatch.setattr(modbus_hv, "time", clock)
    client = FakeClient(_unit_answers(520))
    poller = modbus_hv.HovalModbusPoller(client)

    first = (await poller.poll([520]))[520]
    assert len(client.reads) == 8  # block + maintenance + 5 error entries + clock
    assert first.maintenance_interval_weeks == 52 and first.active_errors == ()

    client.reads.clear()
    clock.now += 60
    second = (await poller.poll([520]))[520]
    assert client.reads == [(modbus_hv.hv_block_base(520), 13)]
    assert second.maintenance_interval_weeks == 52 and second.active_errors == ()

    client.reads.clear()
    clock.now += 240  # 300 s since the last slow read
    await poller.poll([520])
    assert len(client.reads) == 8


async def test_poll_maps_vanished_unit_to_none():
    assert await modbus_hv.HovalModbusPoller(FakeClient(default=None)).poll([520]) == {520: None}


async def test_poll_raises_on_transport_failure():
    client = FakeClient(default=HovalModbusError("down"))
    with pytest.raises(HovalModbusError):
        await modbus_hv.HovalModbusPoller(client).poll([520])


async def test_write_reads_back():
    client = FakeClient({(23624, 1): [66]})
    await modbus_hv.HovalModbusPoller(client).write(520, "eco_modulation", 66)
    assert client.writes == [(23624, 66)]
    assert client.reads == [(23624, 1)]


async def test_write_mismatch_raises():
    client = FakeClient({(23624, 1): [65]})
    with pytest.raises(HovalModbusError):
        await modbus_hv.HovalModbusPoller(client).write(520, "eco_modulation", 66)


async def test_acknowledge_skips_read_back():
    client = FakeClient()
    await modbus_hv.HovalModbusPoller(client).write(520, "acknowledge", 7)
    assert client.writes == [(28939, 7)]
    assert client.reads == []


@pytest.mark.parametrize(
    "name,value", [("eco_modulation", 10), ("eco_modulation", True), ("eco_modulation", 50.0)]
)
async def test_invalid_write_sends_nothing(name, value):
    client = FakeClient()
    with pytest.raises(ValueError):
        await modbus_hv.HovalModbusPoller(client).write(520, name, value)
    assert client.writes == [] and client.reads == []


# --- Gateway clock (datapoint 20 "Unixzeit", registers 19802/19803) -----------

from datetime import UTC, datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

_ZURICH = ZoneInfo("Europe/Zurich")
# Measured 2026-11-20: the gateway answered [27391, 39041] = 1795135617 while the
# PC's UTC epoch was 1795132008 (the gateway ran 9 s ahead, zone offset +3600).
_CLOCK_WORDS = [27391, 39041]
_CLOCK_NOW = datetime.fromtimestamp(1795132008, UTC)


def test_gateway_clock_offset_and_time():
    seconds = (27391 << 16) | 39041
    assert seconds == 1795135617
    offset, gateway_time = modbus_hv.decode_gateway_clock(seconds, _CLOCK_NOW, _ZURICH)
    assert offset == 9
    assert gateway_time.isoformat().endswith("+01:00")
    assert gateway_time.isoformat() == "2026-11-20T00:46:57+01:00"


@pytest.mark.parametrize("naive", [(2026, 10, 25, 2, 30), (2026, 3, 29, 2, 30)])
def test_gateway_clock_dst_edges_never_raise(naive):
    # Autumn: 02:30 happens twice; spring: 02:30 does not exist. Either reading is fine.
    seconds = int((datetime(*naive) - datetime(1970, 1, 1)).total_seconds())
    offset, gateway_time = modbus_hv.decode_gateway_clock(
        seconds, datetime(*naive, tzinfo=_ZURICH).astimezone(UTC), _ZURICH
    )
    assert abs(offset) <= 3600 and gateway_time.tzinfo is not None


def test_gateway_clock_sentinel_is_unknown():
    assert modbus_hv.decode_gateway_clock(None, _CLOCK_NOW, _ZURICH) is None


def _clock_answers(unit=520, clock=_CLOCK_WORDS):
    return {**_unit_answers(unit), (19802, 2): clock}


async def test_poll_reads_clock_with_the_slow_blocks(monkeypatch):
    clock = FakeTime()
    monkeypatch.setattr(modbus_hv, "time", clock)
    client = FakeClient(_clock_answers())
    poller = modbus_hv.HovalModbusPoller(client)
    assert poller.gateway_clock is None

    await poller.poll([520])
    assert client.reads.count((19802, 2)) == 1
    assert poller.gateway_clock.seconds == 1795135617
    assert poller.gateway_clock.read_at.tzinfo is not None

    client.reads.clear()
    clock.now += 60
    await poller.poll([520])
    assert (19802, 2) not in client.reads  # not on every tick
    assert poller.gateway_clock.seconds == 1795135617

    client.reads.clear()
    clock.now += 240
    await poller.poll([520])
    assert client.reads.count((19802, 2)) == 1


async def test_clock_read_once_per_gateway_not_per_unit():
    client = FakeClient({**_clock_answers(520), **_unit_answers(521)})
    await modbus_hv.HovalModbusPoller(client).poll([520, 521])
    assert client.reads.count((19802, 2)) == 1


async def test_clock_exception_2_means_unknown_without_error():
    client = FakeClient(_unit_answers(520))  # 19802 unanswered -> None (exception 2)
    poller = modbus_hv.HovalModbusPoller(client)
    result = await poller.poll([520])
    assert result[520] is not None and poller.gateway_clock is None


async def test_clock_busy_does_not_fail_the_poll_and_is_retried():
    answers = _clock_answers()
    answers[(19802, 2)] = HovalModbusError("busy", exception_code=modbus.EXC_BUSY)
    client = FakeClient(answers)
    poller = modbus_hv.HovalModbusPoller(client)
    assert (await poller.poll([520]))[520] is not None
    assert poller.gateway_clock is None
    client.answers[(19802, 2)] = _CLOCK_WORDS
    await poller.poll([520])
    assert poller.gateway_clock.seconds == 1795135617


async def test_invalidate_slow_forces_one_maintenance_re_read():
    client = FakeClient(_clock_answers())
    poller = modbus_hv.HovalModbusPoller(client)
    await poller.poll([520])
    client.reads.clear()
    poller.invalidate_slow(520)
    await poller.poll([520])
    assert client.reads.count((modbus_hv.hv_maintenance_base(520), 13)) == 1
    client.reads.clear()
    await poller.poll([520])
    assert (modbus_hv.hv_maintenance_base(520), 13) not in client.reads
