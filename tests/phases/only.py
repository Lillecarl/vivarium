#!/usr/bin/env python3
"""Runs only when asked for by name, and records that it did."""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    (vms.artifacts / "only-ran").write_text("yes\n")
    print("[test] the phase asked for by name ran")
