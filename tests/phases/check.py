#!/usr/bin/env python3
"""Needs `cluster`. Must never run when `cluster` failed.

Running it anyway is what nixpkgs' driver avoids by ending the whole run,
and what pytest would do -- producing a second failure about a cluster
that was never built, which tells a reader nothing.
"""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    raise AssertionError(
        "check ran although cluster failed; the skip rule is broken"
    )
