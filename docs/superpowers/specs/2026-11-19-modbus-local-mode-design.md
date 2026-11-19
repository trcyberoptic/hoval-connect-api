# Local Modbus TCP mode (hybrid and local-only) — design

Date: 2026-11-19. Status: draft, awaiting owner approval.

## Intent and why

On 2026-11-19 Hoval blocked this integration's User-Agent for polling too much
(#24). The agreed compliant version polls every 15 minutes at most, no longer
reads raw datapoints, and every release needs Hoval's consent before it ships.
A re-block can arrive unannounced. The integration therefore lost fast values,
the ventilation control status (39652) and CO2/VOC, and it depends on a cloud
it cannot rely on.

The TopTronic E gateway (TTE-GW, "Gateway v2") speaks **Modbus TCP on port 502**
once it is switched on at the TopTronic E panel
(`Service > TTE-GW > Modbus > Einstellungen`, `Mode = TCP`). Modbus is local,
fast, needs no account, and exposes the datapoints the cloud no longer serves,
writable ones included.

The owner wants this as part of `hoval_connect`, **as a drop-in**: existing
entities keep their unique IDs, entity IDs, device, area, history and long-term
statistics, so dashboards, the summer-boost blueprint and external scripts keep
working. Values that only the cloud has (program/week names, weather, events,
temporary change with its end time) should still come from the cloud when an
account is configured. The integration must also run **without any Hoval
account** (local only).

## Goals

- **G1 Hybrid.** An existing cloud entry can be given a Modbus gateway in its
  options. HV circuits whose unit answers on Modbus then take their live values,
  program and setpoints from Modbus on a short interval (default 30 s). The
  cloud keeps supplying what only it has, at Hoval's cadence (≥ 15 min).
- **G2 Local only.** A new config-flow path "Local (Modbus TCP)" needs only host,
  port and Modbus unit id, and discovers the HV units itself.
- **G3 Resilience.** In hybrid mode a cloud failure (block, outage, plant
  offline, rejected login) leaves every Modbus-backed entity working; only
  cloud-sourced entities go unavailable. This includes a Home Assistant restart
  while the cloud is down: the entry starts on Modbus alone.
- **G4 Restore lost datapoints** from Modbus: ventilation control status
  (39652), operating selection (40650), air-quality control (39600), CO2/VOC
  (only where a sensor is fitted), plus new ones: exhaust fan target (38600),
  maintenance and pre-filter counters, active errors.
- **G5 Local control.** Program select and fan on/off write the operating
  selection over Modbus; normal/eco modulation and humidity setpoint become
  writable number entities; two buttons acknowledge a due maintenance and a
  due pre-filter cleaning.
- **G6 Less cloud load.** In hybrid mode the integration stops fetching
  `live-values` and circuit details for Modbus-served HV circuits.

## Non-goals

- HK, WW, BL, PS, SOL over Modbus. Only the owner's HV unit can be verified;
  writing to heating circuits blind is too risky. The register map must be
  per circuit type so these can be added later.
- Modbus RTU (RS485 port).
- A "weeks remaining" sensor for the pre-filter cleaning: neither datapoint
  list has such a register (only its on/off switch and its interval).
- A local stand-in for the cloud's temporary change with auto-revert.
- Turning a local-only entry into a hybrid one (add a cloud entry instead).
- A `pymodbus` dependency (see Decisions).

## Anti-goals (would be a failure even if it works)

- Any change to existing unique IDs or entity IDs, or to the behaviour of an
  entry that has no Modbus configured.
- More cloud requests than today, or any change to the User-Agent handling.
- Silent fallbacks that change meaning — e.g. the fan percentage in hybrid mode
  quietly writing a permanent "constant" program when the cloud call fails.
- Writing a register the datapoint list marks read-only, or a value outside its
  documented range. The one deliberate exception is the acknowledge datapoint 21054
  (Modbus address 28939 for unit 520; see Decisions), which Hoval marks read-only but documents as written.

## Measured facts (2026-11-19, owner's gateway, read-only FC03, Modbus unit id 1)

- Panel settings: `UnitID 0`, `Mode TCP`; the gateway answers on unit id **1**
  (FINDINGS of the firmware notes). Whether it also answers 0 is unchecked.
- Addresses in the datapoint CSV/xlsx are the **protocol addresses** as sent on
  the wire: reading 23622..23634 returns exactly the values the cloud showed at
  the same time (air volume 45, target humidity 54, exhaust 23.1 °C, constant
  50 %, eco 65 %; outside 9.3 vs cloud 9.5 and humidity 50 vs 51 from slightly
  different moments).
- One FC03 request for the 13-register HV block works; each request took
  0.5–1.6 s (new TCP connection per request, gateway on Wi-Fi).
- **Absent units answer exception 2** (Illegal Data Address) in 0.2–1.3 s, for
  single registers and whole blocks (tested: units 513, 514, 521). Discovery by
  scanning is therefore possible.
- Error entries are readable one 9-register entry at a time (24150..24158 for
  unit 520); a 45-register read across all five entries answers exception 2.
  "No error" reads as times 0, source 0xFFFF, FG/FN/type 0xFF, code 0xFFFF.
- The maintenance block 28934..28946 reads in one request:
  `[1, 0, 52, 0, 2, 0, 255, 0, 0, 0, 1, 0, 14]` — maintenance on, interval 52
  weeks, **2 weeks remaining**, ack 0, CO2 255 (not fitted), raw CO2/VOC 0,
  pre-filter cleaning on, its interval 14 weeks.
- Earlier session (firmware notes): the first read of a register the gateway has
  not cached can take > 3 s or return **exception 6** (busy); later reads come
  from its cache. Client timeout 6–10 s, retry on 6.
- Registers 0 and 1476 returned 51 earlier and 1 now; they are **not** an
  outdoor temperature for this plant. Do not use them.
- **Write test (FC06), done with the owner's consent:** eco modulation 23624
  written 65 → 66 while the program was week1 (eco modulation has no effect
  there). The gateway echoed the request (1.2 s), read-back gave 66 at +0/2/5 s;
  written back to 65 and read back 65. Not yet shown: that the value reached the
  HV controller and not only the gateway's cache (the panel or a physical
  effect such as a program switch would show it).
