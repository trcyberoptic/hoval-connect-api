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
- Continuous speed slider: 0–100% (temporary override, keeps time program active)
- Turn on/off toggle (standby mode)
- Configurable turn-on mode: resume last program, or activate week1/week2
- Debounced slider input (1.5s) to prevent API rate-limiting

**Climate entity** (per HK heating circuit):
- Target temperature control
- HVAC modes: Heat / Auto / Off (standby)
- HVAC action reflects actual circuit status

**Program select** (per HV/HK circuit):
- Switch between week1, week2, eco mode, standby, constant
- Shows user-defined program names from the Hoval app
- Current program pre-selected

**Sensor entities** (per circuit, filtered by type):
- **HV:** Outside temperature, exhaust temperature, air volume, humidity (actual/target), program air volume
- **HK:** Outside temperature, flow temperature (actual/target), room temperature setpoint
- **BL:** Heat generator temperature (actual/target), return temperature, operating hours, operating hours >50%, switching cycles, heat produced, electrical energy consumed
- **WW:** Hot water setpoint, tank temperature top (SF1), tank temperature bottom (SF2)
- **All:** Operation mode, active week program, active day program

**Plant-level sensors:**
- Weather condition and forecast temperature
- Latest event type, message, and timestamp
- Active event count

**Binary sensors** (per plant):
- Online/offline (connectivity class)
- Error status (problem class, detects blocking/locking events)

**Diagnostics:**
- Full diagnostic data export with automatic PII redaction (tokens, credentials, plant IDs)

**Options** (configurable per integration entry):
- Turn-on mode: resume / week1 / week2
- Temporary override duration: 4 hours / until midnight
- Polling interval (default: 60s)

**Under the hood:**
- 2-step token management (ID token + Plant Access Token) with TTL caching and auto-refresh
- Skips API calls when plant is offline, invalidates token cache on reconnect
- Parallel API fetches for circuits, live values, programs, events, and weather
- Program cache (5min TTL) reduces API calls
- Dynamic entity discovery — new circuits added without restart
- All circuit reads/writes use the `/v3` API (Hoval removed `/v1` circuit endpoints in April 2026); legacy v1 enum values still get normalized to v3 keys as a fallback

### Troubleshooting

- **Circuit entities (fan, climate, select, circuit-level sensors) stuck on "unavailable" after upgrade or HA restart** — reload the config entry: *Settings → Devices & Services → Hoval Connect → ⋮ → Reload*. The plant-level entities (weather, events, online status) staying available while every circuit-level entity is unavailable is the giveaway. Fixed in **v0.14.2** (the dispatcher now catches up if the first poll after boot came back without circuits); earlier versions need the manual reload once.
- **All entities `unavailable`, with `Circuits endpoint failed for plant …` in the log** — the cloud rejected the circuit list call; usually a transient outage. v0.14.0+ surfaces the failure as `unavailable` rather than silently keeping stale values, so wait for the next poll.
- **Auth keeps failing** — re-trigger the reauth flow from the integration settings; ID-token caching means a stale password is re-tried for ~30 min before the integration prompts.

### Known Limitations

- **HV, HK, BL, and WW circuits only.** Solar (SOL), fresh water (FRIWA), and other circuit types are not yet implemented.
- **BL energy sensors:** Heat produced and electrical energy consumed are in MWh (verified on UltraSource B Compact).
- **No time program editing.** Time programs can be read but not modified through the integration.
- **No energy/temperature history.** Historical statistics endpoints are documented but not yet integrated.
- **No holiday mode control.**
- **Single account only.** Each HA instance supports one Hoval Connect account.

### Requirements

- A Hoval Connect account (same credentials as the Hoval Connect mobile app)
- Home Assistant 2024.1.0 or newer

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
| PS | Pool/Swimming |
| GW | Gateway |

