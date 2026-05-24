# Hoval HomeVent Summer-Boost Blueprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Home Assistant Blueprint that auto-boosts the Hoval HomeVent ventilation to 90 % when at least one non-office room exceeds a comfort threshold AND outside air is moderate AND cooler than indoors, with a comfort-target exit, user-override release, minimum-boost duration, and push notifications on start/end.

**Architecture:** Single self-contained `automation` Blueprint shipped under `blueprints/automation/trcyberoptic/`. State persists across HA restarts via two user-created helpers (`input_boolean` + `input_datetime`) referenced as Blueprint inputs. End-of-boost cleanup uses the `hoval_connect.reset_temporary_change` service shipped in v0.15.1. No further code changes to `custom_components/hoval_connect/`.

**Tech Stack:** Home Assistant Blueprint YAML (Jinja2 templates), Hoval Connect integration v0.15.1+, HA built-ins (`fan.set_percentage`, `input_boolean.turn_*`, `input_datetime.set_datetime`).

**Reference spec:** [`docs/superpowers/specs/2026-05-24-hoval-hv-summer-boost-design.md`](../specs/2026-05-24-hoval-hv-summer-boost-design.md)

**Why no unit-test TDD:** Home Assistant Blueprints have no in-repo unit-test surface; validation runs in HA itself (config check + automation trace + live behavior). Each task ends with an explicit HA-side verification step instead of a pytest invocation.

---

## Task 1: Scaffold Blueprint file with full input schema

**Files:**
- Create: `blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml`

**Why first:** A Blueprint must parse as YAML and pass HA's `blueprint:` schema before any trigger/action can be tested. Shipping the input schema first lets us import it into HA and visually confirm the form before any behavior is wired up.

- [ ] **Step 1: Create the Blueprint file with header, full input schema, and a no-op action**

Create `blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml` with exactly this content:

