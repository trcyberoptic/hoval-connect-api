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

    def async_create_entry(self, *, title, data, options=None, **kw):
        return {
            "type": "create_entry",
            "title": title,
            "data": data,
            "options": options,
            "unique_id": self.unique_id,
        }

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


def test_user_step_offers_cloud_hybrid_and_local_menu(flow_mod):
    result = _run(_flow(flow_mod).async_step_user())
    assert result["type"] == "menu"
    assert result["menu_options"] == ["cloud", "hybrid", "local"]


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


def test_local_step_no_answer_when_connected_but_timing_out(flow_mod):
    flow_mod.state.discover = flow_mod.modbus_error("timed out", no_answer=True)
    result = _run(_flow(flow_mod).async_step_local({"host": "gw", "port": 502, "unit_id": 1}))
    assert result["errors"] == {"base": "modbus_no_answer"}
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


def _options_flow(mod, data, options=None, plants=None, others=()):
    flow = mod.HovalConnectOptionsFlow()
    entry = SimpleNamespace(entry_id="me", data=data, options=options or {})
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry, *others])
    )
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


def test_cloud_options_keep_stored_plant_binding_when_selector_hidden(flow_mod):
    flow = _options_flow(flow_mod, _CLOUD, options={"modbus_plant_id": "P2"})  # not loaded
    result = _run(
        flow.async_step_init(
            {
                "turn_on_mode": "resume",
                "override_duration": "endOfPhase",
                "scan_interval": 900,
                "modbus_host": "",
            }
        )
    )
    assert result["data"]["modbus_plant_id"] == "P2"


def test_options_flow_error_keys_are_translated_in_options_error():
    """HA resolves option-flow errors from options.error, not config.error."""
    import json
    import re

    src = Path("custom_components/hoval_connect/config_flow.py").read_text(encoding="utf-8")
    flow_src = src[src.index("class HovalConnectOptionsFlow") :]
    keys = set(re.findall(r'errors\["base"\] = "([a-z_]+)"', flow_src))
    keys |= {
        "modbus_cannot_connect",
        "modbus_no_answer",
        "modbus_no_units",
    }  # returned via _modbus_error_key
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        data = json.loads(Path("custom_components/hoval_connect", name).read_text(encoding="utf-8"))
        missing = keys - set(data["options"].get("error", {}))
        assert not missing, (name, missing)


HYBRID_OPTIONS = {
    "turn_on_mode": "resume",
    "override_duration": "endOfPhase",
    "scan_interval": 900,
    "modbus_host": "gw.local",
    "modbus_port": 502,
    "modbus_unit_id": 1,
    "modbus_scan_interval": 60,
}
_STORED_GATEWAY = {
    "turn_on_mode": "resume",
    "override_duration": "endOfPhase",
    "scan_interval": 900,
    "modbus_host": "gw.local",
    "modbus_port": 502,
    "modbus_unit_id": 1,
    "modbus_scan_interval": 30,
}


def test_cloud_options_unchanged_gateway_is_not_probed(flow_mod):
    """I5: saving unrelated options must not open a second connection to the gateway;
    with the gateway briefly offline (or allowing one client) saving would fail."""
    flow = _options_flow(flow_mod, _CLOUD, options=dict(_STORED_GATEWAY))
    flow_mod.state.discover = flow_mod.modbus_error("gateway offline")
    user_input = {**_STORED_GATEWAY, "scan_interval": 1800, "modbus_host": " gw.local "}
    result = _run(flow.async_step_init(user_input))
    assert result["type"] == "create_entry"
    assert result["data"]["scan_interval"] == 1800
    assert result["data"]["modbus_host"] == "gw.local"
    assert flow_mod.state.created == []


@pytest.mark.parametrize(
    ("change", "probed"),
    [
        ({"modbus_host": "gw2.local"}, ("gw2.local", 502, 1)),
        ({"modbus_port": 1502}, ("gw.local", 1502, 1)),
        ({"modbus_unit_id": 0}, ("gw.local", 502, 0)),
    ],
)
def test_cloud_options_changed_gateway_is_probed(flow_mod, change, probed):
    flow = _options_flow(flow_mod, _CLOUD, options=dict(_STORED_GATEWAY))
    flow_mod.state.discover = []
    result = _run(flow.async_step_init({**_STORED_GATEWAY, **change}))
    assert result["errors"] == {"base": "modbus_no_units"}
    assert flow_mod.state.created == [probed]


