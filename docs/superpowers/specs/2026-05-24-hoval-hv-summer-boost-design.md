# Hoval HomeVent Summer-Boost Automation — Design Spec

**Status:** Draft for review
**Author:** trcyberoptic (with Claude Opus 4.7)
**Date:** 2026-05-24

## 1. Goal

Auto-boost the Hoval HomeVent ventilation to 90 % during warm afternoons when at least one (non-office) room exceeds a comfort threshold AND the outside air is moderate AND cooler than indoors. When any of those conditions falls away, restore the underlying time program automatically.

The boost is intended as passive cooling assistance — HomeVent doesn't actively cool, but blowing more outside air through the heat exchanger when outside < inside helps drop indoor temperature without opening windows.

## 2. Non-goals

- Not an active cooling control — no compressor, no setpoint chasing.
- Not for HK heating circuits. Boost applies only to a HV (HomeVent) circuit.
- Not a multi-zone system — one HomeVent unit, one boost target.
- Not a permanent override — every boost has a finite lifespan and reverts to the user's normal schedule.
- Not bundled inside the `hoval_connect` integration. Automation logic lives in a Blueprint so it is editable in HA's UI, traceable via Automation Trace, and not coupled to one user's room layout.

## 3. Form

A single Home Assistant Blueprint shipped in this repository under
`blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml`, importable via the standard HA Blueprint import mechanism (raw URL or "Import Blueprint" in the UI).

The integration side ships one supporting service (already implemented in v0.15.1):
`hoval_connect.reset_temporary_change` — targets a fan/climate entity, cancels the underlying v3 `DELETE /v3/.../temporary-change` so the time program takes over again.

## 4. Decisions captured from brainstorming

| Decision | Choice | Reason |
|---|---|---|
| Boost mechanism | `fan.set_percentage` → temp-change override; end via `hoval_connect.reset_temporary_change` | Hoval `temporary-change` preserves the underlying program; the new HA service lets us cancel cleanly without re-applying the program. |
| Outside-temp check | Both `outside < 25 °C` AND `outside < indoor_max` | The fixed cap covers "absolute moderate"; the dynamic check prevents blowing warmer air into a cooler house. |
| Hysteresis | 0.5 °C buffer on both room and outside thresholds | Avoid flapping when a sensor sits on the threshold. |
| Min boost duration | 15 minutes | Once started, suppress immediate re-toggle if a sensor dips for a few seconds. |
| Standby handling | Don't boost if HV is in `standby` program | Honors the user's "no ventilation" intent (e.g. when away). |
| User-override respect | If fan % ≠ boost % while we believe boost is active → release | Don't fight the user; they have priority. |
| Notifications | Companion-App push on start & end, with reason text | Lightweight ops visibility. |
| Sensor list | User-provided Blueprint inputs | One Blueprint serves any home layout; office (excluded) is a separate input for self-documentation. |

## 5. Architecture

### 5.1 Blueprint inputs (Hoval-specific)

| Name | Type | Notes |
|---|---|---|
| `fan_entity` | entity_selector (`domain: fan`, `integration: hoval_connect`) | The HV fan to control. |
| `program_select` | entity_selector (`domain: select`, `integration: hoval_connect`) | Used only to read current program for the standby check and to put the program name in notifications. The boost end uses `reset_temporary_change`, not a program switch. |
| `outside_temp_sensor` | entity_selector (`domain: sensor`, `device_class: temperature`) | Typically the `*_outside_temperature` sensor exposed by the integration. Any source is acceptable. |

### 5.2 Blueprint inputs (room & UX)

| Name | Type | Notes |
|---|---|---|
| `room_sensors` | entity_selector (`domain: sensor`, `device_class: temperature`, multiple) | The rooms that participate in the trigger. |
| `excluded_sensor` | entity_selector (`domain: sensor`, single, optional) | An additional sensor to ignore even if listed in `room_sensors` (e.g. office). Keeps the office input self-documenting. |
| `notify_service` | text (default `notify.notify`) | Service name without leading `service:`. Example: `notify.mobile_app_pixel_8`. |

### 5.3 Blueprint inputs (thresholds & timing)