```yaml
blueprint:
  name: Hoval HomeVent Sommer-Boost
  description: >-
    Bläst die Hoval HomeVent automatisch auf 90 %, wenn an einem warmen
    Nachmittag mindestens ein nicht-ausgeschlossener Raum eine
    Komfortschwelle überschreitet UND die Außenluft moderat UND kühler als
    drinnen ist. Beendet den Boost, sobald ALLE Räume unter der
    Komfort-Zieltemperatur liegen, das Zeitfenster vorbei ist, die Außenluft
    zu warm wird, der Benutzer den Slider manuell ändert oder die HV in
    Standby geht. Voraussetzung: zwei Helper (input_boolean +
    input_datetime), die in der README beschrieben sind, sowie das
    Integration-Plugin in mindestens Version v0.15.1 (liefert den Service
    hoval_connect.reset_temporary_change).
  domain: automation
  source_url: https://github.com/trcyberoptic/hoval-connect-api/blob/master/blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml
  homeassistant:
    min_version: "2024.6.0"
  input:
    fan_entity:
      name: HomeVent Fan-Entity
      description: Die HV-Fan-Entity der Hoval-Integration.
      selector:
        entity:
          domain: fan
          integration: hoval_connect
    program_select:
      name: HomeVent Programm-Select-Entity
      description: >-
        Die Program-Select-Entity der Hoval-Integration. Wird gelesen, um
        Standby-Suppression und den Programmnamen in Notifications zu
        liefern. Wird nicht zum Beenden des Boosts geschrieben.
      selector:
        entity:
          domain: select
          integration: hoval_connect
    outside_temp_sensor:
      name: Außentemperatur-Sensor
      description: >-
        Quelle der Außentemperatur. Üblicherweise sensor.*_outside_temperature
        aus der Hoval-Integration; jede andere Temperatur-Quelle ist auch ok.
      selector:
        entity:
          domain: sensor
          device_class: temperature
    room_sensors:
      name: Raumtemperatur-Sensoren
      description: Räume die im Trigger und im Komfort-Ziel-Check mitzählen.
      selector:
        entity:
          domain: sensor
          device_class: temperature
          multiple: true
    excluded_sensor:
      name: Auszuschließender Sensor (optional)
      description: >-
        Ein Sensor, der trotz Aufnahme in `room_sensors` ignoriert werden
        soll — z. B. das Büro. Leer lassen wenn kein Ausschluss gewünscht.
      default: ""
      selector:
        entity:
          domain: sensor
          device_class: temperature
    boost_active:
      name: Helper input_boolean.* (Boost aktiv)
      description: >-
        Vom Benutzer angelegter input_boolean. Spiegelt wider, ob die
        Automation aktuell boostet. Überlebt HA-Restart. Siehe README.
      selector:
        entity:
          domain: input_boolean
    boost_started_at:
      name: Helper input_datetime.* (Boost-Startzeit)
      description: >-
        Vom Benutzer angelegter input_datetime mit Datum+Zeit. Markiert den
        Startzeitpunkt des aktuellen Boosts für das min_duration-Gate.
      selector:
        entity:
          domain: input_datetime
    notify_service:
      name: Notify-Service
      description: >-
        Vollständiger Service-Name für Push-Benachrichtigungen, z. B.
        notify.mobile_app_pixel_8. Leer lassen oder notify.notify nutzen, um
        Notifications zu deaktivieren bzw. an alle aktiven Notifier zu schicken.
      default: notify.notify
      selector:
        text:
    window_start:
      name: Zeitfenster-Start
      description: Lokale Uhrzeit ab der die Automation evaluiert.
      default: "09:00:00"
      selector:
        time:
    window_end:
      name: Zeitfenster-Ende
      description: Lokale Uhrzeit ab der nicht mehr geboostet wird; aktiver Boost wird erzwungen beendet.
      default: "22:30:00"
      selector:
        time:
    room_high:
      name: Trigger-Temperatur (°C)
      description: Boost beginnt sobald irgendein nicht-ausgeschlossener Raum diese Schwelle überschreitet.
      default: 23.0
      selector:
        number:
          min: 18.0
          max: 30.0
          step: 0.5
          unit_of_measurement: "°C"
          mode: box
    room_target:
      name: Komfort-Zieltemperatur (°C)
      description: >-
        Boost endet erst wenn ALLE nicht-ausgeschlossenen Räume diese Schwelle
        unterschritten haben. Niedriger = aggressivere Kühlung. Der Abstand
        zu `room_high` wirkt zusätzlich als Hysterese.
      default: 21.0
      selector:
        number:
          min: 15.0
          max: 25.0
          step: 0.5
          unit_of_measurement: "°C"
          mode: box
    outside_high:
      name: Außentemperatur-Cap (°C)
      description: Boost beginnt nur wenn die Außenluft unter diesem Wert liegt (verhindert Warmluftzufuhr).
      default: 25.0
      selector:
        number:
          min: 15.0
          max: 35.0
          step: 0.5
          unit_of_measurement: "°C"
          mode: box
    outside_low:
      name: Außentemperatur-Exit (°C)
      description: Aktiver Boost wird beendet sobald die Außenluft diesen Wert überschreitet. Sollte größer als `outside_high` sein (Hysterese).
      default: 25.5
      selector:
        number:
          min: 15.0
          max: 35.0
          step: 0.5
          unit_of_measurement: "°C"
          mode: box
    min_duration_minutes:
      name: Mindest-Boost-Dauer (Minuten)
      description: Einmal gestartet bleibt der Boost mindestens so lange an (außer bei manuellem Override).
      default: 15
      selector:
        number:
          min: 0
          max: 240
          step: 5
          unit_of_measurement: "min"
          mode: box
    boost_percentage:
      name: Boost-Lüfterstufe (%)
      description: Lüfter-Prozentwert beim Boost. Wird auch zur Erkennung von manuellem Override verwendet.
      default: 90
      selector:
        number:
          min: 30
          max: 100
          step: 5
          unit_of_measurement: "%"
          mode: slider
    excluded_programs:
      name: Programme die Boost unterdrücken
      description: >-
        Kommaseparierte Liste von Hoval-Programmwerten in denen der Boost NICHT
        startet bzw. der laufende Boost beendet wird. Kanonische Werte:
        constant, ecoMode, standby, week1, week2, manual, externalConstant.
      default: "standby"
      selector:
        text:

mode: single
max_exceeded: silent

trigger: []

action: []
```