> **API change (2026-04-21):** Hoval removed every `/v1/plants/{id}/circuits/...` endpoint and now returns `HTTP 404 "No static resource ..."`. All circuit reads and writes now use `/v3` (or `/v4` for the newer temporary-change variant). See `docs/openapi-v3.json` for the live spec.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/v3/plants/{plantId}/circuits` | 🔑🏭 | All circuits with overview data |
| GET | `/v3/plants/{plantId}/circuits/{circuitPath}` | 🔑🏭 | Single circuit detail (limits, schedule, plant time) |
| GET | `/v3/plants/{plantId}/circuits/{circuitPath}/programs` | 🔑🏭 | Constant / eco / week1 / week2 program definitions |
| GET,PATCH | `/v3/plants/{plantId}/circuits/{circuitPath}/settings` | 🔑🏭 | Read or rename circuit (`circuitName`) |
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
| POST | `/v3/plants/{plantId}/circuits/{circuitPath}/temporary-change` | 🔑🏭 | Temporary value override. JSON body: `{"value": <float>, "duration": "fourHours"\|"midnight"}` |
| DELETE | `/v3/plants/{plantId}/circuits/{circuitPath}/temporary-change` | 🔑🏭 | Cancel active temporary override |
| POST | `/v4/plants/{plantId}/circuits/{circuitPath}/temporary-change` | 🔑🏭 | Newer variant. JSON body: `{"type": "endOfPhase"\|"duration", "value": <float>, "duration": <hours>\|null}` — supports arbitrary durations |
| POST | `/v3/plants/{plantId}/circuits/{circuitPath}/programs/{program}` | 🔑🏭 | Activate program. `{program}` ∈ `constant`, `ecoMode`, `standby`, `week1`, `week2`, `manual`, `externalConstant` |
| POST | `/v3/plants/{plantId}/circuits/{circuitPath}/air-quality-guided` | 🔑🏭 | Toggle air-quality-guided mode (HV only, requires sensor) |
| POST | `/v3/plants/{plantId}/circuits/{circuitPath}/semi-automatic-cooling` | 🔑🏭 | Toggle semi-automatic cooling |
| POST,DELETE | `/v2/api/holiday/{plantId}` | 🔑🏭 | Activate/cancel holiday mode for selected circuits |

Mode-specific `/v1/.../{constant\|cooling\|standby\|manual\|reset\|time-programs}` endpoints have all been removed; use `programs/{program}` instead. The old `temporary-change/reset` POST has been replaced by `DELETE /v3/.../temporary-change`.

---

### Live Data & Statistics

| Method | Path | Auth | Parameters | Description |
|--------|------|------|------------|-------------|
| GET | `/v3/api/statistics/live-values/{plantId}` | 🔑🏭 | `circuitPath`, `circuitType` | **Live sensor values** |
| GET | `/v2/api/statistics/live-values/{plantId}` | 🔑🏭 | `circuitPath`, `circuitType` | Live values (v2) |
| GET | `/v3/api/statistics/temperature/{plantId}` | 🔑🏭 | `circuitPath`, `circuitType`, `interval` (24h\|3d), `datapoints` | Temperature history |
| GET | `/v2/api/statistics/total-energy/{plantId}` | 🔑🏭 | `circuitPath`, `interval` (7d\|1M\|1y\|7y), `granularity` (1d\|1w\|1M\|1y) | Energy consumption |
| GET | `/v2/api/statistics/heat-consumption/{plantId}` | 🔑🏭 | `circuitPath`, `interval` | Heat consumption |
| GET | `/v2/api/statistics/solar-yield/{plantId}` | 🔑🏭 | `circuitPath`, `interval` | Solar yield |
| GET | `/api/telemetry-data/snapshots/live/{plantId}` | 🔑🏭 | `dataPoints` (array) | Raw telemetry snapshots |

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
| PUT | `/v2/api/holiday/{plantId}` | 🔑🏭 | Set/update holiday mode |

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
