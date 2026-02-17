# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Reverse-engineered API documentation and **Home Assistant custom integration** for the Hoval Connect IoT platform. Hoval Connect is a cloud platform connecting Hoval HVAC systems (heating, ventilation, hot water) via IoT gateways to Azure IoT Hub.

## Repository Structure

- `README.md` — API documentation + HA integration install instructions
- `examples/` — Standalone Python and Bash API client examples
- `custom_components/hoval_connect/` — Home Assistant integration (HACS-compatible)
- `docs/openapi-v3.json` — Full OpenAPI 3.1 spec (~450KB, fetched from `/v3/api-docs`)
- `tests/` — Unit tests (pure function tests, run without HA installed)
- `hacs.json` — HACS repository metadata
- `.github/workflows/` — CI: HACS/Hassfest validation, Ruff linting, automated releases on tags
- `pyproject.toml` — Ruff linter config (Python 3.12+, 100-char lines)

## Home Assistant Integration

The integration lives in `custom_components/hoval_connect/`. User setup is email + password only — plants and circuits are discovered automatically from the Hoval account at runtime.

### Key files

- `api.py` — Async aiohttp client: 2-step auth (ID token + Plant Access Token), auto-refresh with TTL caching, automatic token retry on 401, request timeout (30s), robust error handling with `HovalAuthError`/`HovalApiError` exception hierarchy. Handles HTTP 204 No Content for PUT control endpoints.
- `coordinator.py` — `DataUpdateCoordinator`: polls `get_plants()` → `get_circuits()` → parallel `get_live_values()` + `get_programs()` + `get_events()` + `get_weather()`. Skips API calls when plant is offline, invalidates PAT cache on reconnect. Program cache (5min TTL) reduces API calls. Provides `control_lock` (asyncio.Lock) to serialize control commands, `resolve_fan_speed()` helper for smart fan speed resolution, and `_V1_PROGRAM_MAP` to normalize v1 `activeProgram` values (`tteControlled` → `week1`) to v3 enum keys. Emits `SIGNAL_NEW_CIRCUITS` for dynamic entity discovery.
- `config_flow.py` — UI config flow (email/password) + reauth flow + options flow (turn-on mode, override duration, polling interval)
- `climate.py` — Climate entity for HK heating circuits: target temperature, HVAC modes (heat/auto/off), HVAC action from circuit status. API errors wrapped in `HomeAssistantError`. Only created for HK circuits.
- `fan.py` — Fan entity for HV ventilation: 0–100% speed slider (`FanEntityFeature.SET_SPEED`), on/off toggle (standby ↔ temporary-change), debounced slider input (1.5s), proper cleanup via `async_will_remove_from_hass`. Only created for HV circuits.
- `select.py` — Select entity for program selection (week1/week2/ecoMode/standby/constant). Shows user-defined program names from the API (`circuit.program_names`), falls back to `DEFAULT_NAMES`. Bidirectional mapping via `_display_name()` / `_api_key_from_display()`. Only created for HV/HK circuits.
- `sensor.py` — 9 sensor entities per circuit (outside temp, exhaust temp, air volume, humidity actual/target, operation mode, active week/day program, program air volume) + 6 plant-level sensors (latest event type/message/time, active event count, weather condition/temperature). Diagnostic sensors use `EntityCategory.DIAGNOSTIC`.
- `binary_sensor.py` — 2 binary sensors per plant (online status with connectivity class, error/warning status with problem class — triggers on active blocking/locking/warning events)
- `diagnostics.py` — Diagnostic data export with automatic PII redaction
- `const.py` — Constants: API URLs, OAuth client ID, token TTLs (25min ID, 12min PAT), polling interval (configurable, default 60s), circuit types + human-readable names, operation modes, duration enums (FOUR/MIDNIGHT)
- `__init__.py` — Entry setup, runtime data, platform forwarding (binary_sensor, climate, fan, select, sensor), DeviceInfo helpers (`plant_device_info`, `circuit_device_info`), options update listener for dynamic polling interval changes

### Entity architecture

