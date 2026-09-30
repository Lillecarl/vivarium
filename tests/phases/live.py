"""Holds its guest until something outside the phase makes /tmp/go; see `live-exec`."""

import anyio

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    with anyio.fail_after(120):
        while (await vms.one.execute("test -e /tmp/go"))[0] != 0:
            print("[live] tick")
            await anyio.sleep(0.3)
    print("[live] released")
