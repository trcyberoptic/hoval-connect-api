"""Tests for config-flow schema logic (no Home Assistant required)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# test_api.py/test_coordinator.py stub voluptuous with a MagicMock via
# sys.modules. This file needs the REAL voluptuous (it is installed as a test
# dep), so purge a stale mock before importing — order-independent either way.
if isinstance(sys.modules.get("voluptuous"), MagicMock):
    del sys.modules["voluptuous"]
import voluptuous as vol  # noqa: E402

# Mock homeassistant modules so we can import without HA installed
ha_mock = MagicMock()
sys.modules.setdefault("homeassistant", ha_mock)
sys.modules.setdefault("homeassistant.config_entries", ha_mock)
sys.modules.setdefault("homeassistant.const", ha_mock)
sys.modules.setdefault("homeassistant.core", ha_mock)
sys.modules.setdefault("homeassistant.exceptions", ha_mock)
sys.modules.setdefault("homeassistant.helpers", ha_mock)
sys.modules.setdefault("homeassistant.helpers.update_coordinator", ha_mock)
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", ha_mock)
sys.modules.setdefault("homeassistant.helpers.device_registry", ha_mock)
sys.modules.setdefault("homeassistant.helpers.dispatcher", ha_mock)
sys.modules.setdefault("homeassistant.util", ha_mock)
sys.modules.setdefault("homeassistant.util.dt", ha_mock)

from custom_components.hoval_connect.api import HovalApiError  # noqa: E402
from custom_components.hoval_connect.config_flow import _api_error_key  # noqa: E402
from custom_components.hoval_connect.const import SCAN_INTERVAL_OPTIONS  # noqa: E402


def _scan_interval_schema() -> vol.Schema:
    """Mirror of the options-flow scan_interval validator (config_flow.py)."""
    return vol.Schema(
        {vol.Required("scan_interval"): vol.All(vol.Coerce(int), vol.In(SCAN_INTERVAL_OPTIONS))}
    )


class TestApiErrorKey:
    """403 is an authorisation answer, not a connection failure.

    Issue #11: the login worked, `/api/my-plants` answered 403, and the dialog
    said "unable to connect, please try again later" — which sent the reporter
    looking at their network while the cause sat in their Hoval account. A 403
    never fixes itself on retry, so it must not share a message with one that
    might.
    """

    def test_403_gets_its_own_error(self):
        assert _api_error_key(HovalApiError("nope", status=403)) == "no_plant_access"

    def test_gateway_block_wins_over_the_account_message(self):
        """Both arrive as 403. Only one of them is about the user's account, and
        telling a gateway-blocked user to check their plant assignment is what
        v1.0.7 did to the issue #11 reporters."""
        err = HovalApiError("nope", status=403, gateway_blocked=True)
        assert _api_error_key(err) == "gateway_blocked"

    def test_gateway_block_on_a_non_403_still_wins(self):
        """The gateway is free to answer with any status; the body is the tell."""
        err = HovalApiError("nope", status=406, gateway_blocked=True)
        assert _api_error_key(err) == "gateway_blocked"

    def test_other_statuses_stay_cannot_connect(self):
        for status in (400, 404, 429, 500, 502, 599):
            err = HovalApiError("nope", status=status)
            assert _api_error_key(err) == "cannot_connect", status

    def test_missing_status_stays_cannot_connect(self):
        """Transport failures carry no status and really are connection problems."""
        assert _api_error_key(HovalApiError("boom")) == "cannot_connect"


class TestScanIntervalSchema:
    """The HA frontend submits dict-select keys as STRINGS ("900"), not ints."""

    def test_string_from_frontend_is_coerced_and_accepted(self):
        result = _scan_interval_schema()({"scan_interval": "900"})
        assert result["scan_interval"] == 900
        assert isinstance(result["scan_interval"], int)

    def test_int_value_accepted(self):
        assert _scan_interval_schema()({"scan_interval": 1800})["scan_interval"] == 1800

    def test_unknown_value_rejected(self):
        with pytest_raises_invalid():
            _scan_interval_schema()({"scan_interval": "45"})


def pytest_raises_invalid():
    import pytest

    return pytest.raises(vol.Invalid)


