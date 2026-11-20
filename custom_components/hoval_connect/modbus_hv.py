"""Register map, decoding and write validation for the HV (HomeVent) units.

Pure module: stdlib only, no Home Assistant imports, so tests can load it by path.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from .modbus import HovalModbusClient, HovalModbusError

HV_UNIT_IDS = range(513, 529)
HV_BLOCK_LEN = 13
HV_MAINTENANCE_LEN = 13
HV_ERROR_ENTRY_LEN = 9
HV_ERROR_ENTRIES = 5

# Datapoint 20 "Unixzeit" of the gateway itself (unit GW 1153, FG 0, FN 1): U32 in
# two registers, high word first. Not per HV unit, so a fixed address.
GATEWAY_CLOCK_ADDRESS = 19802
GATEWAY_CLOCK_LEN = 2

_U16_NONE = 0xFFFF
_U8_NONE = 255


def _check_unit(unit_id: int) -> int:
    if unit_id not in HV_UNIT_IDS:
        raise ValueError(f"unit {unit_id} is not an HV unit (513..528)")
    return unit_id - HV_UNIT_IDS.start


# The three base formulas were checked against all 16 HV units of Hoval's
# datapoint list (hoval_datapoints.csv).
def hv_block_base(unit_id: int) -> int:
    """First register of the main HV block."""
    return 23531 + 13 * _check_unit(unit_id)


def hv_error_base(unit_id: int) -> int:
    """First register of the 5-entry error history."""
    return 23835 + 45 * _check_unit(unit_id)


def hv_maintenance_base(unit_id: int) -> int:
    """First register of the maintenance block."""
    return 28843 + 13 * _check_unit(unit_id)


PROGRAM_BY_VALUE = {0: "standby", 1: "week1", 2: "week2", 4: "constant", 5: "ecoMode"}
VALUE_BY_PROGRAM = {name: value for value, name in PROGRAM_BY_VALUE.items()}

# Index = register value.
HV_CONTROL_STATES = (
    "off",
    "normal",
    "voc",
    "humidity",
    "frost_protection",
    "coolvent",
    "fault",
    "summer_humidity",
    "switch_off_stop",
)


@dataclass(frozen=True)
class HvModbusReading:
    """Decoded HV values; None = unknown / not fitted / not read yet."""

    program: str | None
    constant_modulation: int | None
    eco_modulation: int | None
    air_volume: int | None
    humidity_target: int | None
    humidity_actual: int | None
    voc_extract: int | None
    voc_outside: int | None
    air_quality_control: int | None
    control_state: str | None
    outside_temperature: float | None
    exhaust_temperature: float | None
    exhaust_fan_target: int | None
    maintenance_enabled: bool | None
    maintenance_interval_weeks: int | None
    maintenance_weeks_remaining: int | None
    co2_extract: int | None
    filter_cleaning_enabled: bool | None
    filter_cleaning_interval_weeks: int | None
    # None = not read yet, () = none active.
    active_errors: tuple[int, ...] | None


def _u8(word: int) -> int | None:
    return None if word == _U8_NONE else word


def _s16_tenths(word: int) -> float | None:
    if word in (0x8000, 0x7FFF):
        return None
    if word >= 0x8000:
        word -= 0x10000
    return round(word / 10, 1)


def _u32(high: int, low: int) -> int | None:
    value = (high << 16) | low
    return None if value == 0xFFFFFFFF else value


def _s32(high: int, low: int) -> int | None:
    value = (high << 16) | low
    if value in (0x80000000, 0x7FFFFFFF):
        return None
    return value - (1 << 32) if value >= 0x80000000 else value


def _decode_errors(errors: list[list[int] | None] | None) -> tuple[int, ...] | None:
    if errors is None:
        return None
    codes = []
    for entry in errors:
        if entry is None or len(entry) < HV_ERROR_ENTRY_LEN:
            continue
        # Offset 4 = source, offset 8 = code; 0xFFFF in either marks an empty slot.
        if entry[4] != _U16_NONE and entry[8] != _U16_NONE:
            codes.append(entry[8])
    return tuple(codes)


def decode_hv(
    block: list[int],
    maintenance: list[int] | None,
    errors: list[list[int] | None] | None,
) -> HvModbusReading:
    """Decode the raw register words of one HV unit."""
    m = maintenance if maintenance is not None and len(maintenance) >= HV_MAINTENANCE_LEN else None
    return HvModbusReading(
        program=PROGRAM_BY_VALUE.get(block[0]),
        constant_modulation=_u8(block[1]),
        eco_modulation=_u8(block[2]),
        air_volume=_u8(block[3]),
        humidity_target=_u8(block[4]),
        humidity_actual=_u8(block[5]),
        voc_extract=_u8(block[6]),
        voc_outside=_u8(block[7]),
        air_quality_control=_u8(block[8]),
        control_state=HV_CONTROL_STATES[block[9]] if block[9] < len(HV_CONTROL_STATES) else None,
        outside_temperature=_s16_tenths(block[10]),
        exhaust_temperature=_s16_tenths(block[11]),
        exhaust_fan_target=_u8(block[12]),
        maintenance_enabled=(m[0] == 1) if m else None,
        maintenance_interval_weeks=_u32(m[1], m[2]) if m else None,
        maintenance_weeks_remaining=_s32(m[3], m[4]) if m else None,
        co2_extract=_u8(m[6]) if m else None,
        filter_cleaning_enabled=(m[10] == 1) if m else None,
        filter_cleaning_interval_weeks=_u32(m[11], m[12]) if m else None,
        active_errors=_decode_errors(errors),
    )


@dataclass(frozen=True)
class GatewayClockRead:
    """Raw gateway clock value with the (UTC) moment it was read."""

    seconds: int
    read_at: datetime


def decode_gateway_clock(
    seconds: int | None, read_at: datetime, tz: tzinfo
) -> tuple[int, datetime] | None:
    """Return (offset in whole seconds, gateway time) of a raw clock value.

    The gateway sends its LOCAL wall-clock time as seconds since 1970, as if it
    were UTC (measured: +3600 s in CET). `tz` is the zone the gateway runs in,
    taken to be Home Assistant's. Offset = gateway time - `read_at` (aware).

    At the autumn DST change a wall-clock time occurs twice and in spring one
    does not exist; `replace(tzinfo=...)` picks the first occurrence / the
    pre-transition offset, so the offset can be off by an hour for that one
    hour. Accepted: it never raises.
    """
    if seconds is None:
        return None
    local = datetime(1970, 1, 1) + timedelta(seconds=seconds)
    gateway_time = local.replace(tzinfo=tz)
    return round((gateway_time - read_at).total_seconds()), gateway_time


# name -> (block, offset, allowed values). A LIST datapoint's "Min. value" in
# Hoval's list is a bitmask of valid values; that is where the program set
# {0,1,2,4,5} and the acknowledge set {7,8} come from. The acknowledge register
# is marked read-only in that list but is documented as written with 7/8.
HV_WRITES: dict[str, tuple[str, int, frozenset[int] | range]] = {
    "program": ("block", 0, frozenset({0, 1, 2, 4, 5})),
    "constant_modulation": ("block", 1, range(15, 101)),
    "eco_modulation": ("block", 2, range(15, 101)),
    "humidity_target": ("block", 4, range(30, 66)),
    "acknowledge": ("maintenance", 5, frozenset({7, 8})),
}


def hv_write_address(unit_id: int, name: str, value: int) -> int:
    """Validate a write and return its register address."""
    spec = HV_WRITES.get(name)
    if spec is None:
        raise ValueError(f"{name!r} is not writable")
    area, offset, allowed = spec
    if value not in allowed:
        raise ValueError(f"value {value} not allowed for {name!r}")
    base = hv_block_base(unit_id) if area == "block" else hv_maintenance_base(unit_id)
    return base + offset


class HovalModbusPoller:
    """Discovery, polling and validated writes for HV units on one gateway."""

    def __init__(self, client: HovalModbusClient, *, slow_ttl: float = 300.0) -> None:
        self.client = client
        self._slow_ttl = slow_ttl
        # unit -> (monotonic time of last slow read, maintenance words, error entries)
        self._slow: dict[int, tuple[float, list[int] | None, list[list[int] | None]]] = {}
        # Gateway clock, read on the slow cadence; None = unknown / not fitted.
        self.gateway_clock: GatewayClockRead | None = None
        self._clock_read_at: float | None = None

    async def discover(self, candidates: Iterable[int] = HV_UNIT_IDS) -> list[int]:
        """Return the units whose block answers.

        Any HovalModbusError (busy, transport) aborts the scan: a busy or
        unreachable gateway must not be reported as "no units".
        """
        found = []
        for unit_id in candidates:
            if await self.client.read_holding(hv_block_base(unit_id), HV_BLOCK_LEN) is not None:
                found.append(unit_id)
        return found

    async def poll(self, unit_ids: Iterable[int]) -> dict[int, HvModbusReading | None]:
        """Read every unit; the slow blocks only when their TTL has passed."""
        result: dict[int, HvModbusReading | None] = {}
        for unit_id in unit_ids:
            block = await self.client.read_holding(hv_block_base(unit_id), HV_BLOCK_LEN)
            if block is None:
                self._slow.pop(unit_id, None)
                result[unit_id] = None
                continue
            now = time.monotonic()
            cached = self._slow.get(unit_id)
            if cached is None or now - cached[0] >= self._slow_ttl:
                maintenance = await self.client.read_holding(
                    hv_maintenance_base(unit_id), HV_MAINTENANCE_LEN
                )
                base = hv_error_base(unit_id)
                errors = [
                    await self.client.read_holding(
                        base + HV_ERROR_ENTRY_LEN * i, HV_ERROR_ENTRY_LEN
                    )
                    for i in range(HV_ERROR_ENTRIES)
                ]
                cached = (now, maintenance, errors)
                self._slow[unit_id] = cached
            result[unit_id] = decode_hv(block, cached[1], cached[2])
        await self._poll_clock()
        return result

    async def _poll_clock(self) -> None:
        """Read the gateway clock (once per gateway) when the slow TTL has passed.

        A failed read leaves the value unknown and never fails the poll: a
        gateway without the datapoint answers exception 2 (read as None), a busy
        one is retried on the next tick.
        """
        now = time.monotonic()
        if self._clock_read_at is not None and now - self._clock_read_at < self._slow_ttl:
            return
        try:
            words = await self.client.read_holding(GATEWAY_CLOCK_ADDRESS, GATEWAY_CLOCK_LEN)
        except HovalModbusError:
            self.gateway_clock = None
            return
        self._clock_read_at = now
        seconds = _u32(words[0], words[1]) if words is not None and len(words) >= 2 else None
        self.gateway_clock = (
            None if seconds is None else GatewayClockRead(seconds, datetime.now(UTC))
        )

    def invalidate_slow(self, unit_id: int) -> None:
        """Make the next poll re-read the unit's maintenance block and errors."""
        self._slow.pop(unit_id, None)

    async def write(self, unit_id: int, name: str, value: int) -> None:
        """Validate, write, and (except acknowledge) verify by reading back."""
        # bool and float pass the membership checks in hv_write_address; never send them.
        if type(value) is not int:
            raise ValueError(f"value {value!r} must be an int")
        address = hv_write_address(unit_id, name, value)
        await self.client.write_register(address, value)
        if name == "acknowledge":
            return
        back = await self.client.read_holding(address, 1)
        if back is None or back[0] != value:
            raise HovalModbusError(
                f"write to {name!r} (register {address}) not confirmed: "
                f"wrote {value}, read back {None if back is None else back[0]}"
            )
