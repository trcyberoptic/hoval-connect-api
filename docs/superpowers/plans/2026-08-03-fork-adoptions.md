# Fork-Adoptions (GMH224) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the verified, safe subset of the GMH224 fork (their v0.15.3→v0.21.1 work) onto our master (v0.15.6): 2 bug fixes, API/coordinator hardening, and the water-heater feature set.

**Architecture:** All changes are file-local ports into the existing HA custom integration `custom_components/hoval_connect`. No new abstractions; fork code is adapted to our current API design (v4 temporary-change, `DURATION_*` enums, `_pending_*` anti-flicker logic) — never the other way around. Fork source is available in-repo via the git remote `gmh` (`git show gmh/master:<path>`).

**Tech Stack:** Python 3.12+/3.13, Home Assistant custom component, pytest (asyncio_mode=auto), ruff. Tests run WITHOUT homeassistant installed — HA modules are stubbed via `sys.modules` MagicMock at the top of each test file (see `tests/test_api.py:14-28`). `aiohttp` and `voluptuous` ARE really installed.

## Global Constraints

- Branch: all work on `feature/fork-adoptions` (created in Task 1). Never commit to master.
- NEVER copy a fork file wholesale. The fork is missing our v0.15.x features (v4 temporary-change + `build_v4_temporary_change_body`, `DURATION_END_OF_PHASE`, `_pending_temperature`/`_pending_percentage` anti-flicker, PS circuit support, `BOILER_FA_STATES`, select-name disambiguation via `"<name> (<key>)"`, our retry/timeout design with `ClientTimeout(total=30)` and `_MAX_RETRIES=3`). Any fork hunk touching these must be adapted, not adopted.
- Verify after every task: `python -m pytest tests -q` (baseline: 75 passed) and `python -m ruff check custom_components tests` plus `python -m ruff format --check custom_components tests` must be clean.
- Commit after every task with a conventional-commit message ending in the trailer:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Translations: fork has no `de.json` — every new user-facing string needs entries in `strings.json`, `translations/en.json` AND `translations/de.json` (keep the three files' key sets in sync).
- Working dir: `c:\temp\hoval-connect-api` (Windows, PowerShell).

## Deliberately NOT adopted (do not "helpfully" add)

- number.py / weather-impact sliders + `get_circuit_settings` — fork admits it is unverified against a live device.
- Connection-health telemetry (~500 LOC) — deferred.
- Fork's conftest/test_coordinator_fetch.py — tests their rewritten coordinator; not portable.
- Fork's split connect/read timeouts, `_MAX_RETRIES=2`, 401-recursion — ours is better.
- Outer 90 s poll timeout — every request already has `total=30` + finite retries; poll duration is bounded.
- Fork's fan `percentage` source flip (live before setpoint), removal of select disambiguation, removal of climate `_pending_temperature`, removal of `DURATION_END_OF_PHASE`, `CIRCUIT_TYPE_PS: "Pool"` rename — all regressions vs our master.
- `CHANGELOG.md` / `render_readme` — our release workflow generates changelogs; render_readme is a HACS no-op.

---

### Task 1: Branch + Scan-Interval-Speicherbug (config_flow)

The HA frontend submits dict-select keys as strings; `vol.In(SCAN_INTERVAL_OPTIONS)` with int keys rejects `"60"`, so the option silently never saves.

**Files:**
- Modify: `custom_components/hoval_connect/config_flow.py:131,159`
- Create: `tests/test_config_flow.py`

**Interfaces:**
- Produces: `tests/test_config_flow.py` with a module-level real-voluptuous import guard that later tasks reuse (Task 3 adds source-contract tests to the same file).

- [ ] **Step 1: Create branch**

```powershell
git checkout -b feature/fork-adoptions
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_config_flow.py`:

```python
"""Tests for config-flow schema logic (no Home Assistant required)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# test_api.py/test_coordinator.py stub voluptuous with a MagicMock via
# sys.modules. This file needs the REAL voluptuous (it is installed as a test
# dep), so purge a stale mock before importing — order-independent either way.
if isinstance(sys.modules.get("voluptuous"), MagicMock):
    del sys.modules["voluptuous"]
import voluptuous as vol

from custom_components.hoval_connect.const import SCAN_INTERVAL_OPTIONS


def _scan_interval_schema() -> vol.Schema:
    """Mirror of the options-flow scan_interval validator (config_flow.py)."""
    return vol.Schema(
        {vol.Required("scan_interval"): vol.All(vol.Coerce(int), vol.In(SCAN_INTERVAL_OPTIONS))}
    )


class TestScanIntervalSchema:
    """The HA frontend submits dict-select keys as STRINGS ("60"), not ints."""

    def test_string_from_frontend_is_coerced_and_accepted(self):
        result = _scan_interval_schema()({"scan_interval": "60"})
        assert result["scan_interval"] == 60
        assert isinstance(result["scan_interval"], int)

    def test_int_value_accepted(self):
        assert _scan_interval_schema()({"scan_interval": 120})["scan_interval"] == 120

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
```

Note: `from custom_components.hoval_connect.const import ...` does NOT trigger the package `__init__.py`'s HA imports? It DOES (package import). If the import fails because homeassistant is missing and no other test file has stubbed it yet, add the same `sys.modules.setdefault` stub block used at the top of `tests/test_api.py:14-27` BEFORE the const import (copy it verbatim, but WITHOUT the voluptuous line).

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_config_flow.py -q`
Expected: FAIL — `test_scan_interval_uses_coerce` (assertion) and `test_string_from_frontend_is_coerced_and_accepted` is the behavior the fix enables (the schema mirror already passes; the source contract fails).

- [ ] **Step 4: Fix config_flow.py**

In `HovalConnectOptionsFlow.async_step_init` change line 131:

```python
        current_interval = int(self.config_entry.options.get(CONF_SCAN_INTERVAL, 60))
```

and change the validator (line 156-159):

```python
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=current_interval,
                    ): vol.All(vol.Coerce(int), vol.In(SCAN_INTERVAL_OPTIONS)),
