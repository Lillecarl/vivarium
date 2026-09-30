#!/usr/bin/env python3
"""Needs only `boot`. Must run even though `cluster` failed.

This is the phase nixpkgs' driver never reaches: one failure ends the
run there, so a real bug here stays invisible until the unrelated
failure upstream is fixed.
"""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    await vms.one.succeed("true")
    print("[test] an unrelated phase still ran")
