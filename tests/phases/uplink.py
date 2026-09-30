#!/usr/bin/env python3
"""Can the guest reach anything off this host?

Prints the answer rather than asserting it, because the answer depends
on how the run was started. `--offline` must make it no; a plain run on
a connected host must make it yes; and a sandboxed run is no either way,
because the sandbox has no network for passt to use.

DNS and TCP separately: they fail differently, and a run that resolves
but cannot connect is a different fault from one that cannot resolve.
"""

from vivarium_runner import Machines

TARGET = "1.1.1.1"
"""An address, not a name, so the TCP answer does not depend on DNS."""


async def test(vms: Machines) -> None:
    rc, out = await vms.one.execute(
        f"timeout 5 bash -c '</dev/tcp/{TARGET}/53' 2>&1", timeout=20
    )
    print(f"[test] tcp to {TARGET}:53 -> {'reachable' if rc == 0 else 'no route'}")

    rc, out = await vms.one.execute(
        "timeout 5 getent hosts example.com 2>&1", timeout=20
    )
    print(f"[test] dns for example.com -> {'answered' if rc == 0 else 'no answer'}")
