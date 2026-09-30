#!/usr/bin/env python3
"""A userspace filesystem inside a guest.

Two halves.  The kernel one is /dev/fuse, which allnoconfig leaves out and
the kernel package now asks for.  The userspace one is a mount that reads
back and then goes away.

Both as root and as an ordinary user.  Unprivileged mounting is most of
what FUSE is for, and it takes a different path: root opens /dev/fuse
itself, alice goes through the setuid fusermount3 wrapper.

bindfs is the filesystem under test only in the sense that it is the
smallest real one to hand.  What is being asked is whether the kernel
answers at all.
"""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    vm = vms.node

    await vm.succeed("test -c /dev/fuse")
    print(f"[test] {await vm.succeed('ls -l /dev/fuse')}")

    await vm.succeed("mkdir -p /src /mnt")
    await vm.succeed("echo hello > /src/greeting")

    # Root, straight through the device.
    await vm.succeed("bindfs /src /mnt")
    print(
        "[test] root mount: "
        + await vm.succeed("findmnt --noheadings --output FSTYPE,TARGET /mnt")
    )
    await vm.succeed("grep -q hello /mnt/greeting")
    await vm.succeed("umount /mnt")
    await vm.fail("test -e /mnt/greeting")
    print("[test] root: mounted, read, unmounted")

    # alice, through the wrapper. This is what programs.fuse is switched on
    # for: without it there is no fusermount3 under /run/wrappers, and the
    # mount fails before bindfs ever sees it.
    await vm.succeed("test -u /run/wrappers/bin/fusermount3")
    await vm.succeed("install -d -o alice -g users /home/alice/mnt")

    # --no-allow-other, so this is the private mount FUSE gives by default.
    # bindfs asks for allow_other unless told not to.
    await vm.succeed("su alice -c 'bindfs --no-allow-other /src /home/alice/mnt'")
    print(
        "[test] alice mount: "
        + await vm.succeed(
            "findmnt --noheadings --output FSTYPE,TARGET /home/alice/mnt"
        )
    )
    await vm.succeed("su alice -c 'grep -q hello /home/alice/mnt/greeting'")

    # And nobody else's.  Root included: this is one of the few things uid 0
    # does not get, and it is the reason allow_other exists.
    await vm.fail("cat /home/alice/mnt/greeting")
    print("[test] alice's private mount is alice's alone, root included")

    await vm.succeed("su alice -c 'fusermount3 -u /home/alice/mnt'")
    await vm.fail("test -e /home/alice/mnt/greeting")
    print("[test] alice: mounted, read, unmounted")

    # The other half of that: programs.fuse.userAllowOther is what lets an
    # ordinary user hand the mount to everyone, and fusermount3 refuses the
    # option outright without it.  bindfs asks for it by default, so this is
    # the same command with the flag left off.
    await vm.succeed("su alice -c 'bindfs /src /home/alice/mnt'")
    await vm.succeed("grep -q hello /home/alice/mnt/greeting")
    print("[test] with allow_other, root reads alice's mount")
    await vm.succeed("su alice -c 'fusermount3 -u /home/alice/mnt'")

