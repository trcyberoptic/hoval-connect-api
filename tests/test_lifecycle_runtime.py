"""Run integration lifecycle code with isolated, minimal HA stand-ins."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "hoval_connect"


class _ConfigEntry:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self):
        self.entry_id = "synthetic-entry"
        self.data = {"email": "test@example.invalid", "password": "test-password"}
        self.options = {}
        self.unique_id = self.data["email"]
        self.update_listeners = []

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)
        return lambda: self.update_listeners.remove(listener)

    def async_on_unload(self, listener):
        pass


class _Flow:
    def __init_subclass__(cls, **kwargs):
        pass

    def async_show_form(self, **kwargs):
        return kwargs

    def async_create_entry(self, **kwargs):
        return kwargs


class _ReloadingOptionsFlow(_Flow):
    automatic_reload = True


@pytest.fixture
def runtime(monkeypatch):
    packages = set()

    def load(modern=True):
        if isinstance(sys.modules.get("voluptuous"), MagicMock):
            monkeypatch.delitem(sys.modules, "voluptuous")
        importlib.import_module("voluptuous")
        names = (
            "homeassistant",
            "homeassistant.config_entries",
            "homeassistant.const",
            "homeassistant.core",
            "homeassistant.exceptions",
            "homeassistant.helpers",
            "homeassistant.helpers.config_validation",
            "homeassistant.helpers.device_registry",
            "homeassistant.helpers.entity_registry",
            "homeassistant.helpers.aiohttp_client",
        )
        for name in names:
            monkeypatch.setitem(sys.modules, name, ModuleType(name))
        entries = sys.modules["homeassistant.config_entries"]
        entries.ConfigEntry = _ConfigEntry
        entries.ConfigFlow = _Flow
        entries.ConfigFlowResult = dict
        entries.OptionsFlow = _Flow
        if modern:
            entries.OptionsFlowWithReload = _ReloadingOptionsFlow
        ha_const = sys.modules["homeassistant.const"]
        ha_const.ATTR_ENTITY_ID = "entity_id"
        ha_const.Platform = SimpleNamespace(
            **{
                name.upper(): name
                for name in (
                    "binary_sensor",
                    "climate",
                    "fan",
                    "select",
                    "sensor",
                    "water_heater",
                    "number",
                )
            }
        )
        core = sys.modules["homeassistant.core"]
        core.HomeAssistant = object
        core.ServiceCall = object
        errors = sys.modules["homeassistant.exceptions"]
        errors.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
        errors.ServiceValidationError = type("ServiceValidationError", (Exception,), {})
        sys.modules["homeassistant.helpers.config_validation"].entity_ids = list
        device_registry = sys.modules["homeassistant.helpers.device_registry"]
        device_registry.DeviceInfo = dict
        device_registry.async_get = lambda hass: SimpleNamespace(async_get_or_create=MagicMock())
        sys.modules["homeassistant.helpers.entity_registry"].async_get = lambda hass: hass.registry
        sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = (
            lambda hass: object()
        )

        name = f"_lifecycle_component_{modern}"
        packages.add(name)
        api_module = ModuleType(f"{name}.api")
        api_module.HovalApiError = type("HovalApiError", (Exception,), {})
        api_module.HovalAuthError = type("HovalAuthError", (Exception,), {})
        api_module.HovalConnectApi = lambda *args: SimpleNamespace(
            reset_temporary_change=AsyncMock(), get_plants=AsyncMock(return_value=[])
        )
        monkeypatch.setitem(sys.modules, api_module.__name__, api_module)
        coordinator_module = ModuleType(f"{name}.coordinator")
        coordinator_module.HovalCircuitData = object
        coordinator_module.HovalPlantData = object
        coordinator_module.HovalDataCoordinator = lambda *args: SimpleNamespace(
            data=SimpleNamespace(plants={}),
            async_config_entry_first_refresh=AsyncMock(),
        )
        monkeypatch.setitem(sys.modules, coordinator_module.__name__, coordinator_module)
        spec = importlib.util.spec_from_file_location(
            name, _COMPONENT / "__init__.py", submodule_search_locations=[str(_COMPONENT)]
        )
        component = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, component)
        # Imports inside the real package must also be removed after this test.
        for submodule in ("const", "compat", "options", "config_flow"):
            monkeypatch.delitem(sys.modules, f"{name}.{submodule}", raising=False)
        spec.loader.exec_module(component)
        config_flow = importlib.import_module(f"{name}.config_flow")
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_forward_entry_setups=AsyncMock(),
                async_unload_platforms=AsyncMock(return_value=True),
                async_entries=MagicMock(return_value=[]),
            ),
            services=SimpleNamespace(
                has_service=MagicMock(return_value=True),
                async_register=MagicMock(),
                async_remove=MagicMock(),
            ),
        )
        return SimpleNamespace(component=component, config_flow=config_flow, hass=hass)

    yield load
    for name in list(sys.modules):
        if name.partition(".")[0] in packages:
            del sys.modules[name]


@pytest.mark.asyncio
@pytest.mark.parametrize("modern", [False, True])
async def test_setup_registers_listener_only_for_legacy_ha(runtime, modern):
    ctx = runtime(modern)
    entry = _ConfigEntry()
    entry.options = {"scan_interval": "120"}

    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    assert entry.runtime_data.coordinator.update_interval.total_seconds() == 120
    assert len(entry.update_listeners) == (0 if modern else 1)
    flow = ctx.config_flow.HovalConnectOptionsFlow()
    assert bool(getattr(flow, "automatic_reload", False)) is modern
    if not modern:
        entry.options = {"scan_interval": 300}
        await entry.update_listeners[0](ctx.hass, entry)
        assert entry.runtime_data.coordinator.update_interval.total_seconds() == 300


@pytest.mark.asyncio
async def test_corrupt_options_do_not_block_setup_or_form(runtime):
    ctx = runtime()
    entry = _ConfigEntry()
    entry.options = {"scan_interval": [], "override_duration": {}, "turn_on_mode": None}

    assert await ctx.component.async_setup_entry(ctx.hass, entry)
    assert entry.runtime_data.coordinator.update_interval.total_seconds() == 60
    flow = ctx.config_flow.HovalConnectOptionsFlow()
    flow.config_entry = entry
    result = await flow.async_step_init()
    assert result["data_schema"]({}) == {
        "scan_interval": 60,
        "override_duration": "endOfPhase",
        "turn_on_mode": "resume",
    }


@pytest.mark.asyncio
async def test_modern_reauth_reloads_without_conflicting_update_listener(runtime):
    ctx = runtime()
    entry = _ConfigEntry()
    await ctx.component.async_setup_entry(ctx.hass, entry)
    flow = ctx.config_flow.HovalConnectConfigFlow()
    flow.hass = ctx.hass
    flow._get_reauth_entry = lambda: entry
    reload = MagicMock(return_value={"type": "abort", "reason": "reauth_successful"})
    flow.async_update_reload_and_abort = reload

    result = await flow.async_step_reauth_confirm(
        {"email": entry.data["email"], "password": "replacement-password"}
    )

    assert entry.update_listeners == []
    assert result["reason"] == "reauth_successful"
    reload.assert_called_once_with(
        entry, data={"email": entry.data["email"], "password": "replacement-password"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("unloaded, other_entry", [(False, False), (True, False), (True, True)])
async def test_reset_service_removed_only_after_successful_last_unload(
    runtime, unloaded, other_entry
):
    ctx = runtime()
    entry = _ConfigEntry()
    ctx.hass.config_entries.async_unload_platforms.return_value = unloaded
    if other_entry:
        ctx.hass.config_entries.async_entries.return_value = [SimpleNamespace(entry_id="other")]

    assert await ctx.component.async_unload_entry(ctx.hass, entry) is unloaded
    assert ctx.hass.services.async_remove.call_count == int(unloaded and not other_entry)


@pytest.mark.asyncio
async def test_reset_service_defers_and_binds_each_target_command(runtime):
    ctx = runtime()
    entry = _ConfigEntry()
    api = SimpleNamespace(reset_temporary_change=AsyncMock())
    control = AsyncMock()
    entry.runtime_data = SimpleNamespace(
        api=api,
        coordinator=SimpleNamespace(
            data=SimpleNamespace(
                plants={
                    "plant-a": SimpleNamespace(circuits={"1.2.3": None}),
                    "plant-b": SimpleNamespace(circuits={"1.2.3": None}),
                }
            ),
            async_control_and_refresh=control,
        ),
    )
    registry = {
        f"fan.{plant}": SimpleNamespace(
            platform="hoval_connect", config_entry_id=entry.entry_id, unique_id=f"{plant}_1.2.3_fan"
        )
        for plant in ("plant-a", "plant-b")
    }
    ctx.hass.registry = SimpleNamespace(async_get=registry.get)
    ctx.hass.config_entries.async_get_entry = lambda entry_id: entry

    await ctx.component._async_handle_reset_temporary_change(
        ctx.hass, SimpleNamespace(data={"entity_id": list(registry)})
    )

    api.reset_temporary_change.assert_not_called()
    assert control.await_count == 2
    for call in control.call_args_list:
        await call.args[0]()
        assert api.reset_temporary_change.call_args.args == (call.kwargs["plant_id"], "1.2.3")
    assert {call.args for call in api.reset_temporary_change.call_args_list} == {
        ("plant-a", "1.2.3"),
        ("plant-b", "1.2.3"),
    }
