"""Coordinator: Modbus data model, merge helper and local-only mode.

Runs without Home Assistant, reusing the stubbed ``runtime`` fixture of the
program-polling tests (it loads const, api, modbus, modbus_hv and coordinator).
"""

# ruff: noqa: F811  (pytest fixture imported by name, then used as a parameter)

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace

import pytest

from .test_program_polling import _PATH, _Api, _coordinator, _programs, runtime  # noqa: F401

LIVE_BLOCK = [1, 50, 65, 45, 54, 50, 255, 255, 0, 1, 93, 231, 53]
NO_ERROR = [0, 0, 0, 0, 65535, 255, 255, 255, 65535]


def _reading(runtime, errors=None):
    return runtime.modbus_hv.decode_hv(LIVE_BLOCK, None, errors)


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakePoller:
    def __init__(self, runtime, units=(520,), reading=None):
        self.runtime = runtime
        self.units = list(units)
        self.reading = reading if reading is not None else _reading(runtime)
        self.client = _FakeClient()
        self.discover_calls = 0
        self.discover_args = []
        self.poll_calls = []
        self.poll_error = None
        self.discover_errors = []
        self.writes = []

    async def discover(self, candidates=None):
        self.discover_calls += 1
        self.discover_args.append(None if candidates is None else list(candidates))
        if self.discover_errors:
            raise self.discover_errors.pop(0)
        # Units outside self.units answer exception 2 (absent).
        return [unit for unit in self.units if candidates is None or unit in candidates]

    async def poll(self, unit_ids):
        self.poll_calls.append(list(unit_ids))
        if self.poll_error is not None:
            raise self.poll_error
        return {unit: self.reading for unit in unit_ids}

    async def write(self, unit_id, name, value):
        self.writes.append((unit_id, name, value))


def _local(runtime, poller):
    return runtime.coordinator.HovalDataCoordinator(
        object(), None, modbus=poller, local_plant=("local_E1", "Hoval gw")
    )


def _update(coordinator):
    return asyncio.run(coordinator._async_update_data())


def test_cloud_only_path_unchanged(runtime):
    key = ("P1", _PATH)
    coordinator, api = _coordinator(runtime, {key: [_programs("Winter")]})
    assert not coordinator.has_modbus and not coordinator.is_local
    data = _update(coordinator)
    circuit = data.plants["P1"].circuits[_PATH]
    assert circuit.active_week_name == "Winter"
    assert circuit.modbus is None
    assert data.cloud_ok is True
    assert api.program_calls[key] == 1
    assert coordinator.cloud_interval == runtime.const.DEFAULT_SCAN_INTERVAL.total_seconds()


def test_apply_reading_overwrites_cloud_fields(runtime):
    c = runtime.coordinator
    circuit = c.HovalCircuitData(
        "HV",
        "520.50.0",
        "HomeVent",
        active_program="constant",
        live_values={"airVolume": "30", "totalEnergy": "5"},
    )
    reading = _reading(runtime)
    c.apply_modbus_reading(circuit, reading)
    assert circuit.modbus is reading
    assert circuit.live_values["airVolume"] == "45"
    assert circuit.live_values["totalEnergy"] == "5"
    assert circuit.live_values["humidityActual"] == "50"
    assert circuit.live_values["humidityTarget"] == "54"
    assert circuit.live_values["outsideTemperature"] == "9.3"
    assert circuit.live_values["exhaustTemp"] == "23.1"
    assert circuit.active_program == "week1"
    assert circuit.constant_value == 50.0
    assert circuit.eco_mode_value == 65.0
    assert circuit.target_value == 45.0
    assert circuit.is_air_quality_guided is False


def test_apply_reading_removes_unknown_values(runtime):
    c = runtime.coordinator
    circuit = c.HovalCircuitData("HV", "520.50.0", "HomeVent", live_values={"airVolume": "30"})
    block = list(LIVE_BLOCK)
    block[3] = 255  # air volume unknown
    block[8] = 1  # air-quality control on
    c.apply_modbus_reading(circuit, runtime.modbus_hv.decode_hv(block, None, None))
    assert "airVolume" not in circuit.live_values
    assert circuit.target_value is None
    assert circuit.is_air_quality_guided is True