```

- [ ] **Step 5: Run tests + lint**

Run: `python -m pytest tests -q` → all pass (78+). `python -m ruff check custom_components tests` + `python -m ruff format --check custom_components tests` → clean.

- [ ] **Step 6: Commit**

```powershell
git add custom_components/hoval_connect/config_flow.py tests/test_config_flow.py docs/superpowers/plans/2026-08-03-fork-adoptions.md
git commit -m "fix: scan-interval option not saved (frontend submits string keys)"
```

(Include the trailer line via a multi-line commit message here-string; same for all tasks.)

---

### Task 2: Climate — korrekte HK Live-Value-Keys + hvac_action-Quelle

Our own `sensor.py` already reads `roomTempTarget`/`status` — the API schema is `roomTemp*`/`status`. `climate.py` reads `actualTemperature`/`targetTemperature`/`circuitStatus`, so HK current_temperature is likely always None and hvac_action always IDLE.

**Files:**
- Modify: `custom_components/hoval_connect/climate.py:107-178`
- Modify: `tests/test_config_flow.py` (add source contracts) — or create `tests/test_source_contracts.py` (preferred; Task 5/11 extend it)
- KEEP: `_pending_temperature` logic and the `circuit.target_value` fallback — the fork removed both; do NOT.

**Interfaces:**
- Produces: `tests/test_source_contracts.py` with a `_read(module_name) -> str` helper later tasks reuse.

- [ ] **Step 1: Write failing source-contract tests**

Create `tests/test_source_contracts.py`:

```python
"""Source contracts: pin live-value keys and port decisions that runtime tests can't reach.

These read the component source as text. Crude, but they run without
homeassistant installed and fail loudly if a port regresses a key decision.
"""

from __future__ import annotations

import pathlib

_BASE = pathlib.Path("custom_components/hoval_connect")


def _read(module_name: str) -> str:
    return (_BASE / module_name).read_text(encoding="utf-8")


class TestClimateLiveValueKeys:
    def test_current_temperature_prefers_room_temp_actual(self):
        src = _read("climate.py")
        assert '"roomTempActual"' in src
        # legacy keys stay as fallbacks
        assert '"actualTemperature"' in src

    def test_target_temperature_prefers_room_temp_target(self):
        src = _read("climate.py")
        assert '"roomTempTarget"' in src
        assert '"targetTemperature"' in src

    def test_hvac_action_uses_status_key(self):
        src = _read("climate.py")
        assert 'live_values.get("status")' in src

    def test_pending_temperature_survived_the_port(self):
        assert "_pending_temperature" in _read("climate.py")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_source_contracts.py -q`
Expected: 3 FAIL (`roomTempActual`, `roomTempTarget`, `status`), 1 PASS (`_pending_temperature`).

- [ ] **Step 3: Edit climate.py**

`current_temperature` (replace the key lookup only, keep everything else):

```python
    @property
    def current_temperature(self) -> float | None:
        """Return the current room temperature.

        HK live values use 'roomTempActual' (same schema family as our
        roomTempTarget sensor); the other keys are legacy fallbacks.
        """
        circuit = self._circuit
        if circuit is None:
            return None
        for key in ("roomTempActual", "actualTemperature", "roomTemperature"):
            val = circuit.live_values.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return None
```

`target_temperature` — keep pending + `target_value` fallback, add the new key first:

```python
    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        # Show pending value immediately so the card does not snap back to
        # stale data during the in-flight API call + refresh window.
        if self._pending_temperature is not None:
            return self._pending_temperature
        circuit = self._circuit
        if circuit is None:
            return None
        val = circuit.live_values.get("roomTempTarget")
        if val is None:
            val = circuit.live_values.get("targetTemperature")
        if val is None:
            # Circuit-list `target_value` (also a setpoint, in degrees for HK).
            val = circuit.target_value
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None
```

`hvac_action` — change only the status line:

```python
        # Live values report the operating state under 'status' (our status
        # sensor reads the same key); 'circuitStatus' kept as legacy fallback.
        status = (
            circuit.live_values.get("status") or circuit.live_values.get("circuitStatus") or ""
        ).upper()
```

- [ ] **Step 4: Run tests + lint** — full suite green, ruff clean.

- [ ] **Step 5: Commit** — `fix: climate reads HK live values from roomTempActual/roomTempTarget/status`

---

### Task 3: Config-Flow-Härtung — Validierungs-Timeout + Reauth-Account-Pinning

**Files:**
- Modify: `custom_components/hoval_connect/config_flow.py`
- Modify: `custom_components/hoval_connect/strings.json`, `translations/en.json`, `translations/de.json` (error key `wrong_account`)
- Modify: `tests/test_source_contracts.py`

- [ ] **Step 1: Failing source contracts** (append to `tests/test_source_contracts.py`):

```python
class TestConfigFlowHardening:
    def test_validation_has_outer_timeout(self):
        src = _read("config_flow.py")
        assert "asyncio.timeout(_VALIDATION_TIMEOUT_S)" in src
        assert src.count("asyncio.timeout(_VALIDATION_TIMEOUT_S)") == 2  # user + reauth

    def test_reauth_pins_account(self):
        assert "wrong_account" in _read("config_flow.py")

    def test_wrong_account_translated_everywhere(self):
        import json

        for f in ("strings.json", "translations/en.json", "translations/de.json"):
            data = json.loads(_read(f))
            assert "wrong_account" in data["config"]["error"], f
```

- [ ] **Step 2: Run** — 3 FAIL expected.

- [ ] **Step 3: Edit config_flow.py**

Add `import asyncio` after `from __future__ import annotations`. Add module constant after `_LOGGER`:

```python
# Outer bound on credential validation. The per-request timeouts in the API
# client do not bound the whole get_plants() call (pagination loop, retries),
# and the config flow has no coordinator watchdog — without this a
# byte-dripping server hangs the setup dialog indefinitely.
_VALIDATION_TIMEOUT_S = 30
```

In `async_step_user`, wrap the validation:

```python
            try:
                async with asyncio.timeout(_VALIDATION_TIMEOUT_S):
                    await api.get_plants()
            except TimeoutError:
                _LOGGER.warning("Hoval validation timed out after %d s", _VALIDATION_TIMEOUT_S)
                errors["base"] = "cannot_connect"
            except HovalAuthError as err:
                ...  # unchanged
