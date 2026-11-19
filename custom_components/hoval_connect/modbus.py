"""Minimal asyncio Modbus TCP client for the Hoval TTE-GW gateway.

Why not ``pymodbus``: Home Assistant ships its own pinned pymodbus for its
Modbus integration, and a custom component that depends on it either fights that
pin or breaks when core bumps it. The gateway only needs two function codes
(03 read holding, 06 write single), so ~150 lines of stdlib are cheaper than a
dependency. This module must stay free of ``homeassistant`` imports and of any
third-party import so tests can load it by file path.

Gateway behaviour (measured): responses take 0.2-1.6 s, registers that do not
exist answer exception 2, and the first read of an uncached register can answer
exception 6 (busy) and succeed a moment later.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct

EXC_ILLEGAL_ADDRESS = 2
EXC_BUSY = 6
FC_READ_HOLDING = 3
FC_WRITE_SINGLE = 6

_MBAP_LEN = 7  # transaction id, protocol id, length, unit id
_MAX_PDU = 253


class HovalModbusError(Exception):
    """Transport, protocol or Modbus exception error.

    ``exception_code`` is set only when the gateway answered with a Modbus
    exception response.
    """

    def __init__(self, message: str, *, exception_code: int | None = None) -> None:
        super().__init__(message)
        self.exception_code = exception_code


def encode_request(
    transaction_id: int, unit_id: int, function: int, address: int, word: int
) -> bytes:
    """Build an FC03/FC06 request; `word` is the register count (FC03) or value (FC06)."""
    return struct.pack(">HHHBBHH", transaction_id, 0, 6, unit_id, function, address, word)


def parse_pdu(frame: bytes, *, transaction_id: int, unit_id: int, function: int) -> bytes:
    """Validate a full MBAP frame and return the PDU bytes after the function code."""
    if len(frame) < _MBAP_LEN + 1:
        raise HovalModbusError(f"Modbus frame too short ({len(frame)} bytes)")
    tid, protocol, length, unit = struct.unpack(">HHHB", frame[:_MBAP_LEN])
    if tid != transaction_id:
        raise HovalModbusError(
            f"Modbus transaction id mismatch (got {tid}, expected {transaction_id})"
        )
    if protocol != 0:
        raise HovalModbusError(f"Unexpected Modbus protocol id {protocol}")
    if unit != unit_id:
        raise HovalModbusError(f"Modbus unit id mismatch (got {unit}, expected {unit_id})")
    if length != len(frame) - 6:
        raise HovalModbusError(
            f"Modbus length field {length} does not match frame ({len(frame)} bytes)"
        )
    received = frame[_MBAP_LEN]
    if received == function | 0x80:
        if len(frame) < _MBAP_LEN + 2:
            raise HovalModbusError("Truncated Modbus exception response")
        code = frame[_MBAP_LEN + 1]
        raise HovalModbusError(
            f"Modbus exception {code} for function {function}", exception_code=code
        )
    if received != function:
        raise HovalModbusError(f"Modbus function mismatch (got {received}, expected {function})")
    return frame[_MBAP_LEN + 1 :]


class HovalModbusClient:
    """One persistent, lazily opened connection; requests are serialised."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = 1,
        *,
        timeout: float = 10.0,
        busy_retries: int = 3,
        busy_delay: float = 1.0,
        request_gap: float = 0.05,
    ) -> None:
        self._host = host
        self._port = port
        self._unit_id = unit_id
        self._timeout = timeout
        self._busy_retries = busy_retries
        self._busy_delay = busy_delay
        self._request_gap = request_gap
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transaction_id = 0

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def unit_id(self) -> int:
        return self._unit_id

    async def read_holding(self, address: int, count: int) -> list[int] | None:
        """Read `count` holding registers; None if the gateway says the address does not exist."""
        try:
            pdu = await self._request(FC_READ_HOLDING, address, count, retry_transport=True)
        except HovalModbusError as err:
            if err.exception_code == EXC_ILLEGAL_ADDRESS:
                return None
            raise
        if len(pdu) != 1 + 2 * count or pdu[0] != 2 * count:
            raise HovalModbusError(
                f"Unexpected read response length for {count} registers at {address}"
            )
        return list(struct.unpack(f">{count}H", pdu[1:]))

    async def write_register(self, address: int, value: int) -> None:
        """Write one register and check the echo. Never retried after a transport error."""
        pdu = await self._request(FC_WRITE_SINGLE, address, value, retry_transport=False)
        if pdu != struct.pack(">HH", address, value):
            raise HovalModbusError(f"Write echo mismatch at register {address}")

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect()

    async def _disconnect(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _request(
        self, function: int, address: int, word: int, *, retry_transport: bool
    ) -> bytes:
        async with self._lock:
            reconnects_left = 1 if retry_transport else 0
            busy_left = self._busy_retries
            while True:
                try:
                    pdu = await self._exchange(function, address, word)
                except HovalModbusError as err:
                    if err.exception_code == EXC_BUSY and busy_left > 0:
                        # Busy means the request was not executed: safe for writes too.
                        busy_left -= 1
                        await asyncio.sleep(self._busy_delay)
                        continue
                    raise
                except (OSError, asyncio.IncompleteReadError, TimeoutError) as err:
                    await self._disconnect()
                    if reconnects_left > 0:
                        reconnects_left -= 1
                        continue
                    raise HovalModbusError(
                        f"Modbus connection to {self._host}:{self._port} failed: {err or type(err).__name__}"
                    ) from err
                await asyncio.sleep(self._request_gap)
                return pdu

    async def _exchange(self, function: int, address: int, word: int) -> bytes:
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        tid = self._transaction_id
        try:
            async with asyncio.timeout(self._timeout):
                if self._writer is None or self._reader is None:
                    self._reader, self._writer = await asyncio.open_connection(
                        self._host, self._port
                    )
                self._writer.write(encode_request(tid, self._unit_id, function, address, word))
                await self._writer.drain()
                header = await self._reader.readexactly(_MBAP_LEN)
                length = struct.unpack(">H", header[4:6])[0]
                if not 2 <= length <= _MAX_PDU + 1:
                    raise HovalModbusError(f"Implausible Modbus length field {length}")
                body = await self._reader.readexactly(length - 1)
        except HovalModbusError:
            await self._disconnect()
            raise
        try:
            return parse_pdu(
                header + body, transaction_id=tid, unit_id=self._unit_id, function=function
            )
        except HovalModbusError as err:
            if err.exception_code is None:
                # Out-of-sync stream: drop it so stale frames cannot answer later requests.
                await self._disconnect()
            raise
