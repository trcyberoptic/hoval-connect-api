"""Run integration lifecycle code with isolated, minimal HA stand-ins."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TypedDict
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
        self.title = "Synthetic title"
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


class _ModernDeviceInfo(TypedDict, total=False):
    """DeviceInfo as of Home Assistant 2026.8: the parent is linked by registry id."""

    identifiers: set[tuple[str, str]]
    name: str | None
    manufacturer: str | None
    model: str | None
    via_device_id: str


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
                    "button",
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
        device_registry.DeviceInfo = _ModernDeviceInfo if modern else dict
        device_registry.async_get = lambda hass: SimpleNamespace(
            async_get_or_create=MagicMock(), async_get_device=lambda identifiers: None
        )
        entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
        entity_registry.async_get = lambda hass: hass.registry
        entity_registry.async_entries_for_config_entry = lambda registry, entry_id: [
            entity for entity in registry.entities if entity.config_entry_id == entry_id
        ]
        aiohttp_client = sys.modules["homeassistant.helpers.aiohttp_client"]
        aiohttp_client.async_get_clientsession = lambda hass: object()

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
        coordinator_module.HovalDataCoordinator = lambda *args, **kwargs: SimpleNamespace(
            data=SimpleNamespace(plants={}),
            async_config_entry_first_refresh=AsyncMock(),
            async_close=AsyncMock(),
            has_modbus=kwargs.get("modbus") is not None,
            args=args,
            kwargs=kwargs,
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
            registry=SimpleNamespace(entities=[], async_get=lambda entity_id: None),
            config_entries=SimpleNamespace(
                async_forward_entry_setups=AsyncMock(),
                async_unload_platforms=AsyncMock(return_value=True),
                async_entries=MagicMock(return_value=[]),
                async_schedule_reload=MagicMock(),
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
    entry.options = {"scan_interval": "1800"}

    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    assert entry.runtime_data.coordinator.update_interval.total_seconds() == 1800
    assert len(entry.update_listeners) == (0 if modern else 1)
    flow = ctx.config_flow.HovalConnectOptionsFlow()
    assert bool(getattr(flow, "automatic_reload", False)) is modern
    if not modern:
        entry.options = {"scan_interval": 3600}
        await entry.update_listeners[0](ctx.hass, entry)
        assert entry.runtime_data.coordinator.update_interval.total_seconds() == 3600


@pytest.mark.asyncio
async def test_corrupt_options_do_not_block_setup_or_form(runtime):
    ctx = runtime()
    entry = _ConfigEntry()
    entry.options = {"scan_interval": [], "override_duration": {}, "turn_on_mode": None}

    assert await ctx.component.async_setup_entry(ctx.hass, entry)
    assert entry.runtime_data.coordinator.update_interval.total_seconds() == 900
    flow = ctx.config_flow.HovalConnectOptionsFlow()
    flow.config_entry = entry
    result = await flow.async_step_init()
    assert result["data_schema"]({}) == {
        "scan_interval": 900,
        "override_duration": "endOfPhase",
        "turn_on_mode": "resume",
        "modbus_port": 502,
        "modbus_unit_id": 1,
        "modbus_scan_interval": 30,
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
    entry.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(async_close=AsyncMock()))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("modern", [False, True])
async def test_circuit_devices_link_to_their_plant_device(runtime, modern):
    """HA 2026.8 added via_device_id; the via_device tuple breaks in 2027.8."""
    ctx = runtime(modern)
    plant = SimpleNamespace(name="Plant")
    ctx.component.HovalDataCoordinator = lambda *args, **kwargs: SimpleNamespace(
        data=SimpleNamespace(plants={"P1": plant}),
        async_config_entry_first_refresh=AsyncMock(),
        has_modbus=False,
    )
    registry = SimpleNamespace(
        async_get_or_create=MagicMock(return_value=SimpleNamespace(id="device-P1"))
    )
    ctx.component.dr.async_get = lambda hass: registry
    entry = _ConfigEntry()
    assert await ctx.component.async_setup_entry(ctx.hass, entry)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.plant_device_ids == {"P1": "device-P1"}

    circuit = SimpleNamespace(circuit_type="HV", path="520.50.0", name="Ventilation")
    info = ctx.component.circuit_device_info(coordinator, "P1", circuit)
    if modern:
        assert info["via_device_id"] == "device-P1"
        assert "via_device" not in info
    else:
        assert info["via_device"] == ("hoval_connect", "P1")
        assert "via_device_id" not in info

    # A plant without a registered device gets no parent link rather than a bad one.
    orphan = ctx.component.circuit_device_info(coordinator, "P2", circuit)
    assert "via_device_id" not in orphan


_MODBUS_OPTIONS = {"modbus_host": "192.0.2.10", "modbus_scan_interval": 30}


@pytest.mark.asyncio
async def test_setup_cloud_without_modbus_unchanged(runtime):
    ctx = runtime()
    entry = _ConfigEntry()

    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    coordinator = entry.runtime_data.coordinator
    assert coordinator.kwargs["modbus"] is None
    assert coordinator.kwargs["local_plant"] is None
    assert coordinator.update_interval.total_seconds() == 900
    assert coordinator.cloud_interval == 900


@pytest.mark.asyncio
async def test_setup_hybrid_uses_modbus_interval(runtime):
    ctx = runtime()
    entry = _ConfigEntry()
    entry.options = {**_MODBUS_OPTIONS, "modbus_plant_id": "plant-x"}

    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    coordinator = entry.runtime_data.coordinator
    assert coordinator.kwargs["modbus"] is not None
    assert coordinator.kwargs["modbus_plant_id"] == "plant-x"
    assert entry.runtime_data.api is not None
    assert coordinator.update_interval.total_seconds() == 30
    assert coordinator.cloud_interval == 900


@pytest.mark.asyncio
async def test_setup_local_has_no_api(runtime):
    ctx = runtime()
    entry = _ConfigEntry()
    entry.data = {"mode": "local", "host": "192.0.2.10"}

    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    coordinator = entry.runtime_data.coordinator
    assert entry.runtime_data.api is None
    assert coordinator.args[1] is None
    assert coordinator.kwargs["local_plant"] == ("local_synthetic-entry", "Synthetic title")
    assert coordinator.kwargs["modbus"] is not None
    assert coordinator.update_interval.total_seconds() == 30


@pytest.mark.asyncio
async def test_topology_write_keeps_modbus_cadence_on_legacy_ha(runtime):
    ctx = runtime(modern=False)
    entry = _ConfigEntry()
    entry.options = {**_MODBUS_OPTIONS, "modbus_scan_interval": 60}
    assert await ctx.component.async_setup_entry(ctx.hass, entry)
    coordinator = entry.runtime_data.coordinator

    # The coordinator writing the topology into entry.data fires the same listener.
    entry.data = {**entry.data, "topology": {"plant_id": "p"}}
    await entry.update_listeners[0](ctx.hass, entry)

    assert coordinator.update_interval.total_seconds() == 60
    assert coordinator.cloud_interval == 900
    ctx.hass.config_entries.async_schedule_reload.assert_not_called()


@pytest.mark.asyncio
async def test_adding_modbus_on_legacy_ha_reloads_and_never_polls_cloud_at_modbus_rate(runtime):
    """C1: the cloud-only coordinator has no poller; 30 s would be 30 s cloud polls."""
    ctx = runtime(modern=False)
    entry = _ConfigEntry()  # cloud entry, no Modbus at setup
    entry.entry_id = "e1"
    assert await ctx.component.async_setup_entry(ctx.hass, entry)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.kwargs["modbus"] is None

    # Legacy HA does not reload on its own: only the update listener runs.
    entry.options = dict(_MODBUS_OPTIONS)
    await entry.update_listeners[0](ctx.hass, entry)

    assert coordinator.update_interval.total_seconds() == 900
    ctx.hass.config_entries.async_schedule_reload.assert_called_once_with("e1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"modbus_host": "192.0.2.11"},
        {"modbus_port": 1502},
        {"modbus_unit_id": 0},
        {"modbus_plant_id": "plant-y"},
        {"modbus_host": ""},
    ],
)
async def test_changed_gateway_or_binding_on_legacy_ha_reloads(runtime, change):
    ctx = runtime(modern=False)
    entry = _ConfigEntry()
    entry.options = {**_MODBUS_OPTIONS, "modbus_plant_id": "plant-x"}
    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    entry.options = {**entry.options, **change}
    await entry.update_listeners[0](ctx.hass, entry)

    ctx.hass.config_entries.async_schedule_reload.assert_called_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_interval_change_on_legacy_ha_does_not_reload(runtime):
    ctx = runtime(modern=False)
    entry = _ConfigEntry()
    entry.options = {**_MODBUS_OPTIONS, "modbus_plant_id": "plant-x"}
    assert await ctx.component.async_setup_entry(ctx.hass, entry)
    coordinator = entry.runtime_data.coordinator

    entry.options = {**entry.options, "modbus_scan_interval": 120, "scan_interval": 3600}
    await entry.update_listeners[0](ctx.hass, entry)

    assert coordinator.update_interval.total_seconds() == 120
    assert coordinator.cloud_interval == 3600
    ctx.hass.config_entries.async_schedule_reload.assert_not_called()


@pytest.mark.asyncio
async def test_plant_choice_without_modbus_on_legacy_ha_does_not_reload(runtime):
    """An entry without Modbus behaves as before: saving options never reloads it."""
    ctx = runtime(modern=False)
    entry = _ConfigEntry()
    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    entry.options = {"scan_interval": 1800, "modbus_host": "", "modbus_plant_id": "plant-x"}
    await entry.update_listeners[0](ctx.hass, entry)

    assert entry.runtime_data.coordinator.update_interval.total_seconds() == 1800
    ctx.hass.config_entries.async_schedule_reload.assert_not_called()


@pytest.mark.asyncio
async def test_local_options_on_legacy_ha_update_interval_without_reload(runtime):
    """Local entries change their gateway via reconfigure, which reloads by itself."""
    ctx = runtime(modern=False)
    entry = _ConfigEntry()
    entry.data = {"mode": "local", "host": "192.0.2.10"}
    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    entry.options = {"modbus_scan_interval": 60}
    await entry.update_listeners[0](ctx.hass, entry)

    assert entry.runtime_data.coordinator.update_interval.total_seconds() == 60
    ctx.hass.config_entries.async_schedule_reload.assert_not_called()


@pytest.mark.asyncio
async def test_unload_closes_modbus_client(runtime):
    ctx = runtime()
    entry = _ConfigEntry()
    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    assert await ctx.component.async_unload_entry(ctx.hass, entry)

    entry.runtime_data.coordinator.async_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_temporary_change_rejected_for_local_entry(runtime):
    ctx = runtime()
    entry = _ConfigEntry()
    entry.data = {"mode": "local", "host": "192.0.2.10"}
    assert await ctx.component.async_setup_entry(ctx.hass, entry)
    ctx.hass.registry = SimpleNamespace(
        async_get=lambda entity_id: SimpleNamespace(
            platform="hoval_connect", config_entry_id="e", unique_id="x_1_fan"
        )
    )
    ctx.hass.config_entries.async_get_entry = lambda entry_id: entry
    call = SimpleNamespace(data={"entity_id": ["fan.x"]})

    with pytest.raises(Exception, match="Hoval account"):
        await ctx.component._async_handle_reset_temporary_change(ctx.hass, call)


@pytest.mark.asyncio
async def test_failed_first_refresh_closes_modbus_client(runtime):
    ctx = runtime()
    entry = _ConfigEntry()
    entry.options = dict(_MODBUS_OPTIONS)
    closed = AsyncMock()

    def failing(*args, **kwargs):
        return SimpleNamespace(
            data=SimpleNamespace(plants={}),
            async_config_entry_first_refresh=AsyncMock(side_effect=RuntimeError("not ready")),
            async_close=closed,
            has_modbus=True,
        )

    ctx.component.HovalDataCoordinator = failing

    with pytest.raises(RuntimeError, match="not ready"):
        await ctx.component.async_setup_entry(ctx.hass, entry)

    closed.assert_awaited_once()


# --- Seeding the topology of a hybrid entry from its registries (R-SEED, I6) ----

# Same literal as _SEEDED in test_modbus_coordinator.py, which starts a hybrid
# coordinator from it with the cloud down.
_SEEDED = {"plant_id": "P_1", "plant_name": "HRT", "circuits": {"520.50.0": "Lüftung"}}


def _registry_entity(unique_id, *, domain="fan", entry_id="synthetic-entry", platform=None):
    return SimpleNamespace(
        domain=domain,
        platform=platform or "hoval_connect",
        unique_id=unique_id,
        config_entry_id=entry_id,
    )


def _registries(ctx, entities, devices):
    """Entity registry entries and device names (identifier -> registry name)."""
    ctx.hass.registry = SimpleNamespace(entities=entities, async_get=lambda entity_id: None)

    def get_device(identifiers):
        ((_domain, identifier),) = identifiers
        name = devices.get(identifier)
        return None if name is None else SimpleNamespace(name=name)

    ctx.component.dr.async_get = lambda hass: SimpleNamespace(
        async_get_or_create=MagicMock(return_value=SimpleNamespace(id="device")),
        async_get_device=get_device,
    )


@pytest.mark.asyncio
async def test_hybrid_setup_without_topology_seeds_it_from_the_registry(runtime):
    """I6: a first hybrid start while Hoval blocks the account had no topology."""
    ctx = runtime()
    entry = _ConfigEntry()
    entry.options = dict(_MODBUS_OPTIONS)
    _registries(
        ctx,
        [
            _registry_entity("P_1_520.50.0_fan"),
            _registry_entity("P_1_520.50.0_air_volume", domain="sensor"),
            _registry_entity("P_1_1.1.0_climate", domain="climate"),
            _registry_entity("P_9_521.50.0_fan", entry_id="other-entry"),
            _registry_entity("P_8_522.50.0_fan", platform="other_integration"),
        ],
        {"P_1": "Hoval HRT", "P_1_520.50.0": "Hoval Lüftung"},
    )

    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    assert entry.runtime_data.coordinator.kwargs["topology"] == _SEEDED
    # Stored only by the next successful cloud poll, as usual.
    assert "topology" not in entry.data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fans", "plant_option", "expected"),
    [
        (["P1_520.50.0_fan", "P2_520.50.0_fan"], None, None),
        (["P1_520.50.0_fan", "P2_520.50.0_fan", "P2_521.50.0_fan"], "P2", "P2"),
        (["P1_520.50.0_fan"], "P2", None),
        (["P1_520.50.0_fan"], "P1", "P1"),
    ],
)
async def test_seed_needs_one_plant_or_the_chosen_one(runtime, fans, plant_option, expected):
    ctx = runtime()
    entry = _ConfigEntry()
    entry.options = dict(_MODBUS_OPTIONS)
    if plant_option:
        entry.options["modbus_plant_id"] = plant_option
    _registries(ctx, [_registry_entity(uid) for uid in fans], {})

    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    topology = entry.runtime_data.coordinator.kwargs["topology"]
    if expected is None:
        assert topology is None
    else:
        paths = sorted(uid.split("_")[1] for uid in fans if uid.startswith(f"{expected}_"))
        # Without device names: the plant id and the cloud's fallback circuit name.
        assert topology == {
            "plant_id": expected,
            "plant_name": expected,
            "circuits": dict.fromkeys(paths, "HV"),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["stored", "cloud_only", "local"])
async def test_seed_only_for_hybrid_entries_without_stored_topology(runtime, kind):
    ctx = runtime()
    entry = _ConfigEntry()
    stored = {"plant_id": "P_1", "plant_name": "Stored", "circuits": {"520.50.0": "Stored"}}
    if kind == "stored":
        entry.options = dict(_MODBUS_OPTIONS)
        entry.data = {**entry.data, "topology": stored}
    elif kind == "local":
        entry.data = {"mode": "local", "host": "192.0.2.10"}
    _registries(ctx, [_registry_entity("P_1_520.50.0_fan")], {"P_1": "Hoval HRT"})

    assert await ctx.component.async_setup_entry(ctx.hass, entry)

    assert entry.runtime_data.coordinator.kwargs["topology"] == (
        stored if kind == "stored" else None
    )