def test_apply_none_blanks_modbus_fields(runtime):
    c = runtime.coordinator
    circuit = c.HovalCircuitData("HV", "520.50.0", "HomeVent", live_values={"totalEnergy": "5"})
    c.apply_modbus_reading(circuit, _reading(runtime))
    c.apply_modbus_reading(circuit, None)
    assert circuit.modbus is None
    assert circuit.live_values == {"totalEnergy": "5"}
    assert circuit.active_program is None
    assert circuit.constant_value is None and circuit.eco_mode_value is None
    assert circuit.target_value is None
    assert circuit.is_air_quality_guided is False


def test_local_poll_builds_plant(runtime):
    poller = _FakePoller(runtime)
    coordinator = _local(runtime, poller)
    assert coordinator.is_local and coordinator.has_modbus
    data = _update(coordinator)
    plant = data.plants["local_E1"]
    circuit = plant.circuits["520.50.0"]
    assert plant.name == "Hoval gw"
    assert circuit.circuit_type == "HV"
    assert circuit.name == runtime.const.CIRCUIT_TYPE_NAMES["HV"]
    assert plant.is_online is True and plant.has_error is False
    assert data.cloud_ok is False
    assert circuit.modbus is poller.reading


def test_local_multiple_units_get_name_suffix(runtime):
    coordinator = _local(runtime, _FakePoller(runtime, units=(520, 521)))
    circuits = _update(coordinator).plants["local_E1"].circuits
    base = runtime.const.CIRCUIT_TYPE_NAMES["HV"]
    assert circuits["520.50.0"].name == f"{base} 520"
    assert circuits["521.50.0"].name == f"{base} 521"


def test_local_unit_without_reading_is_left_out(runtime):
    poller = _FakePoller(runtime, units=(520, 521))

    async def poll(unit_ids):
        return {520: poller.reading, 521: None}

    poller.poll = poll
    circuits = _update(_local(runtime, poller)).plants["local_E1"].circuits
    assert list(circuits) == ["520.50.0"]


def test_local_active_error_sets_plant_error(runtime):
    active = [100, 1201, 0, 0, 520, 50, 0, 1, 41]
    poller = _FakePoller(runtime, reading=_reading(runtime, [active, NO_ERROR]))
    data = _update(_local(runtime, poller))
    assert data.plants["local_E1"].has_error is True


@pytest.mark.parametrize("error", [OSError("down"), TimeoutError("slow"), "modbus"])
def test_local_modbus_failure_raises_update_failed(runtime, error):
    poller = _FakePoller(runtime)
    poller.poll_error = runtime.modbus.HovalModbusError("busy") if error == "modbus" else error
    with pytest.raises(sys_update_failed(runtime)):
        _update(_local(runtime, poller))


def sys_update_failed(runtime):
    return sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed


def test_local_rediscovers_after_failed_discovery(runtime):
    poller = _FakePoller(runtime)
    poller.discover_errors = [runtime.modbus.HovalModbusError("busy")]
    coordinator = _local(runtime, poller)
    with pytest.raises(sys_update_failed(runtime)):
        _update(coordinator)
    data = _update(coordinator)
    assert "520.50.0" in data.plants["local_E1"].circuits
    assert poller.discover_calls == 2
    # Discovery result is cached afterwards.
    _update(coordinator)
    assert poller.discover_calls == 2


def test_local_empty_discovery_raises_and_retries(runtime):
    poller = _FakePoller(runtime, units=())
    coordinator = _local(runtime, poller)
    with pytest.raises(sys_update_failed(runtime)):
        _update(coordinator)
    poller.units = [520]
    assert "520.50.0" in _update(coordinator).plants["local_E1"].circuits


def test_hv_write_delegates_with_unit_from_path(runtime):
    poller = _FakePoller(runtime)
    coordinator = _local(runtime, poller)
    asyncio.run(coordinator.async_hv_write("local_E1", "520.50.0", "constant_modulation", 55))
    assert poller.writes == [(520, "constant_modulation", 55)]


def test_close_closes_modbus_client(runtime):
    poller = _FakePoller(runtime)
    asyncio.run(_local(runtime, poller).async_close())
    assert poller.client.closed
    # No Modbus: closing is a no-op.
    asyncio.run(_coordinator(runtime, {("P1", _PATH): [None]})[0].async_close())


def test_module_exports(runtime):
    assert "hv_active_error" in runtime.const.MODBUS_BACKED_KEYS
    assert len(runtime.const.MODBUS_BACKED_KEYS) == 18


# --- Hybrid mode (cloud account + Modbus gateway) ---------------------------

