#!/usr/bin/env python3
"""Two container guests and a UML guest on one segment, each reaching the
others.

A container's vec1 is a tap relayed to the same segment fd UML and QEMU
take, so the three kinds share one wire. `systemd-detect-virt` proves the
run holds both kinds, and not three of one.
"""

from vivarium_runner import Machines

ADDRESSES = {"a": "10.56.0.1", "b": "10.56.0.2", "u": "10.56.0.3"}


async def test(vms: Machines) -> None:
    kinds = {name: (await vm.execute("systemd-detect-virt"))[1].strip() for name, vm in vms.items()}
    print(f"[test] machines: {kinds}")
    if kinds != {"a": "container-other", "b": "container-other", "u": "uml"}:
        raise AssertionError(f"expected two containers and one UML guest, got {kinds}")

    for name, vm in vms.items():
        for other, address in ADDRESSES.items():
            if other != name:
                await vm.succeed(f"ping -c 2 -W 5 {address}")
    print("[test] every guest reaches every other across the segment")

    # A frame larger than 1500 bytes: the tap carries the segment's MTU.
    await vms.a.succeed("ping -c 2 -W 5 -M do -s 8000 10.56.0.3")
    print("[test] an 8000-byte ping crosses from a container to UML unfragmented")
