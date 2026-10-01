"""nixos-test's script API over a guest, without booting one."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from vivarium_runner import nixos_test


class FakeGuest:
    """Answers `execute` from a table: the last command sent decides."""

    def __init__(self, name: str, answers: dict[str, tuple[int, str]] | None = None) -> None:
        self.name = name
        self.answers = answers or {}
        self.sent: list[str] = []

    async def execute(self, command: str, timeout: float | None = None) -> tuple[int, str]:
        self.sent.append(command)
        for needle, answer in self.answers.items():
            if command.endswith(needle):
                return answer
        return 0, ""

    def alive(self) -> bool:
        return True


def run(guests: dict[str, Any], script: str) -> Any:
    return anyio.run(nixos_test.run, guests, script)


def test_names_follow_nixos_test() -> None:
    names = nixos_test.symbols({"web-1": FakeGuest("web-1")})  # type: ignore[dict-item]
    assert "web_1" in names
    # One guest is also `machine`, as nixos-test has it.
    assert names["machine"] is names["web_1"]
    two = nixos_test.symbols({"a": FakeGuest("a"), "b": FakeGuest("b")})  # type: ignore[dict-item]
    assert "machine" not in two


def test_a_command_runs_in_root_login_and_strict_mode() -> None:
    guest = FakeGuest("machine", {"hello": (0, "hi\n")})
    run({"machine": guest}, 'assert machine.succeed("hello") == "hi\\n"')
    sent = guest.sent[0]
    assert "source /etc/profile" in sent and "set -euo pipefail; hello" in sent


def test_succeed_raises_with_the_command() -> None:
    guest = FakeGuest("machine", {"false": (1, "nope")})
    with pytest.raises(Exception, match="command `false` failed \\(exit code 1\\)"):
        run({"machine": guest}, 'machine.succeed("false")')


def test_fail_raises_when_the_command_succeeds() -> None:
    with pytest.raises(Exception, match="unexpectedly succeeded"):
        run({"machine": FakeGuest("machine")}, 'machine.fail("true")')


def test_wait_for_unit_reads_active_state() -> None:
    guest = FakeGuest("machine", {'show "sshd"': (0, "ActiveState=active\n")})
    run({"machine": guest}, 'machine.wait_for_unit("sshd")')


def test_a_failed_unit_ends_the_wait() -> None:
    guest = FakeGuest("machine", {'show "sshd"': (0, "ActiveState=failed\n")})
    with pytest.raises(Exception, match='reached state "failed"'):
        run({"machine": guest}, 'machine.wait_for_unit("sshd")')


def test_what_has_no_equivalent_says_so() -> None:
    machine = nixos_test.Machine(FakeGuest("machine"))  # type: ignore[arg-type]
    with pytest.raises(nixos_test.Unsupported, match="send_monitor_command"):
        machine.send_monitor_command("info status")
    assert not hasattr(machine, "send_monitor_command")
    assert hasattr(machine, "send_key")