- **Two lists compared.** The old CSV and Hoval's German datapoint list (copy in
  `BernhardSchlegel/hoval-gateway-modbus/docs/TTE-GW-Modbus-datapoints-german.csv`)
  agree on address, datapoint, type, scale, range and writability for all 26
  registers of unit 520 — except datapoint 21054 Quittierung (address 28939): Hoval's list says
  `Writable = No`, the old CSV says `Yes`. Hoval's own comment on it reads
  "acknowledge with value 7 = maintenance, 8 = cleaning".
- **For LIST types the "Min. value" column is a bitmask of the valid values**:
  Betriebswahl `0x37` = {0, 1, 2, 4, 5} (exactly the five programs, 3 unused),
  Quittierung `0x180` = {7, 8}, Luftqualität Regulierung and both
  "Konfiguration" registers `0x3` = {0, 1}.

## HV register map (offsets within a unit's block)

The register numbers depend on the CAN unit id (HV 513–528); the datapoint ids
do not. Ship a small generated table for HV 513–528 from
`Hoval-GatewayV2-CANBUS-MQTT/hoval_datapoints.csv` (do not ship the 166 KB CSV).
Unit 520 shown:

| Reg | DP | Name (Hoval) | Type | Scale | W | Maps to |
|---|---|---|---|---|---|---|
| 23622 | 40650 | Betriebswahl Lüftung | LIST | – | yes | `active_program`: 0 standby, 1 week1, 2 week2, 4 constant, 5 ecoMode (3 undefined) |
| 23623 | 40651 | Normal-Lüftungsmodulation | U8 | % | yes (15–100) | `constant_value` |
| 23624 | 40686 | Spar-Lüftungsmodulation | U8 | % | yes (15–100) | `eco_mode_value` |
| 23625 | 38606 | Lüftungsmodulation (Luftmenge Soll) | U8 | % | no | `live_values.airVolume`, `target_value` |
| 23626 | 40687 | Feuchte Sollwert | U8 | % | yes (30–65) | `live_values.humidityTarget` |
| 23627 | 37600 | Feuchtigkeit Abluft | U8 | % | no | `live_values.humidityActual` |
| 23628 | 37608 | VOC Abluft | U8 | % | no | sensor, 255 = not fitted |
| 23629 | 37611 | VOC Aussenluft | U8 | % | no | sensor, 255 = not fitted |
| 23630 | 39600 | Luftqualität Regulierung | LIST | – | yes | `is_air_quality_guided` (value labels unknown, see Open) |
| 23631 | 39652 | Status Lüftungsregelung | U8 enum | – | no | 0 off, 1 normal, 2 VOC, 3 humidity, 4 frost protection, 5 CoolVent, 6 fault, 7 summer humidity, 8 switch-off stop |
| 23632 | 0 | Temperatur Aussenluft | S16 | 0.1 °C | no | `live_values.outsideTemperature` |
| 23633 | 37602 | Temperatur Abluft | S16 | 0.1 °C | no | `live_values.exhaustTemp` |
| 23634 | 38600 | Ventilator Fortluft soll | U8 | % | no | new sensor |
| 24150 + 9k | 29042+k | Aktiver Fehler k+1 (k = 0..4) | U16/U8 | – | no | error code, source, FG/FN, type, appear/disappear time+date |
| 28934 | 20034 | Konfiguration Wartung | LIST | – | no | 0 off / 1 on |
| 28935–36 | 20037 | Wartungszählwert (weeks) | U32 | – | no | interval |
| 28937–38 | 21058 | Restlaufzeit Wartungszähler (weeks) | S32 | – | no | new sensor |
| 28939 | 21054 | Quittierung | LIST | – | see Decisions | buttons: 7 = acknowledge maintenance, 8 = acknowledge cleaning |
| 28940 | 37606 | CO2 Abluft | U8 | % | no | sensor, 255 = not fitted |
| 28944 | 41612 | Konfiguration Reinigung Vorfilter | LIST | – | no | 0 off / 1 on |
| 28945–46 | 41613 | Reinigungszählwert (weeks) | U32 | – | no | interval |