- Entities use `CoordinatorEntity` — no direct API calls, all data comes from the coordinator
- Device hierarchy: one parent device per plant, one child device per plant+circuit (linked via `via_device`)
- Circuit devices identified by `{plantId}_{circuitPath}`
- Supports HV (ventilation) and HK (heating) circuit types (`SUPPORTED_CIRCUIT_TYPES` in `const.py`)
- Fan entity uses `coordinator.control_lock` to serialize API control commands (prevents race conditions)
- Fan speed resolution uses smart fallback chain: live airVolume → targetAirVolume → program air volume → default 40% (API rejects value=0)
- DeviceInfo construction centralized in `__init__.py` helper functions, used by all entity platforms
- All entity platforms use `translation_key` for entity names (not hardcoded `_attr_name`)
- Dynamic entity discovery: all platforms listen to `SIGNAL_NEW_CIRCUITS` dispatcher signal to add entities at runtime without restart
- v1 API `activeProgram` values (`tteControlled`, `timePrograms`, etc.) are normalized to v3 enum keys (`week1`, `week2`, `ecoMode`, `standby`, `constant`) via `_V1_PROGRAM_MAP` in the coordinator

## Running Tests

```bash
python -m pytest tests/ -v
```

## Running Examples

```bash
python examples/hoval_client.py <email> <password>
./examples/get-live-values.sh <email> <password> <plantId> <circuitPath> <circuitType>
```

## Authentication Architecture (2-step)

1. **ID Token**: OAuth2 password grant to SAP IAS. Use `id_token` from response, NOT `access_token`. Lifetime: 30min.
2. **Plant Access Token (PAT)**: Fetch via `GET /v1/plants/{plantId}/settings`. Send as `X-Plant-Access-Token` header. Lifetime: ~15min.

## API Base URL

`https://azure-iot-prod.hoval.com/core`

## Key Endpoint Patterns

- `/api/` endpoints need only the id_token (`Authorization: Bearer`)
- `/v1/plants/`, `/v2/api/`, `/v3/` endpoints also require `X-Plant-Access-Token`
- `/business/` endpoints require elevated (partner) access — regular users get 403

## Circuit Types

HK (heating), BL (boiler), WW (warm water), FRIWA (fresh water), HV (ventilation), SOL (solar), SOLB (solar buffer), PS (pool), GW (gateway)

## API Behavior Notes

- Control endpoints return HTTP 204 No Content on success — no response body
- `temporary-change` uses POST with `?duration=FOUR|MIDNIGHT&value={airVolume}` — duration is an **enum** (FOUR = 4 hours, MIDNIGHT = until midnight), NOT a free-form number. Sets air volume/temperature override while keeping time program active.
- `temporary-change/reset` uses POST (no body) to cancel an active override
- `constant` mode (PUT) returns HTTP 500 when a time program (`tteControlled`) is active — use `temporary-change` instead
- `standby`, `manual`, `reset` use POST (no body)
- API always reports `operationMode='REGULAR'` regardless of actual device state — optimistic override needed for standby tracking
- v1 `activeProgram` enum: `constant`, `nightReduction`, `dayCooling`, `timePrograms`, `standby`, `manual`, `externalConstant`, `tteControlled`
- v3 `activeProgram` enum: `constant`, `ecoMode`, `standby`, `week1`, `week2`, `manual`, `externalConstant`
- The integration fetches circuits via v1 but controls via v3 — coordinator normalizes v1 values to v3 via `_V1_PROGRAM_MAP`
- Weather forecast available via `get_weather()` — returns condition + temperature
- `PlantEventDTO` fields: `eventType`, `description`, `timeOccurred`, `timeResolved`, `sourcePath`, `code`, `module`, `functionGroup`, `function`, `category` — event is active when `timeResolved` is null
- Event types: `locking`, `blocking`, `warning`, `info`, `offline`, `ok` — the error binary sensor triggers on active `blocking`, `locking`, or `warning` events

## HA Compatibility Notes

- `OptionsFlow.config_entry` is a **read-only property** in modern HA — do NOT assign it in `__init__`. The base class sets it automatically.
- `async_get_options_flow()` should return the flow instance without passing `config_entry`.

## Known Gaps

- Temperature history (`/v3/api/statistics/temperature/`) requires `datapoints` param — valid IDs not yet discovered
- Energy stats return empty for HV circuit (likely only relevant for HK/WW/SOL)
- `business/plants/{id}/plant-structure` needs business role
- Full OpenAPI 3.1 spec saved at `docs/openapi-v3.json` (also available live at `/v3/api-docs`, no auth required)
- Non-supported circuit types (BL, WW, FRIWA, SOL, SOLB, PS) have endpoint support in the API but no HA entities yet
- HK climate entity: `set_temperature` sends value as integer — may need adjustment for different HK circuit models (some use tenths of degree)