```

Replace the body of `async_step_reauth_confirm`'s `if user_input is not None:` block:

```python
        if user_input is not None:
            reauth_entry = self._get_reauth_entry()
            # Pin reauth to the original account: a reauth must not silently
            # rebind the entry to a different Hoval login (unique_id is the
            # lowercased account email since the first release).
            if user_input["email"].lower() != (reauth_entry.unique_id or "").lower():
                errors["base"] = "wrong_account"
            else:
                session = async_get_clientsession(self.hass)
                api = HovalConnectApi(session, user_input["email"], user_input["password"])

                try:
                    async with asyncio.timeout(_VALIDATION_TIMEOUT_S):
                        await api.get_plants()
                except TimeoutError:
                    _LOGGER.warning(
                        "Hoval reauth validation timed out after %d s", _VALIDATION_TIMEOUT_S
                    )
                    errors["base"] = "cannot_connect"
                except HovalAuthError:
                    errors["base"] = "invalid_auth"
                except HovalApiError:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data={
                            "email": user_input["email"],
                            "password": user_input["password"],
                        },
                    )
```

- [ ] **Step 4: Add the error string** to all three JSON files under `config.error`:
  - strings.json / en.json: `"wrong_account": "This entry belongs to a different Hoval account. Enter the credentials for {account}."` — HA doesn't interpolate here without placeholders config; keep it simple instead: `"wrong_account": "The email does not match the account this entry was set up with."`
  - de.json: `"wrong_account": "Die E-Mail-Adresse gehört nicht zu dem Konto, mit dem dieser Eintrag eingerichtet wurde."`
  (Also check `de.json`/`en.json` have the existing `cannot_connect`/`invalid_auth` keys — they do; just add the sibling.)

- [ ] **Step 5: Run tests + lint** — green/clean.

- [ ] **Step 6: Commit** — `feat: config-flow validation timeout + reauth account pinning`

---

### Task 4: Fan — HV-Clamp 15–100 % + sichtbare Debounce-Fehler

**Files:**
- Modify: `custom_components/hoval_connect/const.py` (append)
- Modify: `custom_components/hoval_connect/fan.py`
- Modify: `tests/test_coordinator.py` (clamp tests — pure function)

**Interfaces:**
- Produces: `clamp_hv_air_volume(percentage: float) -> int`, `HV_AIR_VOLUME_MIN = 15`, `HV_AIR_VOLUME_MAX = 100` in `const.py`.

- [ ] **Step 1: Failing tests** (append to `tests/test_coordinator.py`; `const.py` has no HA imports, so a direct import works under the existing stubs):

```python
from custom_components.hoval_connect.const import (  # noqa: E402
    HV_AIR_VOLUME_MAX,
    HV_AIR_VOLUME_MIN,
    clamp_hv_air_volume,
)


class TestClampHvAirVolume:
    """HA allows 1-14 %, the HV firmware band starts at 15 %."""

    def test_below_minimum_clamps_up(self):
        assert clamp_hv_air_volume(5) == HV_AIR_VOLUME_MIN
        assert clamp_hv_air_volume(14) == HV_AIR_VOLUME_MIN

    def test_zero_clamps_to_minimum(self):
        # 0 never reaches the clamp in fan.py (handled as turn_off first),
        # but the pure helper still has defined behavior.
        assert clamp_hv_air_volume(0) == HV_AIR_VOLUME_MIN

    def test_above_maximum_clamps_down(self):
        assert clamp_hv_air_volume(120) == HV_AIR_VOLUME_MAX

    def test_band_values_pass_through(self):
        for v in (15, 55, 100):
            assert clamp_hv_air_volume(v) == v

    def test_float_truncates_to_int(self):
        assert clamp_hv_air_volume(54.9) == 54
```

- [ ] **Step 2: Run** — ImportError expected.

- [ ] **Step 3: Append to const.py** (after the TURN_ON block):

```python
# HV (HomeVent) air-volume operating bounds, in percent.
# The cloud/firmware rejects (or undefined-behaves on) values below the device
# minimum; fan.py clamps requests into this band before sending.
# ponytail: 15 % minimum is GMH224's empirical device observation — adjust here
# if a HomeVent model with a different band shows up.
HV_AIR_VOLUME_MIN = 15
HV_AIR_VOLUME_MAX = 100


def clamp_hv_air_volume(percentage: float) -> int:
    """Clamp a requested HV air-volume percentage into the device's valid band.

    Pure helper (no HA imports) so it is directly unit-testable.
    """
    return int(max(HV_AIR_VOLUME_MIN, min(HV_AIR_VOLUME_MAX, percentage)))
```

- [ ] **Step 4: Edit fan.py**

Extend the `.const` import with `HV_AIR_VOLUME_MAX, HV_AIR_VOLUME_MIN, clamp_hv_air_volume` (alphabetical order, ruff isort).

`_send_percentage`: clamp before sending, KEEP all `_pending_percentage` logic (compare against the raw `percentage` exactly as today):

```python
    async def _send_percentage(self, percentage: int) -> None:
        """Actually send the percentage to the API (called after debounce).

        Keeps `_pending_percentage` set across the whole API call + refresh
        so the slider does not snap back to the stale, ~30-second-old
        coordinator data during the in-flight request. Clears it only after
        the refresh has fetched the new setpoint from Hoval.

        The value is clamped into the HV device band before sending: HA allows
        1-14 %, which the cloud rejects or handles undefined. 0 never reaches
        here (handled as turn_off in async_set_percentage).
        """
        clamped = clamp_hv_air_volume(percentage)
        if clamped != percentage:
            _LOGGER.debug(
                "Clamped requested air volume %d%% to device band %d-%d%% → %d%%",
                percentage,
                HV_AIR_VOLUME_MIN,
                HV_AIR_VOLUME_MAX,
                clamped,
            )
        try:
            await self.coordinator.async_control_and_refresh(
                self.coordinator.api.set_temporary_change(
                    self._plant_id,
                    self._circuit_path,
                    value=clamped,
                    duration=self._override_duration,
                ),
                ...  # rest unchanged
```

`_debounced_set` — make failures observable (our `_send_percentage` already reverts the pending value; only the log is missing):

```python
    async def _debounced_set(self, percentage: int) -> None:
        """Wait for debounce period, then send the latest percentage.

        Runs as a fire-and-forget task, so a raised HomeAssistantError would
        only reach the event loop's unhandled-task logger. Log it at WARNING
        instead — _send_percentage has already reverted the pending state.
        """
        await asyncio.sleep(DEBOUNCE_SECONDS)
        _LOGGER.debug("Debounce complete, sending %d%%", percentage)
        try:
            await self._send_percentage(percentage)
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Setting fan speed to %d%% failed for circuit %s: %s — "
                "the slider reverts to the device's actual value",
                percentage,
                self._circuit_path,
                err,
            )
