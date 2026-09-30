#!/usr/bin/env python3
"""Collect something from the guest, whatever failed before this ran."""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    await vms.one.succeed("systemctl list-units --failed > /artifacts/failed-units")
    print("[test] collected the failed units")
