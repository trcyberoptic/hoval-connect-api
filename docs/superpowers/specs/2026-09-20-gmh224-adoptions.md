# Selected GMH224 fork improvements

Date: 2026-09-20. Base: `86d9e4e9941a52a45e4dab56e1e43f1e2a7ccf8e` (v1.0.10).
Reviewed fork: [GMH224/hoval-connect-api at dc4f2c7](https://github.com/GMH224/hoval-connect-api/tree/dc4f2c72cc024780f923d3b8c5478550d4df54f6).
Common ancestor: `31e14d28144552584f220ae0b92ec3ff0fc4618e`.

## Decision

Adopt concrete fixes and hardening from the fork, adapted to this integration's telemetry and v4 control architecture. This is not a wholesale merge: the fork has become a controls-only integration, whereas this project continues to provide live-value, raw datapoint, event and weather sensors. The earlier August fork adoption is already part of the base and is not re-applied.

Both repositories use GPL-3.0. Credit for the reviewed improvements belongs to the GMH224 fork; implementations here are adapted and independently tested. The linked revision is the reproducible review source.

## Accepted changes

| Area | Result and validation required |
| --- | --- |
| HTTP retries | Retry transient reads, including nonstandard 5xx, but never automatically repeat an ambiguous control write. Preserve one retry after explicit 401 token rejection and the token-fetch retry policy. Exercise timeout, transport and HTTP failures. |
| Discovery | Prefer v3 `isSelectable`, retain legacy `selectable` and nonselectable BL/WW/PS support. Reject malformed/incomplete plant pagination; filter invalid/duplicate circuits before scheduling requests. |
| Concurrency | Limit circuit fetches to eight, isolate command locks and optimistic mode state by `(plant_id, circuit_path)`, and create each write coroutine only after acquiring its lock. A cancelled lock waiter must not create an abandoned coroutine. |
| Control semantics | HEAT uses `constant`; AUTO, fan resume and hot-water heat-pump mode resume the last observed week1/week2, defaulting to week1. Invalid numeric inputs cannot become cloud commands. Preserve v4 duration semantics and awaited post-control refresh. |
| Fan debounce | ON/OFF cancels unsent slider timers. An already-started write is allowed to finish; subsequent commands queue behind it. Request generations prevent a completed old command from clearing a newer pending value, including a repeated value (30 → 60 → 30). |
| Hot water | Expose working heat-pump/off choices. Remove the misleading high-demand choice, which previously reset the program instead of starting a boost. Setting temperature still starts the midnight boost; existing temporary-change sensors show it. |
| Program labels | Create globally unique display labels even for duplicate user names and collisions with generated suffixes. All displayed choices round-trip to their own API key; unsupported current programs show unknown. |
| Optional data | Malformed optional DTO fields degrade individually without discarding valid circuit measurements. Preserve supported telemetry and program caching, including issue #13's one-hour 417 backoff. |
| Options/lifecycle | Validate persisted option values with shared readers. Use `OptionsFlowWithReload` when available; install the legacy reload listener only on older HA. Keep the HA 2024.11 minimum. Remove services only after successful final-entry unload. |
| Privacy | Redact credential/error response content before limiting its logged length. Redact plant IDs from diagnostic dictionary keys and embedded text/URLs without mutating coordinator data. Keep useful technical circuit paths. |
| Examples | Add bounded timeouts, current circuit discovery, plant pagination, boolean selectability and a Python CLI using prompts/environment. Avoid the partner-only online probe for regular accounts. Preserve live telemetry demonstrations. |

## Excluded or deferred

- **Controls-only rewrite / requests transport:** would remove supported telemetry and HA's shared aiohttp session. Our per-request identifying User-Agent already addresses the verified gateway behavior.
- **Legacy v3 temporary-change body:** retain empirically verified v4 `endOfPhase`/`duration` semantics and v3 DELETE reset.
- **Weather-impact number entities:** the fork itself records these writes as unverified against a live controller; schema presence alone does not establish usable device behavior. No new physical-control endpoints are introduced.
- **Pool renaming for PS / removal of German translations:** PS is a buffer tank here; preserve terminology and translations.
- **Fire-and-forget refresh:** conflicts with our pending-state lifecycle and the bundled summer-boost automation. Await the refresh outside the command lock.
- **Workflow changes claimed in fork audit prose:** reviewed workflow files do not substantiate all pinning/release-gate claims. Preserve our existing manual-release, validation and User-Agent canary workflows.
- **Unconditional `via_device_id` migration:** would require a newer HA baseline; no minimum-version increase is justified by this adoption.

## Acceptance boundary

Run runtime regression tests, the full repository suite with coverage, unscoped Ruff check/format, and Bash syntax validation. Keep API calls mocked for behavioral tests: this change does not claim authenticated live heating-system validation. No release/version bump or deployment is part of this adoption task.

## Local verification result

- Python 3.13.12, complete suite: **477 passed**, with `RuntimeWarning` treated as an error. Coverage **70.22%** (repository threshold: 25%).
- `python -m ruff check --no-cache .` and `python -m ruff format --no-cache --check .`: passed.
- `git diff --check` and Git Bash `bash -n examples/get-live-values.sh`: passed.
- Independent review reproduced and fixed opaque credential leaks in error-body redaction and the fan/climate repeated-value pending-state race. Both pending-state regressions were observed failing before the fix and passing afterwards.
- HA lifecycle variants are exercised with isolated stubs, not a running HA installation. No authenticated live device commands, remote CI run or publication were performed for this branch.
