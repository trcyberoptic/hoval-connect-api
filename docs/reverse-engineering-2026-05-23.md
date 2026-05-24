# Reverse-Engineering Report — Hoval Connect 2 v3.2.0

Date: 2026-05-23
APK: `HovalConnect_3.2.0_APKPure.xapk` (versionCode 7173, build 2026-04-26, target SDK 36)
Source: apkpure / `d.apkpure.com/b/APK/com.hoval.connect2?version=latest`

## Goal

Determine whether the Hoval cloud API exposes settings for HomeVent **CoolVent** (external cooling) and **Heizgrenze** (heating limit / outside-temperature cutoff), which are absent from `docs/openapi-v3.json` and the HA integration.

## Approach

App is **React Native + Expo** with Hermes bytecode. All JS is bundled in `assets/index.android.bundle` (~11 MB, Hermes magic `c61fbc03c1031bc0`). Decompilation chain:

1. `apktool d` — manifest + resources
2. `jadx` — native Java stubs (mostly RN/Expo glue, no business logic)
3. `hbc-disassembler` (from `hermes-dec`) — Hermes bytecode + string table (152 MB disasm)
4. `grep "String: '...'"` over disasm — yields every JS string literal

The disasm contains every `LoadConstString` opcode with the resolved string verbatim, so URL templates, endpoint constants, and i18n keys are all recoverable.

## Endpoint inventory (complete)

All `*_ENDPOINT*` constants used by the app's axios client:

```
ACTIVE_CONTRACTS_ENDPOINT
AIR_QUALITY_GUIDED_ENDPOINT_TEMPLATE
BOOTSTRAP_ENDPOINT
CIRCUITS_ENDPOINT_TEMPLATE
CIRCUIT_ENDPOINT_TEMPLATE
CIRCUIT_PROGRAMS_ENDPOINT_TEMPLATE
CIRCUIT_PROGRAMS_SELECTION_ENDPOINT_TEMPLATE
CIRCUIT_SETTINGS_ENDPOINT_TEMPLATE
ENERGY_MANAGER_AVAILABILITY_ENDPOINT
ENERGY_MANAGER_CHART_DATA_ENDPOINT
ENERGY_MANAGER_LIVE_DATA_ENDPOINT
ENERGY_MANAGER_THRESHOLD_ENDPOINT
FULL_PLANT_EVENTS_ENDPOINT_TEMPLATE
GATEWAY_SOFTWARE_ENDPOINT
HEAT_CONSUMPTION_STATISTICS_ENDPOINT
HIGH_FREQUENCY_ENDPOINT
HOLIDAY_ENDPOINT
LATEST_NEWS_ENDPOINT
LATEST_PLANT_EVENT_ENDPOINT_TEMPLATE
LIVE_VALUES_ENDPOINT
LOGIN_COUNT_ENDPOINT
MY_PLANTS_ENDPOINT
NEWS_CONFIRMATION_ENDPOINT  /  NEWS_ENDPOINT  /  NEWS_ID_ENDPOINT  /  NEWS_IMAGE_ENDPOINT_TEMPLATE
OPEN_ERRORS_ENDPOINT
PLANT_ALREADY_CLAIMED_ENDPOINT
PLANT_NOTIFICATIONS_ENDPOINT_TEMPLATE / PLANT_NOTIFICATION_ENDPOINT_TEMPLATE
PLANT_REGISTRATION_ENDPOINT
PLANT_SETTINGS_ENDPOINT_TEMPLATE
PLANT_SHARES_ENDPOINT / PLANT_SHARE*_ENDPOINT  (8 variants)
PUSH_NOTIFICATIONS_ENDPOINT
SEMI_AUTOMATIC_COOLING_ENDPOINT_TEMPLATE
SHOULD_DISPLAY_ENDPOINT
SOLAR_YIELD_STATISTICS_ENDPOINT
STANDBY_ENDPOINT_TEMPLATE
SUBMIT_RATING_ENDPOINT
TEMPERATURE_STATISTICS_ENDPOINT
TEMPORARY_CHANGE_ENDPOINT_TEMPLATE
TOTAL_ENERGY_STATISTICS_ENDPOINT
USER_AVATAR_ENDPOINT
USER_SETTINGS_ENDPOINT
WEATHER_ENDPOINT_TEMPLATE
```

