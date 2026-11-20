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

> [!IMPORTANT]
> **Use v1.1.0 or newer. Hoval blocks every older release.**
>
> On 2026-11-19 Hoval blocked this integration at its Azure Application Gateway because it
> polled the cloud too often ([#24][issue-24]). Requests from v1.0.18 and earlier are refused
> with HTTP 403 and the gateway's own HTML page (`Microsoft-Azure-Application-Gateway/v2`).
> The login still succeeds, so this can look like a permissions or network problem. It is
> neither: the request never reaches Hoval.
>
> v1.1.0 does what Hoval asked for. It polls at most every 15 minutes, reads slow-changing
> data hourly and no longer reads raw controller datapoints. Hoval agreed to allow it and
> recognises it by the version in its User-Agent (`hoval-connect-api/1.1.0`). Until Hoval
> has done so, v1.1.0 gets the same 403. [#24][issue-24] shows the current status.
>
> Update in HACS, then **restart Home Assistant completely**: a config-entry reload does not
> re-import Python modules. Do not change the User-Agent yourself. Hoval allows the
> integration by the identifier and version it sends.
>
> **History:** from about 2026-09-09 the gateway refused every User-Agent containing
> `homeassistant`, which Home Assistant sends by default for every integration. v1.0.8
> worked around that by sending its own identifier, `hoval-connect-api/<version>`
> ([#11][issue-11]). That identifier is what Hoval blocked in November.

[issue-24]: https://github.com/trcyberoptic/hoval-connect-api/issues/24
[issue-11]: https://github.com/trcyberoptic/hoval-connect-api/issues/11

### What You Get

**Fan entity** (per HV ventilation circuit):
- Continuous speed slider: 0–100% (temporary override, keeps time program active); values below the device minimum are clamped to 15 %, 0 turns the circuit off
- Turn on/off toggle (standby mode)
- Configurable turn-on mode: resume the last observed week1/week2 program, or explicitly activate week1/week2 (resume defaults to week1 until a weekly program has been observed)
- Debounced slider input (1.5s) to prevent API rate-limiting
- Turning on/off cancels any slider command still waiting for its debounce timer; commands already being sent finish in order

**Climate entity** (per HK heating circuit):
- Target temperature control, limited to the range Hoval reports for the circuit (5–30 °C until it is known)
- Current room temperature (v1.0.0 — reads the correct `roomTempActual` live value)
- HVAC modes: Heat (constant program) / Auto (last observed weekly program) / Off (standby)
- HVAC action reflects actual circuit status

**Water heater entity** (per WW hot-water circuit, v1.0.0):
- Target temperature in 1 °C steps (the controller stores whole degrees) — sets a temporary boost that expires at midnight, then the week program resumes. The allowed range is the one Hoval reports for the circuit (for hot water usually 10–70 °C), and 10–65 °C until it is known. A value outside it is rejected instead of being sent. The controller can still refuse a value inside that range: an UltraSource B heat pump took 51 °C in the afternoon and only 50 °C in the evening. Hoval then answers `HTTP 424 "Failed to activate temporary change"`, and the error message says that the value was most likely too high or too low.
- Operation modes: heat pump (last observed weekly program) and off (standby). Start a boost by setting the target temperature; its status and expiry are shown by the temporary-change sensors. The former misleading high-demand selector is no longer offered.
- Current temperature from the top-of-tank sensor

**Program select** (per HV/HK/WW circuit):
- Switch between week1, week2, eco mode, standby, constant
- Shows user-defined program names from the Hoval app
- Disambiguates duplicate names, including names matching built-in labels, so each choice selects exactly one program
- Current program pre-selected

**Sensor entities** (per circuit, filtered by type):
- **HV:** Outside temperature, exhaust temperature, air volume, humidity (actual/target), program air volume, constant- and eco-mode air volume (from the circuit details, refreshed hourly), room air quality (poor / medium / good; only on units with an air-quality sensor)
- **HK:** Outside temperature, flow temperature (actual/target), room temperature (actual + setpoint), holiday end (timestamp, empty when no holiday program runs), heating/cooling control (diagnostic)
- **BL:** Heat generator temperature (actual/target), return temperature, operating hours, operating hours >50%, switching cycles, heat produced, electrical energy consumed, current output heating, modulation, FA status, electric heater (operating hours, switching cycles, heat produced, energy consumed, active)
- **WW:** Hot water setpoint, tank temperature top (SF1), tank temperature bottom (SF2), holiday end
- **PS:** Buffer target temperature, buffer temperature top (PF1) / bottom (PF2)
- **All:** Status (heating / cooling / active / inactive / ventilation), operation mode, active week program, active day program, temporary change ends (timestamp, v1.0.2 — empty when no override is running)

> Outside temperature is created only on HV/HK circuits (v0.15.4+). Upgrading from ≤0.15.3: any outside-temperature sensors you already have on BL/WW circuits are preserved — only newly-discovered circuits get the filtered set.

**Plant-level sensors:**
- Weather condition and forecast temperature
- Latest event type, message, and timestamp
- Active event count

**Binary sensors:**
- Online/offline (per plant, connectivity class)
- Error status (per plant, problem class — on for an active `blocking`, `locking` or `warning` event, or when any circuit reports `hasError`)
- Air quality guided (per HV circuit, v1.1.0) — on while air quality guides the ventilation (`airQuality.isAirQualityGuided` on the circuit)
- Temporary change (per circuit, running class, v1.0.2) — on while a boost/override is active, with the target `value`, the cloud's `type` label and the `end` time as attributes. The cloud tracks this itself, so an automation that starts a boost no longer has to remember that it did.

**Diagnostics:**
- Full diagnostic data export with automatic PII redaction (tokens, credentials, plant IDs, including IDs embedded in dictionary keys and URLs)

**Options** (configurable per integration entry):
- Turn-on mode: resume / week1 / week2
- Temporary override duration: until end of current phase (default, v0.15.0+) / 4 hours / until midnight
- Polling interval (default and minimum: 15 minutes, as agreed with Hoval in #24; 30 and 60 minutes selectable)

**Services:**
- `hoval_connect.reset_temporary_change` — cancel an active temporary override on a HomeVent fan, heating-circuit climate, or hot-water water_heater entity, returning control to the underlying time program. Target = the fan/climate/water_heater entity. Useful in automations that need to end a manual boost cleanly (instead of waiting for the override to expire).

**Under the hood:**
- 2-step token management (ID token + Plant Access Token) with TTL caching, auto-refresh, and single-flight locking (concurrent requests trigger at most one token refresh)
- Skips API calls when plant is offline, invalidates token cache on reconnect. The gateway reconnects to the cloud about every 48 minutes, and a poll sometimes sees this as "offline" for one poll. A single offline poll keeps the previous values, and only the plant's online sensor shows it. If the plant stays offline for two consecutive polls (about 30 minutes), the circuit entities become unavailable, the integration logs one warning, and it logs one info message when the plant is back online
- Parallel API fetches for circuits, live values, programs, events, and weather, with at most eight circuit fetches in flight
- Control commands are serialized per plant and circuit. Ambiguous write failures (timeouts, connection errors, 429/5xx) are reported without automatically repeating the command; reads retain their transient-error retries.
- Tiered caching reduces API calls: programs and circuit details hourly (including empty responses), weather forecast hourly, events with every poll. Program caches are separate for each plant and circuit.
- A circuit's program endpoint returning HTTP 417 is checked again every 6 hours. Only the first 417 per circuit is logged as a warning, repeats at debug level; a 417 after the programs were available again warns again. So far, heat generator (`BL`) and buffer tank (`PS`) circuits have always answered 417 (#13, #15). Live values continue updating normally; authentication errors, gateway blocks and transient failures do not trigger this pause.
- Hardened against upstream API changes (v1.0.0): paginated `{"content": [...]}` responses are normalized on the circuits, live-values and both plant-event endpoints (not on the weather forecast — a wrapped response there yields no forecast), the plant list follows pagination, and a malformed program or live-value field degrades only its own sensors instead of dropping the whole circuit
- Dynamic entity discovery — new circuits added without restart
- All circuit reads use the `/v3` API (Hoval removed `/v1` circuit endpoints in April 2026); legacy v1 enum values still get normalized to v3 keys as a fallback
- Temporary overrides use the `/v4` API (v0.15.0+) for forward-compatibility; `endOfPhase`/`duration` body shape, reset still on `/v3` DELETE

### Standalone examples

The Python reader in [`examples/hoval_client.py`](examples/hoval_client.py) follows plant pagination, uses v3 circuit discovery, and reads live values, weather and events. Install `requests`, then run `python examples/hoval_client.py` without arguments. **It currently gets HTTP 403 after the login**, because Hoval's gateway also refuses its User-Agent (see [Rate Limits & Notes](#rate-limits--notes)). It prompts for the account email and password (password input is hidden); scripts can supply `HOVAL_EMAIL` and `HOVAL_PASSWORD` through their environment. Every request has a connection/read timeout.

The Bash/curl live-value example also has bounded timeouts and encodes special characters in form/query values. The reviewed fork improvements and deliberately excluded changes are documented in the [GMH224 adoption review](docs/superpowers/specs/2026-09-20-gmh224-adoptions.md).

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

### Local Modbus TCP

Hoval's cloud allows one request per 15 minutes at most, and in November 2026 it blocked this
integration outright ([#24][issue-24]). The Hoval gateway (TTE-GW, "Gateway v2") can also be read
directly in your own network over **Modbus TCP**. That is fast (default every 30 seconds), needs no
Hoval account and keeps working when the cloud is down or blocked. The integration can use it in two ways:

- **Hybrid:** you keep your cloud entry and add the gateway in its options, or you set up a new
  entry with **Cloud account + local Modbus (hybrid)**. Live values, program and
  setpoints of the ventilation unit then come from the gateway; the cloud still supplies what only it
  has, at the configured cloud interval (at least 15 minutes).
- **Local only:** no account at all. You add the gateway as its own entry.

When you add the integration, the menu offers three options: **Hoval Connect cloud account** (cloud only,
as before), **Cloud account + local Modbus (hybrid)** and **Local gateway (Modbus TCP, no account)**. The last
two need Modbus switched on at the gateway, see step 1.

Scope: **HomeVent (HV) units only.** Heating, hot-water, solar and other circuits stay cloud-only.

#### 1. Switch Modbus on at the TopTronic E panel

**Modbus is off by default.** On the controller panel open **Service > TTE-GW > Modbus > Einstellungen**
(the panel's menu is in German), switch **Aktiv** on and set **Mode = TCP** (not RTU, which is RS485).
Parity and baud rate only matter for RS485. Hoval recommends a fixed IP address for Modbus TCP (DHCP set to
"No" in Service > TTE-GW > General), and so do we: the integration finds the gateway by that address.
The gateway must be reachable from Home Assistant on port 502. The panel may show UnitID 0, but the gateway
has been seen to answer on **unit id 1**, so enter 1 first. If setup reports that no unit answered, try the
unit id shown on the panel.

If setup says the gateway is reachable but does not answer Modbus requests, Modbus is most likely still
switched off: the gateway accepts the connection on port 502 either way and only stays silent when Modbus is
off. "Could not reach the gateway" means the address or the network is wrong instead.

**The gateway answers only one Modbus client at a time (measured).** While Home Assistant holds its
connection, a second client (Loxone, a Modbus tool, a second Home Assistant) connects but gets no answer, which
looks exactly like "Modbus switched off". Other tools cannot poll the gateway in parallel; close them before
setting up here. For the same reason a second entry for a gateway that another Hoval Connect entry already uses
is refused ("gateway already in use"). When you change the gateway address in the options, the integration
briefly releases its own connection so the check can reach the gateway.

#### 2. Add the gateway

- **Hybrid, new entry:** **Add Integration > Hoval Connect > Cloud account + local Modbus (hybrid)**.
  Step 1 asks for your Hoval account, step 2 for the gateway (host, port 502, unit id 1, Modbus polling
  interval, and the plant if your account has several). The result is the same as the next point: a cloud
  entry whose options hold the gateway.
- **Hybrid, existing entry:** **Settings > Devices & services > Hoval Connect > Configure**, then fill in *Modbus gateway
  host* (IP address or name of the gateway), port (502), unit id (1) and the *Modbus polling interval*
  (10, 30, 60 or 120 seconds, default 30). If your account has several plants, pick the one the
  gateway belongs to. An empty host means cloud only, exactly as before. Existing entities keep their
  entity IDs, history and long-term statistics. Saving checks a new or changed gateway address;
  saving other options leaves the gateway alone.
- **Local only:** **Add Integration > Hoval Connect > Local gateway (Modbus TCP, no account)** and enter
  host, port and unit id. The integration finds the HV units itself. This creates new entities (it is
  not a drop-in for an existing cloud entry). **Reconfigure** changes the gateway's address later.

#### What each mode provides

| | Hybrid | Local only |
|---|---|---|
| Live values: air volume, humidity, target humidity, exhaust and outside temperature | Modbus, every poll | Modbus, every poll |
| Control status, operating selection, air-quality control, CO₂/VOC (the sensors removed in v1.1.0, **back under their old entity IDs**; CO₂/VOC only where a sensor is fitted) | Modbus | Modbus |
| Exhaust fan target, weeks until maintenance, filter-cleaning interval, active error code | Modbus | Modbus |
| *Gateway clock offset* (diagnostic, plant level): how many seconds the gateway's clock differs from Home Assistant's, with the gateway time as attribute. Read every 5 minutes; only created if the gateway has the datapoint | Modbus | Modbus |
| Program select, fan on/off | written over Modbus | written over Modbus |
| Number entities: constant and eco modulation (15–100 %), target humidity (30–65 %) | Modbus | Modbus |
| Buttons: acknowledge maintenance, acknowledge filter cleaning | Modbus | Modbus |
| Week/day names, program air volume, temporary change with end time, holiday, room air quality | cloud | not available |
| Weather, forecast, plant events | cloud | not available |
| *Online* and *Error* of the plant | cloud (unavailable while the cloud is down) | *Online* on while the gateway answers / *Error* on while an error entry is active |
| `hoval_connect.reset_temporary_change` | works | raises an error (there is no cloud temporary change) |

If the gateway stops answering, the Modbus-backed entities become unavailable, and so do the fan and
the program select of that unit. They do not fall back to the cloud's live values. In a local-only
entry this means every entity: *Online* does not turn *off*, it becomes unavailable like the rest.

#### Fan percentage: hybrid and local differ

- **Hybrid:** the slider sets a **temporary change in the cloud**, as before. The time program stays
  active and the change ends by itself; the summer-boost blueprint keeps working. If the cloud call
  fails, you get the error. Nothing is written locally instead.
- **Local only:** there is no temporary change. The slider writes the *constant program* setpoint and
  switches the program to **constant**. This is persistent and visible in the program select until
  you pick another program. The unit then modulates normally around that setpoint.

#### Acknowledge buttons

*Acknowledge maintenance* and *Acknowledge filter cleaning* write 7 or 8 to Hoval's datapoint 21054 of the unit,
as Hoval's own comment for that register describes. Hoval's datapoint list marks the register
**read-only** in the same row, and **the gateway confirms even writes it ignores**: it echoed a write
to its own (read-only) clock correctly and did nothing. A confirmed button press therefore proves
nothing. Press a button only when the panel shows the matching message, then **check the *Weeks until
maintenance* sensor**: the integration re-reads the unit's maintenance values right after the press
and writes the comparison to the Home Assistant log (counter reset from X to Y weeks, or a warning
that the counter did not change). For *Acknowledge filter cleaning* there is no countdown datapoint,
so the result cannot be verified over Modbus; check the panel.

#### If the cloud is unavailable

In hybrid mode every successful cloud poll stores the plant's name and its HV circuits in the config
entry. If the cloud fails (blocked, offline, wrong login) or Home Assistant restarts while it is
down, the entry starts from that stored list and keeps running on Modbus alone. If you add the
gateway while the cloud already refuses the account, nothing has been stored yet; the entry then
takes the plant and its ventilation units from the entities it already has, provided they belong to
one plant (or to the plant chosen in the options). The cloud is retried
only at its own slow interval, never at the Modbus interval. Meanwhile everything only the cloud
supplies is unavailable: heating and hot-water circuits, week and day names, temporary change,
weather, events and the plant's *Online* and *Error*. After a rejected login the cloud stays
suspended until you re-authenticate, and Modbus keeps working. If the login is rejected at start-up
and the gateway does not answer either, the entry stops and asks you to re-authenticate instead of
retrying, because every retry would log in again.

### Known Limitations

- **HV, HK, BL, WW, and PS circuits only.** Solar (SOL), fresh water (FRIWA), and other circuit types are not yet implemented.
- **BL energy sensors:** Heat produced and electrical energy consumed are in MWh (verified on UltraSource B Compact).
- **No time program editing.** Time programs can be read but not modified through the integration.
- **No energy/temperature history.** Historical statistics endpoints are documented but not yet integrated.
- **No holiday mode control.**
- **No detailed controller status and no CO₂/VOC values from the cloud (since v1.1.0; [Local Modbus TCP](#local-modbus-tcp) brings them back for HV units).** Hoval asked for the endpoint they came from to be dropped (#24). Coarse stand-ins from the circuit list follow in the next release. See [Sensors removed in v1.1.0](#sensors-removed-in-v110) for what each one maps to.
- **One config entry per Hoval account.** The same account email cannot be added twice (the entry's unique ID is the lowercased email), but several Hoval accounts can run side by side — each entry gets its own coordinator and polls independently.

### Sensors removed in v1.1.0

Up to v1.0.18 these sensors read raw controller datapoints through
`GET /api/telemetry-data/snapshots/live/{plantId}`. Hoval asked third-party integrations to
stop calling that endpoint ([#24][issue-24]), so v1.1.0 no longer does.

| Sensor (English / German) | Circuit | Datapoint | Status since v1.1.0 |
|---|---|---|---|
| Control status / Regelungsstatus | HV | `39652` | **No replacement.** No other endpoint reports why the unit deviates from its schedule (normal, VOC, humidity, frost protection, CoolVent, fault, summer humidity, switch-off stop). `operationMode` stays `ventilation` during CoolVent and summer humidity. |
| Control status / Regelungsstatus | HK | `2051` | **Partly replaced** in the next release ([#14](https://github.com/trcyberoptic/hoval-connect-api/issues/14)): *Status* now shows heating / cooling / active / inactive, *Holiday end* shows a running holiday program, and *Heating/cooling control* shows the control type on reversible heat pumps. Frost protection, screed drying, SmartGrid and the other detail states have no replacement. |
| CO₂ extract air / CO₂ Abluft | HV | `37606` | **Partly replaced** in the next release by *Room air quality* / *Raumluftqualität*: poor / medium / good instead of a percentage, from `airQuality.actualRoomAirQuality` on the circuit list. Created only on units with an air-quality sensor. |
| VOC extract air / VOC Abluft | HV | `37608` | **Partly replaced.** As above. |
| VOC outside air / VOC Aussenluft | HV | `37611` | **Partly replaced.** As above. |
| Air quality control (code) / Luftqualitätsregelung (Code) | HV | `39600` | **Partly replaced** by the new binary sensor *Air quality guided* / *Luftqualitätsgeführt*: on/off instead of the raw code. |
| Operating selection / Betriebswahl | HV | `40650` | **Covered** by the program select, which shows the same value. |
| Constant mode air volume / Luftmenge Konstantbetrieb | HV | `40651` | **Kept.** Same entity, now read from the circuit details once per hour. |
| Eco mode air volume / Luftmenge Sparbetrieb | HV | `40686` | **Kept.** As above. |

The update deletes the entities of the removed sensors, so they do not linger as
`unavailable`. Automations, scripts and dashboards that use them need to be adjusted. We
have asked Hoval to provide the missing values through another endpoint or in its planned
official API; if that happens, the sensors will come back.

### Requirements

- A Hoval Connect account (same credentials as the Hoval Connect mobile app)
- Hoval Connect (this integration) v1.1.0 or newer: Hoval blocks older releases (see the note under [Installation](#installation-hacs))
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
| POST | `/v4/plants/{plantId}/circuits/{circuitPath}/temporary-change` | 🔑🏭 | Current variant (used by integration v0.15.0+ and Hoval Connect Android app). JSON body: `{"type": "endOfPhase"\|"duration", "value": <float>, "duration": <hours>\|null}` — `duration` in **hours** (fractions allowed, e.g. `0.5`), app range 0.5..24 |
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
| GET | `/api/telemetry-data/snapshots/live/{plantId}` | 🔑🏭 | `dataPoints` (comma-separated) | **Deprecated by Hoval; not used by the integration since v1.1.0 at Hoval's request (#24).** Raw device datapoints. Address as `UnitId.FunctionGroup.FunctionNumber.DatapointId`, i.e. the `circuitPath` plus the CAN DatapointId — e.g. `520.50.0.39652` → `{"520.50.0.39652":"1"}`. Modbus **register** numbers (`23631`) are not accepted and return `200 {}`, exactly like garbage input. |

#### GET `/api/telemetry-data/snapshots/live/{plantId}`

> **Deprecated.** Hoval announced on 2026-11-19 that this endpoint will be retired and asked
> third-party integrations to stop calling it ([#24](https://github.com/trcyberoptic/hoval-connect-api/issues/24)).
> The integration has not called it since v1.1.0. The notes below are kept as documentation only.

Reads raw controller datapoints — considerably more than `live-values` exposes, including the ventilation
control status that no other endpoint reports.

**Addressing:** `UnitId.FunctionGroup.FunctionNumber.DatapointId`. The first three groups are the
circuit path you already know from `/v3/plants/{id}/circuits`; the fourth is the CAN **DatapointId**.
The Modbus *register* number is a gateway-side artefact and is not accepted — it shifts with the unit id
(HV unit 513 → register 23540, unit 520 → 23631) while the DatapointId stays 39652. A wrong identifier
returns `200 {}` rather than an error, so an empty result means "unknown address", not "no data".

```
GET /api/telemetry-data/snapshots/live/{plantId}?dataPoints=520.50.0.39652,520.50.0.38606
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

**Heating circuit (HK) datapoint** — reported in [#14](https://github.com/trcyberoptic/hoval-connect-api/issues/14),
read as `1.1.0.2051` on three heat-pump plants:

| DatapointId | Meaning | Type |
|---|---|---|
| `2051` | **Status Heizkreisregelung** (Modbus register 1501 for HK1, 1502/1503 for HK2/HK3) — 0 off, 1–3 normal/comfort/eco heating, 4 frost, 5 forced heat consumption, 6 forced throttling, 7 holiday, 8 party, 9–11 normal/comfort/eco cooling, 12 fault, 13 manual, 14 cooling protection, 15 party cooling, 16–19 screed drying (heat-up/steady/cool-down/end), 22/23 cooling/heating on external or constant demand, 26 SmartGrid preferred | U8 enum |

Datapoint ids and their enum labels come from Hoval's TopTronic E datapoint list,
`TTE-GW-Modbus-datapoints.xlsx`. Hoval's published link (`www.hoval.com/misc/TTE/TTE-GW-Modbus-datapoints.xlsx`)
redirects to a host that does not resolve (checked 2026-09-29); a copy is kept in
[nliaudat/esp_canbus](https://github.com/nliaudat/esp_canbus/tree/main/hoval_data_processing).

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
- **Poll sparingly.** There are no documented rate limits, but Hoval blocked clients that
  polled too often (#24). Hoval asked for an interval of at least 15 minutes on
  `GET /v3/plants/{id}/circuits` and `GET /v3/api/statistics/live-values/{id}`, and for
  `GET /api/telemetry-data/snapshots/live/{id}` not to be called at all; it is deprecated.
  Hoval says it plans an official API for third-party integrations.
- **Send a `User-Agent` naming your own software.** The gateway in front of the API
  refuses a list of client identifiers before the request reaches Hoval, answering with
  its own HTML block page (`Microsoft-Azure-Application-Gateway/v2`) instead of JSON.
  `homeassistant`, `python-requests` and, since 2026-11-19, `hoval-connect-api` are refused
  as case-insensitive substrings. Python's `requests` library is therefore blocked by its
  default header, while `curl`, `httpx`, `urllib3`, `Go-http-client` and a dozen others pass.
  Do not leave the header out, and do not disguise your client as a browser or as Hoval's own app.
  If Hoval blocks your identifier, contact connect@hoval.com instead of changing the header.
- **`examples/hoval_client.py` is currently blocked too.** Its User-Agent,
  `hoval-connect-api-example/1.0`, contains `hoval-connect-api`, so the gateway answers
  every request after the login with 403.
- The API may lock out accounts after repeated failed auth attempts
- Some endpoints are partner/business-only (403 for regular users)
- `business/` endpoints may require elevated access roles

## Disclaimer

This documentation was created through API analysis and is not officially supported by Hoval. The API may change at any time. Use responsibly and respect Hoval's terms of service.