```

- [ ] **Step 5: Run tests + lint** — green/clean.

- [ ] **Step 6: Commit** — `feat: clamp HV air volume to device band, surface debounce failures as WARNING`

---

### Task 5: Sensor — Negativ-Guard für Zähler + 6 neue Sensoren

**Files:**
- Modify: `custom_components/hoval_connect/sensor.py`
- Modify: `strings.json`, `translations/en.json`, `translations/de.json`
- Modify: `tests/test_source_contracts.py`

- [ ] **Step 1: Failing source contracts** (append):

```python
class TestSensorAdditions:
    def test_new_sensor_keys_exist(self):
        src = _read("sensor.py")
        for key in (
            "room_temp_actual",
            "operating_hours_el_heater",
            "operation_cycles_el_heater",
            "heat_amount_el_heater",
            "energy_el_heater",
            "el_heater_active",
        ):
            assert f'key="{key}"' in src, key

    def test_total_increasing_negative_guard(self):
        assert "TOTAL_INCREASING and num < 0" in _read("sensor.py")

    def test_new_keys_translated_everywhere(self):
        import json

        for f in ("strings.json", "translations/en.json", "translations/de.json"):
            sensors = json.loads(_read(f))["entity"]["sensor"]
            for key in ("room_temp_actual", "energy_el_heater", "el_heater_active"):
                assert key in sensors, f"{key} missing in {f}"
```

- [ ] **Step 2: Run** — FAIL expected.

- [ ] **Step 3: sensor.py — add descriptions**

After the `program_air_volume` entry (HK block starts line ~159), insert as first HK sensor:

```python
    HovalSensorEntityDescription(
        key="room_temp_actual",
        translation_key="room_temp_actual",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HK}),
        value_fn=lambda c: c.live_values.get("roomTempActual"),
    ),
```

After `operating_hours_over_50` (BL block), insert the 5 el-heater sensors exactly as in the fork (`gmh/master:custom_components/hoval_connect/sensor.py` lines 227-269): keys `operating_hours_el_heater` (h, TOTAL_INCREASING, icon `mdi:clock-outline` — note: fork uses `mdi:clock-electric-outline` which does not exist in MDI; use `mdi:clock-outline`), `operation_cycles_el_heater` (TOTAL_INCREASING, `mdi:counter`), `heat_amount_el_heater` (ENERGY, MWh, TOTAL_INCREASING), `energy_el_heater` (ENERGY, MWh, TOTAL_INCREASING), `el_heater_active` (icon `mdi:lightning-bolt`, EntityCategory.DIAGNOSTIC, no unit). All `circuit_types=frozenset({CIRCUIT_TYPE_BL})`, `value_fn` reading live_values keys `operatingHoursElHeater`, `operationCyclesElHeater`, `heatAmountElHeater`, `energyElHeater`, `elHeaterActive`.

- [ ] **Step 4: sensor.py — negative guard** in `HovalCircuitSensor.native_value` (replace the final `try/except`):

```python
        try:
            num = float(val)
        except (ValueError, TypeError):
            return None
        # Guard monotonic counters: a negative reading is never valid for a
        # TOTAL_INCREASING sensor and would be misread by HA's long-term
        # statistics as a meter reset, injecting a spurious spike. Drop it.
        if self.entity_description.state_class == SensorStateClass.TOTAL_INCREASING and num < 0:
            return None
        return num
```

- [ ] **Step 5: Translations** — add to `entity.sensor` in all three files:

strings.json / en.json:
```json
"room_temp_actual": { "name": "Room temperature" },
"operating_hours_el_heater": { "name": "Operating hours electric heater" },
"operation_cycles_el_heater": { "name": "Switching cycles electric heater" },
"heat_amount_el_heater": { "name": "Heat produced electric heater" },
"energy_el_heater": { "name": "Energy consumed electric heater" },
"el_heater_active": { "name": "Electric heater active" }
```
de.json:
```json
"room_temp_actual": { "name": "Raumtemperatur" },
"operating_hours_el_heater": { "name": "Betriebsstunden Elektroheizeinsatz" },
"operation_cycles_el_heater": { "name": "Schaltzyklen Elektroheizeinsatz" },
"heat_amount_el_heater": { "name": "Wärmemenge Elektroheizeinsatz" },
"energy_el_heater": { "name": "Energieverbrauch Elektroheizeinsatz" },
"el_heater_active": { "name": "Elektroheizeinsatz aktiv" }
```
(de.json mirrors the existing German style — check neighboring keys and match tone.)

- [ ] **Step 6: Run tests + lint, commit** — `feat: HK room temperature + electric-heater sensors, guard negative counter readings`

---

### Task 6: API — Pagination-Wrapper-Normalisierung + get_plants-Pagination

Hardening against Hoval's May-2026 Spring-Page wrapper `{"content": [...], "last": bool}`. Highest-priority hardening item.

**Files:**
- Modify: `custom_components/hoval_connect/api.py` (5 methods + 1 constant)
- Modify: `tests/test_api.py` (port ~12 fork tests)

**Interfaces:**
- `get_plants()` now loops pages; return type unchanged (`list[dict]`).
- `get_circuits`/`get_live_values`/`get_events` normalize to `list` (possibly empty); `get_latest_event` normalizes to `dict` (possibly `{}`). Coordinator callers keep working unchanged — `get_latest_event` returning `{}` is falsy, same as today's `None` handling (`latest_result` truthiness check in coordinator.py:454).
- Do NOT add `plant_id=` to the event requests (fork passes it; ours doesn't — changing auth headers is out of scope).

- [ ] **Step 1: Port the failing tests.** Append to `tests/test_api.py` — port these from `git show gmh/master:tests/test_api.py` (adapt: our `_make_response`/`_make_session` helpers already exist and match; strip any `plant_id` header assertions):
  - `test_get_plants_paginated_single_page` (wrapper with `"last": True` → content returned)
  - `test_get_plants_paginated_multiple_pages` (2 pages via `side_effect`, `"last": False` then `True` → concatenated; assert `session.request.call_count == 2`)
  - `test_get_circuits_paginated_wrapper`, `test_get_live_values_paginated_wrapper`, `test_get_live_values_none_returns_empty_list`
  - class `TestEventEndpointNormalisation` (8 tests: plain list passthrough, wrapper extraction, wrapper with non-list content → `[]`, non-list → `[]`; latest: plain dict passthrough, wrapper takes first element, wrapper with empty content → `{}`, non-dict → `{}`)
  - class `TestGetPlantsPageCap` (`test_endless_pagination_is_truncated`: server always answers `"last": False` with 1-plant content → result length == `_MAX_PLANT_PAGES`; `test_cap_does_not_affect_normal_pagination`)

- [ ] **Step 2: Run** — new tests FAIL.

- [ ] **Step 3: Edit api.py.** Add constant near the retry config:

```python
# Hard upper bound on my-plants pagination. 50 pages x 12 plants/page = 600
# plants — far beyond any real account. Without a cap, a server that keeps
# answering `"last": false` would loop get_plants() forever; the config-flow
# validation path (30 s outer timeout) is the tightest caller, but the cap
# belongs in the client.
_MAX_PLANT_PAGES = 50
```

Replace `get_plants` with the fork's pagination loop verbatim (`gmh/master:custom_components/hoval_connect/api.py`, see plan appendix hunk — plain-list passthrough, `content` extraction with isinstance guards, `last`-flag loop, `_MAX_PLANT_PAGES` cap with WARNING).

Replace `get_circuits` / `get_live_values` bodies: after the existing `_request` call,

```python
        if isinstance(result, dict):
            _LOGGER.debug(
                "get_circuits returned paginated wrapper for plant %s; extracting 'content'",
                plant_id,
            )
            return result.get("content", [])
        return result if isinstance(result, list) else []
