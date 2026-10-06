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
        if "Hoval cloud" in record.getMessage() or "New circuits" in record.getMessage()
    ]


def _set_online(api, online):
    api.get_plants = AsyncMock(return_value=[{"plantExternalId": "P1", "isOnline": online}])


def _offline_poll(coordinator):
    data = asyncio.run(coordinator._async_update_data())
    coordinator.data = data
    coordinator._async_refresh_finished()
    return data.plants["P1"]


def test_single_offline_poll_keeps_previous_data_silently(runtime, caplog):
    """The gateway reconnects about every 48 minutes; one offline poll is not an outage."""
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [None] * 3})
    api.invalidate_plant_token = MagicMock()
    _refresh(coordinator, api)
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    live_calls = api.live_calls[key]

    _set_online(api, False)
    plant = _offline_poll(coordinator)
    assert plant.is_online is False
    assert list(plant.circuits) == [_PATH]
    assert api.live_calls[key] == live_calls

    _set_online(api, True)
    _refresh(coordinator, api)
    assert _online_records(caplog) == []


def test_persistent_offline_is_logged_once_each_way(runtime, caplog):
    """A real outage must not be silent: unavailable plus one warning, then one info."""
    caplog.set_level(logging.INFO)
    coordinator, api = _coordinator(runtime, {("P1", _PATH): [None] * 3})
    api.invalidate_plant_token = MagicMock()
    _refresh(coordinator, api)
    caplog.clear()

    _set_online(api, False)
    assert list(_offline_poll(coordinator).circuits) == [_PATH]
    for _ in range(3):
        assert _offline_poll(coordinator).circuits == {}
    records = _online_records(caplog)
    assert [level for level, _ in records] == [logging.WARNING]
    assert "P1" in records[0][1]

    _set_online(api, True)
    _refresh(coordinator, api)
    records = [record for record in _online_records(caplog) if "Hoval cloud" in record[1]]
    assert [level for level, _ in records] == [logging.WARNING, logging.INFO]


def test_plant_offline_at_startup_is_logged_on_second_poll(runtime, caplog):
    caplog.set_level(logging.INFO)
    coordinator, api = _coordinator(runtime, {("P1", _PATH): [None]})
    api.invalidate_plant_token = MagicMock()
    _set_online(api, False)
    assert _offline_poll(coordinator).circuits == {}
    assert _online_records(caplog) == []
    assert _offline_poll(coordinator).circuits == {}
    assert [level for level, _ in _online_records(caplog)] == [logging.WARNING]


def test_circuits_appearing_after_offline_start_reach_the_platforms(runtime, monkeypatch):
    """A plant offline at HA start must get its circuit entities once it is back.

    HA runs dispatcher @callback receivers synchronously, and every platform's
    _add_new() reads coordinator.data. Sent from inside _async_update_data, the
    signal arrived before HA had assigned the new data: _add_new() saw the empty
    offline snapshot, and _known_circuits kept the signal from ever being sent
    again. Seen live on 2026-10-06, every HV entity stayed unavailable until
    the next restart.
    """
    coordinator, api = _coordinator(runtime, {("P1", _PATH): [None] * 3})
    api.invalidate_plant_token = MagicMock()
    seen = []
    monkeypatch.setattr(
        runtime.coordinator,
        "async_dispatcher_send",
        lambda hass, signal: seen.append(list(coordinator.data.plants["P1"].circuits)),
    )

    _set_online(api, False)
    _offline_poll(coordinator)
    _set_online(api, True)
    _refresh(coordinator, api)
    _refresh(coordinator, api)
    assert seen == [[_PATH]]


def test_mode_override_survives_a_poll_served_from_previous_data(runtime):
    """A poll that fetched nothing must not discard a pending optimistic mode."""
    coordinator, api = _coordinator(runtime, {("P1", _PATH): [None] * 3})
    api.invalidate_plant_token = MagicMock()
    _refresh(coordinator, api)
    coordinator.set_mode_override("P1", _PATH, "standby")
    runtime.clock.advance(1)

    _set_online(api, False)
    _offline_poll(coordinator)
    assert coordinator.get_mode_override("P1", _PATH) == "standby"

    _set_online(api, True)
    runtime.clock.advance(1)
    _refresh(coordinator, api)
    assert coordinator.get_mode_override("P1", _PATH) is None


def _limits(low, high):
    return {"temporaryChangeLimits": {"min": low, "max": high, "step": 0.5}}


def test_temporary_change_limits_are_fetched_for_setpoint_circuits_only(runtime):
    ww, hk, hv = ("P1", "1.2.0"), ("P1", "1.1.0"), ("P1", "520.50.0")
    coordinator, api = _coordinator(
        runtime, {ww: [None], hk: [None], hv: [None]}, {ww: "WW", hk: "HK", hv: "HV"}
    )
    api.details = {ww: _limits(10, 51), hk: _limits(5, 30), hv: _limits(15, 100)}
    circuits = _refresh(coordinator, api).plants["P1"].circuits
    assert (circuits["1.2.0"].temporary_change_min, circuits["1.2.0"].temporary_change_max) == (
        10,
        51,
    )
    # HK also fetches datapoints: the details result must not be read as those.
    assert (circuits["1.1.0"].temporary_change_min, circuits["1.1.0"].temporary_change_max) == (
        5,
        30,
    )
    assert circuits["1.2.0"].datapoints == {}
    assert circuits["520.50.0"].temporary_change_max is None
    assert api.details_calls == {ww: 1, hk: 1}


def test_temporary_change_limits_follow_cache_and_survive_failures(runtime):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [None]}, {key: "WW"})
    ttl = runtime.const.PROGRAM_CACHE_TTL.total_seconds()
    api.details = {key: _limits(10, 51)}

    def limits():
        circuit = _refresh(coordinator, api).plants["P1"].circuits[_PATH]
        return circuit.temporary_change_min, circuit.temporary_change_max

    assert limits() == (10, 51)
    assert limits() == (10, 51)
    assert api.details_calls[key] == 1

    # A failed fetch keeps the last known limits instead of widening the range.
    runtime.clock.advance(ttl)
    api.details = {key: runtime.api.HovalApiError("boom", status=503)}
    assert limits() == (10, 51)
    assert api.details_calls[key] == 2

    runtime.clock.advance(ttl)
    api.details = {key: _limits(10, 49)}
    assert limits() == (10, 49)

    # A successful answer without usable limits clears them.
    runtime.clock.advance(ttl)
    api.details = {key: {}}
    assert limits() == (None, None)


@pytest.mark.parametrize(
    "details",
    [
        None,
        [],
        {},
        {"temporaryChangeLimits": None},
        {"temporaryChangeLimits": {"min": 10}},
        {"temporaryChangeLimits": {"min": "x", "max": 50}},
        {"temporaryChangeLimits": {"min": True, "max": 50}},
        {"temporaryChangeLimits": {"min": float("nan"), "max": 50}},
        {"temporaryChangeLimits": {"min": 60, "max": 50}},
    ],
)
def test_unusable_temporary_change_limits_are_ignored(runtime, details):
    assert runtime.coordinator._parse_temporary_change_limits(details) == (None, None)
