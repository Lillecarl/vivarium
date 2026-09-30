#!/usr/bin/env python3
"""Log a line from a unit, see it reach the host, then kill the guest."""

import asyncio
import json
from pathlib import Path

from vivarium_runner import Machines

MARK = "streamed-before-the-crash"


def messages(stream: Path) -> set[str]:
    """Every complete entry's MESSAGE. The last line may be half written."""
    if not stream.exists():
        return set()
    found: set[str] = set()
    for line in stream.read_text(errors="replace").splitlines():
        try:
            found.add(str(json.loads(line).get("MESSAGE")))
        except (json.JSONDecodeError, AttributeError):
            continue
    return found


async def test(vms: Machines) -> None:
    vm = vms.one
    stream = vms.artifacts / "one" / "journal.jsonl"

    await vm.succeed(
        f"systemd-run --unit=probe --wait /run/current-system/sw/bin/echo {MARK}"
    )

    # On the host while the guest still runs: the stream, not a copy
    # taken at the end. The exact MESSAGE, because systemd's "Started
    # ... echo streamed-before-the-crash" carries the mark too, and
    # arrives first.
    for _ in range(40):
        if MARK in messages(stream):
            break
        await asyncio.sleep(0.25)
    else:
        raise AssertionError(f"{MARK} never reached {stream} while the guest ran")
    print("[test] the host had the line while the guest was up")

    await vm.crash()
    # The negative control. Without it this phase passes whether or not
    # the kill worked, and the check proves streaming, not survival.
    if vm.alive():
        raise AssertionError("the guest's process survived crash()")
    print("[test] the guest is dead: SIGKILL, no shutdown")
