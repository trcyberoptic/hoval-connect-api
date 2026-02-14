# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Reverse-engineered API documentation and **Home Assistant custom integration** for the Hoval Connect IoT platform. Hoval Connect is a cloud platform connecting Hoval HVAC systems (heating, ventilation, hot water) via IoT gateways to Azure IoT Hub.

## Repository Structure

- `README.md` — API documentation + HA integration install instructions
- `examples/` — Standalone Python and Bash API client examples
- `custom_components/hoval_connect/` — Home Assistant integration (HACS-compatible)
- `hacs.json` — HACS repository metadata
- `.github/workflows/` — CI: HACS/Hassfest validation, Ruff linting, automated releases on tags
- `pyproject.toml` — Ruff linter config (Python 3.12+, 100-char lines)

## Home Assistant Integration

The integration lives in `custom_components/hoval_connect/`. User setup is email + password only — plants and circuits are discovered automatically from the Hoval account at runtime.

### Key files

- `api.py` — Async aiohttp client: 2-step auth (ID token + Plant Access Token), auto-refresh with TTL caching, robust error handling with `HovalAuthError`/`HovalApiError` exception hierarchy. Handles HTTP 204 No Content for PUT control endpoints.
- `coordinator.py` — `DataUpdateCoordinator`: polls `get_plants()` → `get_circuits()` → `get_live_values()` + `get_programs()` + `get_events()` every 60s. Skips API calls when plant is offline, invalidates PAT cache on reconnect. Provides `control_lock` (asyncio.Lock) to serialize control commands, and `resolve_fan_speed()` helper for smart fan speed resolution.
- `config_flow.py` — UI config flow (email/password) + reauth flow
- `climate.py` — Climate entity for HV ventilation (HVAC modes: Auto/Fan Only/Off, shows exhaust temp + humidity)
- `fan.py` — Fan entity with continuous 0–100% speed slider (`FanEntityFeature.SET_SPEED`), turn on/off (standby)
- `sensor.py` — 8 sensor entities per circuit (outside temp, exhaust temp, air volume, humidity actual/target, active week/day program, program air volume) + 4 plant-level event sensors (latest event type/message/time, active event count)
- `binary_sensor.py` — 2 binary sensors per plant (online status with connectivity class, error status with problem class)
- `diagnostics.py` — Diagnostic data export with automatic PII redaction
- `const.py` — Constants: API URLs, OAuth client ID, token TTLs (25min ID, 12min PAT), polling interval (60s), circuit types, operation modes
- `__init__.py` — Entry setup, runtime data, platform forwarding (binary_sensor, climate, fan, sensor)

### Entity architecture

- Entities use `CoordinatorEntity` — no direct API calls, all data comes from the coordinator
- Device hierarchy: one parent device per plant, one child device per plant+circuit (linked via `via_device`)
- Circuit devices identified by `{plantId}_{circuitPath}`
- Currently supports HV (ventilation) circuits only (`SUPPORTED_CIRCUIT_TYPES` in `const.py`)
- Climate and fan entities use `coordinator.control_lock` to serialize API control commands (prevents race conditions)
- Fan speed resolution uses smart fallback chain: live airVolume → targetAirVolume → program air volume → default 40% (API rejects value=0)

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

- PUT control endpoints (`/v1/plants/{plantId}/circuits/{circuitPath}/...`) return HTTP 204 No Content on success — no response body
- `constant` mode uses PUT with `?value=` query param; `standby`, `manual`, `reset` use POST (no body)
- API returns HTTP 424 Failed Dependency if `constant` is called with `value=0` — always send value >= 1
- `get_weather()` method exists in `api.py` but is not currently used by any entity

## Known Gaps

- Temperature history (`/v3/api/statistics/temperature/`) requires `datapoints` param — valid IDs not yet discovered
- Energy stats return empty for HV circuit (likely only relevant for HK/WW/SOL)
- `business/plants/{id}/plant-structure` needs business role
- Full OpenAPI 3.1 spec available at `/v3/api-docs` (~450KB, auth required)
- Non-HV circuit types (HK, BL, WW, FRIWA, SOL, SOLB, PS) have endpoint support in the API but no HA entities yet
