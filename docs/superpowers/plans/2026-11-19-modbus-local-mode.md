# Local Modbus TCP mode (hybrid and local-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `hoval_connect` read and control HV (HomeVent) units over the TTE-GW's Modbus TCP interface, either combined with the cloud (hybrid) or without any Hoval account (local only), while existing entities keep their unique IDs.

**Architecture:** A stdlib asyncio Modbus client (`modbus.py`) and an HV register map with decoder, write validation and poller (`modbus_hv.py`) feed the existing `HovalDataCoordinator`. The coordinator keeps its cloud fetch unchanged as `_async_fetch_cloud()`, wraps it in a TTL-gated, failure-tolerant snapshot when Modbus is configured, and overlays Modbus readings onto the HV circuits each tick. Entities stay where they are; a few learn to route control actions to Modbus and to stay available when only the cloud is gone.

**Tech Stack:** Python (HA ≥ 2024.11), asyncio streams, Home Assistant config entries / DataUpdateCoordinator, pytest with HA stubbed via `sys.modules`.

**Spec:** `docs/superpowers/specs/2026-11-19-modbus-local-mode-design.md`

## Global Constraints

- No new requirements: `manifest.json` keeps `"requirements": []`; no `pymodbus` import anywhere.
- An entry without Modbus configured behaves exactly as today: same requests, same cadence, same entities, same unique IDs.
- Unique IDs and entity IDs of existing entities never change. Entity unique-id pattern stays `{plant_id}_{circuit_path}_{key}` / `{plant_id}_{key}`.
- Cloud cadence floor stays 15 min (`SCAN_INTERVAL_OPTIONS`); no new cloud endpoints; `USER_AGENT`/`INTEGRATION_VERSION` handling untouched. A failed cloud fetch is retried at the cloud interval, never at the Modbus interval.
- Scope is HV units 513–528 only. Never write a register outside the write table of Task 2; validate every value before sending.
- Repo conventions: tests run without Home Assistant (HA stubbed via `sys.modules`), from the repo root, with `python -m pytest tests/ -v`; lint with `uvx ruff@latest check .` and `uvx ruff@latest format --check .`.
- Every live write on the owner's gateway needs the owner's consent for that specific value (Task 10).
- Work on branch `feat/modbus-local`. No release, no version bump, no tag in this plan (a release needs Hoval's consent in #24).

## Review Focus

1. **Cloud blocked or down in hybrid mode** — the Modbus tick (30 s) must not turn into a cloud retry every 30 s; the next cloud attempt waits a full cloud interval. Test in Task 6.
2. **HA restart while the cloud is down** — a hybrid entry with stored topology must set up with its old unique IDs and Modbus values instead of `ConfigEntryNotReady`. Test in Task 6.
3. **Gateway busy or slow on first read** (exception 6, > 3 s) — retried, and a busy unit is not treated as absent during discovery. Tests in Tasks 1 and 3.
4. **Gateway reboot / dropped TCP connection** — the next request reconnects without a reload; a lost write response is reported, not repeated. Test in Task 1.
5. **Out-of-range values from automations** (number 10 %, unknown program, fan 5 %) — rejected or clamped before anything is sent. Tests in Tasks 2 and 9.

---

### Task 1: Modbus TCP transport

**Files:**
- Create: `custom_components/hoval_connect/modbus.py`
- Test: `tests/test_modbus.py`

**Interfaces:**
- Produces:
  - `class HovalModbusError(Exception)` with `__init__(self, message: str, *, exception_code: int | None = None)` and attribute `exception_code`.
  - `EXC_ILLEGAL_ADDRESS = 2`, `EXC_BUSY = 6`, `FC_READ_HOLDING = 3`, `FC_WRITE_SINGLE = 6`.
  - `encode_request(transaction_id: int, unit_id: int, function: int, address: int, word: int) -> bytes` (for FC03 `word` = count, for FC06 `word` = value).
  - `parse_pdu(frame: bytes, *, transaction_id: int, unit_id: int, function: int) -> bytes` — returns the PDU bytes after the function code; raises `HovalModbusError(exception_code=…)` for an exception response, `HovalModbusError` (no code) for a mismatched transaction id, protocol id, unit id, length or function.
  - `class HovalModbusClient(host: str, port: int = 502, unit_id: int = 1, *, timeout: float = 10.0, busy_retries: int = 3, busy_delay: float = 1.0, request_gap: float = 0.05)` with `async read_holding(address: int, count: int) -> list[int] | None` (None on exception 2), `async write_register(address: int, value: int) -> None`, `async close() -> None`, and properties `host`, `port`, `unit_id`.

- [ ] **Step 1: Write the failing tests** (pure helpers + a fake asyncio server on `127.0.0.1:0`, driven with `asyncio.run`; no HA stubs needed)

```python
def test_encode_read_matches_live_request():
    assert modbus.encode_request(1, 1, 3, 23622, 13) == bytes.fromhex("00010000000601035c46000d")

def test_encode_write_matches_live_request():
    assert modbus.encode_request(2, 1, 6, 23624, 66) == bytes.fromhex("00020000000601065c480042")

def test_parse_read_response():
    words = [1, 50, 65, 45, 54, 50, 255, 255, 0, 1, 93, 231, 53]
    frame = bytes.fromhex("00010000001d0103") + bytes([26]) + b"".join(w.to_bytes(2, "big") for w in words)
    pdu = modbus.parse_pdu(frame, transaction_id=1, unit_id=1, function=3)
    assert pdu[0] == 26 and int.from_bytes(pdu[1:3], "big") == 1

def test_parse_exception_response_carries_code():
    with pytest.raises(modbus.HovalModbusError) as err:
        modbus.parse_pdu(bytes.fromhex("000100000003018302"), transaction_id=1, unit_id=1, function=3)
    assert err.value.exception_code == 2

@pytest.mark.parametrize("frame", [
    bytes.fromhex("000900000003018302"),  # wrong transaction id
    bytes.fromhex("000100000003028302"),  # wrong unit id
    bytes.fromhex("000100000003018402"),  # wrong function
])
def test_parse_rejects_mismatch(frame):
    with pytest.raises(modbus.HovalModbusError) as err:
        modbus.parse_pdu(frame, transaction_id=1, unit_id=1, function=3)
    assert err.value.exception_code is None

def test_read_returns_words_and_none_for_illegal_address(fake_server): ...
    # server answers 23622:13 with the live block, 23531:13 with exception 2
    # assert client.read_holding(23622, 13) == [1, 50, 65, 45, 54, 50, 255, 255, 0, 1, 93, 231, 53]
    # assert client.read_holding(23531, 13) is None

def test_busy_is_retried_then_succeeds(fake_server): ...
    # server answers exception 6 twice, then the block; busy_delay=0
    # assert result == block and server saw 3 requests

def test_busy_exhausted_raises(fake_server): ...
    # 4x exception 6 with busy_retries=3 → HovalModbusError(exception_code=6)

def test_write_checks_echo(fake_server): ...
    # echo of (6, 23624, 66) passes; an echo with value 65 raises HovalModbusError

def test_read_reconnects_after_server_drops_connection(fake_server): ...
    # server closes the socket after the first response; second read_holding still succeeds (one reconnect)

def test_write_is_not_repeated_after_lost_response(fake_server): ...
    # server reads the write request and closes without answering → HovalModbusError; server saw exactly 1 write

def test_concurrent_requests_do_not_interleave(fake_server): ...
    # gather two read_holding calls; server asserts each request is answered before the next arrives
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_modbus.py -v`
Expected: FAIL (`ModuleNotFoundError`/`AttributeError` for `modbus`)

- [ ] **Step 3: Implement `modbus.py`**

One persistent `asyncio.open_connection` per client, opened lazily; an `asyncio.Lock` around each request/response; transaction id increments per request (wrap at 0xFFFF); `asyncio.timeout(timeout)` around connect + exchange; read exactly the 7-byte MBAP header, then `length - 1` bytes. Exception 6 → sleep `busy_delay`, retry up to `busy_retries` (reads and writes: busy means not executed). Transport errors (`OSError`, `asyncio.IncompleteReadError`, `TimeoutError`) close the connection; **reads** reconnect and retry once, **writes** raise `HovalModbusError` without retrying. Sleep `request_gap` after each response. Module docstring states why there is no `pymodbus`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_modbus.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/hoval_connect/modbus.py tests/test_modbus.py
git commit -m "feat(modbus): stdlib asyncio Modbus TCP client"
```

---

### Task 2: HV register map, decoding and write validation

**Files:**
- Create: `custom_components/hoval_connect/modbus_hv.py`
- Test: `tests/test_modbus_hv.py`

**Interfaces:**
- Consumes: nothing (pure module; imports nothing from HA).
- Produces:
  - `HV_UNIT_IDS = range(513, 529)`, `HV_BLOCK_LEN = 13`, `HV_MAINTENANCE_LEN = 13`, `HV_ERROR_ENTRY_LEN = 9`, `HV_ERROR_ENTRIES = 5`.
  - `hv_block_base(unit_id: int) -> int` = `23531 + 13 * (unit_id - 513)`; `hv_error_base(unit_id: int) -> int` = `23835 + 45 * (unit_id - 513)`; `hv_maintenance_base(unit_id: int) -> int` = `28843 + 13 * (unit_id - 513)`; all raise `ValueError` outside 513..528. (Formulas checked against all 16 HV units in `Hoval-GatewayV2-CANBUS-MQTT/hoval_datapoints.csv`; they replace the spec's "generated table", which the spec left to the builder.)
  - `PROGRAM_BY_VALUE = {0: "standby", 1: "week1", 2: "week2", 4: "constant", 5: "ecoMode"}`, `VALUE_BY_PROGRAM` (inverse).
  - `HV_CONTROL_STATES = ("off", "normal", "voc", "humidity", "frost_protection", "coolvent", "fault", "summer_humidity", "switch_off_stop")` (index = register value).
  - `@dataclass(frozen=True) class HvModbusReading` with fields: `program: str | None`, `constant_modulation: int | None`, `eco_modulation: int | None`, `air_volume: int | None`, `humidity_target: int | None`, `humidity_actual: int | None`, `voc_extract: int | None`, `voc_outside: int | None`, `air_quality_control: int | None`, `control_state: str | None`, `outside_temperature: float | None`, `exhaust_temperature: float | None`, `exhaust_fan_target: int | None`, `maintenance_enabled: bool | None`, `maintenance_interval_weeks: int | None`, `maintenance_weeks_remaining: int | None`, `co2_extract: int | None`, `filter_cleaning_enabled: bool | None`, `filter_cleaning_interval_weeks: int | None`, `active_errors: tuple[int, ...] | None` (None = not read yet, `()` = none active).
  - `decode_hv(block: list[int], maintenance: list[int] | None, errors: list[list[int] | None] | None) -> HvModbusReading`.
  - `HV_WRITES: dict[str, tuple[str, int, frozenset[int] | range]]` = name → (block, offset, allowed values): `"program": ("block", 0, frozenset({0, 1, 2, 4, 5}))`, `"constant_modulation": ("block", 1, range(15, 101))`, `"eco_modulation": ("block", 2, range(15, 101))`, `"humidity_target": ("block", 4, range(30, 66))`, `"acknowledge": ("maintenance", 5, frozenset({7, 8}))`.
  - `hv_write_address(unit_id: int, name: str, value: int) -> int` — validates and returns the register address; raises `ValueError` for an unknown name or a value not allowed.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("unit,block,errors,maint", [
    (513, 23531, 23835, 28843), (520, 23622, 24150, 28934), (528, 23726, 24510, 29038)])
def test_bases_match_datapoint_list(unit, block, errors, maint): ...

@pytest.mark.parametrize("unit", [512, 529])
def test_bases_reject_non_hv_units(unit): ...  # ValueError

LIVE_BLOCK = [1, 50, 65, 45, 54, 50, 255, 255, 0, 1, 93, 231, 53]
LIVE_MAINT = [1, 0, 52, 0, 2, 0, 255, 0, 0, 0, 1, 0, 14]
NO_ERROR = [0, 0, 0, 0, 65535, 255, 255, 255, 65535]

def test_decode_live_values():
    r = modbus_hv.decode_hv(LIVE_BLOCK, LIVE_MAINT, [NO_ERROR] * 5)
    assert (r.program, r.constant_modulation, r.eco_modulation, r.air_volume) == ("week1", 50, 65, 45)
    assert (r.humidity_target, r.humidity_actual) == (54, 50)
    assert r.voc_extract is None and r.voc_outside is None          # 255 = not fitted
    assert r.air_quality_control == 0 and r.control_state == "normal"
    assert r.outside_temperature == 9.3 and r.exhaust_temperature == 23.1
    assert r.exhaust_fan_target == 53
    assert (r.maintenance_enabled, r.maintenance_interval_weeks, r.maintenance_weeks_remaining) == (True, 52, 2)
    assert r.co2_extract is None
    assert (r.filter_cleaning_enabled, r.filter_cleaning_interval_weeks) == (True, 14)
    assert r.active_errors == ()

def test_negative_temperature_and_sentinels():
    block = list(LIVE_BLOCK); block[10] = 0xFFF5; block[11] = 0x8000
    r = modbus_hv.decode_hv(block, None, None)
    assert r.outside_temperature == -1.1 and r.exhaust_temperature is None
    assert r.maintenance_enabled is None and r.active_errors is None

def test_unknown_program_and_state_values_decode_to_none():
    block = list(LIVE_BLOCK); block[0] = 3; block[9] = 42
    r = modbus_hv.decode_hv(block, None, None)
    assert r.program is None and r.control_state is None

def test_active_error_codes_collected():
    active = [100, 1201, 0, 0, 520, 50, 0, 1, 41]   # source 520, code 41
    r = modbus_hv.decode_hv(LIVE_BLOCK, None, [active, NO_ERROR, None, NO_ERROR, NO_ERROR])
    assert r.active_errors == (41,)

@pytest.mark.parametrize("name,value,address", [
    ("program", 0, 23622), ("program", 5, 23622), ("constant_modulation", 15, 23623),
    ("eco_modulation", 100, 23624), ("humidity_target", 30, 23626), ("acknowledge", 7, 28939),
    ("acknowledge", 8, 28939)])
def test_write_address_for_allowed_values(name, value, address):
    assert modbus_hv.hv_write_address(520, name, value) == address

@pytest.mark.parametrize("name,value", [
    ("program", 3), ("program", 6), ("constant_modulation", 14), ("constant_modulation", 101),
    ("eco_modulation", 10), ("humidity_target", 29), ("humidity_target", 66), ("acknowledge", 0),
    ("air_volume", 50), ("control_state", 1)])
def test_write_rejects_disallowed(name, value): ...  # ValueError
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_modbus_hv.py -v`
Expected: FAIL (module missing)

- [ ] **Step 3: Implement the map, `decode_hv` and `hv_write_address` in `modbus_hv.py`**

Block offsets as in the spec's register table (0 Betriebswahl … 12 Fortluft). Sentinels: U8 255 → None; S16 0x8000/0x7FFF → None, else signed / 10 rounded to 1 decimal; U32/S32 high word first. Maintenance block offsets: 0 config, 1–2 interval, 3–4 remaining (S32), 5 acknowledge (not decoded), 6 CO2 %, 7–9 raw CO2/VOC (ignored), 10 cleaning config, 11–12 cleaning interval. An error entry is active when its source (offset 4) is not 0xFFFF and its code (offset 8) is not 0xFFFF; `None` entries are skipped.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_modbus_hv.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/hoval_connect/modbus_hv.py tests/test_modbus_hv.py
git commit -m "feat(modbus): HV register map, decoding and write validation"
```

---

### Task 3: HV poller (discovery, polling, validated writes)

**Files:**
- Modify: `custom_components/hoval_connect/modbus_hv.py` (append)
- Test: `tests/test_modbus_hv.py` (append)

**Interfaces:**
- Consumes: Task 1 `HovalModbusClient`, `HovalModbusError`, `EXC_BUSY`; Task 2 bases, `decode_hv`, `hv_write_address`, `HvModbusReading`.
- Produces: `class HovalModbusPoller(client: HovalModbusClient, *, slow_ttl: float = 300.0)` with attribute `client`, and
  - `async discover(candidates: Iterable[int] = HV_UNIT_IDS) -> list[int]` — units whose 13-register block answers.
  - `async poll(unit_ids: Iterable[int]) -> dict[int, HvModbusReading | None]` — block every call; maintenance block and the five error entries only when `slow_ttl` has passed for that unit (otherwise reuse the last words); a unit whose block answers exception 2 maps to `None`; a transport failure raises `HovalModbusError`.
  - `async write(unit_id: int, name: str, value: int) -> None` — `hv_write_address`, `client.write_register`, then (except `"acknowledge"`) read the register back and raise `HovalModbusError` if it differs. `ValueError` from validation propagates unchanged, before any I/O.
  - Module-level `time` import used via `time.monotonic()` (tests patch `modbus_hv.time`).

- [ ] **Step 1: Write the failing tests** (fake client object with `read_holding`/`write_register` coroutines recording calls)

```python
def test_discover_keeps_answering_units(): ...
    # fake answers block reads for 520 only, None (exception 2) for the rest → [520]; 16 block reads

def test_discover_does_not_drop_a_busy_unit(): ...
    # fake raises HovalModbusError(exception_code=6) for 520 → discover raises HovalModbusError (not [])

def test_poll_reads_slow_blocks_only_after_ttl(): ...
    # first poll: 1 block + 1 maintenance + 5 error reads; poll again at +60 s: 1 block read only,
    # reading still carries maintenance values; at +300 s: slow reads again

def test_poll_maps_vanished_unit_to_none(): ...  # block read returns None → {520: None}

def test_poll_raises_on_transport_failure(): ...  # fake raises HovalModbusError() → poll raises

def test_write_reads_back(): ...
    # write(520, "eco_modulation", 66): write_register(23624, 66) then read_holding(23624, 1)

def test_write_mismatch_raises(): ...  # read-back 65 after writing 66 → HovalModbusError

def test_acknowledge_skips_read_back(): ...  # write(520, "acknowledge", 7): one write_register(28939, 7), no read

def test_invalid_write_sends_nothing(): ...  # write(520, "eco_modulation", 10) → ValueError, no client calls
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_modbus_hv.py -v`
Expected: new tests FAIL (`HovalModbusPoller` missing)

- [ ] **Step 3: Implement `HovalModbusPoller`**

Discovery treats any `HovalModbusError` other than "returned None" as a failure of the whole scan (raise), so a busy or unreachable gateway is never reported as "no units". Error entries are read one entry (9 registers) per request; one that answers exception 2 becomes `None`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_modbus_hv.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/hoval_connect/modbus_hv.py tests/test_modbus_hv.py
git commit -m "feat(modbus): HV discovery, polling and validated writes"
```

---

### Task 4: Constants, option readers and config flow (menu, local setup, reconfigure, options)

**Files:**
- Modify: `custom_components/hoval_connect/const.py`, `custom_components/hoval_connect/options.py`, `custom_components/hoval_connect/config_flow.py`, `custom_components/hoval_connect/strings.json`, `custom_components/hoval_connect/translations/en.json`, `custom_components/hoval_connect/translations/de.json`
- Test: `tests/test_config_flow.py`, `tests/test_options_runtime.py`

**Interfaces:**
- Consumes: Task 1 client, Task 3 `HovalModbusPoller.discover()`.
- Produces (const.py): `CONF_MODE = "mode"`, `MODE_LOCAL = "local"`, `CONF_HOST = "host"`, `CONF_PORT = "port"`, `CONF_UNIT_ID = "unit_id"`, `CONF_MODBUS_HOST = "modbus_host"`, `CONF_MODBUS_PORT = "modbus_port"`, `CONF_MODBUS_UNIT_ID = "modbus_unit_id"`, `CONF_MODBUS_SCAN_INTERVAL = "modbus_scan_interval"`, `CONF_MODBUS_PLANT_ID = "modbus_plant_id"`, `DATA_TOPOLOGY = "topology"`, `DEFAULT_MODBUS_PORT = 502`, `DEFAULT_MODBUS_UNIT_ID = 1`, `DEFAULT_MODBUS_SCAN_INTERVAL = 30`, `MODBUS_SCAN_INTERVAL_OPTIONS = {10: "10 seconds", 30: "30 seconds", 60: "1 minute", 120: "2 minutes"}`, `MODBUS_VALIDATION_TIMEOUT_S = 60`.
- Produces (options.py): `@dataclass(frozen=True) class ModbusSettings(host: str, port: int, unit_id: int)`; `is_local_entry(data: Mapping[str, Any]) -> bool`; `get_modbus_settings(data: Mapping[str, Any], options: Mapping[str, Any]) -> ModbusSettings | None` (local entry → from `data`; cloud entry → from options, `None` when `modbus_host` is empty/missing); `get_modbus_scan_interval(options) -> int` (only the four allowed values, else 30); `get_modbus_plant_id(options) -> str | None`.
- Produces (config_flow.py): `async_step_user` shows a menu with options `["cloud", "local"]`; `async_step_cloud` = today's user step unchanged (step id `cloud`); `async_step_local`; `async_step_reconfigure` (local entries; cloud entries abort `reconfigure_not_supported`); options flow per entry type. Error keys: `modbus_cannot_connect`, `modbus_no_units`. Local entry: unique id `modbus:{host.lower()}:{port}:{unit_id}`, title `Hoval {host}`, data `{"mode": "local", "host", "port", "unit_id"}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_user_step_offers_cloud_and_local_menu(): ...  # result type menu, menu_options == ["cloud", "local"]
def test_cloud_step_keeps_existing_behaviour(): ...    # move the existing user-step tests to step "cloud"
def test_local_step_creates_entry_when_units_found(): ...
    # discover → [520]; data == {"mode": "local", "host": "gw.local", "port": 502, "unit_id": 1}
    # unique_id == "modbus:gw.local:502:1", title == "Hoval gw.local"
def test_local_step_no_units(): ...           # discover → [] → errors {"base": "modbus_no_units"}
def test_local_step_cannot_connect(): ...     # discover raises HovalModbusError / OSError / TimeoutError → "modbus_cannot_connect"
def test_local_step_duplicate_aborts(): ...   # same host/port/unit twice → abort already_configured
def test_reconfigure_local_updates_host(): ...
def test_reconfigure_cloud_aborts(): ...      # abort reason "reconfigure_not_supported"
def test_cloud_options_accept_empty_modbus_host(): ...  # no Modbus validation, stored host ""
def test_cloud_options_validate_modbus(): ...  # host set + discover [] → "modbus_no_units"; [520] → saved
def test_cloud_options_plant_selector_only_with_several_plants(): ...
def test_local_options_offer_turn_on_mode_and_modbus_interval_only(): ...

# test_options_runtime.py
def test_get_modbus_settings_cloud_and_local(): ...
def test_get_modbus_scan_interval_rejects_unknown(): ...  # 45, "x", True, None → 30; 10/30/60/120 kept
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_config_flow.py tests/test_options_runtime.py -v`
Expected: new tests FAIL

- [ ] **Step 3: Implement constants, option readers and the flow; add the copy to `strings.json`, `translations/en.json` and `translations/de.json`** (menu labels, local and reconfigure steps, new option fields with a description that names the TopTronic E menu path `Service > TTE-GW > Modbus > Einstellungen` and `Mode = TCP`, the two error keys, the abort key). Validation runs `HovalModbusPoller(HovalModbusClient(...)).discover()` inside `asyncio.timeout(MODBUS_VALIDATION_TIMEOUT_S)` and always closes the client.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_config_flow.py tests/test_options_runtime.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/hoval_connect tests/test_config_flow.py tests/test_options_runtime.py
git commit -m "feat(modbus): config flow menu, local setup, reconfigure and Modbus options"
```

---

### Task 5: Coordinator — data model, merge and local-only mode

**Files:**
- Modify: `custom_components/hoval_connect/coordinator.py`, `custom_components/hoval_connect/const.py`
- Test: `tests/test_modbus_coordinator.py` (new; reuse the `runtime` fixture pattern of `tests/test_program_polling.py`, loading `modbus` and `modbus_hv` too)

**Interfaces:**
- Consumes: Task 2 `HvModbusReading`, Task 3 `HovalModbusPoller`.
- Produces:
  - `HovalCircuitData.modbus: HvModbusReading | None = None`; `HovalData.cloud_ok: bool = True`.
  - const.py: `MODBUS_BACKED_KEYS = frozenset({"outside_temperature", "exhaust_temperature", "air_volume", "humidity_actual", "humidity_target", "hv_constant_modulation", "hv_eco_modulation", "air_quality_guided", "hv_control_state", "hv_operating_selection", "hv_air_quality_control", "hv_co2_extract", "hv_voc_extract", "hv_voc_outside", "hv_exhaust_fan_target", "hv_maintenance_weeks_remaining", "hv_filter_cleaning_interval", "hv_active_error"})`.
  - `apply_modbus_reading(circuit: HovalCircuitData, reading: HvModbusReading | None) -> None` (module level): sets `circuit.modbus`; with a reading it overwrites `live_values` keys `airVolume`, `humidityActual`, `humidityTarget`, `outsideTemperature`, `exhaustTemp` (as `str`, key removed when the value is None), `active_program`, `constant_value`/`eco_mode_value` (float), `target_value` (= `float(air_volume)`), `is_air_quality_guided` (= `air_quality_control == 1`); with `None` it removes those five live-value keys and sets the other fields to None.
  - `HovalDataCoordinator.__init__(self, hass, api: HovalConnectApi | None, *, modbus: HovalModbusPoller | None = None, modbus_plant_id: str | None = None, local_plant: tuple[str, str] | None = None, topology: dict[str, Any] | None = None)`; attribute `cloud_interval: float` (seconds, default `DEFAULT_SCAN_INTERVAL`); properties `has_modbus: bool`, `is_local: bool` (`api is None`).
  - `async async_hv_write(self, plant_id: str, circuit_path: str, name: str, value: int) -> None` — unit = `int(circuit_path.split(".")[0])`; delegates to `modbus.write`.
  - `async async_close(self) -> None` — closes the Modbus client if any.
  - `async_control_and_refresh(..., cloud_action: bool = True)` — new keyword; consumed in Task 6.
  - The existing body of `_async_update_data` moves unchanged into `async def _async_fetch_cloud(self) -> HovalData`; `_async_update_data` dispatches: no Modbus → `return await self._async_fetch_cloud()`; local → local path below; hybrid → Task 6.

- [ ] **Step 1: Write the failing tests**

```python
def test_cloud_only_path_unchanged(runtime): ...
    # coordinator without modbus: same calls as before (reuse an existing program-polling scenario)

def test_apply_reading_overwrites_cloud_fields(runtime): ...
    # circuit with live_values {"airVolume": "30", "totalEnergy": "5"}, active_program "constant"
    # after apply(reading from LIVE_BLOCK): airVolume "45", totalEnergy untouched, active_program "week1",
    # constant_value 50.0, eco_mode_value 65.0, target_value 45.0, is_air_quality_guided False

def test_apply_none_blanks_modbus_fields(runtime): ...

def test_local_poll_builds_plant(runtime): ...
    # local_plant=("local_E1", "Hoval gw"), poller discovers [520], poll → reading
    # data.plants["local_E1"].circuits["520.50.0"].circuit_type == "HV"; .name == CIRCUIT_TYPE_NAMES["HV"]
    # plant.is_online True, has_error False, data.cloud_ok False; no api attribute touched

def test_local_active_error_sets_plant_error(runtime): ...  # reading.active_errors == (41,) → has_error True

def test_local_modbus_failure_raises_update_failed(runtime): ...  # poll raises HovalModbusError → UpdateFailed

def test_local_rediscovers_after_failed_discovery(runtime): ...
    # first discover raises → UpdateFailed; next update discovers [520] and succeeds
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_modbus_coordinator.py -v`
Expected: FAIL

- [ ] **Step 3: Implement the model fields, `apply_modbus_reading`, the constructor changes, `_async_fetch_cloud` extraction and the local path**

Local path: discover once (cache the unit list; retry discovery on the next update after a failure), poll, build one `HovalPlantData(plant_id, name, is_online=True)` with an HV circuit per unit at path `f"{unit}.50.0"`, named `CIRCUIT_TYPE_NAMES["HV"]` (suffix ` {unit}` only when more than one unit); a unit mapped to `None` is left out; `has_error = bool(reading.active_errors)`; `cloud_ok = False`. `HovalModbusError`/`OSError`/`TimeoutError` → `UpdateFailed`.

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: PASS (existing coordinator tests prove the cloud path is unchanged)

- [ ] **Step 5: Commit**

```bash
git add custom_components/hoval_connect tests/test_modbus_coordinator.py
git commit -m "feat(modbus): coordinator merge and local-only polling"
```

---

### Task 6: Coordinator — hybrid mode (cloud TTL, fallback, topology, plant binding, less cloud load)

**Files:**
- Modify: `custom_components/hoval_connect/coordinator.py`
- Test: `tests/test_modbus_coordinator.py` (append)

**Interfaces:**
- Consumes: Task 5.
- Produces:
  - `topology_from_data(data: HovalData, plant_id: str) -> dict[str, Any] | None` → `{"plant_id": str, "plant_name": str, "circuits": {path: name}}` with HV circuits only; `None` when the plant is absent.
  - `data_from_topology(topology: dict[str, Any]) -> HovalData` → one plant (`is_online=False`), HV circuits with names, `cloud_ok=False`.
  - Coordinator attribute `bound_plant_id: str | None` (resolved from `modbus_plant_id`, else the only plant of the first successful cloud fetch; several plants and none chosen → Modbus unused, WARNING once).

- [ ] **Step 1: Write the failing tests**

```python
def test_cloud_fetched_once_per_cloud_interval(runtime): ...
    # update_interval 30 s, cloud_interval 900 s: 3 updates within 899 s → get_plants called once,
    # Modbus polled 3 times; at +900 s the cloud is fetched again

def test_cloud_failure_retried_only_after_cloud_interval(runtime): ...   # Review Focus 1
    # get_plants raises HovalApiError: first update still returns data (Modbus values present,
    # cloud_ok False); updates at +30 s and +60 s make no cloud request; at +900 s one request

def test_auth_failure_starts_reauth_and_keeps_modbus(runtime): ...
    # get_plants raises HovalAuthError → config_entry.async_start_reauth called once, no exception,
    # Modbus values present

def test_startup_with_cloud_down_uses_stored_topology(runtime): ...      # Review Focus 2
    # topology {"plant_id": "P1", "plant_name": "HRT", "circuits": {"520.50.0": "Lüftung"}},
    # get_plants raises → data.plants["P1"].circuits["520.50.0"].name == "Lüftung", modbus reading set

def test_startup_with_cloud_down_and_no_topology_fails(runtime): ...     # → UpdateFailed

def test_both_sources_down_raises_update_failed(runtime): ...

def test_topology_persisted_only_when_changed(runtime): ...
    # hass.config_entries.async_update_entry called after the first cloud fetch, not after the second identical one

def test_modbus_served_hv_skips_live_values_and_details(runtime): ...    # G6
    # HV circuit 520.50.0 served: api.live_calls == 0 and details_calls == 0 for it;
    # programs still fetched; an HK circuit still gets live values

def test_unserved_hv_unit_keeps_cloud_live_values(runtime): ...
    # Modbus block for 520 answers exception 2 → live values fetched from the cloud as before

def test_modbus_failure_in_hybrid_keeps_cloud_entities(runtime): ...
    # poll raises → update succeeds, cloud_ok True, circuit.modbus None, Modbus live keys absent

def test_multi_plant_without_binding_leaves_modbus_unused(runtime): ...
    # two plants, modbus_plant_id None → no merge into either, one WARNING

def test_binding_merges_into_bound_plant_only(runtime): ...
    # plants P1 and P2 both with 520.50.0, modbus_plant_id "P2" → only P2 circuit has .modbus

def test_cloud_action_forces_cloud_refresh_modbus_action_does_not(runtime): ...
    # async_control_and_refresh(cloud_action=True) → next update fetches the cloud although the TTL has not passed;
    # cloud_action=False → it does not
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_modbus_coordinator.py -v`
Expected: new tests FAIL

- [ ] **Step 3: Implement hybrid mode**

`_async_update_data` (hybrid):
1. If `time.monotonic() >= self._cloud_deadline`: try `_async_fetch_cloud()`; on success cache it, set `cloud_ok=True`, resolve `bound_plant_id`, persist the topology if it changed. `_async_fetch_cloud` already wraps failures, so catch what it raises: `ConfigEntryAuthFailed` → call `self.config_entry.async_start_reauth(self.hass)`; `UpdateFailed` (and any `HovalApiError` that escapes) → cloud failure. Log once per failure streak (WARNING on the first, INFO on recovery). Either way set `_cloud_deadline = now + cloud_interval`.
2. Base snapshot = `deepcopy` of the cached cloud data (with `cloud_ok` as last fetched), else `data_from_topology(stored topology)`, else raise `UpdateFailed`.
3. Poll Modbus for the HV units of the bound plant (discover served units against the HV paths once per cloud fetch); merge with `apply_modbus_reading`; a poll failure merges `None` and logs WARNING on the transition, INFO on recovery.
4. If the cloud snapshot is stale-from-topology and the Modbus poll failed, raise `UpdateFailed`.

`_async_fetch_cloud` gets a set `self._modbus_paths: set[tuple[str, str]]`; in `_fetch_circuit` a served HV path skips `get_live_values` and `get_circuit_details`. `async_control_and_refresh(cloud_action=True)` sets `_cloud_deadline = 0` before the refresh when Modbus is configured.

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/hoval_connect/coordinator.py tests/test_modbus_coordinator.py
git commit -m "feat(modbus): hybrid mode with cloud TTL, stored topology and plant binding"
```

---

### Task 7: Setup wiring, service guard, unload, diagnostics

**Files:**
- Modify: `custom_components/hoval_connect/__init__.py`, `custom_components/hoval_connect/diagnostics.py`
- Test: `tests/test_lifecycle_runtime.py`, `tests/test_diagnostics.py`, `tests/test_source_contracts.py`

**Interfaces:**
- Consumes: Tasks 4–6.
- Produces: `HovalRuntimeData.api: HovalConnectApi | None`. (`PLATFORMS` changes in Task 9, together with the new platform modules.)

- [ ] **Step 1: Write the failing tests**

```python
def test_setup_cloud_without_modbus_unchanged(): ...       # coordinator.modbus is None, update_interval = cloud interval
def test_setup_hybrid_uses_modbus_interval(): ...          # options modbus_host set → update_interval 30 s, cloud_interval 900 s
def test_setup_local_has_no_api(): ...                     # mode local → runtime.api is None, local_plant ("local_<entry_id>", title)
def test_unload_closes_modbus_client(): ...
def test_reset_temporary_change_rejected_for_local_entry(): ...  # ServiceValidationError mentions "Hoval account"
def test_diagnostics_redact_modbus_host(): ...             # "host" and "modbus_host" redacted

# test_source_contracts.py
def test_no_pymodbus_anywhere(): ...                       # no "pymodbus" in any component source
def test_manifest_has_no_requirements(): ...               # "requirements": []
def test_modbus_modules_make_no_cloud_calls(): ...         # modbus.py / modbus_hv.py contain no "aiohttp" and no "/v3/"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_lifecycle_runtime.py tests/test_diagnostics.py tests/test_source_contracts.py -v`
Expected: new tests FAIL

- [ ] **Step 3: Implement**

Cloud entries build the API as today, plus a poller when `get_modbus_settings` returns settings; local entries build only the poller. Pass `topology=entry.data.get(DATA_TOPOLOGY)`. `update_interval` = Modbus interval with Modbus, else the cloud interval; `coordinator.cloud_interval` = cloud interval. `async_unload_entry` awaits `coordinator.async_close()` after unloading the platforms. The options-updated listener (old HA) recomputes both intervals. Diagnostics add `host`, `modbus_host` to the redaction sets and include each circuit's `modbus` reading.

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/hoval_connect tests
git commit -m "feat(modbus): wire hybrid and local entries into setup and unload"
```

---

### Task 8: Sensors and binary sensors (availability, revived and new sensors)

**Files:**
- Modify: `custom_components/hoval_connect/sensor.py`, `custom_components/hoval_connect/binary_sensor.py`, `custom_components/hoval_connect/strings.json`, `translations/en.json`, `translations/de.json`
- Test: `tests/test_entity_controls.py` (or a new `tests/test_modbus_entities.py`)

**Interfaces:**
- Consumes: `MODBUS_BACKED_KEYS`, `HovalCircuitData.modbus`, `HovalData.cloud_ok`, `coordinator.is_local`, `coordinator.has_modbus`.
- Produces: availability rule used by every circuit sensor/binary sensor: `available = super().available and circuit is not None and (data.cloud_ok or (circuit.modbus is not None and key in MODBUS_BACKED_KEYS))`; plant entities: `online`/`error` available when `data.cloud_ok or coordinator.is_local`; plant weather/event sensors not created for local entries.
  New/revived circuit sensor keys (created only when `circuit.modbus is not None`; CO2/VOC additionally only when their value is not None): `hv_control_state` (enum, options `HV_CONTROL_STATES`), `hv_operating_selection` (enum, options `["standby", "week1", "week2", "constant", "ecoMode"]`), `hv_air_quality_control` (int), `hv_co2_extract`, `hv_voc_extract`, `hv_voc_outside` (%), `hv_exhaust_fan_target` (%), `hv_maintenance_weeks_remaining` (weeks; attributes `enabled`, `interval_weeks`), `hv_filter_cleaning_interval` (weeks; attribute `enabled`), `hv_active_error` (first active code, `0` = none).

- [ ] **Step 1: Write the failing tests**

```python
def test_modbus_sensor_available_while_cloud_down(): ...    # cloud_ok False, modbus reading → air_volume available
def test_cloud_sensor_unavailable_while_cloud_down(): ...   # active_week_program unavailable
def test_cloud_only_entry_availability_unchanged(): ...     # no modbus, cloud_ok True → all available
def test_revived_sensors_keep_old_unique_ids(): ...         # "P1_520.50.0_hv_control_state" etc.
def test_retired_cleanup_skips_hv_keys_with_modbus(): ...   # has_modbus → hv_* registry entries kept, hk_control_state removed
def test_retired_cleanup_unchanged_without_modbus(): ...
def test_co2_sensor_only_when_fitted(): ...                 # co2_extract None → not created
def test_control_state_values(): ...                        # reading.control_state "coolvent" → native_value "coolvent"
def test_local_entry_has_no_weather_or_event_sensors(): ...
def test_local_plant_online_and_error_available(): ...
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_modbus_entities.py -v`
Expected: FAIL

- [ ] **Step 3: Implement** — add a `modbus_value_fn: Callable[[HvModbusReading], Any] | None = None` field to `HovalSensorEntityDescription` for the new keys (value read from `circuit.modbus`), the availability rule in the entity base, the cleanup change in `_remove_retired_sensors(ent_reg, entry_id, keep_hv: bool)`, and the translation keys (DE/EN names and enum states for `hv_control_state`).

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/hoval_connect tests
git commit -m "feat(modbus): Modbus-backed and revived HV sensors with source-aware availability"
```

---

### Task 9: Control routing (fan, select) and new number/button platforms

**Files:**
- Modify: `custom_components/hoval_connect/fan.py`, `custom_components/hoval_connect/select.py`, `custom_components/hoval_connect/__init__.py` (`PLATFORMS`), `strings.json`, `translations/en.json`, `translations/de.json`
- Create: `custom_components/hoval_connect/number.py`, `custom_components/hoval_connect/button.py`
- Test: `tests/test_entity_controls.py`, `tests/test_modbus_entities.py`

**Interfaces:**
- Consumes: `coordinator.async_hv_write`, `async_control_and_refresh(cloud_action=…)`, `VALUE_BY_PROGRAM`, `clamp_hv_air_volume`.
- Produces: number entities `{plant_id}_{path}_constant_modulation_setpoint` (15–100 %, step 1, writes `constant_modulation`), `…_eco_modulation_setpoint` (15–100 %, writes `eco_modulation`), `…_humidity_setpoint` (30–65 %, writes `humidity_target`); button entities `…_acknowledge_maintenance` (writes `acknowledge` 7) and `…_acknowledge_filter_cleaning` (writes 8), `EntityCategory.CONFIG`. All created only for circuits with `modbus is not None`.

Routing (only when `circuit.modbus is not None`; otherwise today's code path):
- select → `async_hv_write(..., "program", VALUE_BY_PROGRAM[api_program])`, `cloud_action=False`.
- fan off → program 0; fan on → program from the turn-on option (`resume` → `resolve_resume_program`, else week1/week2), `cloud_action=False`.
- fan `is_on` → `circuit.active_program != "standby"` (None when unknown), no optimistic override needed.
- fan percentage: cloud entry → unchanged cloud temporary change (`cloud_action=True`; a cloud failure raises `HomeAssistantError`, no Modbus fallback); local entry → `constant_modulation = clamp_hv_air_volume(p)`, then `program = 4`.
- `ValueError` and `HovalModbusError` from writes → `HomeAssistantError` naming the value and the reason.

- [ ] **Step 1: Write the failing tests**

```python
def test_select_writes_program_over_modbus(): ...           # "standby" → async_hv_write(..., "program", 0); no api.set_program
def test_select_without_modbus_uses_cloud(): ...            # unchanged
def test_fan_off_and_on_write_program(): ...                # off → 0; on with resume after week2 → 2
def test_fan_is_on_from_modbus_program(): ...
def test_hybrid_fan_percentage_uses_cloud_temporary_change(): ...  # api.set_temporary_change called, no Modbus write
def test_hybrid_fan_percentage_cloud_failure_raises_without_fallback(): ...
def test_local_fan_percentage_sets_constant(): ...           # 60 → writes ("constant_modulation", 60) then ("program", 4)
def test_local_fan_percentage_is_clamped(): ...              # 5 → ("constant_modulation", 15)
def test_number_rejects_out_of_range_before_write(): ...     # set 10 → HomeAssistantError, no client call
def test_number_writes_and_refreshes(): ...                  # humidity 55 → ("humidity_target", 55), cloud_action False
def test_buttons_write_acknowledge_values(): ...             # maintenance → 7, filter → 8
def test_button_reports_gateway_exception(): ...             # HovalModbusError(exception_code=2) → HomeAssistantError mentioning "acknowledge"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_entity_controls.py tests/test_modbus_entities.py -v`
Expected: new tests FAIL

- [ ] **Step 3: Implement routing, `number.py`, `button.py`, `PLATFORMS` (+ `Platform.BUTTON`, `Platform.NUMBER`) and the translation keys.** New platforms listen to `SIGNAL_NEW_CIRCUITS` and deduplicate with a `known` set, as the existing platforms do.

- [ ] **Step 4: Run all tests and lint**

Run: `python -m pytest tests/ -v` then `uvx ruff@latest check .` and `uvx ruff@latest format --check .`
Expected: PASS, no lint findings

- [ ] **Step 5: Commit**

```bash
git add custom_components/hoval_connect tests
git commit -m "feat(modbus): Modbus control routing, setpoint numbers and acknowledge buttons"
```

---

### Task 10: Documentation and live verification on the owner's plant

**Files:**
- Modify: `README.md` (section "Local Modbus TCP": enabling Modbus on the panel, hybrid vs local, what each mode provides, the acknowledge buttons and Hoval's read-only flag on 21054)

- [ ] **Step 1: Write the docs and commit**

```bash
git add README.md
git commit -m "docs: local Modbus TCP mode"
```

- [ ] **Step 2: Install the branch on the owner's HA by hand** (copy `custom_components/hoval_connect/` via the File editor add-on as described in the global notes, clear `__pycache__`, full core restart). Configure Modbus in the existing cloud entry's options (host of the TTE-GW, port 502, unit id 1, 30 s).

- [ ] **Step 3: Read-only checks** (REST API with the long-lived token)
  - All entity IDs listed in the spec still exist; `sensor.hoval_luftung_air_volume`, `_humidity`, `_target_humidity`, `_exhaust_temperature`, `_outside_temperature` update every ~30 s and match the gateway's Modbus values.
  - Revived `…_hv_control_state` shows `normal`; maintenance sensor shows 2 weeks (or less); no `hoval` errors in `system_log/list`.
  - Cloud request count per hour (debug log) is below the pre-change count.

- [ ] **Step 4: Write checks — ask the owner before each one, with the exact value**
  - Number eco modulation 65 → 66 → 65 while the program is week1 (no physical effect); confirm on the TopTronic E panel that 66 appears there (proves the write reaches the controller, not only the gateway cache).
  - Program select week1 → constant → week1 (audible change).
  - Acknowledge buttons only at the next real maintenance / filter-cleaning message.

- [ ] **Step 5: Record results** in local project notes (what was verified live, what is still open: 39600 labels, whether 38606 reflects a cloud temporary change, long-lived connection behaviour).