| Name | Type | Default | Notes |
|---|---|---|---|
| `window_start` | time | `09:00:00` | Local time. |
| `window_end` | time | `22:30:00` | Local time. |
| `room_high` | number | `23.0` °C | Boost-on threshold. |
| `room_low` | number | `22.5` °C | Boost-off threshold (hysteresis). |
| `outside_high` | number | `25.0` °C | Hard cap. |
| `outside_low` | number | `25.5` °C | Hysteresis upper bound — boost ends if outside rises above this. |
| `min_duration_minutes` | number | `15` | Suppress release within this many minutes after start, except on user-override. |
| `boost_percentage` | number | `90` | Used for both setting and override detection. |
| `excluded_programs` | text (list) | `["standby"]` | Programs that suppress boost. |

### 5.4 Prerequisite helpers (user-created)

Because Blueprints cannot create helpers, two helpers must exist before importing the automation. They are referenced as Blueprint inputs:

| Name | Type | Purpose |
|---|---|---|
| `boost_active` | `input_boolean` | True while we believe a boost is in effect. Survives HA restart. |
| `boost_started_at` | `input_datetime` (date+time) | Timestamp of the most recent boost start. Used for the min-duration gate. |

The Blueprint description includes the two `input_boolean.yaml` / `input_datetime.yaml` snippets the user pastes into `configuration.yaml`, or instructions for creating them in the UI. Both are surfaced as entity selectors in the Blueprint form (`domain: input_boolean` and `domain: input_datetime` respectively), so the user picks the helper they created.

### 5.5 Trigger model

The automation re-evaluates state on every relevant change. Triggers:

1. `state` on any sensor in `room_sensors` (HA's state trigger accepts a list).
2. `state` on `outside_temp_sensor`.
3. `state` on `fan_entity` (catches manual percentage changes for override detection).
4. `state` on `program_select` (catches the user switching to/from standby).
5. `time` at `window_start` and `window_end` (drives the day boundary).
6. `time_pattern` every minute (safety net — covers the `min_duration` gate and any missed state event).

### 5.6 Action: state-machine sketch

The action block branches with `choose`:

```text
if boost_active == on:
    if fan_percentage != boost_percentage:        # user override
        → set boost_active = off (no API call, no notification)
        stop
    if now < boost_started_at + min_duration:
        stop                                       # honor minimum boost duration
    if should_exit_boost(now):
        → hoval_connect.reset_temporary_change(fan_entity)
        → boost_active = off
        → notify "Boost ended — reason: <X>; program: <current>"
        stop
    # else: nothing to do; boost stays
else:  # boost_active == off
    if should_enter_boost(now):
        → fan.set_percentage(fan_entity, boost_percentage)
        → boost_started_at = now
        → boost_active = on
        → notify "Boost started — room <X> at <T>°C, outside <O>°C"
```

`should_enter_boost` (all must hold):

- `window_start ≤ now ≤ window_end`
- `outside_temp` is a number AND `outside_temp < outside_high`
- For at least one room sensor that is NOT `excluded_sensor` AND is a number: `room_temp > room_high`
- `outside_temp < max(room_temp for non-excluded room sensors)` — don't blow in warmer air
- `program_select` not in `excluded_programs`

`should_exit_boost` (any one triggers exit):

- `now > window_end` OR `now < window_start`
- `outside_temp` unavailable OR `outside_temp > outside_low`
- All non-excluded room sensors that are numbers satisfy `room_temp < room_low` (none of them are warm anymore)
- `program_select` becomes one of `excluded_programs`

### 5.7 Notifications

Both notifications go through `{{ notify_service }}` (rendered as a template at call time so the user can put `notify.mobile_app_pixel_8` etc).

- **Start**: title "HV Boost gestartet", message includes the hottest non-excluded room (name + temp) and the current outside temp.
- **End**: title "HV Boost beendet", message includes the exit reason (`"Außentemp zu warm"` / `"alle Räume abgekühlt"` / `"Zeitfenster vorbei"` / `"HV im Standby"`) and the current program from `program_select`.

The user-override branch deliberately does NOT send a notification — the user just touched the slider and seeing a notification immediately after would be noise.

## 6. Edge cases & failure modes

| Scenario | Behavior |
|---|---|
| Outside sensor `unavailable` | `should_enter_boost` returns false; if boost is active, `should_exit_boost` returns true → boost ends, reason "Außentemp nicht verfügbar". |
| All room sensors `unavailable` | Treated as "no warm room" → boost ends if active, never starts. |
| Some room sensors `unavailable` | Skipped from the `any > room_high` check and from the `max(...)` indoor reference; the rest decide. |
| HA restart mid-boost | `boost_active` and `boost_started_at` persist (they are helpers). On first tick, automation continues from the resumed state. The cloud-side `temporary-change` outlives short HA outages because its TTL is `endOfPhase` (or whatever the user configured). |
| User manually sets fan to 90 % outside boost | `boost_active` is off, fan reads 90 %, the trigger fires on state change. `should_enter_boost` may return true → automation sets it to 90 % again (no-op on the cloud) and marks `boost_active = on`. On the next exit it will call `reset_temporary_change` and revert to program. This is acceptable; it matches the spec ("auto-boost when conditions met"). |
| User changes fan % mid-boost | Detected by trigger 3; action takes the `boost_active && fan_percentage != boost_percentage` branch → release without API call. The cloud override the user just made stays in force until it expires or until they reset it themselves. |
| Hoval API down at boost-start | `fan.set_percentage` raises; the automation logs an error but doesn't set `boost_active = on`. Next trigger retries. |
| Hoval API down at boost-end | `hoval_connect.reset_temporary_change` raises; we leave `boost_active = on` so the next trigger will retry. **Risk:** if the cloud already cleared the override but our DELETE failed for unrelated reasons, we believe a boost is active that isn't. In practice the next `time_pattern` tick will retry and the second DELETE just no-ops. |
| HV circuit transient absence (offline plant, race during refresh) | `fan_entity` becomes unavailable. `should_enter_boost` short-circuits because `outside_temp` (a HV circuit sensor) goes unavailable too. Exit branch fires if boost was active; reset call may fail, see row above. |
| Day boundary at `window_end` while boost active and conditions still met | Exit branch fires; boost ends with reason "Zeitfenster vorbei". |
| `min_duration` not yet elapsed but user override detected | User override always wins (we drop boost tracking, no notify). |

## 7. Files this design touches

```
blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml    # new
README.md                                                          # extend HA section with blueprint install instructions
docs/superpowers/specs/2026-05-24-hoval-hv-summer-boost-design.md # this file
```

No changes to `custom_components/hoval_connect/` — the integration's contribution shipped in v0.15.1.

## 8. Open questions

1. **Helper auto-provisioning.** HA Blueprints cannot create helpers. Acceptable that users create two helpers manually before import, or do we want a small documentation pre-flight script? *Recommendation:* document in the Blueprint description; do not script it.
2. **Notification language.** Spec assumes German strings (matches the user's HA UI). If we want to ship the Blueprint for broader use, all literal strings would need to move to inputs or a translation system. *Recommendation:* ship with German strings for now; extract to inputs if anyone asks.
3. **`excluded_programs` representation.** A `text` input with a comma-separated list is brittle; a multi-select selector keyed off the integration's program list would be better but Blueprints can't introspect another entity's options. *Recommendation:* ship as a comma-separated text input; document the canonical values.

## 9. Verification plan

When the Blueprint is implemented:

- **Cold start** (boost off, conditions met): triggering one room above 23 °C with outside at e.g. 22 °C → automation sets fan to 90 %, `boost_active` flips on, notify fires.
- **Outside crosses 25.0 °C** while boost active and inside still hot: boost continues until outside crosses 25.5 °C (hysteresis), then exit.
- **Min duration gate**: start boost, immediately move all rooms below 22.5 °C — boost should remain on for the configured minutes, then exit.
- **User override**: during boost, set fan to 50 % via the slider → `boost_active` flips off, no reset call, no notification.
- **Restart**: start boost, restart HA, confirm `boost_active = on` and `boost_started_at` survived; next tick continues from there.
- **Outside sensor unavailable**: pull the integration cable / mark sensor unavailable — boost ends if active.
- **Notification format**: visually check the push payload contains a sensible room name and temps.

A short markdown checklist of these will go into the writing-plans output.
