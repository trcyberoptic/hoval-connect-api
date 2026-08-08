# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Reverse-engineered API documentation and **Home Assistant custom integration** for the Hoval Connect IoT platform. Hoval Connect is a cloud platform connecting Hoval HVAC systems (heating, ventilation, hot water) via IoT gateways to Azure IoT Hub.

## Repo Conventions

- Design specs and implementation plans for non-trivial features live in `docs/superpowers/specs/` + `docs/superpowers/plans/` (one file per feature, dated).
- `tests/` are pure-function tests that run WITHOUT homeassistant installed — HA modules are stubbed via `sys.modules` at the top of each test file. On Windows use `python`, not `python3`.
- The bundled Blueprint `blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml` is the primary consumer of the `hoval_connect.reset_temporary_change` service and depends on two user-created helpers (`input_boolean.hoval_hv_boost_active`, `input_datetime.hoval_hv_boost_started_at`); see the README for installation.
- Live testing on a real HA instance and cutting releases: use the `live-testing` skill (`.claude/skills/live-testing/SKILL.md`).

## Dynamic Entity Discovery Gotcha

All platforms listen to the `SIGNAL_NEW_CIRCUITS` dispatcher signal to add entities at runtime without restart. The coordinator must dispatch this signal whenever `_known_circuits` grows — *including* the first time circuits appear. Earlier the coordinator gated the dispatch on `if self._known_circuits and new_circuits`, which silently stranded all circuit-level entities if the very first refresh after `async_setup_entry` came back without circuits (e.g. transient `_fetch_circuit` failure swallowed by `gather(return_exceptions=True)`); they stayed `restored=true`/`unavailable` until HA was restarted. Each platform's `_add_new()` already deduplicates via its `known` set, so unconditional dispatch on any new circuit is safe.

## Authentication Architecture (2-step)

1. **ID Token**: OAuth2 password grant to SAP IAS. Use `id_token` from response, NOT `access_token`. Lifetime: 30min.
2. **Plant Access Token (PAT)**: Fetch via `GET /v1/plants/{plantId}/settings`. Send as `X-Plant-Access-Token` header. Lifetime: ~15min.

## Key Endpoint Patterns

- `/api/` endpoints need only the id_token (`Authorization: Bearer`)
- `/v1/plants/`, `/v2/api/`, `/v3/` endpoints also require `X-Plant-Access-Token`
- `/business/` endpoints require elevated (partner) access — regular users get 403

## Circuit Types

PS = buffer tank (Pufferspeicher), NOT "pool"; PF1/PF2 = Pufferfühler top/bottom. FRIWA, SOL, SOLB exist in the API but have no HA entities yet (full enum in `const.py`).

## API Behavior Notes

