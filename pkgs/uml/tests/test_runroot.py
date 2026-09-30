"""Which run roots the reaper may take."""

from uml.runroot import stale


def test_a_root_with_a_live_owner_is_kept() -> None:
    assert not stale("10 500 11 501\n", {10: 500, 11: None})
    assert not stale("10 500 11 501\n", {10: None, 11: 501})


def test_a_root_whose_owners_are_gone_is_stale() -> None:
    assert stale("10 500 11 501\n", {10: None, 11: None})


def test_a_reused_pid_is_not_the_owner() -> None:
    # Same pid, later start: another process has the number now.
    assert stale("10 500 11 501\n", {10: 900, 11: 901})


def test_an_owner_that_had_exited_when_written_counts_as_gone() -> None:
    assert stale("10 None 11 None\n", {10: None, 11: None})
