# Threat model: hoval-connect-api

## What this project does and where untrusted input enters

A Home Assistant custom integration (`custom_components/hoval_connect/`, installed through
HACS) that polls Hoval's cloud for heating, ventilation and hot-water plants and sends
control commands back. It runs inside the Home Assistant process, with everything that
process can reach: the whole Home Assistant instance, its configuration directory, and the
user's home network. The repository also holds reverse-engineered API notes (`docs/`,
`README.md`, `CLAUDE.md`), two standalone example clients (`examples/`), an automation
Blueprint (`blueprints/`), and the GitHub Actions workflows that test and release it.

Untrusted input, roughly in order of how much we worry about it:

1. **Every HTTP response from the two hosts the integration talks to**: Hoval's API
   (`BASE_URL` in `const.py`) and the SAP IAS login server (`IDP_URL`). That covers status,
   headers and body. Treat both as attacker-controlled. A cloud that turns hostile, or a
   party that can answer in its place, must not be able to execute code, read the
   credentials, or make the integration send them or its tokens anywhere else. Cloud values
   end up in URL path segments (`plantExternalId`, circuit `path`, checked by
   `api._require_identifier`), in entity names, unique IDs and device-registry entries, in
   entity states and attributes, in log messages, in exception text shown in the UI, and in
   the diagnostics download.
2. **Service calls and entity actions** (`hoval_connect.reset_temporary_change`,
   `fan`/`climate`/`water_heater`/`select` actions). In Home Assistant any logged-in user,
   including a non-admin, and any automation can call these. The values they carry
   (percentages, temperatures, preset and program names, entity IDs) are untrusted.
3. **Config-flow and options-flow input** (email, password, scan interval, override
   duration, turn-on mode). Only an admin can enter these, so we treat them as trusted for
   privilege purposes. The credentials in them are still the most valuable thing to protect.

Trusted: Home Assistant core and the admin who installs and configures the integration.
Also trusted: anyone with write access to this repository.

## What must hold

- The Hoval password, the `id_token` and the Plant Access Token (`X-Plant-Access-Token`)
  are sent only to the IDP or the Hoval API, and only over HTTPS. They never appear in logs
  at any level, in the diagnostics download (`diagnostics.py`), in exception messages, or
  in entity attributes. Users paste their logs and diagnostics into **public** GitHub issues
  when they report bugs, so a leak there is a public leak. `privacy.redact_remote_error_body`
  is the barrier for the response bodies we log at WARNING.
- The account email, plant IDs (15 digits), and plant names and locations are personal
  data. The diagnostics download promises to redact them (`README.md`, "Diagnostics"), and
  so do response bodies that get logged. A plant ID in a log line's request path is
  deliberate: the line has to name the failing endpoint (see `CLAUDE.md`). A plant ID
  grants nothing without that account's tokens.
- No value from the cloud or from a service call can send an authenticated request to
  another host, change which endpoint or plant a request addresses (through `/`, `..`, `%`,
  `?`, `#` or whitespace in a path segment), or inject headers or query parameters.
- Control requests (`POST`/`DELETE`) are never repeated after an ambiguous failure
  (timeout, connection loss, 5xx). The cloud may already have executed them, so a repeat
  could execute a heating or ventilation command twice. Only programs in `_VALID_PROGRAMS`
  and finite values are sent.
- A malformed or malicious response can make the integration unavailable, but it must not
  crash Home Assistant, block its event loop, or use unbounded CPU or memory. Examples:
  endless pagination (capped at 50 pages), deep JSON nesting, or regex backtracking in
  `privacy.py` on a crafted body.
- The release workflow builds what every HACS user installs. Nobody without write access
  must be able to change a release, a `v*` tag, or code that runs with a write token.

## Components that matter most / least

- **Most:** `api.py` (HTTP client, token handling, retries, URL building), `privacy.py`
  (log redaction), `diagnostics.py`, `coordinator.py` (parses every cloud response),
  `config_flow.py`, the service handler in `__init__.py`, and `.github/workflows/`
  (`release.yml` above all).
- **Medium:** the entity platforms (`fan.py`, `climate.py`, `water_heater.py`, `select.py`,
  `sensor.py`, `binary_sensor.py`): validating values before a control call, and what ends
  up in states and attributes.
- **Least:** `examples/`. These scripts are run by hand by a developer, with their own
  credentials. They are in scope at low severity.