_HV = "520.50.0"
_HK = "1.1.0"
_TOPOLOGY = {"plant_id": "P1", "plant_name": "HRT", "circuits": {_HV: "Lüftung"}}
_MODBUS_LIVE = (
    "airVolume",
    "humidityActual",
    "humidityTarget",
    "outsideTemperature",
    "exhaustTemp",
)


class _HybridApi(_Api):
    """Cloud fake with a plant list that can fail or report plants offline."""

    def __init__(self, outcomes, circuit_types):
        super().__init__(outcomes, circuit_types)
        self.plants_calls = 0
        self.plants_error = None
        self.offline = set()

    async def get_plants(self):
        self.plants_calls += 1
        if self.plants_error is not None:
            raise self.plants_error
        plants = await super().get_plants()
        for plant in plants:
            plant["isOnline"] = plant["plantExternalId"] not in self.offline
        return plants

    def invalidate_plant_token(self, plant_id):
        pass


class _FakeEntry:
    def __init__(self, data):
        self.data = data
        self.reauth_calls = 0

    def async_start_reauth(self, hass):
        self.reauth_calls += 1


class _FakeConfigEntries:
    def __init__(self):
        self.updates = []

    def async_update_entry(self, entry, *, data):
        self.updates.append(data)
        entry.data = data
        return True


def _hybrid(runtime, *, plants=("P1",), topology=None, modbus_plant_id=None, poller=None):
    outcomes, types = {}, {}
    for plant in plants:
        outcomes[(plant, _HV)] = [_programs("Lüften")]
        types[(plant, _HV)] = "HV"
        outcomes[(plant, _HK)] = [_programs("Heizen")]
        types[(plant, _HK)] = "HK"
    api = _HybridApi(outcomes, types)
    poller = poller or _FakePoller(runtime)
    hass = SimpleNamespace(config_entries=_FakeConfigEntries())
    coordinator = runtime.coordinator.HovalDataCoordinator(
        hass, api, modbus=poller, modbus_plant_id=modbus_plant_id, topology=topology
    )
    data = {"email": "user@example.invalid"}
    if topology is not None:
        data[runtime.const.DATA_TOPOLOGY] = topology
    coordinator.config_entry = _FakeEntry(data)
    coordinator.cloud_interval = 900.0
    return coordinator, api, poller, hass


def _tick(coordinator):
    """One update as HA runs it: fetch, assign the data, then the finished hook."""
    data = asyncio.run(coordinator._async_update_data())
    coordinator.data = data
    coordinator._async_refresh_finished()
    return data


def _assert_modbus_values(circuit, poller):
    assert circuit.modbus is poller.reading
    assert circuit.live_values["airVolume"] == "45"
    assert circuit.live_values["exhaustTemp"] == "23.1"


def _records(caplog, level, needle):
    return [r for r in caplog.records if r.levelno == level and needle in r.getMessage().lower()]


def test_cloud_fetched_once_per_cloud_interval(runtime):
    coordinator, api, poller, _ = _hybrid(runtime)
    _tick(coordinator)
    runtime.clock.advance(30)
    _tick(coordinator)
    runtime.clock.advance(869)  # +899 s
    data = _tick(coordinator)
    assert api.plants_calls == 1
    assert len(poller.poll_calls) == 3
    _assert_modbus_values(data.plants["P1"].circuits[_HV], poller)
    # The cached cloud values are served between fetches.
    assert data.plants["P1"].circuits[_HV].active_week_name == "Lüften"
    assert data.cloud_ok is True
    runtime.clock.advance(1)  # +900 s
    _tick(coordinator)
    assert api.plants_calls == 2
    assert len(poller.poll_calls) == 4


def test_cloud_failure_retried_only_after_cloud_interval(runtime):
    coordinator, api, poller, _ = _hybrid(runtime, topology=_TOPOLOGY)
    api.plants_error = runtime.api.HovalApiError("blocked")
    data = _tick(coordinator)
    assert data.cloud_ok is False
    _assert_modbus_values(data.plants["P1"].circuits[_HV], poller)
    assert api.plants_calls == 1
    runtime.clock.advance(30)
    _tick(coordinator)
    runtime.clock.advance(30)
    _tick(coordinator)
    assert api.plants_calls == 1
    runtime.clock.advance(840)  # +900 s
    _tick(coordinator)
    assert api.plants_calls == 2