class TestOptionsFlowSourceContract:
    """config_flow.py must coerce the frontend string before vol.In."""

    def test_scan_interval_uses_coerce(self):
        import pathlib

        src = pathlib.Path("custom_components/hoval_connect/config_flow.py").read_text(
            encoding="utf-8"
        )
        assert "vol.All(vol.Coerce(int), vol.In(SCAN_INTERVAL_OPTIONS))" in src


# ---------------------------------------------------------------------------
# Flow behaviour (menu, local setup, reconfigure, options)
#
# The module-level stubs above are MagicMocks, so subclassing ConfigFlow there
# yields a MagicMock, not a class. The flow tests therefore load the component
# under a private package name with small functional Home Assistant stand-ins.
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402
from types import ModuleType, SimpleNamespace  # noqa: E402

import pytest  # noqa: E402


class _Abort(Exception):
    def __init__(self, reason):
        self.reason = reason


class _FakeConfigFlow:
    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)

    def __init__(self):
        self.hass = SimpleNamespace()
        self.unique_id = None
        self.entries = []
        self.reconfigure_entry = None

    def async_show_form(self, *, step_id, data_schema=None, errors=None, **kw):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors}

    def async_show_menu(self, *, step_id, menu_options, **kw):
        return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

    def async_create_entry(self, *, title, data, **kw):
        return {"type": "create_entry", "title": title, "data": data, "unique_id": self.unique_id}

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        if any(e.unique_id == self.unique_id for e in self.entries):
            raise _Abort("already_configured")

    def _async_current_entries(self):
        return self.entries

    def async_abort(self, *, reason, **kw):
        return {"type": "abort", "reason": reason}

    def _get_reconfigure_entry(self):
        return self.reconfigure_entry

    def async_update_reload_and_abort(self, entry, **kw):
        return {"type": "abort", "reason": "reconfigure_successful", "entry": entry, **kw}


class _FakeOptionsFlow:
    def async_show_form(self, *, step_id, data_schema=None, errors=None, **kw):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors}

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}


