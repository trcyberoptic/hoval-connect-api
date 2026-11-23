# Changelog

All notable changes to this integration are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The text of a release's section is also the body of its
GitHub release (see `.github/workflows/release.yml`).

## [2.0.0] - 2026-11-23

### How to update

1. Install the update under **Settings > Updates**, or in HACS (called *Marketplace* and built in
   from Home Assistant 2026.11).
2. **Restart Home Assistant completely.** Reloading the integration is not enough.
3. That is all for an existing setup. Entity IDs, history, automations and the summer-boost blueprint
   stay as they are. The cloud is still polled at most every 15 minutes.

**Optional: local Modbus TCP for HomeVent (HV) ventilation units.** Home Assistant then reads the unit
directly from the TopTronic E gateway every 30 seconds, also when the cloud is down or blocked.

- On the controller panel, open **Service > TTE-GW > Modbus > Einstellungen**, switch **Aktiv** on
  and set **Mode = TCP**. Modbus is off by default.
- In Home Assistant, open **Settings > Devices & services > Hoval Connect > Configure** and enter the
  gateway's IP address, port 502 and unit id 1.
- The gateway answers only one Modbus client. Stop other tools that poll it (for example Loxone).
- With Modbus, the sensors that 1.1.0 removed come back under their old entity IDs: control status,
  operating selection, air-quality control, and CO₂/VOC on units with an air-quality sensor.

Details: README, section "Local Modbus TCP".

**After the update, check these entities:**

- *Status* now shows translated values (heating, cooling, active, inactive, ventilation). The stored
  states do not change, so automations that compare them keep working. A value outside this list now
  shows as unknown.
- *Operating selection* (with Modbus): 1.0.x showed "eco" when the unit ran "constant". 2.0.0 shows the correct
  value, so the old history of this sensor can disagree with the new values.

**Coming from 2.0.0-beta.1:** install the update as usual. **To go back to 1.1.0:** HACS (Marketplace) > Hoval
Connect > three-dot menu > Redownload > select `v1.1.0` > restart Home Assistant.

**Kurz auf Deutsch:** Update unter **Einstellungen > Updates** installieren (oder in HACS, ab Home
Assistant 2026.11 «Marketplace») und Home Assistant komplett neu starten. Bestehende
Einrichtungen laufen ohne weitere Schritte weiter. Optional gibt es einen lokalen Modbus-Betrieb für
HomeVent-Lüftungen: am Bedienteil **Service > TTE-GW > Modbus > Einstellungen** öffnen, **Aktiv**
einschalten und **Mode = TCP** setzen, dann in Home Assistant **Einstellungen > Geräte & Dienste >
Hoval Connect > Konfigurieren** die Adresse des Gateways eintragen (Port 502, Unit-ID 1).

### Overview

Version 2.0.0 adds a local mode and includes everything since v1.1.0.

