"""The in-guest half of the control channel.

Started by the ``uml-agent`` systemd unit, this serves arpyc on a serial
line -- the other end of which is a socketpair held by the host runner.
Commands therefore work before (and without) any guest networking,
including inside a Nix build sandbox.

Commands run with ``/run/current-system/sw/bin`` on ``PATH``, so whatever
a test's NixOS config puts in ``environment.systemPackages`` is callable.
They also run synchronously, so one guest serves one command at a time --
a test that wants two things at once wants two guests.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess

from .arpyc import Service, listen

AGENT_READY = "uml-agent: ready"
"""Printed on the guest console once the agent is reading :data:`TTY`;
the host waits for this line to know a guest has finished booting, and
connects the moment it sees it.  Nothing may be printed before the line
is set up, or the first request races the setup and is lost."""

TTY = os.environ.get("UML_AGENT_DEVICE", "/dev/ttyS0")
"""Where the host is listening.  ttyS0 under UML.  Under QEMU the console
takes ttyS0 -- a stock kernel prints there from its first line, before any
virtio driver exists -- and the agent gets hvc0 instead."""
GUEST_PATH = "/run/current-system/sw/bin:/run/current-system/sw/sbin"


def _sh(
    command: str, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        # PATH last: the guest's own is what makes `environment.systemPackages`
        # callable, and a caller that sets PATH here would break every later
        # command in ways that read as a missing package. `dict(a, **b)` cannot
        # do this -- a `PATH` in *env* would collide with the keyword.
        env={**os.environ, **(env or {}), "PATH": GUEST_PATH},
    )


class Agent(Service):
    """What the host may ask this guest to do."""

    def on_disconnect(self, conn) -> None:
        print("uml-agent: host disconnected", flush=True)

    def exposed_run(
        self,
        command: str,
        timeout: float = 900,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Run a shell command; returns (exit code, stdout and stderr).

        *env* is added to this one command's environment.  The same
        argument on both backends -- UML could pass a variable on the
        kernel command line and QEMU has no equivalent, so neither does.
        """
        done = _sh(command, timeout, env)
        return done.returncode, (done.stdout + done.stderr).rstrip("\n")

    def exposed_unit_state(self, unit: str) -> str:
        """ActiveState of *unit*, or ``unknown`` if systemd won't say."""
        out = _sh(f"systemctl show --property=ActiveState -- {unit!r}", 30).stdout
        _, sep, state = out.strip().partition("ActiveState=")
        return state if sep else "unknown"

    def exposed_unit_info(self, unit: str) -> dict[str, str]:
        """Every property ``systemctl show`` reports for *unit*."""
        out = _sh(f"systemctl show --no-pager -- {unit!r}", 30).stdout
        pairs = (line.partition("=") for line in out.strip().splitlines())
        return {key: value for key, sep, value in pairs if sep}

    def exposed_list_units(self, pattern: str = "*") -> list[dict]:
        """Units matching *pattern*, one dict per systemctl column."""
        out = _sh(f"systemctl list-units --all --no-legend -- {pattern!r}", 30).stdout
        units = []
        for line in out.splitlines():
            fields = line.split(maxsplit=4)
            if len(fields) >= 4:
                name, load, active, sub, *rest = fields
                units.append(
                    {
                        "name": name,
                        "load": load,
                        "active": active,
                        "sub": sub,
                        "description": rest[0] if rest else "",
                    }
                )
        return units

    def exposed_listening(self) -> list[int]:
        """Ports this guest has a TCP socket listening on.

        Read out of ``/proc/net/tcp`` rather than asked of ``ss``,
        because that is the file pasta's own ``auto`` forwarding watches
        and because it works whatever the guest has installed.  The
        forwards themselves were decided before boot -- passt cannot be
        told about a new one -- so this is here to say what is reachable
        and what came up somewhere nothing is listening for it.
        """
        ports = set()
        for family in ("tcp", "tcp6"):
            try:
                lines = open(f"/proc/net/{family}").read().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                # st == 0A is TCP_LISTEN; local_address is HEXADDR:HEXPORT.
                if len(fields) > 3 and fields[3] == "0A":
                    ports.add(int(fields[1].rsplit(":", 1)[1], 16))
        return sorted(ports)

    def exposed_processes(self) -> list[dict]:
        """Every process in this guest: pid, ppid, name and command line.

        Out of ``/proc`` and not asked of ``ps``, for the reason
        :meth:`exposed_listening` reads ``/proc/net/tcp``: a guest that
        installs no procps still answers.

        ``name`` is ``stat``'s comm, so the first 15 characters of the
        executable's name and nothing longer.  Match on ``cmdline`` for
        anything named longer than that.
        """
        found = []
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                stat = open(f"/proc/{entry.name}/stat").read()
                raw = open(f"/proc/{entry.name}/cmdline", "rb").read()
            except OSError:
                # It exited between the listing and the read, which is
                # the answer: it is not running.
                continue
            # comm is in brackets and may hold spaces and brackets of its
            # own, so split on the last one rather than on whitespace.
            head, _, rest = stat.rpartition(")")
            name = head.partition("(")[2]
            fields = rest.split()
            found.append(
                {
                    "pid": int(entry.name),
                    "ppid": int(fields[1]) if len(fields) > 1 else 0,
                    "name": name,
                    "cmdline": raw.replace(b"\0", b" ").decode(errors="replace").strip(),
                }
            )
        return sorted(found, key=lambda p: p["pid"])

    def exposed_journal(self, unit: str | None = None, lines: int = 50) -> str:
        """Tail of the journal, optionally restricted to one unit."""
        scope = f"-u {unit!r}" if unit else ""
        return _sh(f"journalctl --no-pager -n {lines:d} {scope}", 30).stdout.rstrip()


UNIX_PREFIX = "unix:"
"""A :data:`TTY` that starts with this names a socket path instead.  A
container has no serial line: the agent listens on a socket in a
directory the host binds in, and the host connects once it sees
:data:`AGENT_READY`."""


def _accept_one(path: str) -> int:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    # Listening before the ready line, so the host's connect never races
    # the bind.
    print(AGENT_READY, flush=True)
    conn, _ = server.accept()
    server.close()
    return conn.detach()


async def _serve() -> None:
    if TTY.startswith(UNIX_PREFIX):
        fd = await asyncio.to_thread(_accept_one, TTY[len(UNIX_PREFIX) :])
        conn = listen(fd, Agent(), raw_tty=False)
    else:
        conn = listen(os.open(TTY, os.O_RDWR), Agent())
        print(AGENT_READY, flush=True)
    await conn.serve_forever()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
