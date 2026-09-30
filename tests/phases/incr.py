#!/usr/bin/env python3
"""Does a guest's incremental write reach the host incrementally?

The streaming design rests on this. Two things could stop it and they
need different fixes, so the probe tells them apart: the guest may not
have flushed, or the host may not see a change it has already made.

Every reading compares what the guest says the file holds against what
the host reads at the same moment.
"""

import asyncio
from pathlib import Path

from vivarium_runner import Machine, Machines


async def sizes(vm: Machine, host_path: Path) -> tuple[int, int]:
    """(what the guest sees, what the host sees), as close to together
    as two different machines allow."""
    guest = await vm.succeed("stat -c %s /artifacts/drip 2>/dev/null || echo 0")
    try:
        host = host_path.stat().st_size
    except OSError:
        host = 0
    return int(guest.strip()), host


async def test(vms: Machines) -> None:
    vm = vms.one
    here = vms.artifacts / "one" / "drip"

    # In a file, not inline: systemd-run expands `$i` itself, so an
    # inline loop runs with every variable empty and writes nothing.
    # A bash `echo` is one write(2) per line -- no userspace buffer
    # stands between it and the kernel.
    await vm.succeed(
        "printf '%s\\n' 'for i in 1 2 3 4 5 6; do echo line-$i; sleep 1; done' "
        "> /tmp/drip.sh"
    )
    await vm.succeed(
        "systemd-run --unit=drip --no-block --service-type=exec "
        "--property=StandardOutput=append:/artifacts/drip "
        "--setenv=PATH=/run/current-system/sw/bin "
        "/run/current-system/sw/bin/bash /tmp/drip.sh"
    )

    readings = []
    for _ in range(10):
        await asyncio.sleep(0.6)
        readings.append(await sizes(vm, here))
    print(f"[test] (guest, host) bytes: {readings}")

    state = await vm.succeed("systemctl is-active drip || true")
    print(f"[test] the writer unit ended as: {state.strip()}")
    print(await vm.succeed("journalctl -u drip --no-pager -o cat | tail -5"))

    final = await sizes(vm, here)
    print(f"[test] after it finished: guest {final[0]}, host {final[1]}")

    # Does asking the guest to sync change what the host sees?
    await vm.succeed("sync")
    after_sync = await sizes(vm, here)
    print(f"[test] after sync: guest {after_sync[0]}, host {after_sync[1]}")