- [ ] **Step 2: Validate Blueprint imports cleanly into HA**

On the user's HA instance:

1. Copy the file to `<HA_config>/blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml` (e.g. via the SSH add-on, Samba, or `Studio Code Server`).
2. Open **Settings → Automations & Scenes → Blueprints**. The new Blueprint should appear under "trcyberoptic".
3. Click **Create automation** on it. The form must render all 17 inputs without errors.

Expected: form renders, all default values pre-filled, no schema errors in HA log.

If the form is missing inputs or HA logs `Invalid blueprint:`, fix the YAML before continuing.

- [ ] **Step 3: Commit**

```bash
git add blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml
git commit -m "feat(blueprint): scaffold HomeVent summer-boost with full input schema"
```

---

## Task 2: Add `variables:` block with all derived template helpers

**Files:**
- Modify: `blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml`

**Why second:** All later branches (triggers don't need it; action and condition templates do) reuse a small set of computed values: hottest room, indoor maximum, current outside temp, etc. Centralizing them in `variables:` keeps the action block readable and avoids re-evaluating expensive expressions.

- [ ] **Step 1: Replace the trailing `trigger: []` and `action: []` lines with a `variables:` block followed by placeholder `trigger:` and `action:`**

Open the file and replace the last three lines (`mode: single`, blank line, `trigger: []`, blank line, `action: []`) with:

```yaml
mode: single
max_exceeded: silent

variables:
  fan_entity: !input fan_entity
  program_select: !input program_select
  outside_temp_sensor: !input outside_temp_sensor
  room_sensors: !input room_sensors
  excluded_sensor: !input excluded_sensor
  boost_active_entity: !input boost_active
  boost_started_at_entity: !input boost_started_at
  notify_service: !input notify_service
  window_start: !input window_start
  window_end: !input window_end
  room_high: !input room_high
  room_target: !input room_target
  outside_high: !input outside_high
  outside_low: !input outside_low
  min_duration_minutes: !input min_duration_minutes
  boost_percentage: !input boost_percentage
  excluded_programs_raw: !input excluded_programs

  excluded_programs: >-
    {{ excluded_programs_raw.split(',') | map('trim') | reject('equalto', '') | list }}

  outside_temp_state: "{{ states(outside_temp_sensor) }}"
  outside_temp_ok: >-
    {{ outside_temp_state not in ['unknown', 'unavailable', 'none', ''] }}
  outside_temp: >-
    {{ outside_temp_state | float(none) if outside_temp_ok else none }}

  valid_room_states: >-
    {% set ns = namespace(items=[]) %}
    {% for s in expand(room_sensors) %}
      {% if s.entity_id != excluded_sensor
            and s.state not in ['unknown', 'unavailable', 'none', ''] %}
        {% set ns.items = ns.items + [{'entity_id': s.entity_id, 'name': s.name, 'temp': s.state | float(0)}] %}
      {% endif %}
    {% endfor %}
    {{ ns.items }}

  hot_rooms: >-
    {{ valid_room_states | selectattr('temp', '>', room_high | float) | list }}
  hottest_room: >-
    {{ (valid_room_states | sort(attribute='temp', reverse=true) | list)[0]
       if (valid_room_states | length) > 0 else none }}
  indoor_max: >-
    {{ (valid_room_states | map(attribute='temp') | max)
       if (valid_room_states | length) > 0 else none }}
  all_rooms_below_target: >-
    {{ valid_room_states | length > 0
       and (valid_room_states | selectattr('temp', '>=', room_target | float) | list | length) == 0 }}

  now_in_window: >-
    {% set now_t = now().strftime('%H:%M:%S') %}
    {{ window_start <= now_t <= window_end }}

  current_program: "{{ states(program_select) }}"
  program_excluded: "{{ current_program in excluded_programs }}"

  fan_percentage: >-
    {{ state_attr(fan_entity, 'percentage') | int(none) if states(fan_entity) not in ['unknown', 'unavailable', 'none'] else none }}

  boost_active: "{{ is_state(boost_active_entity, 'on') }}"
  boost_started_at_ts: "{{ state_attr(boost_started_at_entity, 'timestamp') | float(0) }}"
  min_duration_elapsed: >-
    {{ (as_timestamp(now()) - boost_started_at_ts) >= (min_duration_minutes | int * 60) }}

  should_enter: >-
    {{ now_in_window
       and outside_temp_ok and outside_temp < (outside_high | float)
       and indoor_max is number and outside_temp < indoor_max
       and (hot_rooms | length) > 0
       and not program_excluded }}

  exit_reason: >-
    {% if not now_in_window %}Zeitfenster vorbei
    {% elif not outside_temp_ok or outside_temp > (outside_low | float) %}Außentemp zu warm
    {% elif all_rooms_below_target %}Komforttemperatur erreicht
    {% elif program_excluded %}HV im Standby
    {% else %}none{% endif %}
  should_exit: "{{ exit_reason | trim != 'none' }}"

trigger: []

action: []
```

- [ ] **Step 2: Validate the templates in HA's Developer Tools → Template**

On the user's HA instance, open **Developer Tools → Template** and paste a probe (substituting real entity_ids and values from the user's setup):

