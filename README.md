# Hoval Connect API – Unofficial Documentation & Home Assistant Integration

Reverse-engineered API documentation for the **Hoval Connect** IoT platform (used by Hoval heating/ventilation systems), plus a **Home Assistant custom integration** (HACS-compatible).

> ⚠️ **Unofficial.** Not affiliated with Hoval. Use at your own risk. API may change without notice.

> **Successor to [Hoval-GatewayV2-CANBUS-MQTT](https://github.com/trcyberoptic/Hoval-GatewayV2-CANBUS-MQTT).** This cloud-based integration requires no additional hardware — just your Hoval Connect credentials. The previous project used CAN bus + MQTT via a physical gateway connection.

## Home Assistant Integration

### Installation (HACS)

1. In HACS, go to **Integrations** → **Custom repositories**
2. Add `https://github.com/trcyberoptic/hoval-connect-api` as an **Integration**
3. Install **Hoval Connect**
4. Restart Home Assistant
5. Go to **Settings** → **Integrations** → **Add Integration** → search **Hoval Connect**
6. Enter your Hoval Connect email and password

Plants and circuits are discovered automatically from your account.

### What You Get

**Fan entity** (per HV ventilation circuit):
- Continuous speed slider: 0–100% (temporary override, keeps time program active); values below the device minimum are clamped to 15 %, 0 turns the circuit off
- Turn on/off toggle (standby mode)
- Configurable turn-on mode: resume last program, or activate week1/week2
- Debounced slider input (1.5s) to prevent API rate-limiting

**Climate entity** (per HK heating circuit):
- Target temperature control
- Current room temperature (v1.0.0 — reads the correct `roomTempActual` live value)
- HVAC modes: Heat / Auto / Off (standby)
- HVAC action reflects actual circuit status

**Water heater entity** (per WW hot-water circuit, v1.0.0):
- Target temperature 10–65 °C in 0.5 °C steps — sets a temporary boost that expires at midnight, then the week program resumes
- Operation modes: heat pump (week program), high demand (shown while a boost is active — selecting it does *not* start a boost; it resets the circuit to week1 exactly like heat pump, cancelling an active boost), off (standby)
- Current temperature from the top-of-tank sensor

**Program select** (per HV/HK/WW circuit):
- Switch between week1, week2, eco mode, standby, constant
- Shows user-defined program names from the Hoval app
- Current program pre-selected

**Sensor entities** (per circuit, filtered by type):
- **HV:** Outside temperature, exhaust temperature, air volume, humidity (actual/target), program air volume
- **HK:** Outside temperature, flow temperature (actual/target), room temperature (actual + setpoint)
- **BL:** Heat generator temperature (actual/target), return temperature, operating hours, operating hours >50%, switching cycles, heat produced, electrical energy consumed, current output heating, modulation, FA status, electric heater (operating hours, switching cycles, heat produced, energy consumed, active)
- **WW:** Hot water setpoint, tank temperature top (SF1), tank temperature bottom (SF2)
- **PS:** Buffer target temperature, buffer temperature top (PF1) / bottom (PF2)
- **All:** Status, operation mode, active week program, active day program, temporary change ends (timestamp, v1.0.2 — empty when no override is running)

> Outside temperature is created only on HV/HK circuits (v0.15.4+). Upgrading from ≤0.15.3: any outside-temperature sensors you already have on BL/WW circuits are preserved — only newly-discovered circuits get the filtered set.

**Plant-level sensors:**
- Weather condition and forecast temperature
- Latest event type, message, and timestamp
- Active event count

**Binary sensors:**
- Online/offline (per plant, connectivity class)
- Error status (per plant, problem class — on for an active `blocking`, `locking` or `warning` event, or when any circuit reports `hasError`)
- Temporary change (per circuit, running class, v1.0.2) — on while a boost/override is active, with the target `value`, the cloud's `type` label and the `end` time as attributes. The cloud tracks this itself, so an automation that starts a boost no longer has to remember that it did.

**Diagnostics:**
- Full diagnostic data export with automatic PII redaction (tokens, credentials, plant IDs)

**Options** (configurable per integration entry):
- Turn-on mode: resume / week1 / week2
- Temporary override duration: until end of current phase (default, v0.15.0+) / 4 hours / until midnight
- Polling interval (default: 60s)

**Services:**
- `hoval_connect.reset_temporary_change` — cancel an active temporary override on a HomeVent fan, heating-circuit climate, or hot-water water_heater entity, returning control to the underlying time program. Target = the fan/climate/water_heater entity. Useful in automations that need to end a manual boost cleanly (instead of waiting for the override to expire).

**Under the hood:**
- 2-step token management (ID token + Plant Access Token) with TTL caching, auto-refresh, and single-flight locking (concurrent requests trigger at most one token refresh)
- Skips API calls when plant is offline, invalidates token cache on reconnect
- Parallel API fetches for circuits, live values, programs, events, and weather
- Tiered caching reduces API calls: programs 5 min, events 3 min, weather forecast 15 min
- Hardened against upstream API changes (v1.0.0): paginated `{"content": [...]}` responses are normalized on the circuits, live-values and both plant-event endpoints (not on the weather forecast — a wrapped response there yields no forecast), the plant list follows pagination, and a malformed program or live-value field degrades only its own sensors instead of dropping the whole circuit
- Dynamic entity discovery — new circuits added without restart
- All circuit reads use the `/v3` API (Hoval removed `/v1` circuit endpoints in April 2026); legacy v1 enum values still get normalized to v3 keys as a fallback
- Temporary overrides use the `/v4` API (v0.15.0+) for forward-compatibility; `endOfPhase`/`duration` body shape, reset still on `/v3` DELETE

### Summer-boost Blueprint (optional)

A bundled Home Assistant Blueprint auto-boosts the HomeVent to 90 % on warm
afternoons when at least one (non-office) room exceeds a comfort threshold AND
the outside air is moderate AND cooler than indoors. The boost ends when every
non-excluded room drops below a configurable comfort target (default 21 °C),
when the outside air gets too warm, when the time window expires, when the HV
goes into standby, or when the user changes the fan slider by hand.

Blueprint source: [`blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml`](blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml).

**Prerequisites:**

1. Hoval Connect integration **v0.15.1 or later** (ships the
   `hoval_connect.reset_temporary_change` service used to end the boost
   cleanly).
2. Two helpers that persist across HA restarts. Either create them in the UI
   under **Settings → Devices & Services → Helpers**, or paste this into
   `configuration.yaml`:

   ```yaml
   input_boolean:
     hoval_hv_boost_active:
       name: HV Boost aktiv
       icon: mdi:fan-speed-3

   input_datetime:
     hoval_hv_boost_started_at:
       name: HV Boost Startzeit
       has_date: true
       has_time: true
   ```

   Reload YAML configuration (or restart HA).

**Install the Blueprint:**

- *Option A — Import from URL:* In HA, **Settings → Automations & Scenes →
  Blueprints → Import Blueprint** and paste the raw URL of the YAML file
  (the `source_url` inside the file points there).
- *Option B — Copy the file:* drop the YAML into
  `<HA_config_dir>/blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml`
  and reload Blueprints from the UI.

**Configure:** **Settings → Automations & Scenes → Blueprints → Hoval HomeVent
Sommer-Boost → Create Automation**. Pick the HV fan entity, program-select
entity, outside-temperature sensor, the room sensors that should participate,
the office sensor to exclude, the two helpers, and your notify service. The
thresholds default to 23 °C / 21 °C / 25 °C / 25.5 °C / 15 min / 90 %; adjust
to taste.

### Known Limitations

- **HV, HK, BL, WW, and PS circuits only.** Solar (SOL), fresh water (FRIWA), and other circuit types are not yet implemented.
- **BL energy sensors:** Heat produced and electrical energy consumed are in MWh (verified on UltraSource B Compact).
- **No time program editing.** Time programs can be read but not modified through the integration.
- **No energy/temperature history.** Historical statistics endpoints are documented but not yet integrated.
- **No holiday mode control.**
- **One config entry per Hoval account.** The same account email cannot be added twice (the entry's unique ID is the lowercased email), but several Hoval accounts can run side by side — each entry gets its own coordinator and polls independently.

### Requirements

- A Hoval Connect account (same credentials as the Hoval Connect mobile app)
- Home Assistant 2024.11.0 or newer — the config flow calls `self._get_reauth_entry()` (2024.11) and relies on the base class setting `OptionsFlow.config_entry`, and the integration keeps its state in `entry.runtime_data` (2024.6). The bundled Blueprint separately declares `min_version: "2024.6.0"`.

---

## API Documentation

### Overview

Hoval Connect is a cloud platform that connects Hoval HVAC systems (heating, ventilation, hot water) via an IoT gateway to Azure IoT Hub. The mobile app (Android/iOS) communicates through a REST API hosted on Azure.

### Architecture

```
Hoval Device ←→ IoT Gateway ←→ Azure IoT Hub ←→ Hoval Core API ←→ Mobile App / HA Integration
                                                  (REST/JSON)
```

### Infrastructure

| Component | URL |
|-----------|-----|
| **Core API** | `https://azure-iot-prod.hoval.com/core` |
| **Identity Provider** | SAP Cloud Identity Services (IAS) |
| **OIDC Discovery** | `https://akwc5scsc.accounts.ondemand.com/.well-known/openid-configuration` |
| **IoT Hub** | `iot-hub-neu-prod.azure-devices.net` |
| **Gateway Desk** | `gateway.hovaldesk.com` |
| **Monitoring** | Grafana Cloud (`logs-prod-012.grafana.net`) |

## Authentication

### Step 1: Get ID Token (OAuth2 Password Grant)

> **Note:** The `client_id` below is the public OAuth2 client ID for the Hoval Connect mobile app, extracted from the official Android/iOS app. It is the same for all users and is required by the SAP IAS identity provider.

```bash
curl -X POST "https://akwc5scsc.accounts.ondemand.com/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" \
  -d "client_id=991b54b2-7e67-47ef-81fe-572e21c59899" \
  -d "username=YOUR_EMAIL" \
  -d "password=YOUR_PASSWORD" \
  -d "scope=openid"
```

**Response:**
```json
{
  "access_token": "opaque-token...",
  "id_token": "eyJhbGci...",
  "token_type": "Bearer",
  "expires_in": 1800
}
```

> **Important:** Use the `id_token` (JWT) as your Bearer token, NOT the `access_token`.

**Token lifetime:** 30 minutes.

**JWT Claims:**
| Claim | Description |
|-------|-------------|
| `sub` | User ID (e.g., `P000001`) |
| `groups` | `Hoval-IoT-Prod-BasicUser` |
| `aud` | Array of audience IDs |
| `app_tid` | Application tenant ID |

### Step 2: Get Plant Access Token

Most plant-specific endpoints require an additional `X-Plant-Access-Token` header.

```bash
curl "https://azure-iot-prod.hoval.com/core/v1/plants/{plantExternalId}/settings" \
  -H "Authorization: Bearer {id_token}"
```

**Response:**
```json
{
  "token": "eyJhbGci...",
  "featureMap": { "OP": "OWN_PLANT", "PE": "PROGRAMS_EDIT", ... },
  "plantSetting": {
    "plantExternalId": "123456789012345",
    "address": { "street": "...", "city": "...", "countryCode": "CH" },
    "plantName": "MyPlant"
  },
  "isPlantOwner": true
}
```

**Plant Access Token lifetime:** ~15 minutes (JWT with `exp` claim).

### Auth Summary

```
id_token (30min) → Authorization: Bearer {id_token}
plant_access_token (15min) → X-Plant-Access-Token: {token}
```

## API Endpoints

### Base URL

```
https://azure-iot-prod.hoval.com/core
```

### Notation

- `{plantId}` = Plant External ID (15-digit number, e.g., `123456789012345`)
- `{circuitPath}` = Circuit path (e.g., `520.50.0`)
- 🔑 = Requires `Authorization: Bearer {id_token}`
- 🏭 = Also requires `X-Plant-Access-Token`

---

### Bootstrap & User

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/bootstrap` | 🔑 | Environment info + user settings |
| GET | `/api/my-plants?size=12&page=0` | 🔑 | List user's plants |
| GET | `/api/user-settings` | 🔑 | User profile |
| GET | `/api/contracts/active?plantExternalId={plantId}` | 🔑 | Active service contracts |

#### POST `/api/bootstrap`
```json
{
  "environmentInfo": {
    "environment": "prod",
    "solarWebUiBaseUrl": "https://helio.sun"
  },
  "userSetting": {
    "userId": "P000001",
    "email": "user@example.com",
    "firstName": "...",
    "lastName": "...",
    "platformFeatures": ["REDEEM_PLANT_ACCESS_CODE"],
    "language": "DE",
    "availableLanguages": ["DE", "EN", "FR", "IT"]
  }
}
```

#### GET `/api/my-plants`
```json
[
  {
    "plantExternalId": "123456789012345",
    "description": "MyPlant",
    "isOnline": true,
    "isOnboarded": true,
    "isContractValid": true
  }
]
```

#### GET `/api/contracts/active`
```json
[
  {
    "ContractID": "0000000000",
    "ContractType": "000000000000000000",
    "ValidFrom": "2025-01-01",
    "ValidTo": "2050-12-30",
    "GatewaySerialID": "123456789012345",
    "isOnboarded": true,
    "Latitude": "47.000000",
    "Longitude": "8.000000"
  }
]
```

---

### Plant Settings & Access

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/v1/plants/{plantId}/settings` | 🔑 | Plant access token + features + address |
| GET | `/v1/plant-shares?plantExternalId={plantId}` | 🔑🏭 | Shared access list |
| GET | `/business/plants/{plantId}/is-online` | 🔑🏭 | Online status (boolean) |

---

### Circuits

Circuits represent the controllable components of a plant (heating, ventilation, hot water, etc.).

#### Circuit Types

| Code | Description |
|------|-------------|
| HK | Heating circuit (Heizkreis) |
| BL | Boiler |
| WW | Warm water (Warmwasser) |
| FRIWA | Fresh water station (Frischwasser) |
| HV | Home ventilation (Lüftung) |
| SOL | Solar |
| SOLB | Solar buffer |
| PS | Buffer tank (Pufferspeicher) |
| GW | Gateway |

> **API change (2026-04-21):** Hoval removed every `/v1/plants/{id}/circuits/...` endpoint and now returns `HTTP 404 "No static resource ..."`. All circuit reads and writes now use `/v3` (or `/v4` for the newer temporary-change variant). See `docs/openapi-v3.json` for the live spec.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/v3/plants/{plantId}/circuits` | 🔑🏭 | All circuits with overview data |
| GET | `/v3/plants/{plantId}/circuits/{circuitPath}` | 🔑🏭 | Single circuit detail (limits, schedule, plant time) |
| GET,PATCH | `/v3/plants/{plantId}/circuits/{circuitPath}/programs` | 🔑🏭 | Constant / eco / week1 / week2 program definitions. `PATCH` (`ProgramsConfigurationV3DTO`) is declared in the spec but never called by this project — untested |
| GET,PATCH | `/v3/plants/{plantId}/circuits/{circuitPath}/settings` | 🔑🏭 | Read or update circuit settings — `CircuitSettingsDTO` = `circuitName` (rename) + `weatherImpact` (`outsideTemperature` 0–100, `solarRadiation` −10…0; semantics unverified) |
| GET | `/business/plants/{plantId}/circuits` | 🔑🏭 | Circuit paths list (partner only) |
| GET | `/business/plants/{plantId}/heat-generators` | 🔑🏭 | Heat generator info (partner only) |

#### GET `/v3/plants/{plantId}/circuits`
```json
[
  {
    "type": "GW",
    "moduleType": "GW",
    "path": "1153.0.0",
    "name": null,
    "isSelectable": false,
    "selectable": false,
    "hasError": false,
    "activeProgram": null,
    "operationMode": null,
    "targetValue": 0.0,
    "actualValue": null,
    "airQuality": null,
    "manualValue": null,
    "holidayEnd": null,
    "isAdditionalBoiler": false,
    "additionalBoiler": false,
    "week1OrWeek2Active": false
  },
  {
    "type": "HV",
    "moduleType": "HV",
    "path": "520.50.0",
    "name": "Lüftung",
    "isSelectable": true,
    "selectable": true,
    "activeProgram": "week2",
    "activeWeekProgramName": "Sommer",
    "activeDayProgramName": "Früh+Abend",
    "circuitStatus": "active",
    "operationMode": "ventilation",
    "opMode": "ventilation",
    "manualStatus": "heating",
    "temporaryChange": { "type": "away", "value": 100.0, "end": "2026-08-10T00:00:14+02:00" },
    "targetValue": 60.0,
    "actualValue": null,
    "manualValue": null,
    "airQuality": {
      "isAirQualityGuided": false,
      "hasAirQualitySensor": false,
      "actualRoomAirQuality": null,
      "airQualityGuided": false
    },
    "holidayEnd": null,
    "isAdditionalBoiler": false,
    "hasError": false,
    "week1OrWeek2Active": true
  }
]
```

`activeProgram` enum: `constant`, `ecoMode`, `standby`, `week1`, `week2`, `manual`, `externalConstant`. `targetValue` is the percentage for HV and degrees Celsius for HK.

`opMode` and `manualStatus` are returned by the live API but appear in no OpenAPI schema; `opMode` mirrored `operationMode` in every observed response. Circuit *status* is only ever here — the `live-values` endpoint returns measurements and no status key.

`temporaryChange` is present only while an override is running and disappears once it expires. Its `type` is the cloud's own label rather than an echo of the v4 request — a boost sent as `{"type": "duration", …}` came back as `"away"`. The `end` timestamp carries the plant's UTC offset.

#### GET `/v3/plants/{plantId}/circuits/{circuitPath}`
```json
{
  "week1Name": "Winter",
  "week2Name": "Sommer",
  "plantTime": "2026-04-26T00:10:53+02:00",
  "constantValue": 50.0,
  "ecoModeValue": 60.0,
  "activeProgramConfiguration": {
    "baseValue": 40.0,
    "phases": [
      { "phaseValue": 60.0, "start": {"hours": 0, "minutes": 0}, "end": {"hours": 9, "minutes": 0} },
      { "phaseValue": 40.0, "start": {"hours": 9, "minutes": 0}, "end": {"hours": 19, "minutes": 0} },
      { "phaseValue": 60.0, "start": {"hours": 19, "minutes": 0}, "end": {"hours": 24, "minutes": 0} }
    ],
    "limits": null
  },
  "temporaryChangeLimits": { "max": 100.0, "min": 15.0, "step": 1.0 }
}
```

#### Circuit Control Endpoints

> **Note:** Control endpoints return **HTTP 204 No Content** on success (no response body).

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/v3/plants/{plantId}/circuits/{circuitPath}/temporary-change` | 🔑🏭 | Legacy variant (operationId `activateTemporaryChange_1`). JSON body: `{"value": <float>, "duration": "fourHours"\|"midnight"}` |
| DELETE | `/v3/plants/{plantId}/circuits/{circuitPath}/temporary-change` | 🔑🏭 | Cancel active temporary override (no v4 equivalent) |
| POST | `/v4/plants/{plantId}/circuits/{circuitPath}/temporary-change` | 🔑🏭 | Current variant (used by integration v0.15.0+ and Hoval Connect Android app). JSON body: `{"type": "endOfPhase"\|"duration", "value": <float>, "duration": <minutes>\|null}` — `duration` in **minutes**, accepted range 30..1440 |
| POST | `/v3/plants/{plantId}/circuits/{circuitPath}/programs/{program}` | 🔑🏭 | Activate program. `{program}` ∈ `constant`, `ecoMode`, `standby`, `week1`, `week2`, `manual`, `externalConstant` |
| POST | `/v3/plants/{plantId}/circuits/{circuitPath}/air-quality-guided` | 🔑🏭 | Air-quality-guided mode (HV only, requires sensor). Requires `guided=true\|false` — a required query param per the spec; the decompiled app sends it form-encoded. Not live-verified |
| POST | `/v3/plants/{plantId}/circuits/{circuitPath}/semi-automatic-cooling` | 🔑🏭 | Set the semi-automatic cooling setpoint (`updateCoolingTemperature`) — **not** a toggle. Requires `value=<°C>`; a bare POST is invalid. Belongs to **HK** circuits on reversible heat pumps, not HV (see `docs/reverse-engineering-2026-05-23.md`) |
| POST,DELETE | `/v2/api/holiday/{plantId}` | 🔑🏭 | Activate/cancel holiday mode for selected circuits |

Mode-specific `/v1/.../{constant\|cooling\|standby\|manual\|reset\|time-programs}` endpoints have all been removed; use `programs/{program}` instead. The old `temporary-change/reset` POST has been replaced by `DELETE /v3/.../temporary-change`.

---

### Live Data & Statistics

| Method | Path | Auth | Parameters | Description |
|--------|------|------|------------|-------------|
| GET | `/v3/api/statistics/live-values/{plantId}` | 🔑🏭 | `circuitPath`, `circuitType` | **Live sensor values** |
| GET | `/v3/api/statistics/temperature/{plantId}` | 🔑🏭 | `circuitPath`, `circuitType`, `interval` (24h\|3d), + an unknown datapoint parameter | Temperature history — always `400 "At least one datapoint list must contain values"`; `datapoints=…` does not satisfy it |
| GET | `/v2/api/statistics/total-energy/{plantId}` | 🔑🏭 | `circuitPath`, `interval` (7d\|1M\|1y\|7y), `granularity` (1d\|1w\|1M\|1y) | Energy consumption |
| GET | `/v2/api/statistics/heat-consumption/{plantId}` | 🔑🏭 | `circuitPath`, `interval` (7d\|1M\|1y\|7y), `granularity` (1d\|1w\|1M\|1y) | Heat consumption |
| GET | `/v2/api/statistics/solar-yield/{plantId}` | 🔑🏭 | `circuitPath`, `interval` (7d\|1M\|1y\|7y), `granularity` (1d\|1w\|1M\|1y) | Solar yield |
| GET | `/api/telemetry-data/snapshots/live/{plantId}` | 🔑🏭 | `dataPoints` (comma-separated) | **Raw device datapoints.** Address as `UnitId.FunctionGroup.FunctionNumber.DatapointId`, i.e. the `circuitPath` plus the CAN DatapointId — e.g. `520.50.0.39652` → `{"520.50.0.39652":"1"}`. Modbus **register** numbers (`23631`) are not accepted and return `200 {}`, exactly like garbage input. |

#### GET `/api/telemetry-data/snapshots/live/{plantId}`

Reads raw controller datapoints — considerably more than `live-values` exposes, including the ventilation
control status that no other endpoint reports.

**Addressing:** `UnitId.FunctionGroup.FunctionNumber.DatapointId`. The first three groups are the
circuit path you already know from `/v3/plants/{id}/circuits`; the fourth is the CAN **DatapointId**.
The Modbus *register* number is a gateway-side artefact and is not accepted — it shifts with the unit id
(HV unit 513 → register 23540, unit 520 → 23631) while the DatapointId stays 39652. A wrong identifier
returns `200 {}` rather than an error, so an empty result means "unknown address", not "no data".

```
GET /api/telemetry-data/snapshots/live/604961716240055?dataPoints=520.50.0.39652,520.50.0.38606
→ 200 { "520.50.0.39652": "1", "520.50.0.38606": "100" }
```

**HomeVent (HV) datapoints that answer** — verified on a HomeVent behind a Hoval Connect gateway:

| DatapointId | Meaning | Type |
|---|---|---|
| `39652` | **Status Lüftungsregelung** — 0 off, 1 normal, 2 VOC, 3 humidity, 4 frost protection, 5 CoolVent, 6 fault, 7 summer humidity, 8 switch-off stop | U8 enum |
| `40650` | Betriebswahl Lüftung | list |
| `39600` | Luftqualität Regulierung | list |
| `38606` | Lüftungsmodulation (= `airVolume`) | U8 % |
| `40651` / `40686` | Normal- / Spar-Lüftungsmodulation | U8 % |
| `40687` | Feuchte Sollwert (= `humidityTarget`) | U8 % |
| `37600` | Feuchtigkeit Abluft (= `humidityActual`) | U8 % |
| `0` / `37602` | Temperatur Aussenluft / Abluft | S16 °C |
| `37606` / `37608` / `37611` | CO2 Abluft, VOC Abluft / Aussenluft — `255` when the sensor is not fitted | U8 |

Service and error addresses under `520.0.0.*` (active faults, maintenance counters) returned nothing.
Datapoint ids and their enum labels come from Hoval's TopTronic E datapoint list.

#### GET `/v3/api/statistics/live-values/{plantId}`

**Parameters:**
- `circuitPath` (required): e.g., `520.50.0`
- `circuitType` (required): `HK`, `BL`, `WW`, `FRIWA`, `HV`, `SOL`, `SOLB`, `PS`, `GW`

**Response (HV circuit):**
```json
[
  { "key": "outsideTemperature", "value": "5.5" },
  { "key": "airVolume", "value": "40" },
  { "key": "humidityTarget", "value": "50" },
  { "key": "humidityActual", "value": "42" },
  { "key": "exhaustTemp", "value": "22.4" }
]
```

---

### Weather

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/v2/api/weather/forecast/{plantId}` | 🔑🏭 | 4-day weather forecast for plant location |

```json
[
  {
    "_time": "2026-02-10T00:00:00Z",
    "weatherCode": 3,
    "weatherType": "partialCloud",
    "outsideTemperatureMin": 4,
    "outsideTemperature": 6
  }
]
```

---

### Events & Notifications

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/v1/plant-events/{plantId}` | 🔑 | Plant error events |
| GET | `/v1/plant-events/latest/{plantId}` | 🔑 | Latest event |
| GET | `/v1/plants/{plantId}/notifications` | 🔑🏭 | Notification settings |
| GET | `/business/notifications` | 🔑🏭 | Business notifications |

#### GET `/v1/plants/{plantId}/notifications`
```json
[
  {
    "language": "DE",
    "eventTypes": ["offline", "blocking", "warning", "info", "locking"],
    "id": "xxxxxxxx-...",
    "plantExternalId": "123456789012345",
    "userId": "P000001",
    "email": "user@example.com"
  }
]
```

---

### Gateway & Software

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/gateway-software/{plantId}/versions/current` | 🔑🏭 | Current gateway SW version |

---

### Holiday

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST,DELETE | `/v2/api/holiday/{plantId}` | 🔑🏭 | Activate (`POST`) / cancel (`DELETE`) holiday mode — same endpoint as under Circuit Control above; there is no `PUT` |

---

### Energy Manager (PV Smart)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/v2/api/energy-manager-pv-smart/available/{plantId}` | 🔑🏭 | PV smart availability |
| GET | `/v2/api/energy-manager-pv-smart/live/{plantId}` | 🔑🏭 | PV live data |
| GET | `/v2/api/energy-manager-pv-smart/chart-data/{plantId}` | 🔑🏭 | PV chart data |

---

### Plant Registration

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/v1/plant-registrations` | 🔑 | Register a new plant |

---

### News

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/v1/news/latest` | 🔑 | Latest news (requires `Hovalconnect-Frontend-App-Version` header) |

---

### OpenAPI Spec

The full OpenAPI 3.1 specification is available at:
```
GET /v3/api-docs
```
(~450KB JSON, requires Bearer auth)

## Feature Map

The plant access token response includes a feature map indicating what operations are available:

| Code | Feature |
|------|---------|
| OP | Own Plant |
| PA | Plant Access |
| PE | Programs Edit |
| GSV | Gateway Software View |
| GSU | Gateway Software Update |
| GSD | Gateway Software Downgrade |
| SP | Share Plant |
| DM | Diagnosis Mode |
| PT | Parameter Tree |
| PVV | Plant Visualisation View |
| PVE | Plant Visualisation Edit |
| MUV | Meters Unassigned View |
| MUNE | Meters Unassigned Name Edit |
| EAS | Energy Accounting Share |
| PAE | Plant Address Edit |
| PNE | Plant Name Edit |
| PAV | Plant Address View |
| NVR | Notification View Receivers |
| NMR | Notification Manage Receivers |

## Rate Limits & Notes

- Token lifetime: 30 min (id_token), ~15 min (plant access token)
- No documented rate limits, but be respectful
- The API may lock out accounts after repeated failed auth attempts
- Some endpoints are partner/business-only (403 for regular users)
- `business/` endpoints may require elevated access roles

## Disclaimer

This documentation was created through API analysis and is not officially supported by Hoval. The API may change at any time. Use responsibly and respect Hoval's terms of service.