**Towards Hoval's cloud, 2.0.0 identifies as `hoval-connect-api/1.1.0`**, the only version Hoval has
admitted so far ([#24](https://github.com/trcyberoptic/hoval-connect-api/issues/24)). Its cloud requests are those of 1.1.0 or
fewer: the same 15-minute minimum and hourly slow data, and no `live-values` for units that Modbus
serves. It will send its own version once Hoval admits 2.x. Whenever the cloud is blocked, the local
Modbus mode keeps the ventilation working.

### Added

- **Local Modbus TCP mode for HomeVent (HV) ventilation units** behind Hoval's TopTronic E gateway
  (TTE-GW). Values are read every 10, 30, 60 or 120 seconds (default 30) without the cloud. Setup
  offers three options: **Hoval Connect cloud account** (as before), **Cloud account + local Modbus
  (hybrid)** and **Local gateway (Modbus TCP, no account)**. See the README section "Local Modbus TCP".
- **Hybrid as a drop-in.** An existing cloud entry gets a gateway in its options (**Configure**).
  Entity IDs, history, long-term statistics and automations stay. Modbus serves the ventilation
  values and controls; the cloud still supplies program names, weather, events and the temporary
  change.
- **Sensors from v1.0.x are back for HV units, under their old entity IDs and with their old
  history:** control status, operating selection, air-quality control, and CO₂/VOC where a sensor is
  fitted.
- **New entities (HV, via Modbus):** exhaust fan target, weeks until maintenance, pre-filter
  cleaning interval, active error code, *Gateway clock offset* (diagnostic, plant level), number
  entities for the constant and eco air volume and the target humidity, and buttons to acknowledge
  maintenance and filter cleaning.
- **Controls over Modbus:** the program select and fan on/off are written to the gateway at once
  and read back. In hybrid mode the fan percentage still sets a cloud temporary change, so the
  summer-boost blueprint keeps working. In local mode it switches to the constant program with that
  air volume.
- **Resilience in hybrid mode.** The plant and its HV circuits are stored in the config entry. If the
  cloud is down or blocks the integration, or Home Assistant restarts while it is, the Modbus
  entities keep working; cloud-only entities become unavailable. The cloud is retried only at its
  own slow interval. A rejected login starts re-authentication once and stops further logins.
- **Setup hints.** The dialogs explain that Modbus is off by default and how to switch it on at the
  panel (**Service > TTE-GW > Modbus > Einstellungen**: *Aktiv* on, *Mode* = TCP). "Gateway not
  reachable" and "reachable but no Modbus answer" are separate errors.
- **One client at a time.** The gateway answers only one Modbus client. A second entry for a gateway
  that another entry already uses is refused (`gateway_in_use`). While the options check a changed
  gateway address, the integration releases its own connection for a moment.
- **Status sensors from the circuit list** as coarse stand-ins for datapoints the cloud no longer
  offers (no extra request): *Status* is now an enum (heating, cooling, active, inactive,
  ventilation), *Room air quality* (poor, medium, good; only on HV units that report an air-quality
  sensor), *Heating/cooling control* (HK, diagnostic) and *Holiday end* (HK and WW, timestamp). The
  value sets come from the Hoval Connect app; none has been seen on a real plant yet.

### Changed

- **User-Agent stays `hoval-connect-api/1.1.0`** (see above). The version Home Assistant and HACS
  show is 2.0.0.
- **Fewer cloud requests in hybrid mode.** `live-values` are no longer fetched for units that
  Modbus serves. Cloud-only entries send the same requests as v1.1.0.
- **Local entries skip cloud-only entities.** A local-only entry has no weather, week names or
  temporary change; *Online* and *Error* come from the gateway.

### Fixed

- **Operating selection mapping.** v1.0.x decoded the operating selection as 3 = constant and
  4 = eco. Hoval's datapoint list says 4 = constant and 5 = eco, which 2.0.0 follows. The history of
  that sensor from v1.0.x may show "eco" where the unit actually ran "constant".
- **User-Agent canary in CI** counts a 401 from the backend as accepted (the request passed the
  gateway), instead of reporting a false block.

### Notes

- HV (ventilation) units only. Heating, hot-water and other circuits stay cloud-only.
- The acknowledge register (datapoint 21054) is marked read-only in Hoval's list although its
  comment describes acknowledging with 7 or 8. This has not been verified on a real maintenance.
  The gateway also echoes writes it ignores, so a confirmed button press proves nothing; check the
  *Weeks until maintenance* sensor and the Home Assistant log.
- CO₂ and VOC are only available on units with an air-quality sensor.
- Writes over Modbus (program, fan, air volumes, humidity) are read back from the gateway, but it
  is not yet verified that they reach the unit's own controller and display panel.

## Earlier releases

Releases before 2.0.0 are listed on [GitHub Releases](https://github.com/trcyberoptic/hoval-connect-api/releases).

[2.0.0]: https://github.com/trcyberoptic/hoval-connect-api/compare/v1.1.0...v2.0.0