```jinja
{% set room_sensors = ['sensor.wohnzimmer_temperatur', 'sensor.schlafzimmer_temperatur'] %}
{% set excluded_sensor = 'sensor.buero_temperatur' %}
{% set ns = namespace(items=[]) %}
{% for s in expand(room_sensors) %}
  {% if s.entity_id != excluded_sensor and s.state not in ['unknown','unavailable','none',''] %}
    {% set ns.items = ns.items + [{'name': s.name, 'temp': s.state | float(0)}] %}
  {% endif %}
{% endfor %}
{{ ns.items }}
```

Expected: a list of `{name, temp}` dicts, one per accessible room sensor.

If a template throws `UndefinedError` or returns the wrong shape, fix the corresponding `variables:` entry.

- [ ] **Step 3: Reload Blueprints + commit**

In HA: **Developer Tools → YAML → Reload all YAML configuration → Blueprints**. The new variables block must parse without errors (errors show up under **Settings → System → Logs**, search `blueprint`).

```bash
git add blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml
git commit -m "feat(blueprint): add variables block with comfort-target + exit-reason computation"
```

---

## Task 3: Add the `trigger:` block

**Files:**
- Modify: `blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml`

**Why third:** With the variables in place we can wire up the triggers. The action is still a no-op; this task verifies the automation listens to the right sources by inspecting the trace.

- [ ] **Step 1: Replace the placeholder `trigger: []` with the real trigger list**

Find the `trigger: []` line and replace it with:

```yaml
trigger:
  - platform: state
    entity_id: !input room_sensors
  - platform: state
    entity_id: !input outside_temp_sensor
  - platform: state
    entity_id: !input fan_entity
    attribute: percentage
  - platform: state
    entity_id: !input program_select
  - platform: time
    at: !input window_start
  - platform: time
    at: !input window_end
  - platform: time_pattern
    minutes: "/1"
```

Leave the `action: []` line as it is for now.

- [ ] **Step 2: Create the automation from the Blueprint in HA**

In HA: **Settings → Automations & Scenes → Blueprints → Hoval HomeVent Sommer-Boost → Create Automation**.

Fill in the form with the user's actual entity_ids and helpers. Save the automation. Name it `Hoval HV Sommer-Boost`.

- [ ] **Step 3: Verify triggers fire in the trace**