# --- Hybrid setup: cloud login, then the Modbus gateway --------------------------

_HYBRID_LOGIN = {"email": "A@B.c", "password": "pw"}
_HYBRID_GATEWAY = {"host": " gw.local ", "port": 502, "unit_id": 1, "modbus_scan_interval": 60}
_TWO_PLANTS = [
    {"plantExternalId": "P1", "description": "House"},
    {"plantExternalId": "P2", "description": "Cabin"},
]


def _hybrid_api(monkeypatch, flow_mod, plants=None, error=None):
    class Api:
        def __init__(self, session, email, password):
            pass

        async def get_plants(self):
            if error is not None:
                raise error
            return plants if plants is not None else [{"plantExternalId": "P1"}]

    monkeypatch.setattr(flow_mod, "HovalConnectApi", Api)


def _hybrid_to_gateway(flow_mod, flow=None):
    flow = flow or _flow(flow_mod)
    assert _run(flow.async_step_hybrid())["step_id"] == "hybrid"
    return flow, _run(flow.async_step_hybrid(_HYBRID_LOGIN))


def test_hybrid_creates_a_cloud_entry_with_modbus_options(flow_mod, monkeypatch):
    _hybrid_api(monkeypatch, flow_mod)
    flow, form = _hybrid_to_gateway(flow_mod)
    assert form["type"] == "form" and form["step_id"] == "hybrid_gateway"
    assert "modbus_plant_id" not in _schema_keys(form)

    result = _run(flow.async_step_hybrid_gateway(_HYBRID_GATEWAY))

    assert result["type"] == "create_entry"
    assert result["title"] == "A@B.c"
    assert result["unique_id"] == "a@b.c"
    # Exactly what a cloud entry gets, no extra keys and no new mode.
    assert result["data"] == {"email": "A@B.c", "password": "pw"}
    assert result["options"] == HYBRID_OPTIONS
    assert flow_mod.state.created == [("gw.local", 502, 1)]
    assert flow_mod.state.closed == 1


def test_hybrid_options_are_keys_the_options_flow_writes(flow_mod, monkeypatch):
    _hybrid_api(monkeypatch, flow_mod)
    flow, _ = _hybrid_to_gateway(flow_mod)
    options = _run(flow.async_step_hybrid_gateway(_HYBRID_GATEWAY))["options"]
    form = _run(_options_flow(flow_mod, _CLOUD).async_step_init())
    assert set(options) <= _schema_keys(form)


@pytest.mark.parametrize(
    ("error", "key"),
    [
        ("auth", "invalid_auth"),
        ("api", "cannot_connect"),
        ("blocked", "gateway_blocked"),
        ("403", "no_plant_access"),
        ("timeout", "cannot_connect"),
    ],
)
def test_hybrid_login_errors_match_the_cloud_step(flow_mod, monkeypatch, error, key):
    errors = flow_mod
    api_error = errors.HovalApiError
    err = {
        "auth": errors.HovalAuthError("bad"),
        "api": api_error("down"),
        "blocked": api_error("blocked", gateway_blocked=True),
        "403": api_error("forbidden", status=403),
        "timeout": TimeoutError(),
    }[error]
    _hybrid_api(monkeypatch, flow_mod, error=err)
    hybrid = _run(_flow(flow_mod).async_step_hybrid(_HYBRID_LOGIN))
    cloud = _run(_flow(flow_mod).async_step_cloud(_HYBRID_LOGIN))
    assert hybrid["step_id"] == "hybrid"
    assert hybrid["errors"] == cloud["errors"] == {"base": key}


