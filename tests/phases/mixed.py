#!/usr/bin/env python3
"""A UML guest and a QEMU guest on one segment, each reaching the other.

`systemd-detect-virt` names the machine each guest is: `uml` and `kvm`.
That is the proof the run holds both kinds, and not two of one. Not
`uname -m`, which a UML guest answers `x86_64`, like the host.
"""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    kinds = {name: (await vm.execute("systemd-detect-virt"))[1].strip() for name, vm in vms.items()}
    print(f"[test] machines: {kinds}")
    if kinds != {"small": "uml", "fast": "kvm"}:
        raise AssertionError(f"expected one UML and one QEMU guest, got {kinds}")

    await vms.small.succeed("ping -c 3 -W 5 10.55.0.2")
    await vms.fast.succeed("ping -c 3 -W 5 10.55.0.1")
    print("[test] each reaches the other across the segment")

    for name, vm in vms.items():
        print(f"[test] {name}: host pays {vm.host_memory_kib() // 1024} MiB")