32-bit values are high word first. Null/sentinel values become `None`: U8 255,
U16 0xFFFF, S16 0x8000/0x7FFF.

## Approaches considered

- **A — Modbus as a second data source inside `hoval_connect` (chosen).** Same
  domain, same entities, same unique IDs; the cloud entry gets Modbus via its
  options, and a local-only path is added to the config flow. True drop-in, and
  every blocked user of the integration gets a way out. Cost: cloud and local
  code share one package, and every release still needs Hoval's consent in #24
  because the cloud path sends the version in the User-Agent.
- B — a separate `hoval_modbus` integration that takes over the old entity IDs.
  Free release cadence and side-by-side comparison, but duplicates the entity
  code, needs takeover logic, and loses device/area assignments.
- C — a YAML package for HA's core `modbus` integration. No code, but fan and
  program select only via template workarounds; not "like hoval-connect-api".

The owner chose A, extended by the hybrid combination and the local-only path.

## Decisions

Decided by the owner:
- Approach A; hybrid mode (Modbus + cloud) **and** local-only mode.
- Fan percentage in hybrid mode keeps setting a **cloud temporary change**, so
  the summer-boost blueprint and `reset_temporary_change` keep working.

Calls made in the design (the owner can overrule):
- **Scope HV only** in this step.
- **Config.** First config-flow step becomes a menu: "Hoval Connect (cloud)" →
  today's flow unchanged; "Local (Modbus TCP)" → host, port (502), Modbus unit
  id (1). The cloud entry's options flow gains optional Modbus host/port/unit id
  and a Modbus interval (10 / 30 / 60 / 120 s, default 30). Empty host = cloud
  only, exactly as today.
- **Local entry identity.** Config-entry unique id `modbus:<host>:<port>:<unit>`
  (duplicate guard); a reconfigure step changes host/port. The entity-level
  `plant_id` of a local entry is derived from the entry id, so entity unique ids
  survive a host change. Circuit paths use the cloud format `<unit>.50.0`, so a
  later type extension and the hybrid merge use one key.
- **Discovery.** Local entry: scan the 13-register HV block of units 513–528 at
  setup and on reload; units answering are circuits, exception 2 means absent.
  Hybrid: the cloud circuit list names the HV paths; Modbus serves a path when
  its unit's block answers.
- **Plant binding (hybrid).** One Modbus gateway per entry, bound to one plant
  id stored in the options. The options flow shows a plant selector only when
  the account has more than one plant; with one plant it is bound implicitly.
  Modbus data is merged into that plant only, and writes go only to it.
- **Starting without the cloud (hybrid).** Each successful cloud poll stores
  the bound plant's id and name and its HV circuit paths and names in the
  entry. At startup, if the cloud fails (any error, including a rejected
  login), the entry starts from that stored topology on Modbus alone instead of
  raising `ConfigEntryNotReady`; a rejected login still starts the reauth flow.
  Unique ids stay `{plant_id}_{path}_{key}`, so this is also how the old entity
  IDs survive a permanent block or a deleted account. A local-only entry gets
  new unique ids and is therefore not a drop-in; that is intended.
- **One coordinator per entry.** With Modbus configured its `update_interval` is
  the Modbus interval; the cloud fetch inside the update is gated by its own
  TTL (the existing cloud scan interval, floor 15 min), and between cloud
  fetches the last cloud snapshot is reused. Without Modbus nothing changes.
- **Merge.** For a Modbus-served circuit, the Modbus fields in the map above
  overwrite the cloud ones each tick. Cloud-only fields (`operation_mode`,
  `circuit_status`, temporary change, program/week/day names, program air
  volume, holiday end, room air quality, weather, events, plant online) stay
  cloud-sourced. The fan's `is_on` uses the Modbus operating selection (exact)
  instead of the optimistic standby override when Modbus is present.
