# Contributing to Hoval Connect API

Thanks for your interest! This repo contains two things that welcome different
kinds of contributions:

1. **Reverse-engineered API documentation** for the Hoval Connect cloud
   (README + `docs/openapi-v3.json`)
2. A **Home Assistant custom integration** (`custom_components/hoval_connect/`)

> ⚠️ Everything here is unofficial and based on observation of the live API.
> Hoval can change or remove endpoints without notice — they have done so
> before (the entire `/v1/plants/.../circuits` family disappeared in April
> 2026, response pagination was enforced in May 2026).

## Reporting API observations

API knowledge is this project's foundation, and it decays. If you notice
changed behavior (new fields, new endpoints, different response shapes,
errors where there were none):

- Open an issue with the **endpoint, the date you observed it, and the
  response shape** (status code + abbreviated JSON structure).
- **Redact everything identifying**: plant IDs, tokens, e-mail addresses,
  serial numbers, coordinates. The integration's diagnostics export
  (Settings → Devices → Hoval Connect → Download diagnostics) already
  redacts these and is a good attachment.
- State your system type if you can (e.g. HomeVent, UltraSource) — many
  fields are circuit-type-specific.

Undocumented quirks with a date and a live observation are worth more than
speculation — the README and `CLAUDE.md` follow that rule throughout.

## Development setup

Python 3.12+ is required. Home Assistant does **not** need to be installed:

```bash
pip install pytest pytest-asyncio pytest-cov aiohttp voluptuous ruff
```

### Running tests

```bash
python -m pytest tests/ -v
```

- Run from the **repo root** — `tests/test_source_contracts.py` and
  `tests/test_config_flow.py` resolve `custom_components/hoval_connect`
  relative to the working directory.
- On Windows use `python`, not `python3`.
- HA modules are stubbed via `sys.modules` at the top of each test file;
  `aiohttp` and `voluptuous` must be real.
- Two test styles exist:
  - **Pure-function tests** for API/coordinator logic (mock the HTTP layer,
    test the parsing/retry/cache behavior).
  - **Source-contract tests** (`test_source_contracts.py`) that read the
    component source as text and pin decisions runtime tests can't reach
    (live-value keys, port decisions, guard placement). If your change
    intentionally breaks a contract, update the contract in the same PR and
    say why in the commit message.
- New non-trivial logic needs a test. Coverage gate is enforced
  (`fail_under` in `pyproject.toml`).

### Linting

Run ruff exactly the way CI does — **unscoped**, from the repo root:

```bash
ruff check .
ruff format --check .
```

`pyproject.toml` already excludes `docs/`. Scoping to
`custom_components/ tests/` silently skips `examples/` — don't.

## Integration conventions

- **Translations come in threes.** Every user-facing string needs matching
  keys in `strings.json`, `translations/en.json` **and**
  `translations/de.json`. A source-contract test enforces this for several
  key groups.
- **Entities read from the coordinator only** (`CoordinatorEntity`) — no
  direct API calls from entity code.
- **Optimistic state:** control paths use `_pending_*` attributes plus the
  coordinator's mode override. `async_control_and_refresh` intentionally
  blocks until fresh data arrived — don't "optimize" it into a background
  task; that caused a notification-loop regression once (see the docstring).
- **Defensive parsing:** a malformed field must degrade its own sensor, not
  drop the circuit or fail the poll. Follow the isolation-barrier patterns in
  `coordinator.py`.
- **New circuit types** need: the enum in `const.py`, sensor descriptions
  with `circuit_types=`, translations, and a README row.
- **Blueprint changes** (`blueprints/`): keep in mind users must re-import
  manually — call breaking changes out in the PR.

## Pull requests

- Target `master`. CI runs ruff, the test suite, HACS validation and
  hassfest on every PR.
- Use conventional-commit style messages as in the history
  (`feat:`, `fix:`, `docs:`, `chore:`, optional scope).
- If your change claims something about live API behavior, say how you
  verified it (date + system type), or mark it explicitly as untested.
- Keep PRs focused — one feature or fix per PR reviews fastest.

## Releases (maintainer)

Releases are cut by tagging: bump `"version"` in
`custom_components/hoval_connect/manifest.json`, commit, then push a
`vX.Y.Z` tag. The release workflow generates notes from the commits.
A pushed tag is the only thing that publishes — a version bump alone does
nothing.

## Security

Never commit credentials, tokens, plant IDs or personal data — not in code,
fixtures, docs or issue attachments. If you find a security-relevant issue in
the cloud API itself, report it to Hoval, not here.
