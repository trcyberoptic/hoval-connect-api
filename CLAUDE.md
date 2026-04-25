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

- `api.py` — Async aiohttp client: 2-step auth, auto-refresh, token retry on 401, handles 204 and empty-body (content_length==0) responses
- `coordinator.py` — DataUpdateCoordinator: parallel fetch of circuits/events/weather, offline plant skip, program cache (5min TTL), `control_lock`, `_V1_PROGRAM_MAP`, `SIGNAL_NEW_CIRCUITS`
- `config_flow.py` — Config + reauth + options flow (turn-on mode, override duration, polling interval)
- `climate.py` — HK heating: target temp, HVAC modes (heat/auto/off)
- `fan.py` — HV ventilation: speed slider 0–100%, on/off (standby ↔ temporary-change), debounced 1.5s
- `select.py` — Program selection (week1/week2/ecoMode/standby/constant) with user-defined names
- `sensor.py` — Circuit-type-filtered sensors (HV/HK/BL/WW) + 6 plant-level sensors (events, weather)
- `binary_sensor.py` — Plant online status + error/warning status
- `diagnostics.py` — Diagnostic export with PII redaction
- `const.py` — API URLs, OAuth client ID, token TTLs, polling interval, circuit types, duration enums
- `__init__.py` — Entry setup, platform forwarding, `plant_device_info`/`circuit_device_info` helpers

### Entity architecture

- Entities use `CoordinatorEntity` — no direct API calls, all data comes from the coordinator
- Device hierarchy: one parent device per plant, one child device per plant+circuit (linked via `via_device`)
- Circuit devices identified by `{plantId}_{circuitPath}`
- Supports HV (ventilation), HK (heating), BL (boiler), and WW (warm water) circuit types (`SUPPORTED_CIRCUIT_TYPES` in `const.py`)
- Sensor descriptions use `circuit_types: frozenset[str] | None` to filter which sensors appear on which circuit types (`None` = all types)
- Fan speed resolution uses smart fallback chain: live airVolume → targetAirVolume → program air volume → default 40% (API rejects value=0)
- All entity platforms use `translation_key` for entity names (not hardcoded `_attr_name`)
- Dynamic entity discovery: all platforms listen to `SIGNAL_NEW_CIRCUITS` dispatcher signal to add entities at runtime without restart

## Running Tests

```bash
python -m pytest tests/ -v
```

## Linting

```bash
ruff check custom_components/ tests/
ruff format --check custom_components/ tests/
```

## Running Examples

```bash
python examples/hoval_client.py <email> <password>
./examples/get-live-values.sh <email> <password> <plantId> <circuitPath> <circuitType>
```

## Live Testing & Release Workflow

- `homeassistant.reload_config_entry` does NOT re-import Python modules — `custom_components/hoval_connect/` code changes only take effect after a full HA core restart (`POST http://supervisor/core/restart`). Clear `__pycache__/` first.
- HA core logs on HAOS are not in `/config/home-assistant.log` (that file usually doesn't exist). Fetch via `GET http://supervisor/core/logs?tail=N` with `Authorization: Bearer <SUPERVISOR_TOKEN>`. The token isn't exposed in the SSH addon's shell env but is in another addon process: `sudo sh -c 'for p in /proc/[0-9]*/environ; do tr "\0" "\n" <$p 2>/dev/null | grep -m1 SUPERVISOR_TOKEN; done | head -1'`.
- Release CI (`.github/workflows/release.yml`) triggers on `v*` tag pushes only. Bumping `manifest.json` does nothing on its own — also `git tag vX.Y.Z && git push origin vX.Y.Z`.
- Live API probes: the SSH addon's `python3` is stdlib-only, but `urllib.request` is enough for the OAuth + Plant-Access-Token + JSON flow. Write the probe locally, `pscp` it to `/tmp/`, run via plink.

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
- Some GET endpoints (e.g. `/v1/plant-events/latest/`) return HTTP 200 with Content-Length: 0 (empty body) instead of 204 or empty JSON when no data exists — `_request` handles this via `content_length == 0` check
- **Around 2026-04-21 Hoval removed every `/v1/plants/{id}/circuits/...` endpoint** (list, mode setters, `temporary-change`, `reset`). The integration uses `/v3/plants/{id}/circuits` everywhere now. The cloud responds to v1 paths with HTTP 404 `{"detail":"No static resource ..."}`. Restoring those paths is not expected.
- `temporary-change` (v3): `POST /v3/.../temporary-change` with JSON body `{"value": <float>, "duration": "fourHours"|"midnight"}`. The HV value is a percentage; the HK value is degrees Celsius (no tenths). Stored option values `FOUR`/`MIDNIGHT` from older configs are translated to the v3 camelCase form inside `set_temporary_change`.
- `temporary-change/reset` (v3): `DELETE /v3/.../temporary-change` (no body). Replaces the removed v1 POST `/temporary-change/reset`.
- Mode endpoints `/v1/.../{standby|manual|constant|reset|cooling|time-programs}` are gone. Use `POST /v3/.../programs/{program}` where program ∈ {`constant`,`ecoMode`,`standby`,`week1`,`week2`,`manual`,`externalConstant`}.
- v1 had a separate `/reset` endpoint that auto-resumed the configured time program. v3 has no such auto-pick — `reset_circuit()` defaults to `week1`; pass `program="week2"` for the second weekly schedule.
- API always reports `operationMode='REGULAR'` regardless of actual device state — optimistic override needed for standby tracking
- v1 `activeProgram` enum (legacy, only relevant if Hoval rolls back): `constant`, `nightReduction`, `dayCooling`, `timePrograms`, `standby`, `manual`, `externalConstant`, `tteControlled`
- v3 `activeProgram` enum: `constant`, `ecoMode`, `standby`, `week1`, `week2`, `manual`, `externalConstant`
- v3 circuit list field renames vs the old v1 shape: `targetAirVolume` → `targetValue` (now `float`, percentage for HV / degrees for HK), `isAirQualityGuided` is now nested under `airQuality.isAirQualityGuided`, `targetAirHumidity` is no longer in the list (humidity comes from `live-values`).
- Weather forecast available via `get_weather()` — returns condition + temperature
- `PlantEventDTO` fields: `eventType`, `description`, `timeOccurred`, `timeResolved`, `sourcePath`, `code`, `module`, `functionGroup`, `function`, `category` — event is active when `timeResolved` is null
- Event types: `locking`, `blocking`, `warning`, `info`, `offline`, `ok` — the error binary sensor triggers on active `blocking`, `locking`, or `warning` events

## HA Compatibility Notes

- `OptionsFlow.config_entry` is a **read-only property** in modern HA — do NOT assign it in `__init__`. The base class sets it automatically.
- `async_get_options_flow()` should return the flow instance without passing `config_entry`.

## Known Pitfalls

- `aiohttp.resp.json()` on empty body throws `ContentTypeError` (subclass of `ClientError`) — easily misidentified as connection error in generic exception handlers

## Known Gaps

- Temperature history (`/v3/api/statistics/temperature/`) requires `datapoints` param — valid IDs not yet discovered
- Energy stats return empty for HV circuit (likely only relevant for HK/WW/SOL)
- `business/plants/{id}/plant-structure` needs business role
- Full OpenAPI 3.1 spec saved at `docs/openapi-v3.json` (also available live at `/v3/api-docs`, no auth required)
- Non-supported circuit types (FRIWA, SOL, SOLB, PS) have endpoint support in the API but no HA entities yet
- HK climate entity: `set_temperature` sends value as integer — may need adjustment for different HK circuit models (some use tenths of degree)
