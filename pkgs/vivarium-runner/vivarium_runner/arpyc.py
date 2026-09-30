"""Minimal async RPC over a raw fd, wire-compatible with rpyc.

rpyc's own ``Connection`` is thread-based and costs a round trip per
attribute access.  This keeps rpyc's framing and serialisation (brine for
values, vinegar for exceptions) but drives it from asyncio, and adds a
single handler that calls a service method by name so that a call is
exactly one round trip::

    rc, out = await conn.root.run("hostname")

Frame:    [4B BE length][1B compressed flag][payload][b"\\n"]
Payload:  [1B message type] + brine.dump((seq, args))
"""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import termios
import tty
import zlib
from typing import Any

from rpyc.core import brine, consts, vinegar

HANDLE_METHOD = 100
"""Custom handler: args are (method_name, args, kwargs_items).

The server calls ``exposed_<method_name>(*args, **dict(kwargs_items))``.
"""

LABEL_DICT = 99
"""Box label for dicts; brine has no dict support and rpyc's own labels
stop at 4."""

_FRAME_HEADER = struct.Struct("!LB")
_FLUSHER = b"\n"
_READ_CHUNK = 65536


def _dump_exc(typ, val, tb) -> tuple:
    """Serialise an exception; vinegar takes every flag positionally."""
    return vinegar.dump(typ, val, tb, True, True)


def _load_exc(raw: tuple) -> BaseException:
    """Rebuild a serialised exception, refusing to import guest modules."""
    return vinegar.load(raw, False, False, False)


class Service:
    """Base class for objects exposed over arpyc.

    Callable methods are named ``exposed_<name>`` and are reached from the
    peer as ``conn.root.<name>(...)``.
    """

    def on_connect(self, conn: AsyncConnection) -> None:
        pass

    def on_disconnect(self, conn: AsyncConnection) -> None:
        pass


class FdStream:
    """Async byte stream over a bidirectional fd.

    Reads are fed into a ``StreamReader`` from the event loop's reader
    callback; writes go straight to ``os.write`` (rpyc frames are small
    enough that the kernel buffer absorbs them).

    A ``/dev/ttyS0`` fd must be put in raw mode first, otherwise the line
    discipline mangles the binary frames -- pass ``raw_tty=True``.
    """

    def __init__(
        self,
        fd: int,
        loop: asyncio.AbstractEventLoop,
        *,
        raw_tty: bool = False,
    ) -> None:
        self._fd = fd
        self._loop = loop
        self._closed = False
        self._reader = asyncio.StreamReader()
        self._saved_tty = termios.tcgetattr(fd) if raw_tty else None
        if raw_tty:
            # TCSANOW, not tty.setraw's default TCSAFLUSH: this is a data
            # channel, and discarding whatever the peer has already sent
            # loses a request with no way to notice.
            tty.setraw(fd, termios.TCSANOW)
        loop.add_reader(fd, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self._fd, _READ_CHUNK)
        except OSError:
            data = b""
        if data:
            self._reader.feed_data(data)
        else:
            self._reader.feed_eof()

    async def read(self, count: int) -> bytes:
        return await self._reader.readexactly(count)

    async def write(self, data: bytes) -> None:
        while data:
            n = os.write(self._fd, data)
            if n <= 0:
                raise EOFError("fd write failed")
            data = data[n:]

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.remove_reader(self._fd)
        self._reader.feed_eof()
        if self._saved_tty is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSANOW, self._saved_tty)
            except termios.error:
                pass
        os.close(self._fd)


class Channel:
    """rpyc's framing, with async I/O."""

    def __init__(self, stream: FdStream) -> None:
        self._stream = stream

    async def recv(self) -> bytes:
        header = await self._stream.read(_FRAME_HEADER.size)
        length, compressed = _FRAME_HEADER.unpack(header)
        data = (await self._stream.read(length + len(_FLUSHER)))[: -len(_FLUSHER)]
        return zlib.decompress(data) if compressed else data

    async def send(self, data: bytes) -> None:
        await self._stream.write(_FRAME_HEADER.pack(len(data), 0) + data + _FLUSHER)

    @property
    def closed(self) -> bool:
        return self._stream.closed

    def close(self) -> None:
        self._stream.close()


class _RemoteMethod:
    __slots__ = ("_conn", "_name")

    def __init__(self, conn: AsyncConnection, name: str) -> None:
        self._conn = conn
        self._name = name

    def __call__(self, *args: Any, **kwargs: Any):
        return self._conn.call(self._name, args, kwargs)


class _RemoteRoot:
    """Turns ``conn.root.foo(...)`` into a call of the peer's
    ``exposed_foo``."""

    __slots__ = ("_conn",)

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> _RemoteMethod:
        if name.startswith("_"):
            raise AttributeError(name)
        return _RemoteMethod(self._conn, name)