```

(analog for live_values with circuit_path in the log message). `get_events`:

```python
        result = await self._request("GET", f"/v1/plant-events/{plant_id}")
        if isinstance(result, dict):
            content = result.get("content", [])
            return content if isinstance(content, list) else []
        return result if isinstance(result, list) else []
```

`get_latest_event`:

```python
        result = await self._request("GET", f"/v1/plant-events/latest/{plant_id}")
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            content = result["content"]
            return content[0] if content and isinstance(content[0], dict) else {}
        return result if isinstance(result, dict) else {}
```

Keep each method's existing docstring and extend it with one sentence about the wrapper normalization.

- [ ] **Step 4: Run full suite + lint.** Also confirm the two pre-existing `get_plants` tests still pass (they mock a plain-list response → passthrough branch).

- [ ] **Step 5: Commit** — `feat: normalize Spring-Page wrappers on all list endpoints, paginate get_plants with cap`

---

### Task 7: Coordinator — defensiver Programm-Resolver + Guards in _fetch_circuit

A single malformed program/live-value field must degrade only its own fields, never drop the whole circuit (our documented BL-circuit incident class).

**Files:**
- Modify: `custom_components/hoval_connect/coordinator.py` (`_resolve_active_program_value`, `_fetch_circuit`)
- Modify: `tests/test_coordinator.py` (port fork's robustness tests)

**Interfaces:**
- `_resolve_active_program_value(programs: dict[str, Any] | None, now, active_program=None)` — signature widens to accept `None`; still returns the same 3-tuple.

- [ ] **Step 1: Port failing tests.** From `git show gmh/master:tests/test_coordinator.py` port class `TestResolveActiveProgramRobustness` (lines ~755-884; ~12 tests: `None`, `[]`, non-dict programs; day_configs entries without `id`; non-dict week; missing `start`/`end`; non-numeric times; non-list phases/dayProgramIds — each asserting no exception and sensible `(None, ...)` results). Adapt imports to our file's existing style (top-of-file imports already present).

- [ ] **Step 2: Run** — most new tests ERROR (KeyError/AttributeError) against our current resolver.

- [ ] **Step 3: Replace `_resolve_active_program_value`** with the fork's defensive version (plan appendix / `gmh/master:...coordinator.py` lines 52-131): isinstance guards on `programs`, `dayPrograms`, `dayConfigurations`, week dict, `dayProgramIds` list, phases list, per-phase dict/start/end guards, `int()` conversion in try/except. Keep our docstring's week1/week2 explanation (both trees share the same week-selection logic).

- [ ] **Step 4: Harden `_fetch_circuit`:**

Live-values comprehension (replace line ~398):

```python
                    if not isinstance(results[0], BaseException):
                        lv_raw = results[0]
                        # api.get_live_values() already normalises the wrapper;
                        # one lightweight guard against future shape regressions.
                        if not isinstance(lv_raw, list):
                            _LOGGER.warning(
                                "Live-values for %s returned unexpected type %s; treating as empty",
                                path,
                                type(lv_raw).__name__,
                            )
                            lv_raw = []
                        circuit_data.live_values = {
                            v["key"]: v["value"]
                            for v in lv_raw
                            if isinstance(v, dict) and "key" in v and "value" in v
                        }
```

Programs block: change the condition from `not isinstance(programs, BaseException)` to `isinstance(programs, dict)` (handles `None`/`[]` from non-programmable circuits), wrap the whole resolution in an isolation barrier, and guard the week-name extraction:

```python
                    programs = results[1]
                    if isinstance(programs, dict):
                        if need_programs:
                            self._program_cache[path] = (programs, time.time())
                        # Isolation barrier: any residual exception here must
                        # degrade the program fields only — never propagate out
                        # of _fetch_circuit, which would discard the whole
                        # circuit (incl. its live values) via
                        # gather(return_exceptions=True).
                        try:
                            now = dt_util.now()
                            week_name, day_name, phase_value = _resolve_active_program_value(
                                programs, now, circuit_data.active_program
                            )
                            circuit_data.active_week_name = week_name
                            circuit_data.active_day_program_name = day_name
                            circuit_data.program_air_volume = phase_value
                            w1 = programs.get("week1")
                            w2 = programs.get("week2")
                            if isinstance(w1, dict) and w1.get("name"):
                                circuit_data.program_names["week1"] = w1["name"]
                            if isinstance(w2, dict) and w2.get("name"):
                                circuit_data.program_names["week2"] = w2["name"]
                        except Exception:  # noqa: BLE001 — see isolation note
                            _LOGGER.warning(
                                "Program data for circuit %s could not be parsed; "
                                "program sensors will be unknown this cycle "
                                "(live values are unaffected)",
                                path,
                                exc_info=True,
                            )
                    elif isinstance(programs, BaseException):
                        _LOGGER.debug("Programs not available for %s: %s", path, programs)
                    else:
                        _LOGGER.debug(
                            "Programs endpoint for %s returned %r (type=%s); skipping",
                            path,
                            programs,
                            type(programs).__name__,
                        )
