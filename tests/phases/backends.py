"""A UML, a QEMU and a container guest on one segment, each reaching the
others over IP by hostname.

`systemd-detect-virt` proves the run holds one of each kind. The names
come from the /etc/hosts every guest builds from its peers.
"""

from uml_runner import Machines

KINDS = {"u": "uml", "q": "kvm", "c": "container-other"}


async def test(vms: Machines) -> None:
    kinds = {name: (await vm.execute("systemd-detect-virt"))[1].strip() for name, vm in vms.items()}
    print(f"[test] machines: {kinds}")
    if kinds != KINDS:
        raise AssertionError(f"expected one guest of each kind {KINDS}, got {kinds}")

    for name, vm in vms.items():
        for other in KINDS:
            if other != name:
                await vm.succeed(f"ping -c 2 -W 5 {other}")
    print("[test] every guest reaches every other by name")
