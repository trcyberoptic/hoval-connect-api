# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Reverse-engineered API documentation and **Home Assistant custom integration** for the Hoval Connect IoT platform. Hoval Connect is a cloud platform connecting Hoval HVAC systems (heating, ventilation, hot water) via IoT gateways to Azure IoT Hub.

## Repository Structure

- `README.md` — API documentation + HA integration install instructions
- `examples/` — Standalone Python and Bash API client examples
- `custom_components/hoval_connect/` — Home Assistant integration (HACS-compatible)
- `hacs.json` — HACS repository metadata

## Home Assistant Integration

The integration lives in `custom_components/hoval_connect/`. User setup is email + password only — plants and circuits are discovered automatically from the Hoval account at runtime.

### Key files

- `api.py` — Async aiohttp client: 2-step auth (ID token + Plant Access Token), auto-refresh with TTL caching
- `coordinator.py` — `DataUpdateCoordinator`: polls `get_plants()` → `get_circuits()` → `get_live_values()` every 60s
- `config_flow.py` — UI config flow (email/password) + reauth flow
- `climate.py` — Climate entity for HV ventilation (mode, fan speed, humidity)
- `sensor.py` — 5 sensor entities per circuit (outside temp, exhaust temp, air volume, humidity, target humidity)
- `__init__.py` — Entry setup, runtime data, platform forwarding

### Entity architecture

- Entities use `CoordinatorEntity` — no direct API calls, all data comes from the coordinator
- One HA device per plant+circuit, identified by `{plantId}_{circuitPath}`
- Currently supports HV (ventilation) circuits only (`SUPPORTED_CIRCUIT_TYPES` in `const.py`)

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

## Known Gaps

- Temperature history (`/v3/api/statistics/temperature/`) requires `datapoints` param — valid IDs not yet discovered
- Energy stats return empty for HV circuit (likely only relevant for HK/WW/SOL)
- `business/plants/{id}/plant-structure` needs business role
- Full OpenAPI 3.1 spec available at `/v3/api-docs` (~450KB, auth required)