```

Note: non-dict `programs` must NOT be cached (the current code caches whatever came back; with the isinstance-dict condition, caching stays inside the dict branch — verify this holds after the edit).

- [ ] **Step 5: Run full suite + lint, commit** — `feat: defensive program resolver + circuit isolation barriers in coordinator`

---

### Task 8: API — Single-Flight-Token-Locks

Concurrent 401s from the parallel circuit fan-out currently trigger parallel IDP logins.

**Files:**
- Modify: `custom_components/hoval_connect/api.py` (`__init__`, `_get_id_token`, `_get_plant_access_token`)
- Modify: `tests/test_api.py`

- [ ] **Step 1: Failing test** (append to `TestHovalConnectApiAuth`):

```python
    @pytest.mark.asyncio
    async def test_concurrent_id_token_requests_single_flight(self):
        session = _make_session()
        resp = _make_response(200, {"id_token": "test-token-123"})
        session.post = MagicMock(return_value=resp)

        api = HovalConnectApi(session, "test@example.com", "password123")
        tokens = await _real_asyncio.gather(*(api._get_id_token() for _ in range(5)))

        assert set(tokens) == {"test-token-123"}
        # Without the single-flight lock every concurrent caller fires its
        # own IDP login; with it, exactly one request goes out.
        assert session.post.call_count == 1
```

(`_real_asyncio` already exists at the top of test_api.py. Note: with a shared mock response object the awaits may or may not interleave — the AsyncMock `json` yields control, so without a lock `call_count` > 1. Verify the test actually fails on master before fixing; if it doesn't reliably fail, make `resp.json` an async function that does `await _real_asyncio.sleep(0)` before returning.)

- [ ] **Step 2: Run** — FAIL (call_count > 1).

- [ ] **Step 3: Edit api.py.** In `__init__` add:

```python
        # Single-flight locks: the coordinator fans out one task per circuit,
        # so a burst of concurrent 401s must trigger at most ONE token refresh
        # instead of a thundering herd against the rate-limited IDP. Separate
        # locks because _get_plant_access_token() calls _get_id_token().
        self._id_token_lock = asyncio.Lock()
        self._pat_lock = asyncio.Lock()
```

Wrap the refresh paths in double-checked locking — keep our request bodies exactly as they are (no timeout changes):

```python
    async def _get_id_token(self) -> str:
        """Get or refresh the ID token via OAuth2 password grant.

        Double-checked locking: the fast path returns the cached token without
        the lock; only a refresh serialises through _id_token_lock.
        """
        if self._id_token and time.time() < self._id_token_exp:
            return self._id_token

        async with self._id_token_lock:
            if self._id_token and time.time() < self._id_token_exp:
                return self._id_token
            # ... existing request/validation body, indented one level ...
```

Same pattern for `_get_plant_access_token` with `self._pat_lock` and the `self._pat_cache.get(plant_id)` re-check inside the lock.

- [ ] **Step 4: Run full suite + lint, commit** — `feat: single-flight locks for token refresh`

---

### Task 9: Coordinator — Override-Lifecycle (TTL, Clear am Ende, nicht-blockierender Refresh)

Today `_mode_override.clear()` runs at the START of `_async_update_data` — a failed refresh snaps entities back to stale data. And `async_control_and_refresh` holds the lock through `sleep(2)` + refresh, blocking the caller.

**Files:**
- Modify: `custom_components/hoval_connect/coordinator.py`
- Modify: `tests/test_source_contracts.py`

- [ ] **Step 1: Failing source contracts** (append):

```python
class TestOverrideLifecycle:
    def test_override_has_ttl(self):
        src = _read("coordinator.py")
        assert "_MODE_OVERRIDE_TTL_S" in src

    def test_clear_not_at_start_of_update(self):
        src = _read("coordinator.py")
        body = src.split("async def _async_update_data", 1)[1]
        first_stmt_zone = body[:400]
        assert "_mode_override.clear()" not in first_stmt_zone
        assert "_mode_override.clear()" in body
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Edit coordinator.py.**

Module constant (near `SIGNAL_NEW_CIRCUITS`):

```python
# Maximum lifetime of an optimistic mode override (seconds). Overrides are
# normally cleared at the end of the next successful poll, but if polls keep
# failing an override must not mask the device's real state indefinitely.
_MODE_OVERRIDE_TTL_S = 120.0
```

`__init__`: `self._mode_override: dict[str, tuple[str, float]] = {}` (update the comment: cleared at the END of the next successful poll or after TTL).

```python
    def set_mode_override(self, circuit_path: str, mode: str) -> None:
        """Set optimistic mode override after a control action."""
        self._mode_override[circuit_path] = (mode, time.monotonic())

    def get_mode_override(self, circuit_path: str) -> str | None:
        """Get the optimistic mode override for a circuit.

        Returns None once the override exceeds _MODE_OVERRIDE_TTL_S so a stale
        optimistic value cannot mask the device's real state when polls fail.
        """
        entry = self._mode_override.get(circuit_path)
        if entry is None:
            return None
        mode, ts = entry
        if time.monotonic() - ts > _MODE_OVERRIDE_TTL_S:
            self._mode_override.pop(circuit_path, None)
            return None
        return mode
```

`async_control_and_refresh` (fork design, our style):