- **Cloud load (G6).** For Modbus-served HV circuits skip `live-values` and the
  circuit-details fetch (constant/eco values come from Modbus). Circuits list,
  programs, weather and events stay on their current cadence.
- **Routing of control actions.** Program select, fan off (standby) and fan on
  (resume / week1 / week2 per the existing turn-on option) write register
  40650 over Modbus in both modes. Fan percentage: hybrid → cloud temporary
  change (unchanged; if the cloud fails, raise the error, no fallback); local →
  write normal modulation (40651) and set the program to constant (4), which is
  persistent and visible in the select. `reset_temporary_change` in a local
  entry raises a clear "not supported without Hoval account" error.
- **After a control action** refresh Modbus at once; refresh the cloud only after
  a cloud action (temporary change), as today.
- **Cloud failure in hybrid.** Cloud-sourced entities go unavailable as today;
  the update must not fail as a whole while Modbus data is fresh.
- **Modbus failure.** Modbus-backed fields become `None` (entities unavailable),
  logged once at WARNING on the transition and at INFO on recovery, mirroring
  the offline handling. No automatic fallback to cloud `live-values`.
- **New/revived entities (Modbus only).** Revive the retired v1.1.0 sensors
  under their **old keys** (`hv_control_state`, `hv_operating_selection`,
  `hv_air_quality_control`, `hv_co2_extract`, `hv_voc_extract`,
  `hv_voc_outside`) so their old history continues; the retired-entity cleanup
  must skip keys a Modbus-served circuit provides. New: exhaust fan target,
  maintenance weeks remaining, pre-filter cleaning (on/off and interval), an
  active-error sensor (code of the first active entry) feeding the plant error
  binary sensor in local mode. Numbers: normal modulation, eco modulation
  (15–100 %), humidity setpoint (30–65 %). CO2/VOC entities only when the
  first read is not 255 (existing `exists_fn` pattern).
- **Local-only plant level.** One plant device per local entry; `online` =
  Modbus reachable; `error` = an active error entry. No weather/forecast/event
  entities.
- **Modbus client.** Own small asyncio client on the stdlib, no `pymodbus`
  (the manifest has no requirements today, and HA core pins its own
  `pymodbus`). FC03 read and FC06 write single register; one persistent TCP
  connection per gateway with reconnect; one request in flight (lock);
  timeout 10 s; on exception 6 retry up to 3 times with a short backoff;
  ~50 ms pause between requests; exception 2 → field `None`, not an error;
  check transaction id and unit id of every response.
- **Write safety.** Only registers marked writable; validate the value against
  the datapoint's min/max, and for LIST types against the bitmask in
  "Min. value", before sending; read the register back after writing and raise
  if it differs.
- **Acknowledge buttons (owner's request).** Two button entities per HV unit,
  "Acknowledge maintenance" (writes 7) and "Acknowledge filter cleaning"
  (writes 8) to datapoint 21054 (Modbus address 28939 for unit 520). This is the one register written although Hoval's list
  marks it read-only, allowed only with those two values. No read-back
  comparison (it is an action, not a setting); success is the gateway's echo,
  and the next maintenance read should show the counter reset. If the gateway
  answers with an exception, the button raises a clear error naming it. Not
  tested live yet: acknowledging now would reset a counter that is not due;
  verify at the next real maintenance or cleaning message.
- **Error-log poll cadence.** HV block every tick; error entries and the
  maintenance block on a slower TTL (e.g. 5 min), since they are 6 extra
  requests at ~1 s each.
- **Release.** Unchanged process: bump `INTEGRATION_VERSION` and agree the
  version with Hoval in #24 before shipping. Local-only users do not depend on
  that, but the package is the same.
- **Tests** follow the repo convention (pure functions, HA stubbed): frame
  encode/decode, decoding and sentinels, register-map generation for 513–528,
  merge rules, routing per mode, write validation, discovery against a fake
  asyncio Modbus server on 127.0.0.1, and source contracts (no `pymodbus`
  import, no new cloud calls on the Modbus path).
- **Live verification** on the owner's gateway: compare Modbus and cloud values
  side by side; every write test (program, modulation, humidity setpoint) only
  after the owner agrees to that specific write, because it changes the real
  ventilation.

## Left to the builder

File layout and class names, translation keys and labels (DE/EN/FR as the repo
has them), option-flow layout, exact backoff numbers, how the register table is
generated and stored, test file split.

## Open (to verify live, not blocking the plan)

- Value labels of 39600 (the CSV has none); which value means "air-quality
  guided".
- Whether 38606 (air volume setpoint) reflects a running cloud temporary change.
- Whether the gateway accepts a persistent connection for hours and a second
  concurrent client.
- Whether unit id 0 is answered too.
- Exception-6 frequency in steady state.