These are 1:1 with `/v3/api-docs` — **no hidden endpoint** is referenced by the app.

Path templates referenced verbatim:
- `/v1/plants/:plantExternalId`
- `/v2/api/holiday/:plantExternalId`
- `/v2/api/weather/forecast/:plantExternalId`
- `/v3/plants/:plantExternalId/circuits`
- **`/v4/plants/:plantExternalId/circuits/:circuitPath/temporary-change`** ← the app already migrated `temporary-change` to v4. Our HA integration is still on v3 — works, but v4 exists.

Suffixes joined at runtime: `/settings`, `/programs`, `/programs/:program`, `/semi-automatic-cooling`, `/temporary-change`, `/air-quality-guided`, `/standby`, `/reset`, `/circuit`, `/partner`, `/notifications`, `/status`, `/device`, `/diagnostic`, `/bulk`, `/invitation-mail`.

### v4 temporary-change — body shape and units (verified live 2026-05-23)

```
POST /v4/plants/{id}/circuits/{path}/temporary-change
Body: {"type": "endOfPhase" | "duration", "value": <float>, "duration": <minutes>|null}
```

Key findings from live probing against the user's HV circuit:

- **`duration` is in MINUTES, not seconds.** OpenAPI declares it loosely as a `double`; the app's `CustomDurationContent` picker exposes `{hours:0,minutes:30}..{hours:24,minutes:0}` = 30..1440 minutes. Live test: `duration=30` → HTTP 204 (success), `duration=1800` → HTTP 424 (1800 minutes = 30 hours, out of range).
- **HV accepts `type=duration`.** My earlier conclusion "HV only supports endOfPhase" was wrong — it was a duration-out-of-range artifact. Both HV and HK accept the same body shape; the cloud's `424 "Failed to activate"` was the API's way of saying "value out of accepted range" rather than "wrong circuit type".
- **v3 body shape rejected by v4 URL.** `POST /v4/... {"value": 70, "duration": "fourHours"}` → HTTP 400 "Failed to read request". The app actually does send the new shape — earlier disasm confusion was mine, not the app's.
- **No v4 DELETE.** Reset stays on `/v3/.../temporary-change` DELETE. Works fine in parallel; the integration uses POST `/v4` for set and DELETE `/v3` for reset.

### Display bug discovered along the way

`coordinator._resolve_active_program_value()` hardcoded `programs["week1"]`. Users running `activeProgram=="week2"` saw the wrong week/day-program names and the wrong phase value in the diagnostic sensors. Fixed in v0.15.0 to pick week1 or week2 based on the circuit's `activeProgram`; non-weekly programs (ecoMode, standby, …) fall back to week1.

## CoolVent / semi-automatic-cooling — belongs to **HK heating circuit** (reversible heat pump), NOT HomeVent

**Initial conclusion was wrong.** I conflated two independent feature areas of the same JS bundle. `/v3/plants/{plantExternalId}/circuits/{circuitPath}/semi-automatic-cooling` is the cooling setpoint endpoint for **HK circuits on reversible heat pumps** (e.g. UltraSource B). It is NOT a HomeVent endpoint.

Evidence for the correction:
- The endpoint body is `value=<°C>` — HomeVent operates in *percent* (fan speed), not degrees. A degree value makes no sense for HV.
- Live probe on user's HV circuit `520.50.0`: `circuitControlType: null` (not `semiAutomatic`), no `cooling` field in `/programs`, no `coolingValue` on any phase. The whole cooling mechanism is absent for HV.
- The `controlStrategy.semiAutomatic.*` i18n keys sitting near `homeVent.*` keys in the bundle proved nothing — both feature areas live in the same monorepo / RN app.

`hasExternalCooling`, `isSemiAutomaticCooling*`, `setCooling`, `activateCooling` etc. all belong to the HK heating-circuit UI for reversible heat pumps — not HomeVent.

### Original (now-superseded) endpoint mechanics, kept for reference

Evidence from `updateCircuits` reducer (disasm `~function #5xxxx`):