def test_cloud_failure_logged_once_per_streak(runtime, caplog):
    coordinator, api, _, _ = _hybrid(runtime, topology=_TOPOLOGY)
    api.plants_error = runtime.api.HovalApiError("blocked")
    with caplog.at_level(logging.DEBUG):
        _tick(coordinator)
        runtime.clock.advance(900)
        _tick(coordinator)
        assert len(_records(caplog, logging.WARNING, "hoval cloud")) == 1
        api.plants_error = None
        runtime.clock.advance(900)
        data = _tick(coordinator)
    assert data.cloud_ok is True
    assert len(_records(caplog, logging.INFO, "hoval cloud")) == 1


def test_auth_failure_starts_reauth_and_keeps_modbus(runtime):
    coordinator, api, poller, _ = _hybrid(runtime)
    _tick(coordinator)
    api.plants_error = runtime.api.HovalAuthError("rejected")
    runtime.clock.advance(900)
    data = _tick(coordinator)  # no exception
    assert coordinator.config_entry.reauth_calls == 1
    assert data.cloud_ok is False
    _assert_modbus_values(data.plants["P1"].circuits[_HV], poller)
    # The last cloud snapshot keeps the circuit structure.
    assert _HK in data.plants["P1"].circuits


def test_unexpected_cloud_error_counts_as_cloud_failure(runtime, caplog):
    coordinator, api, poller, _ = _hybrid(runtime)
    _tick(coordinator)
    api.plants_error = ValueError("unexpected response shape")
    with caplog.at_level(logging.DEBUG):
        runtime.clock.advance(900)
        data = _tick(coordinator)  # no exception
        assert data.cloud_ok is False
        _assert_modbus_values(data.plants["P1"].circuits[_HV], poller)
        # The frozen snapshot is never served as fresh cloud data.
        runtime.clock.advance(30)
        assert _tick(coordinator).cloud_ok is False
        runtime.clock.advance(870)
        _tick(coordinator)
        assert api.plants_calls == 3
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1 and errors[0].exc_info is not None
        api.plants_error = None
        runtime.clock.advance(900)
        assert _tick(coordinator).cloud_ok is True
    assert len(_records(caplog, logging.INFO, "hoval cloud")) == 1


def test_rejected_login_suspends_cloud_until_reload(runtime, monkeypatch):
    coordinator, api, poller, _ = _hybrid(runtime)
    _tick(coordinator)
    api.plants_error = runtime.api.HovalAuthError("rejected")
    runtime.clock.advance(900)
    _tick(coordinator)
    assert api.plants_calls == 2
    for _ in range(3):
        runtime.clock.advance(900)
        data = _tick(coordinator)
        assert data.cloud_ok is False
        _assert_modbus_values(data.plants["P1"].circuits[_HV], poller)
    # Not even a cloud control action retries the rejected login.

    async def no_sleep(delay, result=None):
        return result

    async def refresh():
        coordinator.data = await coordinator._async_update_data()

    async def action():
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    coordinator.async_request_refresh = refresh
    asyncio.run(
        coordinator.async_control_and_refresh(
            action, plant_id="P1", circuit_path=_HV, mode_override="manual"
        )
    )
    assert api.plants_calls == 2
    assert coordinator.config_entry.reauth_calls == 1


def test_overlapping_updates_fetch_the_cloud_once(runtime):
    coordinator, api, poller, _ = _hybrid(runtime, topology=_TOPOLOGY)
    discover = poller.discover
    gate = {}

    async def slow_discover(candidates=None):
        await gate["event"].wait()
        return await discover(candidates)

    poller.discover = slow_discover

    async def main():
        gate["event"] = asyncio.Event()
        first = asyncio.create_task(coordinator._async_update_data())
        await asyncio.sleep(0)  # first now waits in the discovery before its cloud fetch
        second = asyncio.create_task(coordinator._async_update_data())
        await asyncio.sleep(0)
        gate["event"].set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(main())
    assert api.plants_calls == 1
    assert first.cloud_ok is True
    _assert_modbus_values(second.plants["P1"].circuits[_HV], poller)


def test_auth_failure_at_startup_uses_topology_and_starts_reauth(runtime):
    coordinator, api, poller, _ = _hybrid(runtime, topology=_TOPOLOGY)
    api.plants_error = runtime.api.HovalAuthError("rejected")
    data = _tick(coordinator)
    assert coordinator.config_entry.reauth_calls == 1
    _assert_modbus_values(data.plants["P1"].circuits[_HV], poller)


