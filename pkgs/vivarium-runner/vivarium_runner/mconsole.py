"""UML's management console, spoken from the host.

The guest listens on a unix datagram socket in its ``umid`` directory and
answers one command per datagram.  There is a ``uml_mconsole`` client in
uml-utilities; this is the same protocol in forty lines, which is less
than packaging it would be.

    magic 0xcafebabe, version 2, the command's length, the command
    err, more, the reply's length, the reply

``arch/um/drivers/mconsole.h`` is the definition of both structs, and the
version number is checked: a guest built against a different one answers
"This driver only supports version N clients" rather than guessing.

What this is for is ``config mem=``.  See :meth:`Machine.shrink`.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from pathlib import Path

MAGIC = 0xCAFEBABE
VERSION = 2
MAX_DATA = 512
"""``MCONSOLE_MAX_DATA``.  A longer command is refused by the guest, and a
longer reply arrives split across datagrams with ``more`` set."""

UMID = "mc"
"""The ``umid=`` every guest gets.  The socket is
``<uml_dir>/<umid>/mconsole`` and ``uml_dir`` is the guest's own run
directory, so one fixed name per guest collides with nothing.

Short on purpose: the path goes in a ``sockaddr_un``, which holds 108
bytes including the NUL.  The same limit passt hits -- see
``forward.py``."""

_REQUEST = struct.Struct("=III")
_REPLY = struct.Struct("=III")


class MconsoleError(RuntimeError):
    """The guest refused a command, or did not answer one."""


UNIX_PATH_MAX = 108
"""``sizeof(struct sockaddr_un.sun_path)``, including the NUL.  UML does
not check: ``bind`` fails, the console does not come up, and the guest
carries on booting with one line about it in a very long log."""


def socket_path(uml_dir: Path) -> Path:
    """Where a guest started with ``uml_dir=<uml_dir> umid=mc`` listens."""
    return uml_dir / UMID / "mconsole"


class Mconsole:
    """One guest's console.  Not connected until :meth:`request` is called.

    The client socket is bound to a path rather than left to the kernel's
    autobind, because the guest replies with ``sendto`` to the address the
    request came from and an unbound sender has none to reply to.
    """

    def __init__(self, path: Path, client: Path) -> None:
        self.path = path
        self._client = client
        self._socket: socket.socket | None = None

    def _open(self) -> socket.socket:
        if self._socket is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.setblocking(False)
            self._client.parent.mkdir(parents=True, exist_ok=True)
            self._client.unlink(missing_ok=True)
            sock.bind(str(self._client))
            self._socket = sock
        return self._socket

    async def close(self) -> None:
        """Async to match the QEMU monitor's, which has tasks to stop."""
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._client.unlink(missing_ok=True)

    async def request(self, command: str, timeout: float = 30.0) -> str:
        """Send *command* and return what the guest says.

        *timeout* is generous because the guest answers in a kernel
        thread and ``config mem=`` holds a mutex while it allocates a
        page at a time: 256 MiB is 65536 iterations.
        """
        payload = command.encode()
        if len(payload) >= MAX_DATA:
            raise MconsoleError(f"command is longer than {MAX_DATA} bytes: {command!r}")
        if not self.path.exists():
            raise MconsoleError(
                f"no mconsole socket at {self.path}: this guest was started "
                "without one, or its kernel has no CONFIG_MCONSOLE"
            )

        sock = self._open()
        loop = asyncio.get_running_loop()
        message = _REQUEST.pack(MAGIC, VERSION, len(payload)) + payload
        await loop.sock_sendto(sock, message, str(self.path))

        parts: list[str] = []
        failed = False
        while True:
            datagram, _ = await asyncio.wait_for(
                loop.sock_recvfrom(sock, _REPLY.size + MAX_DATA), timeout
            )
            err, more, length = _REPLY.unpack_from(datagram)
            # The reply's length counts the NUL the guest appends.
            text = datagram[_REPLY.size : _REPLY.size + length].rstrip(b"\0")
            parts.append(text.decode(errors="replace"))
            # `err` is set on the first datagram only, and a long error
            # still arrives in several. Read them all before raising:
            # one left on the socket becomes the next command's reply.
            failed = failed or bool(err)
            if not more:
                break
        message = "".join(parts)
        if failed:
            raise MconsoleError(f"{command}: {message}")
        return message

    async def balloon(self, delta: int) -> None:
        """Move this guest's memory by *delta* bytes.

        Negative takes memory away from the guest and gives the host back
        the pages behind it; positive returns pages an earlier negative
        took, and cannot go above the `mem=` the guest booted with.

        Bytes, and the console wants a size, so this rounds to whole
        kibibytes -- `memparse` reads what `mem=` does.
        """
        sign = "-" if delta < 0 else "+"
        await self.request(f"config mem={sign}{abs(delta) // 1024}K")


__all__ = ["UMID", "UNIX_PATH_MAX", "Mconsole", "MconsoleError", "socket_path"]