- **Out of scope:** Home Assistant core, aiohttp, voluptuous and other third-party code
  (report those upstream); Hoval's cloud itself; `docs/` (prose, plus
  `docs/openapi-v3.json`, a copy of Hoval's public API spec); `tests/` and
  `.oss-scanner/test_ha_harness.py` (not shipped).

## How to exercise it

The scan has no network, so the real cloud cannot be reached and nothing should try to. All
commands run from `/src`:

- `python -m pytest tests/`: the repository's ~500 tests, done in seconds. They stub Home
  Assistant out through `sys.modules`. Pure functions are tested directly and are good
  fuzzing targets: `privacy.redact_remote_error_body`, `api._require_identifier`,
  `api.build_v4_temporary_change_body`, `diagnostics._anonymise_coordinator_data`.
  `tests/test_source_contracts.py` asserts on the source as **text**, so some changes need a
  matching contract update.
- `ruff check . && ruff format --check .`: the lint gate a patch has to pass.
- `/opt/ha/bin/python -m pytest .oss-scanner/test_ha_harness.py`: loads the integration into
  a **real** Home Assistant (in the `/opt/ha` venv, with
  `pytest-homeassistant-custom-component`) against a fake cloud on 127.0.0.1. To check
  whether something is really reachable, edit the fake cloud's `responses` there, or call a
  service with `hass.services.async_call`. That beats reasoning from the stubs. The file's
  docstring explains why `aioclient_mock` does not work here.

## How you rate severity

This is a single-maintainer integration whose users are homeowners. Calibrate as follows:

- **Critical:** code execution in the Home Assistant process triggered by a cloud response
  or by a non-admin Home Assistant user. Also: anyone without write access getting code
  into a release or a `v*` tag.
- **High:** the password, `id_token` or Plant Access Token disclosed anywhere except the
  IDP or the Hoval API. That includes logs at any level and the diagnostics download.
  Also: credentials or tokens sent to another host or over plain HTTP; a cloud value or a
  service-call value redirecting an authenticated request to another host, endpoint or
  plant; GitHub Actions script injection that an outside contributor can reach (fork pull
  request, issue, comment).
- **Medium:**
  - Any personal data in the diagnostics download, or the account email in a log at
    WARNING or above.
  - A control command repeated, or sent with a value or program the user did not ask for.
  - A non-admin user using this integration's services to do something Home Assistant's
    permission model would otherwise refuse.
  - A response that crashes Home Assistant, blocks its event loop, or exhausts memory or
    CPU. A cloud-only attack is capped at medium unless it gives code execution: the
    attacker must already answer for Hoval's TLS endpoints.
- **Low:** personal data in DEBUG logs (users enable those only on purpose); a plant name
  or location in a WARNING log; a denial of service that heals at the next poll; anything
  in `examples/`; missing hardening without a demonstrated path to one of the outcomes
  above.

## Anything to leave alone

- `CLIENT_ID` in `const.py` and `examples/` is the public OAuth client ID of Hoval's own app.
  It is not a secret.
- The integration deliberately sends its own `User-Agent` instead of Home Assistant's. Hoval's
  gateway refuses Home Assistant's (see `CLAUDE.md`), so this is not spoofing.
- TLS goes through Home Assistant's shared aiohttp session and its SSL context. Lack of
  certificate pinning is not a finding.
- Plant IDs in log lines (see "What must hold"). Report them only where they reach the
  diagnostics download.
- `hacs/action@main` and `home-assistant/actions/hassfest@master` are on branch refs on
  purpose; the reason is in `validate.yml`. Do not report the branch ref itself.
- Token-like strings in `tests/` are fixtures.
- Weaknesses of Hoval's cloud inferred from the notes in `docs/`, `README.md` or `CLAUDE.md`
  are not ours to fix; do not probe for them.
- Anything that needs a malicious Home Assistant admin, or write access to this repository,
  is out of scope.

## Reports and patches

Please keep patches small and in the style of the surrounding code. A patch must keep
`python -m pytest tests/` and `ruff check . && ruff format --check .` green and must keep
working on the oldest supported Home Assistant (2024.11, see `hacs.json`). It must not add
runtime requirements (`manifest.json` has none). Name the file and function, and give the
cloud response or service call that triggers the problem. A failing test in `tests/` or a
run of the harness is the most useful reproducer.