Manually nudge one room temperature sensor (e.g. by template-overriding via Developer Tools → States, or just wait for a real change). Open the automation in **Settings → Automations → Hoval HV Sommer-Boost → Traces** and confirm the trace shows the state-change trigger firing.

Also confirm the every-minute `time_pattern` fires (visible as periodic trace entries with no action since `action: []`).

- [ ] **Step 4: Commit**

```bash
git add blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml
git commit -m "feat(blueprint): wire triggers (state, time, time_pattern)"
```

---

## Task 4: Add `action:` — boost-active branch (user override, min-duration, exit)

**Files:**
- Modify: `blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml`

**Why fourth:** The boost-active branch is the more complex one — it has three sub-decisions (override release, min-duration gate, exit-trigger). Building and verifying it before the entry branch lets us test the release path in isolation by manually flipping the helper.

- [ ] **Step 1: Replace `action: []` with the choose block (boost-active branch only)**

Find the `action: []` line and replace it with:

```yaml
action:
  - choose:
      # ─── Branch A: boost is currently active ───
      - conditions:
          - condition: template
            value_template: "{{ boost_active }}"
        sequence:
          - choose:
              # User manually changed the fan speed → release silently
              - conditions:
                  - condition: template
                    value_template: >-
                      {{ fan_percentage is not none
                         and fan_percentage != (boost_percentage | int) }}
                sequence:
                  - service: input_boolean.turn_off
                    target:
                      entity_id: !input boost_active
              # Min duration not elapsed → leave running
              - conditions:
                  - condition: template
                    value_template: "{{ not min_duration_elapsed }}"
                sequence: []
              # An exit reason is active → cancel temp-change + notify
              - conditions:
                  - condition: template
                    value_template: "{{ should_exit }}"
                sequence:
                  - service: hoval_connect.reset_temporary_change
                    target:
                      entity_id: !input fan_entity
                  - service: input_boolean.turn_off
                    target:
                      entity_id: !input boost_active
                  - choose:
                      - conditions:
                          - condition: template
                            value_template: "{{ notify_service | length > 0 }}"
                        sequence:
                          - service: "{{ notify_service }}"
                            data:
                              title: "HV Boost beendet"
                              message: >-
                                Grund: {{ exit_reason | trim }}. Programm: {{ current_program }}.
```

Leave the file with a single open `choose:` (one branch); we'll add the entry branch in Task 5.

- [ ] **Step 2: Verify the user-override release branch**

On the user's HA, manually:
1. Turn on `input_boolean.hoval_hv_boost_active` (in **Developer Tools → States**).
2. Set the fan slider to something ≠ `boost_percentage` (e.g. 50 %).
3. Wait for the next `time_pattern` minute tick (or push another state change to force the trigger).
4. Open the trace — the user-override condition branch should fire, and `input_boolean.hoval_hv_boost_active` should flip back off.

No API call should reach Hoval (verify in **Settings → System → Logs**, search `set_temporary_change` and `reset_temporary_change`).

- [ ] **Step 3: Verify the exit-with-reset branch**

1. Turn on `input_boolean.hoval_hv_boost_active`.
2. Set `input_datetime.hoval_hv_boost_started_at` to a timestamp >`min_duration_minutes` ago (e.g. yesterday).
3. Force conditions to fail (e.g. push the outside-temperature sensor to 30 °C via Developer Tools, OR pick a time outside the window).
4. Wait for the next trigger tick.
5. Expected: trace shows `should_exit = true` → `hoval_connect.reset_temporary_change` called → input_boolean off → notification fired.

Verify in HA logs: `reset_temporary_change` ran successfully; in the Hoval app or via a sensor refresh, the temp-change override should be cleared.

- [ ] **Step 4: Commit**

```bash
git add blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml
git commit -m "feat(blueprint): add boost-active branch (override release, min-duration, exit)"
```

---

## Task 5: Add `action:` — boost-inactive entry branch

**Files:**
- Modify: `blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml`

**Why fifth:** Mirrors Task 4 on the other side of the state machine. Splitting them lets us verify the entry path independently — we can intentionally have `boost_active = off` and watch the conditions evaluate.

