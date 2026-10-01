"""A QEMU guest's balloon, screen and input devices, over the monitor.

The protocol is QEMU's own ``qemu.qmp``, which the QEMU project ships and
nixpkgs packages: it does the greeting, the capabilities handshake, the
difference between an event and a reply, and the errors. What is here is
only the part that is ours. One client for everything: QEMU's ``-qmp``
socket takes one connection at a time.

See :meth:`Machine.shrink` and :meth:`Machine.screenshot`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from qemu.qmp import QMPClient, QMPError

__all__ = ["Qmp", "QmpError"]

QmpError = QMPError
"""The library's own, re-exported so a caller catches one name whichever
control channel its guest has."""


class Qmp:
    """One guest's monitor. Not connected until a command needs it."""

    #: How close to the target counts as arrived. The guest inflates in
    #: whole pages and stops when it has enough, so it lands near the
    #: number rather than on it.
    SLACK = 8 * 1024 * 1024

    #: How long the guest may sit still before this stops waiting for it.
    #: A guest inflating a balloon pauses while it reclaims, so a short
    #: value reads a pause as an arrival: measured, 3.0 gave up at 189 MiB
    #: of a 256 MiB request that 5.0 completes.
    STALL = 5.0

    def __init__(self, path: Path) -> None:
        self.path = path
        self._client: QMPClient | None = None
        # Two first calls at once would open two connections, and the
        # socket serves one at a time.
        self._connecting = asyncio.Lock()

    async def _connected(self) -> QMPClient:
        async with self._connecting:
            if self._client is None:
                client = QMPClient("vivarium-runner")
                await client.connect(str(self.path))
                self._client = client
            return self._client

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.disconnect()

    async def screendump(self, path: Path, png: bool = True) -> None:
        """Write the screen to *path*, PNG or PPM. QEMU writes the file."""
        client = await self._connected()
        await client.execute("screendump", {"filename": str(path), "format": "png" if png else "ppm"})

    async def sendkey(self, keys: str) -> None:
        """Press and release *keys*, in the monitor's ``sendkey`` syntax.

        Through the human monitor, not ``send-key``: it takes nixos-test's
        spelling as is, ``ctrl-alt-delete`` and the ``0x0C`` scancodes in
        its character table alike, and holds the keys 100 ms.
        """
        client = await self._connected()
        reply = await client.execute("human-monitor-command", {"command-line": f"sendkey {keys}"})
        if reply:
            raise QmpError(f"sendkey {keys}: {reply}")

    async def input(self, events: list[dict]) -> None:
        """Send ``input-send-event`` events, as one batch."""
        client = await self._connected()
        await client.execute("input-send-event", {"events": events})

    @staticmethod
    async def _actual(client: QMPClient) -> int:
        reply = cast(dict, await client.execute("query-balloon"))
        return int(reply["actual"])

    async def balloon(self, delta: int, timeout: float = 60.0) -> None:
        """Move the guest's memory target by *delta* bytes, and wait.

        ``query-balloon`` reports what the guest has now, which is what
        makes a relative move possible at all: QEMU's ``balloon`` command
        takes the target size and nothing else.

        **And returns immediately.** It asks the guest, and the guest
        inflates over the following seconds -- so a caller that reads
        ``MemFree`` straight afterwards sees nothing move. Measured: 0 of
        256 MiB. This waits for the guest to arrive.

        Asking for more than the guest booted with is not an error and
        does nothing: QEMU clamps the target to ``-m``, which is the same
        limit UML's console has. That is also why waiting has to give up
        quietly rather than raise -- the guest is already where it can be.
        """
        client = await asyncio.wait_for(self._connected(), timeout)
        actual = await self._actual(client)
        target = max(0, actual + delta)
        await client.execute("balloon", {"value": target})

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last, moved_at = actual, loop.time()
        while loop.time() < deadline:
            await asyncio.sleep(0.2)
            now = await self._actual(client)
            if abs(now - target) <= self.SLACK:
                return
            if now != last:
                last, moved_at = now, loop.time()
            elif loop.time() - moved_at > self.STALL:
                # It has stopped short and is not coming: the target was
                # above `-m`, or the guest cannot free any more. Either
                # way the caller measures what happened rather than being
                # told it worked.
                return