def test_hybrid_duplicate_account_aborts(flow_mod, monkeypatch):
    _hybrid_api(monkeypatch, flow_mod)
    flow = _flow(flow_mod)
    flow.entries = [SimpleNamespace(unique_id="a@b.c")]
    with pytest.raises(_Abort) as exc:
        _run(flow.async_step_hybrid(_HYBRID_LOGIN))
    assert exc.value.reason == "already_configured"


def test_hybrid_gateway_without_units(flow_mod, monkeypatch):
    _hybrid_api(monkeypatch, flow_mod)
    flow_mod.state.discover = []
    flow, _ = _hybrid_to_gateway(flow_mod)
    result = _run(flow.async_step_hybrid_gateway(_HYBRID_GATEWAY))
    assert result["type"] == "form" and result["step_id"] == "hybrid_gateway"
    assert result["errors"] == {"base": "modbus_no_units"}
    assert flow_mod.state.closed == 1


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        ("no_answer", "modbus_no_answer"),
        ("refused", "modbus_cannot_connect"),
        ("os", "modbus_cannot_connect"),
        ("timeout", "modbus_cannot_connect"),
    ],
)
def test_hybrid_gateway_error_split(flow_mod, monkeypatch, kind, key):
    _hybrid_api(monkeypatch, flow_mod)
    flow_mod.state.discover = {
        "no_answer": flow_mod.modbus_error("timed out", no_answer=True),
        "refused": flow_mod.modbus_error("refused"),
        "os": OSError("unreachable"),
        "timeout": TimeoutError(),
    }[kind]
    flow, _ = _hybrid_to_gateway(flow_mod)
    result = _run(flow.async_step_hybrid_gateway(_HYBRID_GATEWAY))
    assert result["errors"] == {"base": key}


def test_hybrid_gateway_error_keeps_the_entered_values(flow_mod, monkeypatch):
    _hybrid_api(monkeypatch, flow_mod)
    flow_mod.state.discover = []
    flow, _ = _hybrid_to_gateway(flow_mod)
    result = _run(flow.async_step_hybrid_gateway({**_HYBRID_GATEWAY, "port": 1502}))
    assert result["data_schema"]({"host": "x"})["port"] == 1502


def test_hybrid_two_plants_show_and_store_the_selector(flow_mod, monkeypatch):
    _hybrid_api(monkeypatch, flow_mod, plants=_TWO_PLANTS)
    flow, form = _hybrid_to_gateway(flow_mod)
    assert "modbus_plant_id" in _schema_keys(form)
    result = _run(flow.async_step_hybrid_gateway({**_HYBRID_GATEWAY, "modbus_plant_id": "P2"}))
    assert result["options"] == {**HYBRID_OPTIONS, "modbus_plant_id": "P2"}


def test_hybrid_one_plant_stores_no_plant(flow_mod, monkeypatch):
    _hybrid_api(monkeypatch, flow_mod, plants=[{"plantExternalId": "P1"}])
    flow, _ = _hybrid_to_gateway(flow_mod)
    result = _run(flow.async_step_hybrid_gateway({**_HYBRID_GATEWAY, "modbus_plant_id": "P1"}))
    assert "modbus_plant_id" not in result["options"]


def test_hybrid_gateway_ignores_unknown_plant(flow_mod, monkeypatch):
    _hybrid_api(monkeypatch, flow_mod, plants=_TWO_PLANTS)
    flow, _ = _hybrid_to_gateway(flow_mod)
    result = _run(flow.async_step_hybrid_gateway({**_HYBRID_GATEWAY, "modbus_plant_id": "nope"}))
    assert "modbus_plant_id" not in result["options"]


# --- Translations of the hybrid setup and the activation hints ---------------

_FILES = ("strings.json", "translations/en.json", "translations/de.json")