@pytest.fixture
def flow_mod(monkeypatch):
    component = Path(__file__).resolve().parents[1] / "custom_components" / "hoval_connect"
    # Other test modules stub voluptuous with a MagicMock at collection time.
    if isinstance(sys.modules.get("voluptuous"), MagicMock):
        monkeypatch.delitem(sys.modules, "voluptuous")
    fake_ce = ModuleType("homeassistant.config_entries")
    fake_ce.ConfigFlow = _FakeConfigFlow
    fake_ce.ConfigFlowResult = dict
    fake_ce.OptionsFlowWithReload = _FakeOptionsFlow
    fake_session = ModuleType("homeassistant.helpers.aiohttp_client")
    fake_session.async_get_clientsession = lambda hass: object()
    fake_dr = ModuleType("homeassistant.helpers.device_registry")
    fake_dr.DeviceInfo = dict
    for name, mod in (
        ("homeassistant", ModuleType("homeassistant")),
        ("homeassistant.config_entries", fake_ce),
        ("homeassistant.helpers", ModuleType("homeassistant.helpers")),
        ("homeassistant.helpers.aiohttp_client", fake_session),
        ("homeassistant.helpers.device_registry", fake_dr),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    pkg_name = "_flow_component"
    pkg = ModuleType(pkg_name)
    pkg.__path__ = [str(component)]
    monkeypatch.setitem(sys.modules, pkg_name, pkg)
    loaded = {}
    for module_name in ("const", "options", "api", "compat", "modbus", "modbus_hv", "config_flow"):
        name = f"{pkg_name}.{module_name}"
        spec = importlib.util.spec_from_file_location(name, component / f"{module_name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[module_name] = module
    mod = loaded["config_flow"]

    state = SimpleNamespace(discover=[520], closed=0, created=[])

    class FakeClient:
        def __init__(self, host, port=502, unit_id=1, **kw):
            state.created.append((host, port, unit_id))

        async def close(self):
            state.closed += 1

    class FakePoller:
        def __init__(self, client, **kw):
            pass

        async def discover(self):
            result = state.discover
            if isinstance(result, BaseException):
                raise result
            return result

    monkeypatch.setattr(mod, "HovalModbusClient", FakeClient)
    monkeypatch.setattr(mod, "HovalModbusPoller", FakePoller)
    mod.state = state
    mod.modbus_error = loaded["modbus"].HovalModbusError
    return mod


def _run(coro):
    return asyncio.run(coro)


def _flow(mod):
    return mod.HovalConnectConfigFlow()


def test_user_step_offers_cloud_and_local_menu(flow_mod):
    result = _run(_flow(flow_mod).async_step_user())
    assert result["type"] == "menu"
    assert result["menu_options"] == ["cloud", "local"]


def test_cloud_step_keeps_existing_behaviour(flow_mod, monkeypatch):
    class Api:
        def __init__(self, session, email, password):
            pass

        async def get_plants(self):
            return {}

    monkeypatch.setattr(flow_mod, "HovalConnectApi", Api)
    flow = _flow(flow_mod)
    form = _run(flow.async_step_cloud())
    assert form["type"] == "form" and form["step_id"] == "cloud"
    result = _run(flow.async_step_cloud({"email": "A@B.c", "password": "pw"}))
    assert result["type"] == "create_entry"
    assert result["unique_id"] == "a@b.c"
    assert result["data"] == {"email": "A@B.c", "password": "pw"}


def test_local_step_creates_entry_when_units_found(flow_mod):
    flow = _flow(flow_mod)
    assert _run(flow.async_step_local())["step_id"] == "local"
    result = _run(flow.async_step_local({"host": "gw.local", "port": 502, "unit_id": 1}))
    assert result["type"] == "create_entry"
    assert result["data"] == {"mode": "local", "host": "gw.local", "port": 502, "unit_id": 1}
    assert result["unique_id"] == "modbus:gw.local:502:1"
    assert result["title"] == "Hoval gw.local"
    assert flow_mod.state.closed == 1


def test_local_step_no_units(flow_mod):
    flow_mod.state.discover = []
    result = _run(_flow(flow_mod).async_step_local({"host": "gw", "port": 502, "unit_id": 1}))
    assert result["type"] == "form"
    assert result["errors"] == {"base": "modbus_no_units"}
    assert flow_mod.state.closed == 1


@pytest.mark.parametrize("kind", ["modbus", "os", "timeout"])
def test_local_step_cannot_connect(flow_mod, kind):
    flow_mod.state.discover = {
        "modbus": flow_mod.modbus_error("boom"),
        "os": OSError("down"),
        "timeout": TimeoutError(),
    }[kind]
    result = _run(_flow(flow_mod).async_step_local({"host": "gw", "port": 502, "unit_id": 1}))
    assert result["errors"] == {"base": "modbus_cannot_connect"}
    assert flow_mod.state.closed == 1


def test_local_step_duplicate_aborts(flow_mod):
    first = _flow(flow_mod)
    created = _run(first.async_step_local({"host": "GW.local", "port": 502, "unit_id": 1}))
    second = _flow(flow_mod)
    second.entries = [SimpleNamespace(unique_id=created["unique_id"])]
    with pytest.raises(_Abort) as exc:
        _run(second.async_step_local({"host": "gw.LOCAL", "port": 502, "unit_id": 1}))
    assert exc.value.reason == "already_configured"


def test_reconfigure_local_updates_host(flow_mod):
    flow = _flow(flow_mod)
    flow.reconfigure_entry = SimpleNamespace(
        entry_id="e1",
        unique_id="modbus:old:502:1",
        data={"mode": "local", "host": "old", "port": 502, "unit_id": 1},
    )
    flow.entries = [flow.reconfigure_entry]
    form = _run(flow.async_step_reconfigure())
    assert form["type"] == "form" and form["step_id"] == "reconfigure"
    result = _run(flow.async_step_reconfigure({"host": "new", "port": 503, "unit_id": 2}))
    assert result["reason"] == "reconfigure_successful"
    assert result["unique_id"] == "modbus:new:503:2"
    assert result["data_updates"] == {"host": "new", "port": 503, "unit_id": 2}


def test_reconfigure_rejects_address_of_another_entry(flow_mod):
    flow = _flow(flow_mod)
    mine = SimpleNamespace(
        entry_id="e1",
        unique_id="modbus:old:502:1",
        data={"mode": "local", "host": "old", "port": 502, "unit_id": 1},
    )
    other = SimpleNamespace(entry_id="e2", unique_id="modbus:new:502:1", data={})
    flow.reconfigure_entry = mine
    flow.entries = [mine, other]
    result = _run(flow.async_step_reconfigure({"host": "new", "port": 502, "unit_id": 1}))
    assert result["type"] == "abort" and result["reason"] == "already_configured"


def test_reconfigure_cloud_aborts(flow_mod):
    flow = _flow(flow_mod)
    flow.reconfigure_entry = SimpleNamespace(unique_id="a@b.c", data={"email": "a@b.c"})
    result = _run(flow.async_step_reconfigure())
    assert result == {"type": "abort", "reason": "reconfigure_not_supported"}


def _options_flow(mod, data, options=None, plants=None):
    flow = mod.HovalConnectOptionsFlow()
    entry = SimpleNamespace(data=data, options=options or {})
    if plants is not None:
        entry.runtime_data = SimpleNamespace(
            coordinator=SimpleNamespace(
                data=SimpleNamespace(
                    plants={pid: SimpleNamespace(name=name) for pid, name in plants.items()}
                )
            )
        )
    flow.config_entry = entry
    return flow


_CLOUD = {"email": "a@b.c", "password": "x"}


def _schema_keys(form):
    return {str(k) for k in form["data_schema"].schema}


def test_cloud_options_accept_empty_modbus_host(flow_mod):
    flow = _options_flow(flow_mod, _CLOUD)
    user_input = {
        "turn_on_mode": "resume",
        "override_duration": "endOfPhase",
        "scan_interval": 900,
        "modbus_host": "",
    }
    result = _run(flow.async_step_init(user_input))
    assert result["type"] == "create_entry"
    assert result["data"]["modbus_host"] == ""
    assert flow_mod.state.created == []  # no validation without a host


def test_cloud_options_validate_modbus(flow_mod):
    flow = _options_flow(flow_mod, _CLOUD)
    user_input = {
        "turn_on_mode": "resume",
        "override_duration": "endOfPhase",
        "scan_interval": 900,
        "modbus_host": " gw.local ",
        "modbus_port": 502,
        "modbus_unit_id": 1,
    }
    flow_mod.state.discover = []
    result = _run(flow.async_step_init(dict(user_input)))
    assert result["type"] == "form" and result["errors"] == {"base": "modbus_no_units"}
    flow_mod.state.discover = [520]
    result = _run(flow.async_step_init(dict(user_input)))
    assert result["type"] == "create_entry"
    assert result["data"]["modbus_host"] == "gw.local"
    assert flow_mod.state.closed == 2


def test_cloud_options_plant_selector_only_with_several_plants(flow_mod):
    one = _run(_options_flow(flow_mod, _CLOUD, plants={"p1": "Home"}).async_step_init())
    assert "modbus_plant_id" not in _schema_keys(one)
    none = _run(_options_flow(flow_mod, _CLOUD).async_step_init())
    assert "modbus_plant_id" not in _schema_keys(none)
    flow = _options_flow(flow_mod, _CLOUD, plants={"p1": "Home", "p2": "Cabin"})
    many = _run(flow.async_step_init())
    assert "modbus_plant_id" in _schema_keys(many)
    # Without a selector the value is never stored.
    result = _run(
        _options_flow(flow_mod, _CLOUD, plants={"p1": "Home"}).async_step_init(
            {
                "turn_on_mode": "resume",
                "override_duration": "endOfPhase",
                "scan_interval": 900,
                "modbus_host": "",
            }
        )
    )
    assert "modbus_plant_id" not in result["data"]


def test_local_options_offer_turn_on_mode_and_modbus_interval_only(flow_mod):
    local = {"mode": "local", "host": "gw", "port": 502, "unit_id": 1}
    form = _run(_options_flow(flow_mod, local).async_step_init())
    assert _schema_keys(form) == {"turn_on_mode", "modbus_scan_interval"}
    result = _run(
        _options_flow(flow_mod, local).async_step_init(
            {"turn_on_mode": "week1", "modbus_scan_interval": 60}
        )
    )
    assert result["data"] == {"turn_on_mode": "week1", "modbus_scan_interval": 60}
    assert flow_mod.state.created == []
