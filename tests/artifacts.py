#!/usr/bin/env python3
"""What a guest writes to /artifacts, and who is still running at the end.

`/artifacts` is a host directory, not a copy made afterwards: a guest that
wedges or is killed cannot be asked for anything, and a file it wrote a
minute earlier is already on the host.  So this writes in the guest and
reads on the host, with nothing in between.

Two guests, because each one must get its own directory. A user other
than root writes too: a test suite in a guest seldom runs as root, and
the host side must accept a file from an id it may not map.
"""

from vivarium_runner import Machines

BLOB = 1 << 20
"""A megabyte: hostfs and virtiofs are different code paths for a write
that does not fit in one page."""

AGENT = "vivarium-agent"
"""In the agent's own command line -- see modules/guest.nix."""


async def test(vms: Machines) -> None:
    for name, vm in vms.items():
        await vm.succeed("mountpoint -q /artifacts")
        await vm.succeed(f"echo {name} > /artifacts/who")
        await vm.succeed(
            f"dd if=/dev/urandom of=/artifacts/blob "
            f"bs=1024 count={BLOB // 1024} 2>/dev/null"
        )
        await vm.succeed("runuser -u tester -- sh -c 'echo tester > /artifacts/by-user'")
        await vm.succeed("sync")
        print(f"[test] {name} wrote to its /artifacts, as root and as tester")

    # No command runs to fetch any of this.
    for name in vms:
        here = vms.artifacts / name
        assert (here / "who").read_text().strip() == name, (
            f"{here}/who does not say {name}, so the guests share a directory"
        )
        size = (here / "blob").stat().st_size
        assert size == BLOB, f"{here}/blob is {size} bytes, not {BLOB}"
        by_user = (here / "by-user").read_text().strip()
        assert by_user == "tester", f"{here}/by-user says {by_user!r}, not tester"
        print(f"[test] the host reads {here}/who and a {size}-byte blob")

    (vms.artifacts / "one" / "from-the-host").write_text("hello\n")
    await vms.one.succeed("grep -q hello /artifacts/from-the-host")
    print("[test] and the guest reads what the host put there")

    procs = await vms.one.processes()
    running = await vms.one.count_processes(AGENT)
    assert running >= 1, (
        f"the agent answering this call is not in its own list of {len(procs)}"
    )
    print(f"[test] the guest lists {len(procs)} processes, and finds its own agent")

    assert await vms.one.count_processes("no-such-program") == 0
    print("[test] and counts none of a program that is not there")