def _translation(name):
    import json

    return json.loads(Path("custom_components/hoval_connect", name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", _FILES)
def test_hybrid_steps_and_error_keys_are_translated(name):
    data = _translation(name)
    steps = data["config"]["step"]
    assert set(steps["user"]["menu_options"]) == {"cloud", "hybrid", "local"}
    assert set(steps["hybrid"]["data"]) == {"email", "password"}
    gateway = steps["hybrid_gateway"]
    expected = {"host", "port", "unit_id", "modbus_scan_interval", "modbus_plant_id"}
    assert set(gateway["data"]) == expected
    assert set(gateway["data_description"]) == expected
    for scope in (data["config"]["error"], data["options"]["error"]):
        assert {"modbus_cannot_connect", "modbus_no_answer", "modbus_no_units"} <= set(scope)


def test_en_mirrors_strings():
    assert _translation("strings.json") == _translation("translations/en.json")


@pytest.mark.parametrize("name", _FILES)
def test_activation_hint_is_in_every_modbus_setup_text(name):
    data = _translation(name)
    on, mode = ("«Aktiv»", "«Mode»") if name.endswith("de.json") else ('"Aktiv"', '"Mode"')
    texts = [
        data["config"]["step"]["local"]["description"],
        data["config"]["step"]["reconfigure"]["description"],
        data["config"]["step"]["hybrid_gateway"]["description"],
        data["options"]["step"]["init"]["data_description"]["modbus_host"],
        data["config"]["error"]["modbus_no_answer"],
        data["options"]["error"]["modbus_no_answer"],
    ]
    for text in texts:
        assert "Service > TTE-GW > Modbus > Einstellungen" in text
        assert on in text and mode in text and "TCP" in text and "RS485" in text
    assert "Modbus" in data["config"]["step"]["user"]["description"]
    assert "Aktiv" not in data["config"]["error"]["modbus_cannot_connect"]


def test_german_hybrid_texts_use_swiss_orthography():
    data = _translation("translations/de.json")
    new = [
        data["config"]["step"]["user"]["description"],
        data["config"]["step"]["user"]["menu_options"]["hybrid"],
        *data["config"]["step"]["hybrid"].values(),
        data["config"]["step"]["hybrid_gateway"]["description"],
        data["config"]["error"]["modbus_no_answer"],
        data["config"]["error"]["modbus_cannot_connect"],
    ]
    assert not any("ß" in text for text in new if isinstance(text, str))


# --- One Modbus client at a time (live finding 2026-11-20) ---------------------


def _entry(entry_id, data=None, options=None):
    return SimpleNamespace(
        entry_id=entry_id, unique_id=None, data=data or {}, options=options or {}
    )


_HYBRID_OTHER = _entry("h1", {"email": "x@y.z"}, {"modbus_host": "GW.local ", "modbus_port": 502})
_LOCAL_OTHER = _entry("l1", {"mode": "local", "host": "gw.local", "port": 502, "unit_id": 1})


def test_local_step_rejects_gateway_used_by_a_hybrid_entry(flow_mod):
    flow = _flow(flow_mod)
    flow.entries = [_HYBRID_OTHER]
    result = _run(flow.async_step_local({"host": " gw.LOCAL", "port": 502, "unit_id": 1}))
    assert result["type"] == "form" and result["errors"] == {"base": "gateway_in_use"}
    assert flow_mod.state.created == []


def test_local_step_same_host_other_port_is_allowed(flow_mod):
    flow = _flow(flow_mod)
    flow.entries = [_HYBRID_OTHER]
    result = _run(flow.async_step_local({"host": "gw.local", "port": 1502, "unit_id": 1}))
    assert result["type"] == "create_entry"


def test_hybrid_gateway_rejects_gateway_used_by_a_local_entry(flow_mod, monkeypatch):
    _hybrid_api(monkeypatch, flow_mod)
    flow, _ = _hybrid_to_gateway(flow_mod)
    flow.entries = [_LOCAL_OTHER]
    result = _run(flow.async_step_hybrid_gateway(dict(_HYBRID_GATEWAY)))
    assert result["type"] == "form" and result["errors"] == {"base": "gateway_in_use"}
    assert flow_mod.state.created == []


def test_hybrid_gateway_other_port_is_allowed(flow_mod, monkeypatch):
    _hybrid_api(monkeypatch, flow_mod)
    flow, _ = _hybrid_to_gateway(flow_mod)
    flow.entries = [_LOCAL_OTHER]
    result = _run(flow.async_step_hybrid_gateway({**_HYBRID_GATEWAY, "port": 1502}))
    assert result["type"] == "create_entry"


def test_reconfigure_rejects_gateway_used_by_a_hybrid_entry(flow_mod):
    flow = _flow(flow_mod)
    mine = _entry("e1", {"mode": "local", "host": "old", "port": 502, "unit_id": 1})
    mine.unique_id = "modbus:old:502:1"
    flow.reconfigure_entry = mine
    flow.entries = [mine, _HYBRID_OTHER]
    result = _run(flow.async_step_reconfigure({"host": "gw.local", "port": 502, "unit_id": 1}))
    assert result["type"] == "form" and result["errors"] == {"base": "gateway_in_use"}


def test_reconfigure_to_own_current_host_is_allowed(flow_mod):
    flow = _flow(flow_mod)
    mine = _entry("e1", {"mode": "local", "host": "gw.local", "port": 502, "unit_id": 1})
    mine.unique_id = "modbus:gw.local:502:1"
    flow.reconfigure_entry = mine
    flow.entries = [mine]
    result = _run(flow.async_step_reconfigure({"host": "gw.local", "port": 502, "unit_id": 2}))
    assert result["reason"] == "reconfigure_successful"


def test_cloud_options_reject_gateway_of_another_entry(flow_mod):
    flow = _options_flow(
        flow_mod, _CLOUD, options=dict(_STORED_GATEWAY), others=[_entry("l2", _LOCAL_OTHER.data)]
    )
    result = _run(
        flow.async_step_init({**_STORED_GATEWAY, "modbus_host": "GW.local ", "modbus_port": 503})
    )
    assert result["type"] == "create_entry"  # other port: allowed
    result = _run(flow.async_step_init({**_STORED_GATEWAY, "modbus_host": "other"}))
    assert result["type"] == "create_entry"
    flow = _options_flow(
        flow_mod,
        _CLOUD,
        options={**_STORED_GATEWAY, "modbus_host": "mine.local"},
        others=[_entry("l2", _LOCAL_OTHER.data)],
    )
    flow_mod.state.created.clear()
    result = _run(flow.async_step_init({**_STORED_GATEWAY, "modbus_host": "GW.local "}))
    assert result["type"] == "form" and result["errors"] == {"base": "gateway_in_use"}
    assert flow_mod.state.created == []


def test_options_probe_frees_the_running_connection_first(flow_mod):
    order = []

    class Client:
        async def disconnect(self):
            order.append("disconnect")

    flow = _options_flow(flow_mod, _CLOUD, options=dict(_STORED_GATEWAY))
    flow.config_entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(modbus=SimpleNamespace(client=Client()))
    )
    original = flow_mod.HovalModbusClient

    def probing_client(*a, **kw):
        order.append("probe")
        return original(*a, **kw)

    flow_mod.HovalModbusClient = probing_client
    result = _run(flow.async_step_init({**_STORED_GATEWAY, "modbus_host": "gw2.local"}))
    assert result["type"] == "create_entry"
    assert order == ["disconnect", "probe"]


def test_options_unchanged_gateway_keeps_the_connection(flow_mod):
    calls = []

    class Client:
        async def disconnect(self):
            calls.append(1)

    flow = _options_flow(flow_mod, _CLOUD, options=dict(_STORED_GATEWAY))
    flow.config_entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(modbus=SimpleNamespace(client=Client()))
    )
    _run(flow.async_step_init({**_STORED_GATEWAY, "scan_interval": 1800}))
    assert calls == []


@pytest.mark.parametrize("name", _FILES)
def test_one_client_hint_and_gateway_in_use_are_translated(name):
    data = _translation(name)
    for scope in (data["config"]["error"], data["options"]["error"]):
        assert "one Modbus client at a time" in scope["modbus_no_answer"] or (
            "nur einen Modbus-Client gleichzeitig" in scope["modbus_no_answer"]
        )
        assert "Loxone" in scope["modbus_no_answer"]
        assert scope["gateway_in_use"]
    assert "ß" not in data["config"]["error"]["gateway_in_use"]