- [ ] **Step 1: Add the boost-inactive branch to the existing `choose:`**

Inside the file find the existing single-branch `choose:` from Task 4. Add a second branch *after* the boost-active one (still inside the outer `choose:` list — same indentation as Branch A):

```yaml
      # ─── Branch B: boost is currently inactive ───
      - conditions:
          - condition: template
            value_template: "{{ not boost_active }}"
        sequence:
          - choose:
              - conditions:
                  - condition: template
                    value_template: "{{ should_enter }}"
                sequence:
                  - service: fan.set_percentage
                    target:
                      entity_id: !input fan_entity
                    data:
                      percentage: "{{ boost_percentage | int }}"
                  - service: input_datetime.set_datetime
                    target:
                      entity_id: !input boost_started_at
                    data:
                      timestamp: "{{ as_timestamp(now()) | int }}"
                  - service: input_boolean.turn_on
                    target:
                      entity_id: !input boost_active
                  - choose:
                      - conditions:
                          - condition: template
                            value_template: "{{ notify_service | length > 0 }}"
                        sequence:
                          - service: "{{ notify_service }}"
                            data:
                              title: "HV Boost gestartet"
                              message: >-
                                {% set hr = hottest_room %}
                                {% if hr is not none %}{{ hr.name }} {{ hr.temp }} °C{% else %}Innenraum warm{% endif %},
                                draußen {{ outside_temp }} °C.
```

After this edit the full `action:` should contain one outer `choose:` with two branches: Branch A (boost active) from Task 4 and Branch B (boost inactive) from this task.

- [ ] **Step 2: Verify the entry path**

On the user's HA:
1. Make sure `input_boolean.hoval_hv_boost_active` is **off**.
2. Ensure the HV is NOT in standby (program-select = `week1` / `week2` / `ecoMode` / `constant`).
3. Force conditions to enter: pick a time inside the window; push the outside temp to e.g. 22 °C; push one room sensor to 24 °C.
4. Wait for the next trigger.
5. Expected: trace shows `should_enter = true` → `fan.set_percentage` with the configured boost percentage → `input_datetime.set_datetime` updates the timestamp → `input_boolean.turn_on` flips active → push notification arrives.

Confirm on the Hoval-side: fan reaches the boost percentage; an active temporary override is visible on the circuit.

- [ ] **Step 3: Commit**

```bash
git add blueprints/automation/trcyberoptic/hoval_hv_summer_boost.yaml
git commit -m "feat(blueprint): add boost-inactive entry branch"
```

---

## Task 6: README — Blueprint install instructions + helper YAML

**Files:**
- Modify: `README.md`

**Why sixth:** The Blueprint is unusable without the two helpers. Document them next to the import instructions so a fresh user reading the README gets a working setup on the first try.

- [ ] **Step 1: Locate the right insertion point**

Find the closing of the `### Known Limitations` section (or the next top-level `##` after the HA-integration block). Insert a new `### Summer-boost Blueprint (optional)` section immediately before any `##` heading that follows the HA-integration block. Use `Grep` to find the heading:

Run (Grep tool):
```
pattern: ^### Known Limitations|^## (?!Home Assistant Integration)
path: README.md
output_mode: content
-n: true
```

This gives the exact line number to insert before.

- [ ] **Step 2: Write the new section**

Insert the following at the line found in Step 1 (before that line):

````markdown
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

````

- [ ] **Step 3: Verify the README still renders**

Run (Bash tool):
```
grep -n '^##' README.md | head -20
```