def test_startup_with_cloud_down_uses_stored_topology(runtime):
    coordinator, api, poller, _ = _hybrid(runtime, topology=_TOPOLOGY)
    api.plants_error = runtime.api.HovalApiError("blocked")
    data = _tick(coordinator)
    plant = data.plants["P1"]
    assert plant.name == "HRT"
    assert plant.is_online is False
    assert data.cloud_ok is False
    circuit = plant.circuits[_HV]
    assert circuit.name == "Lüftung"
    assert circuit.circuit_type == "HV"
    _assert_modbus_values(circuit, poller)
    assert poller.discover_args == [[520]]


def test_startup_with_cloud_down_and_no_topology_fails(runtime):
    coordinator, api, _, _ = _hybrid(runtime)
    api.plants_error = runtime.api.HovalApiError("blocked")
    with pytest.raises(sys_update_failed(runtime)):
        asyncio.run(coordinator._async_update_data())


@pytest.mark.parametrize("cloud_before", ["topology", "cached"])
@pytest.mark.parametrize("modbus_failure", ["poll", "discover"])
def test_both_sources_down_raises_update_failed(runtime, cloud_before, modbus_failure):
    topology = _TOPOLOGY if cloud_before == "topology" else None
    coordinator, api, poller, _ = _hybrid(runtime, topology=topology)
    if cloud_before == "cached":
        _tick(coordinator)
        runtime.clock.advance(900)
    api.plants_error = runtime.api.HovalApiError("blocked")
    error = runtime.modbus.HovalModbusError("down")
    if modbus_failure == "poll":
        poller.poll_error = error
    else:
        poller.discover_errors = [error]
    with pytest.raises(sys_update_failed(runtime)):
        asyncio.run(coordinator._async_update_data())


def test_topology_persisted_only_when_changed(runtime):
    coordinator, _, _, hass = _hybrid(runtime)
    _tick(coordinator)
    expected = {"plant_id": "P1", "plant_name": "P1", "circuits": {_HV: "HV"}}
    assert hass.config_entries.updates == [
        {"email": "user@example.invalid", runtime.const.DATA_TOPOLOGY: expected}
    ]
    assert coordinator.topology == expected
    runtime.clock.advance(900)
    _tick(coordinator)
    assert len(hass.config_entries.updates) == 1


def test_topology_helpers_round_trip(runtime):
    c = runtime.coordinator
    data = c.data_from_topology(_TOPOLOGY)
    assert data.cloud_ok is False
    plant = data.plants["P1"]
    assert (plant.name, plant.is_online) == ("HRT", False)
    assert c.topology_from_data(data, "P1") == _TOPOLOGY
    assert c.topology_from_data(data, "P2") is None
    # Only HV circuits are stored.
    plant.circuits[_HK] = c.HovalCircuitData("HK", _HK, "Heizkreis")
    assert c.topology_from_data(data, "P1") == _TOPOLOGY


@pytest.mark.parametrize(
    ("path", "unit"),
    [("520.50.0", 520), ("513.50.0", 513), ("512.50.0", None), ("1.2.3", None), ("².50.0", None)],
)
def test_hv_unit_from_path(runtime, path, unit):
    assert runtime.coordinator._hv_unit(path) == unit


def test_malformed_stored_topology_is_ignored(runtime):
    coordinator, api, _, _ = _hybrid(runtime, topology={"plant_id": "P1", "circuits": [_HV]})
    assert coordinator.topology is None
    assert coordinator.bound_plant_id is None
    api.plants_error = runtime.api.HovalApiError("blocked")
    with pytest.raises(sys_update_failed(runtime)):
        asyncio.run(coordinator._async_update_data())


def test_modbus_served_hv_skips_live_values_and_details(runtime):
    # A stored topology lets discovery run before the very first cloud fetch.
    coordinator, api, poller, _ = _hybrid(runtime, topology=_TOPOLOGY)
    data = _tick(coordinator)
    assert api.live_calls[("P1", _HV)] == 0
    assert api.details_calls[("P1", _HV)] == 0
    assert api.program_calls[("P1", _HV)] == 1
    assert api.live_calls[("P1", _HK)] == 1
    assert data.plants["P1"].circuits[_HK].live_values["totalEnergy"] == "101"
    circuit = data.plants["P1"].circuits[_HV]
    _assert_modbus_values(circuit, poller)
    assert circuit.active_week_name == "Lüften"
    runtime.clock.advance(900)
    _tick(coordinator)
    assert api.live_calls[("P1", _HV)] == 0
    assert api.details_calls[("P1", _HV)] == 0


