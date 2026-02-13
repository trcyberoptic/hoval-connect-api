# Hoval Connect API – Unofficial Documentation & Home Assistant Integration

Reverse-engineered API documentation for the **Hoval Connect** IoT platform (used by Hoval heating/ventilation systems), plus a **Home Assistant custom integration** (HACS-compatible).

> ⚠️ **Unofficial.** Not affiliated with Hoval. Use at your own risk. API may change without notice.

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

- **Climate entity** per HV (ventilation) circuit — mode control (Auto/Fan Only/Off), fan speed (20–100%), target humidity
- **Sensor entities** per circuit — outside temperature, exhaust temperature, air volume, humidity, target humidity
- Automatic token management (ID token + Plant Access Token, refreshed transparently)
- Polls every 60 seconds

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
    "ContractType": "000000000004505010",
    "ValidFrom": "2025-12-01",
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

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/v1/plants/{plantId}/circuits` | 🔑🏭 | All circuits with details |
| GET | `/v1/plants/{plantId}/circuits/{circuitPath}` | 🔑🏭 | Single circuit detail |
| GET | `/v3/plants/{plantId}/circuits` | 🔑🏭 | Circuit overview (v3) |
| GET | `/v3/plants/{plantId}/circuits/{circuitPath}` | 🔑🏭 | Single circuit (v3) |
| GET | `/v3/plants/{plantId}/circuits/{circuitPath}/programs` | 🔑🏭 | Time programs for circuit |
| GET | `/business/plants/{plantId}/circuits` | 🔑🏭 | Circuit paths list |
| GET | `/business/plants/{plantId}/heat-generators` | 🔑🏭 | Heat generator info |

#### GET `/v1/plants/{plantId}/circuits`
```json
[
  {
    "type": "GW",
    "moduleType": "GW",
    "path": "1153.0.0",
    "name": null,
    "selectable": false,
    "configuredCorrectly": true,
    "hasError": false,
    "operationMode": null
  },
  {
    "type": "HV",
    "moduleType": "HV",
    "path": "520.50.0",
    "name": "Lüftung",
    "activeProgram": "tteControlled",
    "targetAirVolume": 40,
    "targetAirHumidity": 50,
    "isAirQualityGuided": false,
    "selectable": true,
    "homeVent": true,
    "hasError": false,
    "operationMode": "REGULAR",
    "plantTime": "2026-02-10T13:24:17+01:00"
  }
]
```

#### Circuit Control Endpoints (PUT/POST)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| PUT | `/v1/plants/{plantId}/circuits/{circuitPath}/constant` | 🔑🏭 | Set constant mode |
| PUT | `/v1/plants/{plantId}/circuits/{circuitPath}/standby` | 🔑🏭 | Set standby mode |
| PUT | `/v1/plants/{plantId}/circuits/{circuitPath}/manual` | 🔑🏭 | Set manual mode |
| POST | `/v1/plants/{plantId}/circuits/{circuitPath}/time-programs` | 🔑🏭 | Set time programs |
| PUT | `/v1/plants/{plantId}/circuits/{circuitPath}/temporary-change` | 🔑🏭 | Temporary override |
| PUT | `/v1/plants/{plantId}/circuits/{circuitPath}/reset` | 🔑🏭 | Reset to auto |
| PUT | `/v3/plants/{plantId}/circuits/{circuitPath}/settings` | 🔑🏭 | Update circuit settings |

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
