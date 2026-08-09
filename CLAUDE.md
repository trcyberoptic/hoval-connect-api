# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Reverse-engineered API documentation and **Home Assistant custom integration** for the Hoval Connect IoT platform. Hoval Connect is a cloud platform connecting Hoval HVAC systems (heating, ventilation, hot water) via IoT gateways to Azure IoT Hub.

## Repo Conventions

- Design specs and implementation plans for non-trivial features live in `docs/superpowers/specs/` + `docs/superpowers/plans/` (one file per feature, dated).
- `tests/` are pure-function tests that run WITHOUT homeassistant installed — HA modules are stubbed via `sys.modules` at the top of each test file. `tests/test_source_contracts.py` is the exception: it stubs and imports nothing, and asserts against the component sources read as *text*. Real `aiohttp` and `voluptuous` must still be installed (`test_config_flow.py` deliberately purges the voluptuous mock), so a checkout with only `pytest` dies at collection. Run **from the repo root** — `test_source_contracts.py` and `test_config_flow.py` resolve `custom_components/hoval_connect` relative to the CWD. On Windows use `python`, not `python3`.
- Commands: `python -m pytest tests/ -v`, and lint **unscoped the way CI does** — `ruff check .` plus `ruff format --check .`. `.github/workflows/lint.yml` passes ruff no path arguments; `pyproject.toml` already excludes `docs/`, while scoping to `custom_components/ tests/` silently skips `examples/`.
- The bundled Blueprint `blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml` is the primary consumer of the `hoval_connect.reset_temporary_change` service and depends on two user-created helpers (`input_boolean.hoval_hv_boost_active`, `input_datetime.hoval_hv_boost_started_at`); see the README for installation.

## Live Testing & Release Workflow

Commit `ec99ecf` moved this section into a `live-testing` skill that does not exist and never can be shared: `.claude/` is gitignored, so nothing under it reaches a clone. The facts stay here. Anything else from the deleted version is still readable via `git show ec99ecf^:CLAUDE.md`.