def test_first_start_without_topology_skips_from_second_cloud_fetch(runtime):
    coordinator, api, poller, _ = _hybrid(runtime)
    data = _tick(coordinator)
    # Which HV paths Modbus serves is only known after the first circuit list.
    assert api.live_calls[("P1", _HV)] == 1
    assert poller.discover_args == [[520]]
    _assert_modbus_values(data.plants["P1"].circuits[_HV], poller)
    runtime.clock.advance(900)
    _tick(coordinator)
    assert api.live_calls[("P1", _HV)] == 1
    assert api.details_calls[("P1", _HV)] == 1
    # Rediscovered once per cloud fetch, before it.
    assert poller.discover_args == [[520], [520]]


def test_unserved_hv_unit_keeps_cloud_live_values(runtime):
    poller = _FakePoller(runtime, units=())  # block of 520 answers exception 2
    coordinator, api, _, _ = _hybrid(runtime, poller=poller)
    data = _tick(coordinator)
    circuit = data.plants["P1"].circuits[_HV]
    assert circuit.modbus is None
    assert circuit.live_values["totalEnergy"] == "101"
    assert poller.poll_calls == []
    runtime.clock.advance(900)
    _tick(coordinator)
    assert api.live_calls[("P1", _HV)] == 2


def test_modbus_failure_in_hybrid_keeps_cloud_entities(runtime):
    coordinator, _, poller, _ = _hybrid(runtime)
    poller.poll_error = OSError("down")
    data = _tick(coordinator)
    assert data.cloud_ok is True
    circuit = data.plants["P1"].circuits[_HV]
    assert circuit.modbus is None
    for key in _MODBUS_LIVE:
        assert key not in circuit.live_values
    assert circuit.active_week_name == "Lüften"
    assert _HK in data.plants["P1"].circuits


def test_modbus_failure_logged_on_transition_only(runtime, caplog):
    coordinator, _, poller, _ = _hybrid(runtime)
    _tick(coordinator)
    poller.poll_error = runtime.modbus.HovalModbusError("busy")
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            runtime.clock.advance(30)
            _tick(coordinator)
        assert len(_records(caplog, logging.WARNING, "modbus")) == 1
        poller.poll_error = None
        runtime.clock.advance(30)
        data = _tick(coordinator)
    _assert_modbus_values(data.plants["P1"].circuits[_HV], poller)
    assert len(_records(caplog, logging.INFO, "modbus")) == 1


def test_multi_plant_without_binding_leaves_modbus_unused(runtime, caplog):
    coordinator, api, poller, _ = _hybrid(runtime, plants=("P1", "P2"))
    with caplog.at_level(logging.WARNING):
        data = _tick(coordinator)
        runtime.clock.advance(900)
        _tick(coordinator)
    assert coordinator.bound_plant_id is None
    for plant in ("P1", "P2"):
        assert data.plants[plant].circuits[_HV].modbus is None
        assert api.live_calls[(plant, _HV)] == 2
    assert poller.poll_calls == [] and poller.discover_calls == 0
    assert len(_records(caplog, logging.WARNING, "plant")) == 1


def test_binding_merges_into_bound_plant_only(runtime):
    coordinator, api, poller, hass = _hybrid(runtime, plants=("P1", "P2"), modbus_plant_id="P2")
    assert coordinator.bound_plant_id == "P2"
    data = _tick(coordinator)
    assert data.plants["P1"].circuits[_HV].modbus is None
    _assert_modbus_values(data.plants["P2"].circuits[_HV], poller)
    assert hass.config_entries.updates[-1][runtime.const.DATA_TOPOLOGY]["plant_id"] == "P2"
    runtime.clock.advance(900)
    _tick(coordinator)
    assert api.live_calls[("P1", _HV)] == 2
    assert api.live_calls[("P2", _HV)] == 1


def test_binding_before_first_fetch_comes_from_topology(runtime):
    coordinator, _, _, _ = _hybrid(runtime, plants=("P1", "P2"), topology=_TOPOLOGY)
    assert coordinator.bound_plant_id == "P1"
    # The account gained a second plant: the stored binding still holds.
    data = _tick(coordinator)
    assert coordinator.bound_plant_id == "P1"
    assert data.plants["P1"].circuits[_HV].modbus is not None
    assert data.plants["P2"].circuits[_HV].modbus is None