```python
    async def async_control_and_refresh(
        self,
        coro: Any,
        circuit_path: str,
        mode_override: str,
    ) -> None:
        """Execute a control command with lock, optimistic state, and refresh.

        The API call and optimistic override are serialised inside
        control_lock; the refresh runs as a fire-and-forget background task
        OUTSIDE the lock (with a 2 s settle delay) so the calling entity
        method returns promptly and a slow refresh cannot starve the lock.
        A failed background refresh is dropped — the coordinator retries on
        its normal poll schedule and entities stay on their optimistic state.
        """
        async with self.control_lock:
            await coro
            self.set_mode_override(circuit_path, mode_override)

        async def _do_refresh() -> None:
            await asyncio.sleep(2)
            try:
                await self.async_request_refresh()
            except Exception:  # noqa: BLE001 — see docstring
                _LOGGER.debug(
                    "Post-control refresh failed for %s; coordinator will retry on next poll",
                    circuit_path,
                )

        self.hass.async_create_task(_do_refresh())
```

`_async_update_data`: DELETE the two lines at the top (`# Clear optimistic overrides...` + `self._mode_override.clear()`); add immediately before `return data` at the end:

```python
        # Clear optimistic overrides only after a SUCCESSFUL fetch — fresh data
        # replaces them. Clearing at the start meant a failed refresh snapped
        # entities back to stale pre-override data.
        self._mode_override.clear()
        return data
```

- [ ] **Step 4: Run full suite + lint, commit** — `feat: override TTL, clear after successful poll, non-blocking post-control refresh`

---

### Task 10: Coordinator — Events-/Weather-Cache + Events-Isolationsbarriere

Saves 2-3 plant-level requests per 60 s poll; a shape surprise in event parsing must not fail the whole poll.

**Files:**
- Modify: `custom_components/hoval_connect/const.py` (2 TTLs)
- Modify: `custom_components/hoval_connect/coordinator.py` (`__init__`, plant-level section of `_async_update_data`)
- Modify: `tests/test_coordinator.py`

- [ ] **Step 1: Failing test** (append to test_coordinator.py; timedelta is importable — const has no HA deps):

```python
from datetime import timedelta as _timedelta  # noqa: E402

from custom_components.hoval_connect.const import (  # noqa: E402
    EVENTS_CACHE_TTL,
    PROGRAM_CACHE_TTL,
    WEATHER_CACHE_TTL,
)


class TestCacheTtls:
    def test_events_ttl_longer_than_fastest_poll(self):
        assert EVENTS_CACHE_TTL >= _timedelta(minutes=1)

    def test_weather_ttl_longer_than_events(self):
        assert WEATHER_CACHE_TTL > EVENTS_CACHE_TTL

    def test_program_ttl_unchanged(self):
        assert PROGRAM_CACHE_TTL == _timedelta(minutes=5)
```

- [ ] **Step 2: Run** — ImportError.

- [ ] **Step 3: const.py** — after `PROGRAM_CACHE_TTL`:

```python
# Plant-level cache TTLs — weather and events are slow-changing, so fetching
# them on every (default 60 s) poll wastes round-trips and risks rate limits.
# They refresh on their own cadence; the last good value is reused in between.
WEATHER_CACHE_TTL = timedelta(minutes=15)
EVENTS_CACHE_TTL = timedelta(minutes=3)
```

- [ ] **Step 4: coordinator.py.** Import the TTLs. `__init__` additions:

```python
        # Plant-level caches: (parsed value(s), monotonic timestamp)
        self._weather_cache: dict[str, tuple[HovalWeatherData | None, float]] = {}
        self._weather_cache_ttl = WEATHER_CACHE_TTL.total_seconds()
        self._events_cache: dict[str, tuple[HovalEventData | None, list[HovalEventData], float]] = {}
        self._events_cache_ttl = EVENTS_CACHE_TTL.total_seconds()
```

Replace the plant-level task scheduling + processing (currently lines 426-490, the fixed `latest_idx`/`events_idx`/`weather_idx` block) with the fork's conditional structure — port `gmh/master:custom_components/hoval_connect/coordinator.py` lines 1388-1511 verbatim except: our variable names (`all_tasks`, `plant_data`) already match; keep `num_circuits = len(all_tasks)` before appending. Key parts:
  - `need_events` / `need_weather` staleness checks against `time.monotonic()`
  - tasks appended only when needed, indices tracked as `latest_idx = events_idx = weather_idx = None`
  - events parsing wrapped in `try/except Exception` (isolation barrier) with isinstance guards (`isinstance(latest_result, dict) and latest_result`; `isinstance(events_result, list)`; per-event `isinstance(ev, dict)` in the `[:10]` slice)
  - cache write only on non-empty parse; on a miss reuse the previous cache instead of wiping (`elif events_cached is not None: parsed_latest, parsed_events, _ = events_cached`)
  - weather: guard `isinstance(weather_result[0], dict)`, same cache-or-reuse pattern
  - `plant_data.latest_event` / `plant_data.events` / `plant_data.weather` assigned from the parsed-or-cached values; `has_error` logic preserved (`_is_problem_event` over latest + list)

- [ ] **Step 5: Run full suite + lint** (the existing event-related coordinator tests are pure `_parse_event`/`_is_problem_event` tests — unaffected).

- [ ] **Step 6: Commit** — `feat: cache events (3 min) and weather (15 min), isolate event parsing from the poll`

---

### Task 11: water_heater-Plattform für WW-Kreise

**Files:**
- Create: `custom_components/hoval_connect/water_heater.py`
- Modify: `custom_components/hoval_connect/__init__.py` (PLATFORMS)
- Modify: `custom_components/hoval_connect/services.yaml` (target domain)
- Modify: `strings.json`, `translations/en.json`, `translations/de.json`
- Modify: `tests/test_source_contracts.py`

**Interfaces:**
- Consumes (all exist on master, verified): `circuit_device_info`, `HovalConnectConfigEntry`, `SIGNAL_NEW_CIRCUITS`, `HovalCircuitData`, `HovalDataCoordinator.get_mode_override/async_control_and_refresh`, `api.set_temporary_change(plant_id, circuit_path, value, duration)`, `api.set_program`, `api.reset_circuit`, `CIRCUIT_TYPE_WW`, `OPERATION_MODE_REGULAR/STANDBY`, `DURATION_MIDNIGHT`.
- Decision: NO new `reset_ww_boost` service — our existing `hoval_connect.reset_temporary_change` already resolves any circuit-bound entity; we just add `water_heater` to its target domains.

