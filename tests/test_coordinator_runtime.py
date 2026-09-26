"""Runtime regressions for topology isolation and serialized controls."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from . import test_program_polling as polling

_PATH = polling._PATH
_coordinator = polling._coordinator
_refresh = polling._refresh
runtime = polling.runtime


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"isSelectable": True}, True),
        ({"selectable": True}, True),
        ({"isSelectable": False, "selectable": True}, False),
        ({"isSelectable": True, "selectable": False}, True),
        ({"isSelectable": "false", "selectable": True}, False),
        ({"isSelectable": None, "selectable": True}, False),
        ({"selectable": "false"}, False),
    ],
)
@pytest.mark.parametrize("circuit_type", ["HK", "HV"])
def test_v3_selectability_and_strict_legacy_fallback(runtime, fields, expected, circuit_type):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [None]})
    api.get_circuits = AsyncMock(return_value=[{"type": circuit_type, "path": _PATH, **fields}])
    data = _refresh(coordinator, api)
    assert (_PATH in data.plants["P1"].circuits) is expected
    assert api.live_calls[key] == int(expected)


@pytest.mark.parametrize("circuit_type", ["BL", "WW", "PS"])
def test_nonselectable_measurement_circuits_remain_supported(runtime, circuit_type):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [None]})
    api.get_circuits = AsyncMock(
        return_value=[{"type": circuit_type, "path": _PATH, "isSelectable": False}]
    )
    assert _refresh(coordinator, api).plants["P1"].circuits[_PATH].circuit_type == circuit_type


def test_bad_and_duplicate_circuit_rows_do_not_disrupt_good_siblings(runtime):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [None]})
    good = {"type": "BL", "path": _PATH}
    api.get_circuits = AsyncMock(
        return_value=[
            None,
            [],
            {"type": []},
            {"type": "BL"},
            {"type": "BL", "path": []},
            {"type": "BL", "path": "../../other"},
            good,
            dict(good),
        ]
    )
    data = _refresh(coordinator, api)
    assert list(data.plants["P1"].circuits) == [_PATH]
    assert api.live_calls[key] == 1
    assert api.program_calls[key] == 1


def test_duplicate_plants_do_not_double_requests(runtime):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [None]})
    api.get_plants = AsyncMock(return_value=[{"plantExternalId": "P1"}] * 2)
    assert list(_refresh(coordinator, api).plants) == ["P1"]
    assert api.live_calls[key] == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"activeProgram": {}},
        {"airQuality": [1]},
        {"temporaryChange": [1]},
        {"operationMode": []},
        {"circuitStatus": {}},
        {"name": []},
        {"targetValue": "nan"},
        {"targetValue": float("inf")},
        {"temporaryChange": {"value": "nan", "end": [], "type": {}}},
    ],
)
def test_malformed_optional_dto_fields_preserve_live_values(runtime, fields):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [None]})
    api.get_circuits = AsyncMock(return_value=[{"type": "BL", "path": _PATH, **fields}])
    circuit = _refresh(coordinator, api).plants["P1"].circuits[_PATH]
    assert circuit.target_value is None
    assert circuit.temporary_change_value is None
    assert circuit.name == "BL"


def test_boolean_strings_are_not_true(runtime):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [None]})
    api.get_circuits = AsyncMock(
        return_value=[
            {
                "type": "BL",
                "path": _PATH,
                "hasError": "false",
                "airQuality": {"isAirQualityGuided": "false"},
            }
        ]
    )
    circuit = _refresh(coordinator, api).plants["P1"].circuits[_PATH]
    assert not circuit.has_error
    assert not circuit.is_air_quality_guided
    api.invalidate_plant_token = MagicMock()
    api.get_plants = AsyncMock(return_value=[{"plantExternalId": "P1", "isOnline": "false"}])
    data = asyncio.run(coordinator._async_update_data())
    assert not data.plants["P1"].is_online
    assert api.live_calls[key] == 1


@pytest.mark.parametrize("defect", ["config-id", "week-id", "infinite-time", "infinite-value"])
def test_malformed_program_fields_keep_circuit_and_live_readings(runtime, defect):
    programs = polling._programs("Valid week")
    day_config = programs["dayPrograms"]["dayConfigurations"][0]
    if defect == "config-id":
        programs["dayPrograms"]["dayConfigurations"].insert(0, {"id": [], "name": "Broken"})
    elif defect == "week-id":
        programs["week1"]["dayProgramIds"] = [{}] * 7
    elif defect == "infinite-time":
        day_config["phases"].insert(
            0,
            {
                "start": {"hours": float("inf"), "minutes": 0},
                "end": {"hours": 24, "minutes": 0},
                "value": 99,
            },
        )
    else:
        day_config["phases"][0]["value"] = float("inf")
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [programs]})
    circuit = _refresh(coordinator, api).plants["P1"].circuits[_PATH]
    assert circuit.active_week_name == "Valid week"
    assert circuit.program_air_volume == (None if defect in {"week-id", "infinite-value"} else 21.5)


def test_resume_remembers_week2_per_plant_across_standby(runtime):
    first, second = ("P1", _PATH), ("P2", _PATH)
    coordinator, api = _coordinator(runtime, {first: [None], second: [None]})
    assert coordinator.resolve_resume_program(*first) == "week1"
    api.get_circuits = AsyncMock(
        return_value=[{"type": "BL", "path": _PATH, "activeProgram": "week2"}]
    )
    _refresh(coordinator, api)
    assert coordinator.resolve_resume_program(*first) == "week2"
    api.get_circuits = AsyncMock(
        side_effect=lambda plant: [
            {"type": "BL", "path": _PATH, "activeProgram": "standby" if plant == "P1" else "week1"}
        ]
    )
    _refresh(coordinator, api)
    assert coordinator.resolve_resume_program(*first) == "week2"
    assert coordinator.resolve_resume_program(*second) == "week1"


def test_mode_override_is_scoped_and_expires(runtime):
    key = ("P1", _PATH)
    coordinator, _ = _coordinator(runtime, {key: [None]})
    coordinator.set_mode_override(*key, "standby")
    assert coordinator.get_mode_override(*key) == "standby"
    assert coordinator.get_mode_override("P2", _PATH) is None
    runtime.clock.advance(runtime.coordinator._MODE_OVERRIDE_TTL_S + 1)
    assert coordinator.get_mode_override(*key) is None


@pytest.mark.asyncio
async def test_circuit_fetch_fanout_is_bounded_without_losing_results(runtime):
    outcomes = {("P1", f"1.2.{index}"): [None] for index in range(19)}
    coordinator, api = _coordinator(runtime, outcomes)
    release = asyncio.Event()
    active = peak = 0
    original = api.get_live_values

    async def blocked_live(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        result = await original(*args)
        active -= 1
        return result

    api.get_live_values = blocked_live
    task = asyncio.create_task(coordinator._async_update_data())
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert peak == 8
    finally:
        release.set()
    data = await task
    assert len(data.plants["P1"].circuits) == 19
    assert peak == 8
    assert sum(api.live_calls.values()) == 19


@pytest.mark.asyncio
async def test_control_locks_serialize_only_the_same_plant_and_circuit(runtime, monkeypatch):
    coordinator, _ = _coordinator(runtime, {("P1", _PATH): [None]})
    monkeypatch.setattr(
        runtime.coordinator, "asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=AsyncMock())
    )
    coordinator.async_request_refresh = AsyncMock()
    release = asyncio.Event()
    started = []

    async def action(label, wait=False):
        started.append(label)
        if wait:
            await release.wait()

    async def control(plant, label, wait=False):
        await coordinator.async_control_and_refresh(
            lambda: action(label, wait), plant_id=plant, circuit_path=_PATH, mode_override="manual"
        )

    first = asyncio.create_task(control("P1", "first", True))
    await asyncio.sleep(0)
    second = asyncio.create_task(control("P1", "second"))
    independent = asyncio.create_task(control("P2", "other plant"))
    await asyncio.sleep(0)
    assert started == ["first", "other plant"]
    release.set()
    await asyncio.gather(first, second, independent)
    assert started == ["first", "other plant", "second"]
    assert coordinator.async_request_refresh.await_count == 3


@pytest.mark.asyncio
async def test_cancelled_lock_waiter_never_constructs_action_coroutine(runtime):
    coordinator, _ = _coordinator(runtime, {("P1", _PATH): [None]})
    lock = coordinator._get_circuit_lock("P1", _PATH)
    await lock.acquire()
    factory = MagicMock(side_effect=AsyncMock())
    task = asyncio.create_task(
        coordinator.async_control_and_refresh(
            factory, plant_id="P1", circuit_path=_PATH, mode_override="manual"
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lock.release()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_control_waits_for_refresh_before_returning(runtime, monkeypatch):
    coordinator, _ = _coordinator(runtime, {("P1", _PATH): [None]})
    monkeypatch.setattr(
        runtime.coordinator, "asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=AsyncMock())
    )
    release = asyncio.Event()
    coordinator.async_request_refresh = AsyncMock(side_effect=release.wait)
    task = asyncio.create_task(
        coordinator.async_control_and_refresh(
            AsyncMock(), plant_id="P1", circuit_path=_PATH, mode_override="manual"
        )
    )
    await asyncio.sleep(0)
    assert not task.done()
    assert not coordinator._get_circuit_lock("P1", _PATH).locked()
    release.set()
    await task


def _online_records(caplog):
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if "reports plant" in record.getMessage()
    ]


def _set_online(api, online):
    api.get_plants = AsyncMock(return_value=[{"plantExternalId": "P1", "isOnline": online}])


def test_offline_transitions_are_logged_once_each(runtime, caplog):
    """An offline plant turns every entity unavailable; that must not be silent."""
    caplog.set_level(logging.INFO)
    coordinator, api = _coordinator(runtime, {("P1", _PATH): [None] * 3})
    api.invalidate_plant_token = MagicMock()

    _refresh(coordinator, api)
    assert _online_records(caplog) == []

    _set_online(api, False)
    for _ in range(2):
        data = asyncio.run(coordinator._async_update_data())
        assert data.plants["P1"].circuits == {}
    records = _online_records(caplog)
    assert len(records) == 1
    assert records[0][0] == logging.WARNING
    assert "P1" in records[0][1]

    _set_online(api, True)
    _refresh(coordinator, api)
    records = _online_records(caplog)
    assert len(records) == 2
    assert records[1][0] == logging.INFO
    assert "P1" in records[1][1]


def test_plant_offline_at_startup_is_logged(runtime, caplog):
    caplog.set_level(logging.INFO)
    coordinator, api = _coordinator(runtime, {("P1", _PATH): [None]})
    api.invalidate_plant_token = MagicMock()
    _set_online(api, False)
    asyncio.run(coordinator._async_update_data())
    assert [level for level, _ in _online_records(caplog)] == [logging.WARNING]
