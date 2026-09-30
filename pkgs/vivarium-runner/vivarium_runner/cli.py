"""``vivarium-run``: boot one guest interactively, or run one command in it.

The NixOS side generates a wrapper with every path already filled in
(see ``system.build.vivariumRunner``), so from a shell this is::

    nix run .#speedtest
    result/bin/vivarium-run --command 'systemctl status'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .forward import ForwardError, Rule
from .machine import Machine, MachineError, MachineSpec, Toolchain

_WATCH_INTERVAL = 1.0
"""How often to ask the guest what it is listening on.  The same rate
pasta polls ``/proc/net/tcp`` at for its own forwarding, and the agent
serves one request at a time, so this is only done when nothing else is
using the channel."""


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Boot a NixOS system under UML")
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--root-image", type=Path, required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--passt", type=Path, required=True)
    parser.add_argument("--ssh-port", type=int, default=4325)
    parser.add_argument(
        "--forward",
        default="[]",
        help="JSON list of forward rules, as vivarium.forward generates",
    )
    parser.add_argument("--mem", default="128M")
    parser.add_argument("--mtu", type=int, default=65000)
    parser.add_argument(
        "--command",
        help="run this in the guest, print its output, and power off",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="seconds to allow --command (default: %(default)s)",
    )
    return parser.parse_args(argv)


def _report(machine: Machine, ports: list[int]) -> None:
    """Say what the guest is listening on, and where that answers."""
    if not ports:
        return
    print("[vivarium] listening in guest:", flush=True)
    for port in ports:
        where = machine.reachable(port)
        print(
            f"[vivarium]     {port:<6}-> {', '.join(where) if where else 'not forwarded'}",
            flush=True,
        )


async def _watch(machine: Machine) -> None:
    """Report the guest's listening ports as they come and go.

    Only run when there is no ``--command``: the agent serves one
    request at a time, so polling would otherwise take turns with
    whatever the caller actually wanted to do.
    """
    seen: list[int] = []
    while True:
        try:
            ports = await machine.listening()
        except (MachineError, OSError, EOFError):
            return
        if ports != seen:
            _report(machine, [port for port in ports if port not in seen])
            seen = ports
        await asyncio.sleep(_WATCH_INTERVAL)


async def _run(args: argparse.Namespace) -> int:
    machine = Machine(
        MachineSpec(
            name="uml",
            image=args.root_image,
            memory=args.mem,
            ssh_port=args.ssh_port,
            mtu=args.mtu,
            forward=tuple(Rule.from_json(rule) for rule in json.loads(args.forward)),
        ),
        Toolchain(kernel=args.kernel, bridge=args.bridge, passt=args.passt),
    )
    machine.resolve_forward(set())
    await machine.start()
    watcher: asyncio.Task | None = None
    try:
        if args.command:
            rc, out = await machine.execute(args.command, timeout=args.timeout)
            print(out, flush=True)
            return rc
        # No command: leave it running and stream the console until the
        # guest exits or the user interrupts us.
        where = machine.reachable(args.ssh_port)
        if where:
            address, _, port = where[0].rpartition(":")
            print(f"[vivarium] up; ssh -p {port} root@{address}, ^C to stop", flush=True)
        else:
            print(
                f"[vivarium] up; guest port {args.ssh_port} is not forwarded, ^C to stop",
                flush=True,
            )
        watcher = asyncio.ensure_future(_watch(machine))
        return await machine.wait(timeout=None)
    finally:
        if watcher is not None:
            watcher.cancel()
        await machine.shutdown()


def main(argv: list[str] | None = None) -> None:
    try:
        raise SystemExit(asyncio.run(_run(_parse(argv))))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (MachineError, ForwardError) as error:
        print(f"[vivarium] {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