- Control endpoints return HTTP 204 No Content on success — no response body
- Some GET endpoints (e.g. `/v1/plant-events/latest/`) return HTTP 200 with Content-Length: 0 (empty body) instead of 204 or empty JSON when no data exists — `_request` handles this via `content_length == 0` check
- **Around 2026-04-21 Hoval removed every `/v1/plants/{id}/circuits/...` endpoint** (list, mode setters, `temporary-change`, `reset`). The integration uses `/v3/plants/{id}/circuits` everywhere now. The cloud responds to v1 paths with HTTP 404 `{"detail":"No static resource ..."}`. Restoring those paths is not expected.
- `temporary-change` (v4 as of v0.15.0): `POST /v4/.../temporary-change` with JSON body `{"type": "endOfPhase"|"duration", "value": <float>, "duration": <minutes>|null}`. The HV value is a percentage; the HK value is degrees Celsius (no tenths). **`duration` is in MINUTES, not seconds** — verified empirically (OpenAPI declares it loosely as `double`). Accepted range: 30..1440. The v3 path still works (`activateTemporaryChange_1`, legacy-suffixed operationId) but takes the older `{"value", "duration": "fourHours"|"midnight"}` body; v4 rejects that v3 shape with `400 "Failed to read request"`. The pure helper `build_v4_temporary_change_body(value, duration)` in `api.py` translates the user-facing enum (`DURATION_END_OF_PHASE`, legacy `FOUR`, `MIDNIGHT`) to the v4 body. Earlier wrong belief: "HV only accepts endOfPhase" — that was a duration-out-of-range artifact, not a circuit-type limit.
- `temporary-change/reset`: `DELETE /v3/.../temporary-change` (no body). v4 has no documented DELETE — reset stays on v3 unless/until Hoval deprecates it.
- Mode endpoints `/v1/.../{standby|manual|constant|reset|cooling|time-programs}` are gone. Use `POST /v3/.../programs/{program}` where program ∈ {`constant`,`ecoMode`,`standby`,`week1`,`week2`,`manual`,`externalConstant`}.
- v1 had a separate `/reset` endpoint that auto-resumed the configured time program. v3 has no such auto-pick — `reset_circuit()` defaults to `week1`; pass `program="week2"` for the second weekly schedule.
- API always reports `operationMode='REGULAR'` regardless of actual device state — optimistic override needed for standby tracking
- v1 `activeProgram` enum (legacy, only relevant if Hoval rolls back): `constant`, `nightReduction`, `dayCooling`, `timePrograms`, `standby`, `manual`, `externalConstant`, `tteControlled`
- v3 `activeProgram` enum: `constant`, `ecoMode`, `standby`, `week1`, `week2`, `manual`, `externalConstant`
- v3 circuit list field renames vs the old v1 shape: `targetAirVolume` → `targetValue` (now `float`, percentage for HV / degrees for HK), `isAirQualityGuided` is now nested under `airQuality.isAirQualityGuided`, `targetAirHumidity` is no longer in the list (humidity comes from `live-values`).
- **Circuit *status* lives on the circuit DTO, never in `live-values`.** `GET /v3/plants/{id}/circuits` returns `circuitStatus` (observed: `"active"`), plus two fields that appear in no OpenAPI schema: `opMode` (duplicate of `operationMode`) and `manualStatus`. The `live-values` payload carries measurements only — for HV exactly `airVolume`, `humidityTarget`, `outsideTemperature`, `humidityActual`, `exhaustTemp` (verified live 2026-08-09). Both the `circuit_status` sensor and `HovalClimate.hvac_action` used to read a status key out of `live_values` and therefore always resolved to `None`/`IDLE`; they now read `HovalCircuitData.circuit_status`.
- **No Modbus register access.** `GET /api/telemetry-data/snapshots/live/{id}?dataPoints=…` answers `200 {}` for *every* input — raw register numbers (`23631`), path-prefixed spellings, live-value key names, and deliberate garbage alike. It never errors, so it gives no hint about the ID format either; the Android app never calls it (only `/api/telemetry-data/high-frequency-mode`). Modbus datapoints such as 23631 "Status Lüftungsregelung" (VOC / humidity / frost / CoolVent modes) are **not** reachable through the cloud API. The nearest cloud analogue is `operationMode`/`opMode` on the HV circuit, observed as `"ventilation"` during normal operation — whether it changes for the other register states is unverified.
- Weather forecast available via `get_weather()` — returns condition + temperature
- `PlantEventDTO` fields: `eventType`, `description`, `timeOccurred`, `timeResolved`, `sourcePath`, `code`, `module`, `functionGroup`, `function`, `category` — event is active when `timeResolved` is null
- Event types: `locking`, `blocking`, `warning`, `info`, `offline`, `ok` — the error binary sensor triggers on active `blocking`, `locking`, or `warning` events

## HA Compatibility Notes

- `OptionsFlow.config_entry` is a **read-only property** in modern HA — do NOT assign it in `__init__`. The base class sets it automatically.
- `async_get_options_flow()` should return the flow instance without passing `config_entry`.

## Known Pitfalls

- `aiohttp.resp.json()` on empty body throws `ContentTypeError` (subclass of `ClientError`) — easily misidentified as connection error in generic exception handlers
- A coordinator refresh can return `success=True` while `plant_data.circuits` is empty — `_fetch_circuit` exceptions are captured per-circuit by `gather(return_exceptions=True)`, plant-level fetches still succeed. Anything keying off "did the coordinator refresh" rather than "did this specific circuit appear" can drift; the `SIGNAL_NEW_CIRCUITS` dispatcher pitfall above is one consequence.
- `_resolve_active_program_value()` MUST be passed the circuit's current `active_program` so it picks `week1` vs `week2` from the programs blob. Before v0.15.0 it hardcoded `week1`, so users running week2 saw the wrong week/day names + the wrong phase value. Always thread `circuit_data.active_program` through.

## Known Gaps

- Temperature history (`/v3/api/statistics/temperature/`) rejects every request with `400 "At least one datapoint list must contain values"` — a `datapoints=…` query param does *not* satisfy it (identical error with and without), so the real parameter name is still unknown. It appears in no OpenAPI schema and the app never calls the endpoint; only brute-forcing parameter names is left.
- Energy stats return empty for HV circuit (likely only relevant for HK/WW/SOL)
- `business/plants/{id}/plant-structure` needs business role
- Full OpenAPI 3.1 spec saved at `docs/openapi-v3.json` (also available live at `/v3/api-docs`, no auth required)
- HK climate entity: `set_temperature` sends value as integer — may need adjustment for different HK circuit models (some use tenths of degree)
