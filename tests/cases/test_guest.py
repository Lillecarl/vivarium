"""A pytest phase against a booted guest. One test fails on purpose."""

import asyncio
import json

import pytest
from vivarium_runner import Machine, Machines


async def test_hostname(one: Machine) -> None:
    assert (await one.succeed("hostname")).strip() == "one"


async def test_an_async_fixture(marker: str) -> None:
    assert marker == "marked"


async def test_logs_and_returns(one: Machine) -> None:
    """Returns before journald has handed the line on. The phase's
    settle is what keeps it from being lost to the teardown."""
    await one.succeed(
        "systemd-run --unit=left --wait /run/current-system/sw/bin/echo logged-and-left"
    )


async def test_the_journal_names_the_test(one: Machine, vms: Machines) -> None:
    """Waits for its own line, as a test waiting on a log line does, so
    the line arrives while the test is still the one running."""
    await one.succeed(
        "systemd-run --unit=from-a-test --wait /run/current-system/sw/bin/echo from-a-test"
    )
    stream = vms.artifacts / "one" / "journal.jsonl"
    for _ in range(40):
        for line in stream.read_text(errors="replace").splitlines():
            try:
                if json.loads(line).get("MESSAGE") == "from-a-test":
                    return
            except json.JSONDecodeError:
                continue
        await asyncio.sleep(0.1)
    raise AssertionError("the line never reached the host")


@pytest.mark.parametrize("n", [1, 2])
async def test_parametrized(one: Machine, n: int) -> None:
    assert (await one.succeed(f"echo {n}")).strip() == str(n)


def test_skipped() -> None:
    pytest.skip("on purpose")


async def test_fails_on_purpose(one: Machine) -> None:
    assert (await one.succeed("echo 2")).strip() == "3"
