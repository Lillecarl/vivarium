#!/usr/bin/env python3
"""Wait for every guest to finish booting, and say what did not.

The first phase of nearly every run, and the one whose failure is worth
reading. `systemctl is-system-running --wait` alone answers `degraded`
and stops there, which tells a reader that something is wrong and not
what -- so this asks for the failed units and puts them in the error.
"""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    for name, vm in vms.items():
        state = (await vm.execute("systemctl is-system-running --wait", timeout=180))[1]
        state = state.strip()
        if state in ("running", "starting"):
            print(f"[boot] {name} is {state}")
            continue

        failed = await vm.succeed(
            "systemctl list-units --state=failed --no-legend --plain || true"
        )
        raise AssertionError(
            f"{name} came up {state!r}, with these units failed:\n{failed or '  (none)'}"
        )
