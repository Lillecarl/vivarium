#!/usr/bin/env python3
"""The guest is up and answers. Everything after this depends on it."""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    name = await vms.one.succeed("hostname")
    assert name.strip() == "one", f"the guest calls itself {name!r}"
    print("[test] the guest answers")
