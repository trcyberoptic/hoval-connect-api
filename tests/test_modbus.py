"""Tests for the stdlib Modbus TCP client (no Home Assistant needed)."""

from __future__ import annotations

import asyncio
import importlib.util
import struct
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "hoval_connect" / "modbus.py"
_SPEC = importlib.util.spec_from_file_location("_hoval_modbus_under_test", _PATH)
modbus = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(modbus)

BLOCK = [1, 50, 65, 45, 54, 50, 255, 255, 0, 1, 93, 231, 53]


def _read_response(tid: int, words: list[int]) -> bytes:
    body = bytes([26]) + b"".join(w.to_bytes(2, "big") for w in words)
    return struct.pack(">HHHBB", tid, 0, len(body) + 2, 1, 3) + body


def _exception_response(tid: int, function: int, code: int) -> bytes:
    return struct.pack(">HHHBBB", tid, 0, 3, 1, function | 0x80, code)


def _write_echo(tid: int, address: int, value: int) -> bytes:
    return struct.pack(">HHHBBHH", tid, 0, 6, 1, 6, address, value)


class FakeServer:
    """Loopback server; `script(server, conn_index, request)` returns reply bytes.

    Returning None closes the connection without answering.
    """

    def __init__(self, script, *, delay=0.02):
        self._script = script
        self.delay = delay  # seconds between receiving a request and replying
        self.requests: list[tuple[int, int, int, int]] = []  # tid, fc, address, word
        self.connections = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._server = None
        self.port = 0

    async def _handle(self, reader, writer):
        conn = self.connections
        self.connections += 1
        try:
            while True:
                data = await reader.readexactly(12)
                tid, _proto, _length, _unit, fc, address, word = struct.unpack(">HHHBBHH", data)
                self.requests.append((tid, fc, address, word))
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                await asyncio.sleep(self.delay)
                reply = self._script(self, conn, (tid, fc, address, word))
                self.in_flight -= 1
                if reply is None:
                    break
                writer.write(reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            # The client dropped the connection (e.g. after a cancelled request).
            pass
        finally:
            writer.close()

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


def _client(server, **kwargs):
    kwargs.setdefault("busy_delay", 0)
    kwargs.setdefault("request_gap", 0)
    kwargs.setdefault("timeout", 2.0)
    return modbus.HovalModbusClient("127.0.0.1", server.port, 1, **kwargs)


def test_encode_read_matches_live_request():
    assert modbus.encode_request(1, 1, 3, 23622, 13) == bytes.fromhex("00010000000601035c46000d")


def test_encode_write_matches_live_request():
    assert modbus.encode_request(2, 1, 6, 23624, 66) == bytes.fromhex("00020000000601065c480042")


def test_parse_read_response():
    words = [1, 50, 65, 45, 54, 50, 255, 255, 0, 1, 93, 231, 53]
    frame = (
        bytes.fromhex("00010000001d0103")
        + bytes([26])
        + b"".join(w.to_bytes(2, "big") for w in words)
    )
    pdu = modbus.parse_pdu(frame, transaction_id=1, unit_id=1, function=3)
    assert pdu[0] == 26 and int.from_bytes(pdu[1:3], "big") == 1


def test_parse_exception_response_carries_code():
    with pytest.raises(modbus.HovalModbusError) as err:
        modbus.parse_pdu(
            bytes.fromhex("000100000003018302"), transaction_id=1, unit_id=1, function=3
        )
    assert err.value.exception_code == 2


@pytest.mark.parametrize(
    "frame",
    [
        bytes.fromhex("000900000003018302"),  # wrong transaction id
        bytes.fromhex("000100000003028302"),  # wrong unit id
        bytes.fromhex("000100000003018402"),  # wrong function
        bytes.fromhex("000100010003018302"),  # wrong protocol id
        bytes.fromhex("0001000000050183"),  # length field disagrees with frame
    ],
)
def test_parse_rejects_mismatch(frame):
    with pytest.raises(modbus.HovalModbusError) as err:
        modbus.parse_pdu(frame, transaction_id=1, unit_id=1, function=3)
    assert err.value.exception_code is None


async def test_read_returns_words_and_none_for_illegal_address():
    def script(_srv, _conn, req):
        tid, fc, address, _word = req
        if address == 23622:
            return _read_response(tid, BLOCK)
        return _exception_response(tid, fc, 2)

    async with FakeServer(script) as server:
        client = _client(server)
        try:
            assert await client.read_holding(23622, 13) == BLOCK
            assert await client.read_holding(23531, 13) is None
        finally:
            await client.close()


async def test_busy_is_retried_then_succeeds():
    def script(srv, _conn, req):
        if len(srv.requests) <= 2:
            return _exception_response(req[0], req[1], 6)
        return _read_response(req[0], BLOCK)

    async with FakeServer(script) as server:
        client = _client(server)
        try:
            assert await client.read_holding(23622, 13) == BLOCK
        finally:
            await client.close()
        assert len(server.requests) == 3


async def test_busy_exhausted_raises():
    def script(_srv, _conn, req):
        return _exception_response(req[0], req[1], 6)

    async with FakeServer(script) as server:
        client = _client(server, busy_retries=3)
        try:
            with pytest.raises(modbus.HovalModbusError) as err:
                await client.read_holding(23622, 13)
        finally:
            await client.close()
        assert err.value.exception_code == 6
        assert len(server.requests) == 4


async def test_write_checks_echo():
    def good(_srv, _conn, req):
        return _write_echo(req[0], req[2], req[3])

    def bad(_srv, _conn, req):
        return _write_echo(req[0], req[2], req[3] - 1)

    async with FakeServer(good) as server:
        client = _client(server)
        try:
            await client.write_register(23624, 66)
        finally:
            await client.close()
        assert server.requests[0][1:] == (6, 23624, 66)
    async with FakeServer(bad) as server:
        client = _client(server)
        try:
            with pytest.raises(modbus.HovalModbusError):
                await client.write_register(23624, 66)
        finally:
            await client.close()


async def test_read_reconnects_after_server_drops_connection():
    def script(srv, conn, req):
        # Drop connection 0 on its second request instead of answering.
        if conn == 0 and len(srv.requests) > 1:
            return None
        return _read_response(req[0], BLOCK)

    async with FakeServer(script) as server:
        client = _client(server)
        try:
            assert await client.read_holding(23622, 13) == BLOCK
            assert await client.read_holding(23622, 13) == BLOCK
        finally:
            await client.close()
        assert server.connections == 2


async def test_write_is_not_repeated_after_lost_response():
    async with FakeServer(lambda *_: None) as server:
        client = _client(server)
        try:
            with pytest.raises(modbus.HovalModbusError):
                await client.write_register(23624, 66)
        finally:
            await client.close()
        assert len(server.requests) == 1
        assert server.requests[0][1] == 6


async def test_concurrent_requests_do_not_interleave():
    def script(_srv, _conn, req):
        return _read_response(req[0], BLOCK)

    async with FakeServer(script) as server:
        client = _client(server)
        try:
            results = await asyncio.gather(
                client.read_holding(23622, 13), client.read_holding(23623, 13)
            )
        finally:
            await client.close()
        assert results == [BLOCK, BLOCK]
        assert server.max_in_flight == 1
        assert len({r[0] for r in server.requests}) == 2  # distinct transaction ids


async def test_connection_refused_raises_modbus_error():
    async with FakeServer(lambda *_: None) as server:
        port = server.port
    client = modbus.HovalModbusClient("127.0.0.1", port, timeout=1.0, request_gap=0)
    with pytest.raises(modbus.HovalModbusError):
        await client.read_holding(1, 1)


async def test_write_reconnects_when_idle_connection_was_closed_by_peer():
    writers = []

    def script(srv, _conn, req):
        if req[1] == 3:
            return _read_response(req[0], BLOCK)
        return _write_echo(req[0], req[2], req[3])

    async with FakeServer(script) as server:
        orig = server._handle

        async def handle(reader, writer):
            writers.append(writer)
            await orig(reader, writer)

        server._server.close()
        await server._server.wait_closed()
        server._server = await asyncio.start_server(handle, "127.0.0.1", server.port)
        client = _client(server)
        try:
            assert await client.read_holding(23622, 13) == BLOCK
            writers[0].close()  # server drops the idle connection
            await asyncio.sleep(0.1)
            await client.write_register(23624, 66)
        finally:
            await client.close()
        assert server.connections == 2
        assert [r for r in server.requests if r[1] == 6] == [(server.requests[-1][0], 6, 23624, 66)]


async def test_timeout_error_text_names_the_exception():
    """str(TimeoutError()) is empty; the message must not end in "failed: " (D1)."""
    async with FakeServer(lambda *_: b"") as server:  # reads, never answers
        client = _client(server, timeout=0.2)
        try:
            with pytest.raises(modbus.HovalModbusError) as err:
                await client.write_register(23624, 66)
        finally:
            await client.close()
    assert str(err.value).endswith("failed: TimeoutError")


async def test_cancelled_request_does_not_leave_a_stale_reply_for_the_next():
    """A request cancelled mid-exchange must not let its late reply answer the next one (D2)."""

    def script(_srv, _conn, req):
        return _read_response(req[0], BLOCK)

    async with FakeServer(script, delay=0.3) as server:
        client = _client(server)
        try:
            task = asyncio.create_task(client.read_holding(23622, 13))
            await asyncio.sleep(0.1)  # request sent, reply not yet received
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.4)  # the late reply to the cancelled request arrives
            server.delay = 0.0
            assert await client.read_holding(23622, 13) == BLOCK
        finally:
            await client.close()
        assert server.connections == 2


async def test_requests_after_close_raise_instead_of_reconnecting():
    """close() is final: a poll still running must not open a new connection (M2)."""

    def script(_srv, _conn, req):
        return _read_response(req[0], BLOCK)

    async with FakeServer(script, delay=0.1) as server:
        client = _client(server)
        try:
            # close() runs while the first read is in flight and waits for it.
            first, _ = await asyncio.gather(client.read_holding(23622, 13), client.close())
            assert first == BLOCK
            with pytest.raises(modbus.HovalModbusError, match="closed"):
                await client.read_holding(23622, 13)
            with pytest.raises(modbus.HovalModbusError, match="closed"):
                await client.write_register(23624, 66)
        finally:
            # Without the fix the reads above reconnect; the server only shuts
            # down once every connection is closed.
            await client.close()
        assert server.connections == 1
        assert len(server.requests) == 1