def test_cloud_reports_plant_offline_keeps_modbus_circuits(runtime):
    coordinator, api, poller, hass = _hybrid(runtime)
    _tick(coordinator)
    api.offline = {"P1"}
    for _ in range(2):  # the first offline poll keeps the previous data anyway
        runtime.clock.advance(900)
        data = _tick(coordinator)
    plant = data.plants["P1"]
    assert plant.is_online is False
    assert list(plant.circuits) == [_HV]
    _assert_modbus_values(plant.circuits[_HV], poller)
    # The offline plant's empty circuit list never replaces the stored topology.
    assert len(hass.config_entries.updates) == 1


def test_cloud_action_forces_cloud_refresh_modbus_action_does_not(runtime, monkeypatch):
    coordinator, api, poller, _ = _hybrid(runtime)
    _tick(coordinator)
    sleeps = []

    async def fake_sleep(delay, result=None):
        sleeps.append(delay)
        return result

    async def refresh():
        coordinator.data = await coordinator._async_update_data()

    async def action():
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    coordinator.async_request_refresh = refresh

    def control(cloud_action):
        asyncio.run(
            coordinator.async_control_and_refresh(
                action,
                plant_id="P1",
                circuit_path=_HV,
                mode_override="standby",
                cloud_action=cloud_action,
            )
        )

    runtime.clock.advance(30)
    control(False)
    assert api.plants_calls == 1
    assert len(poller.poll_calls) == 2
    assert sleeps == []  # Modbus actions refresh at once
    runtime.clock.advance(30)
    control(True)
    assert api.plants_calls == 2
    assert sleeps == [2]
    # The forced fetch restarts the cloud interval.
    runtime.clock.advance(30)
    _tick(coordinator)
    assert api.plants_calls == 2


def test_first_modbus_reading_after_start_signals_platforms(runtime, monkeypatch):
    sent = []
    monkeypatch.setattr(runtime.coordinator, "async_dispatcher_send", lambda *a: sent.append(a))
    coordinator, _, poller, _ = _hybrid(runtime)
    coordinator.last_update_success = True
    poller.discover_errors = [runtime.modbus.HovalModbusError("gateway down")]
    data = _tick(coordinator)
    assert data.plants["P1"].circuits[_HV].modbus is None
    assert len(sent) == 1  # the circuits themselves are new
    data = _tick(coordinator)  # discovery retried, the gateway answers now
    assert data.plants["P1"].circuits[_HV].modbus is not None
    assert len(sent) == 2  # first reading of a known circuit
    _tick(coordinator)
    assert len(sent) == 2  # not again for a known reading


def _program_reading(runtime, value):
    return runtime.modbus_hv.decode_hv([value, *LIVE_BLOCK[1:]], None, None)


def _resume_after_standby(runtime, coordinator, poller, plant_id, path, advance=None):
    """Week 2 reading, then standby: what the real resolver offers for fan-on."""
    step = _update if advance is None else _tick
    poller.reading = _program_reading(runtime, 2)
    coordinator.data = step(coordinator)
    poller.reading = _program_reading(runtime, 0)
    coordinator.data = step(coordinator)
    circuit = coordinator.data.plants[plant_id].circuits[path]
    assert circuit.active_program == "standby"
    return coordinator.resolve_resume_program(plant_id, path)


def test_local_resume_after_standby_returns_last_week_program(runtime):
    poller = _FakePoller(runtime)
    coordinator = _local(runtime, poller)
    resolved = _resume_after_standby(runtime, coordinator, poller, "local_E1", "520.50.0")
    assert resolved == "week2"
    assert runtime.modbus_hv.VALUE_BY_PROGRAM[resolved] == 2


def test_hybrid_resume_after_standby_uses_modbus_week_program(runtime):
    coordinator, _api, poller, _ = _hybrid(runtime)
    resolved = _resume_after_standby(runtime, coordinator, poller, "P1", _HV, advance=True)
    assert resolved == "week2"


def test_modbus_served_circuits_are_marked_in_hybrid(runtime):
    """I2: a served circuit stays marked while the gateway fails, so its keys go unavailable."""
    coordinator, _, poller, _ = _hybrid(runtime)
    data = _tick(coordinator)
    assert data.plants["P1"].circuits[_HV].modbus_served is True
    assert data.plants["P1"].circuits[_HK].modbus_served is False
    poller.poll_error = OSError("down")
    runtime.clock.advance(30)
    hv = _tick(coordinator).plants["P1"].circuits[_HV]
    assert hv.modbus is None
    assert hv.modbus_served is True
    # The marking lives on the per-update copy, never on the cached cloud snapshot.
    assert coordinator._cloud_data.plants["P1"].circuits[_HV].modbus_served is False