class AsyncConnection:
    """A symmetric arpyc endpoint: serves *service* and calls the peer's."""

    def __init__(self, service: Service, channel: Channel) -> None:
        self._channel = channel
        self._service = service
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._closed = False
        self._serving: asyncio.Task | None = None
        self.root = _RemoteRoot(self)
        service.on_connect(self)

    # ── serialisation ──────────────────────────────────────────────

    def _box(self, obj: Any) -> tuple:
        """Wrap *obj* in a label so brine can carry lists and dicts."""
        if brine.dumpable(obj):
            return consts.LABEL_VALUE, obj
        if isinstance(obj, (tuple, list)):
            return consts.LABEL_TUPLE, tuple(self._box(item) for item in obj)
        if isinstance(obj, dict):
            return LABEL_DICT, tuple((k, self._box(v)) for k, v in obj.items())
        return consts.LABEL_VALUE, obj

    def _unbox(self, package: tuple) -> Any:
        label, value = package
        if label == consts.LABEL_TUPLE:
            return tuple(self._unbox(item) for item in value)
        if label == LABEL_DICT:
            return {k: self._unbox(v) for k, v in value}
        return value

    async def _send(self, msg_type: int, seq: int, data: Any) -> None:
        await self._channel.send(brine.I1.pack(msg_type) + brine.dump((seq, data)))

    # ── serving ────────────────────────────────────────────────────

    async def _dispatch(self, seq: int, args: tuple) -> None:
        handler_id, boxed_args = args
        try:
            if handler_id == HANDLE_METHOD:
                method, call_args, kwargs = self._unbox(boxed_args)
                func = getattr(self._service, "exposed_" + method)
                result = func(*call_args, **dict(kwargs))
            elif handler_id == consts.HANDLE_PING:
                result = self._unbox(boxed_args)
            elif handler_id == consts.HANDLE_CLOSE:
                self.close()
                return
            else:
                raise ValueError(f"unknown handler {handler_id}")
        except Exception:
            await self._send(consts.MSG_EXCEPTION, seq, _dump_exc(*sys.exc_info()))
        else:
            await self._send(consts.MSG_REPLY, seq, self._box(result))

    async def _serve_one(self) -> None:
        """Read one frame and dispatch it."""
        data = await self._channel.recv()
        msg = brine.I1.unpack(data[:1])[0]
        seq, args = brine.load(data[1:])

        if msg == consts.MSG_REQUEST:
            await self._dispatch(seq, args)
            return

        fut = self._pending.pop(seq, None)
        if fut is None or fut.done():
            return
        if msg == consts.MSG_REPLY:
            fut.set_result(self._unbox(args))
        elif msg == consts.MSG_EXCEPTION:
            fut.set_exception(_load_exc(args))

    async def serve_forever(self) -> None:
        """Serve until the peer goes away."""
        try:
            while not self._closed:
                await self._serve_one()
        except (EOFError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self.close()

    def start_serving(self) -> None:
        """Serve in the background, for a side that mostly calls out."""
        if self._serving is None:
            # Kept on the connection: a bare task may be collected.
            self._serving = asyncio.ensure_future(self.serve_forever())

    # ── calling ────────────────────────────────────────────────────

    async def call(self, method: str, args: tuple, kwargs: dict) -> Any:
        """Invoke ``exposed_<method>`` on the peer and await its result."""
        seq = self._seq
        self._seq += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[seq] = fut
        boxed = self._box((method, args, tuple(kwargs.items())))
        await self._send(consts.MSG_REQUEST, seq, (HANDLE_METHOD, boxed))
        return await fut

    # ── lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._service.on_disconnect(self)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(EOFError("arpyc connection closed"))
        self._pending.clear()
        self._channel.close()

    @property
    def closed(self) -> bool:
        return self._closed


def _connection(service: Service, fd: int, *, raw_tty: bool) -> AsyncConnection:
    loop = asyncio.get_running_loop()
    return AsyncConnection(service, Channel(FdStream(fd, loop, raw_tty=raw_tty)))


def connect(fd: int) -> AsyncConnection:
    """Client side: talk to a guest agent over *fd* (a socketpair end)."""
    conn = _connection(Service(), fd, raw_tty=False)
    conn.start_serving()
    return conn


def listen(fd: int, service: Service, *, raw_tty: bool = True) -> AsyncConnection:
    """Guest side: put *fd* (``/dev/ttyS0``) in raw mode and start
    reading it.  Call ``serve_forever`` on the result to answer.

    Reading starts here rather than in ``serve_forever`` so that a guest
    can announce itself and be certain that nothing sent afterwards is
    missed: from this point requests queue up in the stream.

    ``raw_tty=False`` for a socket, which has no line discipline to turn
    off and fails ``tcgetattr``.
    """
    return _connection(service, fd, raw_tty=raw_tty)
