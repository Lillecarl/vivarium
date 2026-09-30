#!/usr/bin/env python3
"""`nested` can create a virtual machine; `plain`, beside it, has no KVM.

KVM_CREATE_VM is the claim, not the device node: it fails unless the
guest's CPU really does virtualization. KVM_GET_API_VERSION alone would
pass on a module that loaded and can do nothing.
"""

from vivarium_runner import Machines

PROBE = (
    "python3 -c 'import fcntl, os; kvm = os.open(\"/dev/kvm\", os.O_RDWR);"
    " print(fcntl.ioctl(kvm, 0xAE00), fcntl.ioctl(kvm, 0xAE01, 0) >= 0)'"
)
"""_IO(0xAE, 0x00) is KVM_GET_API_VERSION, which is 12; _IO(0xAE, 0x01)
is KVM_CREATE_VM, which returns a descriptor."""


async def test(vms: Machines) -> None:
    await vms.nested.wait_for_unit("vivarium-kvm.service", timeout=60)
    answer = (await vms.nested.succeed(PROBE)).strip()
    print(f"[test] nested: KVM api and create-vm: {answer}")
    if answer != "12 True":
        raise AssertionError(f"KVM in the nested guest answered {answer!r}")

    rc, _ = await vms.plain.execute("test -e /dev/kvm")
    if rc == 0:
        raise AssertionError("the guest without nestedVirtualization has /dev/kvm; the check proves nothing")
    print("[test] plain: no /dev/kvm")