- Release CI (`.github/workflows/release.yml`) fires **only** on `v*` tag pushes. Bumping `"version"` in `manifest.json` publishes nothing on its own — also `git tag vX.Y.Z && git push origin vX.Y.Z`. `lint.yml` and `validate.yml` run on push/PR to `master` and never on tags.
- `homeassistant.reload_config_entry` does NOT re-import Python modules — changes under `custom_components/hoval_connect/` take effect only after a full HA core restart. Clear `__pycache__/` first when copying files in by hand.
- **A release can be installed and verified end-to-end over the REST API alone, no SSH** (done 2026-08-09, ~50 s from restart to fresh state). With a long-lived token: `POST /api/services/update/install` on `update.hoval_connect_update`, then `POST /api/services/homeassistant/restart`; poll the entity you changed until it leaves `unavailable`.
- **`homeassistant.update_entity` does NOT make HACS re-read GitHub** — it only re-renders HACS's cached release data, so a tag pushed minutes ago still shows `latest_version` = the previous release and `update.install` silently does nothing. Force the refetch over the WebSocket API: `{"type":"hacs/repositories/list"}` → find the entry whose `full_name` is the repo and take its `id` → `{"type":"hacs/repository/refresh","repository":<id>}`. Only then does `latest_version` move. (`hacs/repository/update` and `hacs/repository` with `action` are *not* commands — both answer "Unknown command".)
- **`GET /api/error_log` is gone as of HA 2026.8.1 — it answers `404: Not Found`.** Grepping that body for `hoval` yields nothing and reads exactly like "no errors", which is how a real, logged failure was missed here on 2026-08-09. Use the WebSocket API instead: connect to `/api/websocket`, `{"type":"auth","access_token":…}`, then `{"id":1,"type":"system_log/list"}`, and filter the returned entries on `name`/`message`. `aiohttp.ClientSession.ws_connect` is enough — no extra dependency.
- HA core logs on HAOS are not in `/config/home-assistant.log` (that file usually doesn't exist). Fetch via `GET http://supervisor/core/logs?tail=N` with `Authorization: Bearer <SUPERVISOR_TOKEN>`. The token is not in the SSH addon's shell env; read it from another addon process: `sudo sh -c 'for p in /proc/[0-9]*/environ; do tr "\0" "\n" <$p 2>/dev/null | grep -m1 SUPERVISOR_TOKEN; done | head -1'`.
- A newly added integration *service* is registered only after a full core restart following the HACS update; for 10–30 s afterwards automations calling it fail with `Action <domain>.<service> not found`. Verify with `GET /api/services` before re-triggering.
- Live API probes need no dependencies: `urllib.request` covers the OAuth + Plant-Access-Token + JSON flow (the SSH addon's `python3` is stdlib-only).

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
- **`operationMode` is NOT reported as `'REGULAR'`** (that claim stood here for months and is wrong): the circuit DTO carries a real value — `"ventilation"` on HV, verified live 2026-08-09. `OPERATION_MODE_REGULAR` in `const.py` is only the optimistic-override sentinel that control actions pass as `mode_override=` to `async_control_and_refresh`; it is never compared against API data. Entities compare the effective mode (fresh override, else the DTO value) against `OPERATION_MODE_STANDBY` only. Whether the DTO ever reports `standby` itself is unverified — which is why standby still needs the coordinator's TTL'd optimistic override.
- v1 `activeProgram` enum (legacy, only relevant if Hoval rolls back): `constant`, `nightReduction`, `dayCooling`, `timePrograms`, `standby`, `manual`, `externalConstant`, `tteControlled`
- v3 `activeProgram` enum: `constant`, `ecoMode`, `standby`, `week1`, `week2`, `manual`, `externalConstant`
- v3 circuit list field renames vs the old v1 shape: `targetAirVolume` → `targetValue` (now `float`, percentage for HV / degrees for HK), `isAirQualityGuided` is now nested under `airQuality.isAirQualityGuided`, `targetAirHumidity` is no longer in the list (humidity comes from `live-values`).
- **Circuit *status* lives on the circuit DTO, never in `live-values`.** `GET /v3/plants/{id}/circuits` returns `circuitStatus` (observed: `"active"`), plus two fields that appear in no OpenAPI schema: `opMode` (duplicate of `operationMode`) and `manualStatus`. The `live-values` payload carries measurements only — for HV exactly `airVolume`, `humidityTarget`, `outsideTemperature`, `humidityActual`, `exhaustTemp` (verified live 2026-08-09). Both the `circuit_status` sensor and `HovalClimate.hvac_action` used to read a status key out of `live_values` and therefore always resolved to `None`/`IDLE`; they now read `HovalCircuitData.circuit_status`.
- **The gateway emits non-standard 5xx codes — retry on a range, never an enumeration.** `HTTP 599` ("network connect timeout", a proxy-ism) hit the circuits endpoint ten times on 2026-08-09. It was missing from the old `_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}`, so `_request` raised immediately, the coordinator refresh failed, and **every entity went `unavailable` for the full 60 s scan interval**. Now `_is_retryable_status()` covers `429` plus all `>= 500`.
- **A running override is reported in the circuit DTO as `temporaryChange`** — `{"type": "away", "value": 100.0, "end": "2026-08-10T00:00:14+02:00"}`, absent when none is active. `type` is the cloud's own label and does not echo the v4 request's `endOfPhase`/`duration`; `"away"` was observed for a boost started with `MIDNIGHT`. Surfaced since v1.0.2 as `binary_sensor.…_temporary_change` (+ value/type/end attributes) and the timestamp sensor `sensor.…_temporary_change_end`, so automations no longer need their own helpers to remember that they started a boost.
- The circuit *details* endpoint `GET /v3/plants/{id}/circuits/{path}` returns `activeProgramConfiguration`, the **realised** day curve including any override phase (an active boost shows up as an extra phase starting at the minute it began), plus `constantValue`, `ecoModeValue` and both week names. Nothing in the integration reads it yet.
- **Raw device datapoints ARE readable — address them `UnitId.FunctionGroup.FunctionNumber.DatapointId`.** `GET /api/telemetry-data/snapshots/live/{id}?dataPoints=520.50.0.39652` returns `200 {"520.50.0.39652":"1"}`. Comma-separate for a batch; the response is a flat `{address: "value"}` map of strings. The first three groups are exactly the `circuitPath` (`520.50.0`), the fourth is the **CAN DatapointId**.
  - **The Modbus register number is NOT the identifier.** Probing `23631` (and every path-prefixed spelling of it) returns `200 {}` — as does deliberate garbage, so the endpoint gives no hint that the format is wrong. That silent `{}` cost a full session and produced the since-deleted claim that Modbus data was unreachable. The register number is a gateway-side artefact that shifts with the CAN unit id (HV unit 513 → 23540, 520 → 23631) while the DatapointId stays 39652.
  - The mapping lives in `E:\Code\Hoval-GatewayV2-CANBUS-MQTT\hoval_datapoints.csv` (columns `Register;UnitName;UnitId;FunctionGroup;FunctionNumber;DatapointId;DatapointName;…` plus the enum labels for LIST/U8 status types) — a different local repo, not this one.
  - Verified live 2026-08-09 on plant 604961716240055: of 28 documented addresses for HV unit 520, 13 answer. `39652` Status Lüftungsregelung (`0` Aus, `1` Normal, `2` VOC, `3` Feuchte, `4` Frostschutz, `5` CoolVent, `6` Fehler, `7` Sommerfeuchte, `8` Ausschaltstopp), `40650` Betriebswahl, `39600` Luftqualität-Regulierung, `40651`/`40686` Normal-/Spar-Modulation, `38606` Modulation, `40687` Feuchte-Sollwert, `37600` Feuchte Abluft, `37602` Temp Abluft, `0` Temp Aussenluft, `37606`/`37608`/`37611` CO2/VOC (all `255` = sensor not fitted). The `520.0.0.*` service/error addresses return nothing.
  - Caveat: `39652` has so far only been *observed* as `1`. That it reports `5`/`7` during CoolVent/Sommerfeuchte follows from the datapoint documentation, not yet from a live observation.
  - `operationMode`/`opMode` on the circuit DTO stayed `"ventilation"` across a full day in which CoolVent and Sommerfeuchte both ran — it is not a substitute for `39652`.
  - `/admin/commands/get-value/{id}` ("read data points directly from device") answers 403 for a normal account.
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
- HK climate entity: `set_temperature` passes the setpoint through unchanged as a `float` in degrees (`float(temperature)` → `set_temporary_change` → `build_v4_temporary_change_body`), and the entity advertises `_attr_target_temperature_step = 0.5`, so half-degree setpoints go out as `21.5`. The old `int(temperature * 10)` scaling was dropped in the v3 migration (v0.14.0). Whether every HK controller model accepts a fractional setpoint is unverified — as is the "(no tenths)" remark in the `temporary-change` note above, which may contradict this.
