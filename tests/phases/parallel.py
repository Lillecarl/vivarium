#!/usr/bin/env python3
"""One script, three phases: `left` on `a`, `right` on `b`, `both` on both.

Each guest logs a marker per step and sleeps, so `left` and `right` take
long enough to overlap when they run at once. The check reads the
overlap, and the phase each marker was filed under, from events.jsonl.
"""

from vivarium_runner import Machines

EXPECTED = {"left": ["a"], "right": ["b"], "both": ["a", "b"]}


async def test(vms: Machines) -> None:
    phase = vms.phase or ""
    if sorted(vms) != EXPECTED[phase]:
        raise AssertionError(f"{phase} was given {sorted(vms)}, declared {EXPECTED[phase]}")
    for step in range(3):
        print(f"{phase} step {step}")
        for vm in vms.values():
            await vm.succeed(f"echo {phase}-{step} | systemd-cat --identifier=parallel && sleep 1")
