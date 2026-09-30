#!/usr/bin/env python3
"""A phase that fails on purpose.

Stands in for the expensive thing that goes wrong -- a cluster that does
not come up. What matters is not this failure but what happens to the
phases around it.
"""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    print("[test] pretending to build something, and failing")
    await vms.one.succeed("exit 1")