```js
case 'setCooling':       // toggle off → re-arm with default
  body = new URLSearchParams({ value: param.value
                                 ?? heatingCircuit.manual.cooling.defaultValue });
  axiosPlantInstance.post(
    getCompletePath(SEMI_AUTOMATIC_COOLING_ENDPOINT_TEMPLATE,
                    { plantExternalId, circuitPath }), body);
case 'activateCooling':  // user enters target °C
  body = new URLSearchParams({ value });
  axiosPlantInstance.post(SEMI_AUTOMATIC_COOLING…, body);
case 'activateAirQualityGuided':
  body = new URLSearchParams({ guided: <bool>.toString() });
  axiosPlantInstance.post(AIR_QUALITY_GUIDED…, body);
```

UI-side gating strings prove this is the HomeVent CoolVent UI:

- `activateManualCoolingRow.externalCoolingInfoModal.title`
- `controlStrategy.semiAutomatic.coolingOperatedManually`
- `hasExternalCooling`, `isSemiAutomaticCooling`, `isSemiAutomaticCoolingActive`
- `onToggleCoolingInSemiManual`
- `homeVent.controlStrategy.airQualityGuided`
- `standbyProgramInfo.homeVent`

The "Manual Cooling" row only renders when `hasExternalCooling === true && circuitControlType === 'semiAutomatic'` — i.e. HomeVent comfort FR with the external cooling option.

### Important: wire format

The app sends `Content-Type: application/x-www-form-urlencoded` (axios + URLSearchParams), body = `value=<float>`. The OpenAPI spec declares `value` as a query parameter — Spring accepts both since `@RequestParam` reads form bodies and query identically. Our HA integration currently has no caller for this endpoint.

## Heizgrenze — confirmed NOT exposed

Searched for: `heizgrenze`, `heatingLimit`, `heatingThreshold`, `heatingPeriod`, `outsideTempLimit`, `outdoorLimit`, `switchPoint`, `summerWinter`, `summerCool`, `nightCool`, `bypass` (Hoval sense), `preHeat`, `postHeat`, `reheater`, `frostProtect`, `heatRecovery`, `enthalpy`, `defrost`.

**Zero matches** in the JS bundle (the only `bypass` hits are MSW mock-library plumbing).

The closest thing the app *does* expose is `weatherImpact` (PATCH `/v3/.../circuits/{path}/settings`):

```
weatherImpact.sliderLabels.comfort
weatherImpact.sliderLabels.eco
weatherImpact.status.{balanced|comfort|eco}
weatherImpact.modal.bulletPoints.global.{comfort|middle|eco}
```

That's a **3-position Comfort↔Eco slider** — not a temperature cutoff. The DTO fields confirm: `outsideTemperature: 0–100` (a percentage / weighting factor) and `solarRadiation: -10..0` (a coefficient), neither is an absolute °C threshold.

Heizgrenze configuration on HomeVent appears to be controller-only (TopTronic E) and not exposed through the Hoval Connect cloud API at all — not just hidden from `/v3/api-docs`, but **not implemented** on the cloud side either, given the app would otherwise call it.

## Implications for the HA integration

1. **HomeVent has NO additional setters** beyond what's already implemented. CoolVent (in Hoval marketing terms) is a HomeVent feature in name only; it is not exposed through the cloud API for HV circuits. Setting Heizgrenze or any bypass/cooling parameter for HomeVent is not possible cloud-side.
2. `semi-automatic-cooling` *could* be added to support reversible-heat-pump HK circuits (UltraSource B etc.), gated on `circuitControlType == semiAutomatic` + `hasExternalCooling`. Out of scope for the user's current setup (HomeVent-only). Skip unless a user with that hardware shows up.
3. Consider upgrading `temporary-change` callers from `/v3` to `/v4` in a future release — the app has already moved.

## Artifacts

`re/` (gitignored):
- `HovalConnect_3.2.0.xapk` — original bundle
- `xapk_extracted/com.hoval.connect2.apk` — base APK
- `apktool_out/` — apktool decoded resources
- `jadx_out/` — jadx Java sources (mostly RN bridge stubs)
- `hermes_disasm.txt` — full Hermes disassembly (152 MB) — primary source of truth
- `all_strings.txt` — 47 425 unique string literals extracted
- `all_endpoints.txt` — endpoint paths only