- [ ] **Step 1: Failing source contracts** (append):

```python
class TestWaterHeater:
    def test_platform_registered(self):
        assert "Platform.WATER_HEATER" in _read("__init__.py")

    def test_uses_v4_duration_constant_not_lowercase_midnight(self):
        src = _read("water_heater.py")
        assert "DURATION_MIDNIGHT" in src
        assert '"midnight"' not in src  # fork's v3 literal would silently degrade on our v4 body builder

    def test_no_duplicate_reset_service(self):
        assert "reset_ww_boost" not in _read("water_heater.py")

    def test_translated_everywhere(self):
        import json

        for f in ("strings.json", "translations/en.json", "translations/de.json"):
            data = json.loads(_read(f))
            assert "hot_water" in data["entity"]["water_heater"], f
```

- [ ] **Step 2: Run** — FAIL (file missing).

- [ ] **Step 3: Create water_heater.py.** Base: `git show gmh/master:custom_components/hoval_connect/water_heater.py`, with these exact deviations:
  1. Import block: drop `SERVICE_RESET_WW_BOOST`, add `DURATION_MIDNIGHT` to the `.const` imports; drop `async_get_current_platform` from the entity_platform import.
  2. In `async_setup_entry`: DELETE the `platform = async_get_current_platform()` + `platform.async_register_entity_service(...)` block (our generic service covers reset).
  3. DELETE the `async_reset_temporary_change` method (dead without the entity service).
  4. In `async_set_temperature`: `duration=DURATION_MIDNIGHT` instead of `duration="midnight"` (the fork's lowercase v3 literal would hit our v4 body builder's unknown-option fallback and end the boost at the next phase boundary instead of midnight).
  5. Drop the unused `self._entry` assignment? NO — keep constructor signature identical to fork for symmetry with climate.py (which also stores `_entry`); it's harmless. (If ruff flags it, keep it — ruff doesn't flag unused attributes.)
  Everything else verbatim: `WW_MIN_TEMP = 10.0`, `WW_MAX_TEMP = 65.0`, `WW_TEMP_STEP = 0.5`, operation modes heat_pump/high_demand/off, `current_temperature` from `tempSf1Actual`→`tempActual`, `target_temperature` from `tempTarget`, `current_operation` incl. `temporaryChangeActive == "true"` boost detection, `async_set_operation_mode` (off→`set_program(standby)`, heat_pump/high_demand→`reset_circuit`).

- [ ] **Step 4: __init__.py** — add `Platform.WATER_HEATER` to `PLATFORMS` (alphabetical: after SENSOR).

- [ ] **Step 5: services.yaml** — extend the domain list:

```yaml
        domain:
          - fan
          - climate
          - water_heater
```

Also update the service description sentence in `services.yaml` and `strings.json`/`en.json`/`de.json` (`services.reset_temporary_change.description`) to mention hot-water circuits, e.g. append ", or hot-water water_heater" in the parenthetical.

- [ ] **Step 6: Translations** — add to all three files:

```json
"water_heater": {
  "hot_water": { "name": "Hot water" }
}
```

de.json: `"name": "Warmwasser"`.

- [ ] **Step 7: Run full suite + lint, commit** — `feat: water_heater platform for WW circuits (setpoint boost, standby, program resume)`

---

### Task 12: Select — WW-Freischaltung + VALID_API_PROGRAMS-Guard

**Files:**
- Modify: `custom_components/hoval_connect/select.py`
- Modify: `tests/test_source_contracts.py`

KEEP our `_display_name`/`_api_key_from_display` disambiguation (`"<default> (<api_key>)"`) — the fork's simpler variant reintroduces duplicate option lists.

- [ ] **Step 1: Failing source contracts** (append):

```python
class TestProgramSelect:
    def test_ww_circuits_get_select(self):
        assert "CIRCUIT_TYPE_WW" in _read("select.py")

    def test_program_key_validated_before_send(self):
        src = _read("select.py")
        assert "VALID_API_PROGRAMS" in src

    def test_disambiguation_survived(self):
        assert '({api_key})' in _read("select.py") or '" ({key})"' in _read("select.py") or 'f"{default} ({api_key})"' in _read("select.py")
```

(The third assert pins our disambiguation format string — check the exact literal in select.py:108 (`f"{default} ({api_key})"`) and assert that.)

- [ ] **Step 2: Run** — first two FAIL.

- [ ] **Step 3: Edit select.py.**

Import `CIRCUIT_TYPE_WW`. After `API_PROGRAMS` add:

```python
# Full set of program identifiers the cloud accepts on the programs endpoint.
# A resolved key is validated against this before sending so an unmapped
# display string can't be forwarded verbatim to the API (→ HTTP 400).
VALID_API_PROGRAMS = frozenset(
    {"week1", "week2", "ecoMode", "standby", "constant", "manual", "externalConstant"}
)
```

Setup filter:

```python
                if (
                    circuit.circuit_type
                    not in (CIRCUIT_TYPE_HV, CIRCUIT_TYPE_HK, CIRCUIT_TYPE_WW)
                    or uid in known
                ):
                    continue
```

`async_select_option` — insert after `api_program = self._api_key_from_display(option)`:

```python
        if api_program not in VALID_API_PROGRAMS:
            raise HomeAssistantError(
                f"Unknown program '{option}' (resolved to '{api_program}'); "
                f"valid programs: {', '.join(sorted(VALID_API_PROGRAMS))}"
            )
```

- [ ] **Step 4: Run full suite + lint, commit** — `feat: program select for WW circuits, validate program key before sending`

---

### Task 13: Finale Verifikation

- [ ] **Step 1:** `python -m pytest tests -q --cov` — all green, coverage ≥ 25 (gate).
- [ ] **Step 2:** `python -m ruff check custom_components tests` and `python -m ruff format --check custom_components tests` — clean.
- [ ] **Step 3:** `git log --oneline master..HEAD` — one commit per task, each with the trailer.
- [ ] **Step 4:** Grep sanity: `git grep -n '"midnight"' custom_components` → no hits; `git grep -n 'reset_ww_boost'` → no hits.
- [ ] **Step 5:** Report: list of adopted items, skipped items (with reasons), test count before/after. Do NOT push, do NOT merge, do NOT tag — master stays untouched until the user reviews.