def test_unserved_and_local_circuits_marking(runtime):
    poller = _FakePoller(runtime, units=())  # block of 520 answers exception 2
    coordinator, _, _, _ = _hybrid(runtime, poller=poller)
    assert _tick(coordinator).plants["P1"].circuits[_HV].modbus_served is False
    local = _update(_local(runtime, _FakePoller(runtime)))
    assert local.plants["local_E1"].circuits["520.50.0"].modbus_served is True


def _auth_failed():
    return sys.modules["homeassistant.exceptions"].ConfigEntryAuthFailed


@pytest.mark.parametrize("topology", [_TOPOLOGY, None])
def test_rejected_login_without_modbus_data_at_setup_raises_auth_failed(runtime, topology):
    """I1: UpdateFailed at the first refresh is ConfigEntryNotReady, and HA would
    retry the setup forever, each retry a new coordinator and a new login."""
    coordinator, api, poller, _ = _hybrid(runtime, topology=topology)
    api.plants_error = runtime.api.HovalAuthError("rejected")
    poller.discover_errors = [runtime.modbus.HovalModbusError("down")]
    poller.poll_error = runtime.modbus.HovalModbusError("down")
    with pytest.raises(_auth_failed()):
        asyncio.run(coordinator._async_update_data())
    assert api.plants_calls == 1
    assert coordinator.config_entry.reauth_calls == 1


def test_rejected_login_at_setup_with_modbus_data_still_starts(runtime):
    coordinator, api, poller, _ = _hybrid(runtime, topology=_TOPOLOGY)
    api.plants_error = runtime.api.HovalAuthError("rejected")
    data = _tick(coordinator)
    _assert_modbus_values(data.plants["P1"].circuits[_HV], poller)


def test_gateway_failure_after_rejected_login_keeps_polling(runtime):
    """Once an update succeeded, a failing gateway raises UpdateFailed, never
    ConfigEntryAuthFailed: that would stop HA's scheduled updates for good, and
    the Modbus values could not come back when the gateway answers again."""
    coordinator, api, poller, _ = _hybrid(runtime)
    _tick(coordinator)
    api.plants_error = runtime.api.HovalAuthError("rejected")
    runtime.clock.advance(900)
    _tick(coordinator)
    poller.poll_error = runtime.modbus.HovalModbusError("down")
    runtime.clock.advance(30)
    with pytest.raises(sys_update_failed(runtime)) as err:
        asyncio.run(coordinator._async_update_data())
    assert not isinstance(err.value, _auth_failed())
    poller.poll_error = None
    runtime.clock.advance(30)
    _assert_modbus_values(_tick(coordinator).plants["P1"].circuits[_HV], poller)
    assert api.plants_calls == 2


# Same literal as _SEEDED in test_lifecycle_runtime.py, where setup seeds it
# from the registry entry of the fan "P_1_520.50.0_fan" and its device names.
_SEEDED = {"plant_id": "P_1", "plant_name": "HRT", "circuits": {_HV: "Lüftung"}}


def test_seeded_topology_starts_hybrid_with_cloud_down_and_is_stored_later(runtime):
    """R-SEED: the seed is used like a stored topology and stored on the next good poll."""
    coordinator, api, poller, hass = _hybrid(runtime, plants=("P_1",), topology=_SEEDED)
    del coordinator.config_entry.data[runtime.const.DATA_TOPOLOGY]  # seeded, never stored
    api.plants_error = runtime.api.HovalApiError("blocked")
    data = _tick(coordinator)
    assert data.cloud_ok is False
    plant = data.plants["P_1"]
    circuit = plant.circuits[_HV]
    _assert_modbus_values(circuit, poller)
    # The fan entity gets the unique id the registry already holds.
    assert f"{plant.plant_id}_{circuit.path}_fan" == "P_1_520.50.0_fan"
    assert hass.config_entries.updates == []
    api.plants_error = None
    runtime.clock.advance(900)
    _tick(coordinator)
    stored = hass.config_entries.updates[-1][runtime.const.DATA_TOPOLOGY]
    assert (stored["plant_id"], list(stored["circuits"])) == ("P_1", [_HV])
