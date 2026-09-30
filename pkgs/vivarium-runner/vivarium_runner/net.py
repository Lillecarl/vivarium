"""Host-side wiring for the guests' ``vec1`` interfaces.

UML's vector transport can take a pre-opened fd carrying raw Ethernet
frames, so an L2 segment is just a set of connected sockets.  Two guests
on a segment get the two ends of one ``SOCK_SEQPACKET`` socketpair and
the host stays out of the data path entirely.  Three or more need
something to fan frames out, which is what :class:`Lan` does when it has
to -- a learning-free hub that floods every frame to every other port.
One guest on its own gets a segment with nobody else on it, which is a
cable into a bucket: `vec1` exists and carries an address, and whatever
it says goes nowhere.

How much a segment carries comes down to how many frames may be in
flight in a socketpair, and AF_UNIX bounds that two ways:

  * the receiver's queue length, capped by ``net.unix.max_dgram_qlen``
    -- 10 in a fresh network namespace, which is what a Nix build
    sandbox gets, and not writable there;
  * the *sender's* ``SO_SNDBUF``, which every queued frame is charged
    against until the receiver reads it.

Only the second is ours to set, so we set it to the maximum the host
allows.  The first is why guests use a large MTU (see ``boot.uml.mtu``):
ten 64K frames is a window worth having, ten 1500-byte ones is 15K, and
that difference is most of the throughput between two guests.
"""

from __future__ import annotations

import asyncio
import socket

_FRAME_MAX = 65536

_BURST = 64
"""Frames to take from one port before giving the others a turn."""

_WANTED_SNDBUF = 8 << 20
"""Asked for on both ends of every port.  The kernel silently clamps it
to ``net.core.wmem_max``, so asking for more than we can have costs
nothing.  There is no matching ``SO_RCVBUF``: an AF_UNIX datagram is
charged to whoever sent it until the far side reads it, so the sending
socket's buffer is the only one that bounds a segment."""


class Lan:
    """One Ethernet segment connecting the named machines.

    ``fds[name]`` is the fd to hand that machine's UML process; it must
    stay open in this process until the child has been spawned.
    """

    def __init__(self, name: str, members: list[str]) -> None:
        if not members:
            raise ValueError(f"lan {name!r} has no machines on it")
        self.name = name
        self.fds: dict[str, int] = {}
        self.dropped = 0
        self._guest_ends: list[socket.socket] = []
        self._ports: list[socket.socket] = []

        if len(members) == 1:
            # A guest alone on a segment: it still wants vec1 to exist and
            # carry its address -- a node in a cluster of one is a real
            # thing to test -- so it gets one end of a pair and the host
            # quietly holds the other.
            guest_end, host_end = self._pair()
            self._guest_ends = [guest_end]
            self._ports = [host_end]
            self.fds = {members[0]: guest_end.fileno()}
        elif len(members) == 2:
            # Point to point: no host involvement, no copying.
            a, b = self._pair()
            self._guest_ends = [a, b]
            self.fds = dict(zip(members, (a.fileno(), b.fileno())))
        else:
            for member in members:
                host_end, guest_end = self._pair()
                self._ports.append(host_end)
                self._guest_ends.append(guest_end)
                self.fds[member] = guest_end.fileno()

    @staticmethod
    def _pair() -> tuple[socket.socket, socket.socket]:
        pair = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        for sock in pair:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _WANTED_SNDBUF)
        return pair

    def start(self) -> None:
        """Start flooding frames between ports, if this segment needs a hub."""
        loop = asyncio.get_running_loop()
        for port in self._ports:
            port.setblocking(False)
            others = [other for other in self._ports if other is not port]
            loop.add_reader(port, self._flood, port, others)

    def _flood(self, src: socket.socket, others: list[socket.socket]) -> None:
        """Copy everything readable on *src* to every other port.

        A callback rather than a task per frame: two awaits and the
        futures behind them cost more than the copy itself, and this runs
        once per frame on the segment.  Ports that will not take a frame
        have it dropped, which is what a switch does with a congested
        port -- blocking here would stall every other port as well.
        """
        for _ in range(_BURST):
            try:
                frame = src.recv(_FRAME_MAX)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self._drop_port(src)
                return
            if not frame:
                self._drop_port(src)
                return
            for dst in others:
                try:
                    dst.send(frame)
                except OSError:
                    self.dropped += 1

    def _drop_port(self, src: socket.socket) -> None:
        """Stop listening to a port whose guest has gone away."""
        asyncio.get_running_loop().remove_reader(src)

    def detach(self) -> None:
        """Drop our copies of the guests' fds, once they have all spawned.

        Until this runs, every guest's end is held open here too, so no
        guest would ever see the segment go quiet when a peer dies.
        """
        for sock in self._guest_ends:
            sock.close()
        self._guest_ends.clear()

    def close(self) -> None:
        if self.dropped:
            print(
                f"[test] lan {self.name}: dropped {self.dropped} frames "
                f"on congested ports",
                flush=True,
            )
        loop = asyncio.get_running_loop()
        for sock in self._ports:
            loop.remove_reader(sock)
        self.detach()
        for sock in self._ports:
            sock.close()
        self._ports.clear()


def build_lans(networks: dict[str, list[str]]) -> list[Lan]:
    """Create a :class:`Lan` per segment in ``{segment: [machine, ...]}``."""
    return [Lan(name, members) for name, members in sorted(networks.items())]