Expected: a clean heading hierarchy with the new `### Summer-boost Blueprint (optional)` slotted under `## Home Assistant Integration`.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: README — summer-boost Blueprint install + helper snippets"
```

---

## Task 7: Acceptance verification on the user's HA + final push

**Files:** none changed; this task only verifies and pushes.

**Why last:** The full behavior set from Section 9 of the spec needs to be exercised against a live HV circuit before we declare the feature done.

- [ ] **Step 1: Push the branch**

```bash
git push origin master
```

- [ ] **Step 2: Run the full verification checklist on the user's HA**

Each of these must pass before declaring the feature shipped. Take notes per row.

| # | Scenario | Setup | Expected | Pass? |
|---|---|---|---|---|
| 1 | Cold start (boost off, conditions met) | Move one room sensor >23 °C, outside ≈22 °C, in time window, HV in `week1` | Fan goes to boost_percentage, `boost_active` on, start notification fires | |
| 2 | Outside hysteresis | While boosting, push outside-temp to 25.2 °C (between cap and exit), then to 25.6 °C | Boost continues at 25.2 °C, ends at 25.6 °C with reason "Außentemp zu warm" | |
| 3 | Comfort target | While boosting, move all room sensors below `room_target` | Boost ends after `min_duration_minutes` with reason "Komforttemperatur erreicht" | |
| 4 | Min-duration gate | Start boost; immediately move all rooms below `room_target` | Boost remains on until `min_duration_minutes` elapsed, then exits | |
| 5 | User override | During boost, slide the fan to 50 % manually | `boost_active` flips off silently; no notification; no API call to Hoval | |
| 6 | HA restart resilience | Start boost, restart HA core (`POST http://supervisor/core/restart`) | After restart `boost_active`=on, `boost_started_at` survived; next tick continues from there | |
| 7 | Outside sensor unavailable | While boosting, mark `outside_temp_sensor` unavailable | Boost ends with reason "Außentemp zu warm" (the catch-all for non-numeric outside) | |
| 8 | Standby suppression | Set HV program to `standby`; force all other conditions to "should enter" | Boost does NOT start; active boost ends with reason "HV im Standby" | |
| 9 | Time-window exit | Boost active, conditions still met, clock hits `window_end` | Boost ends with reason "Zeitfenster vorbei" | |

For each row, capture the relevant trace from **Settings → Automations → Hoval HV Sommer-Boost → Traces** to confirm the right branch fired.

- [ ] **Step 3: Tag a release if everything passes**

If all rows pass, tag a release that documents the Blueprint shipped:

```bash
git tag v0.15.2 -m "Blueprint: HomeVent summer-boost"
git push origin v0.15.2
```

(Pure-doc/Blueprint release; no `manifest.json` bump needed because nothing in `custom_components/hoval_connect/` changed.)

Update the project memory file via the `remember` skill if any non-obvious live finding came out of verification.

- [ ] **Step 4: Done — close out the todos**

Final TodoWrite mark-completed and a short status summary back to the user.

---

## Notes for the implementing engineer

- **Templates with `!input`:** HA replaces `!input <name>` at automation-creation time with the value the user picked, *not* at trigger time. That's why we copy each input into a `variables:` block before referencing it in Jinja — `{{ !input foo }}` is not valid Jinja, but `{{ foo }}` after `foo: !input foo` is.
- **`expand(...)` over an input list:** When `room_sensors` is a Blueprint `entity` input with `multiple: true`, HA passes a Python list of entity_ids. `expand()` accepts that directly; no `state_attr` gymnastics needed.
- **Service name templating:** `service: "{{ notify_service }}"` works in HA YAML actions. Empty-string ↔ no-notification handled with a `choose:` guard rather than a `condition:` to avoid the whole branch failing.
- **`mode: single` + `max_exceeded: silent`:** The minute-pattern trigger fires every 60 s; with single-mode a slow action could drop a tick. `max_exceeded: silent` keeps the log clean while still gracefully dropping overlaps.
- **Why `input_datetime.set_datetime` with `timestamp:`:** HA's `input_datetime` accepts either a `datetime:` string or a `timestamp:` integer. Templates with epoch seconds (`as_timestamp(now()) | int`) are the most reliable — they sidestep timezone-string parsing.
- **Why `reset_temporary_change` is in v0.15.1 only:** The HA service that the exit path calls is shipped by the Hoval Connect integration. Older Hoval Connect versions won't have it; the README prereq line covers this.
