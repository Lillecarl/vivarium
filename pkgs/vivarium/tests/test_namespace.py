"""The run's namespace: what `unshare` is asked for, and when."""

from pathlib import Path

from vivarium.namespace import SUBORDINATE_IDS, _subordinate, describe, unshare_argv, user_argv


def test_a_user_becomes_root_of_a_new_user_namespace() -> None:
    argv = unshare_argv("/u", root=False, subordinate=False)
    assert argv == ["/u", "--user", "--map-root-user", "--mount", "--propagation", "private", "--"]


def test_subordinate_ids_are_mapped_when_the_host_has_them() -> None:
    assert "--map-auto" in unshare_argv("/u", root=False, subordinate=True)
    assert "--map-auto" not in unshare_argv("/u", root=False, subordinate=False)


def test_root_already_takes_only_a_mount_namespace() -> None:
    # A `uid-range` build: root with its ids. A new user namespace would
    # map root alone and lose the ids a container guest needs.
    argv = unshare_argv("/u", root=True, subordinate=True)
    assert "--user" not in argv and "--map-auto" not in argv
    assert "--mount" in argv


def test_subordinate_ranges_are_summed_by_name_or_uid(tmp_path: Path) -> None:
    subuid = tmp_path / "subuid"
    subuid.write_text("other:1:99999\ncarl:100000:30000\n1000:200000:35536\n")
    assert _subordinate(subuid, "carl", 1000) == SUBORDINATE_IDS
    assert _subordinate(subuid, "nobody", 65534) == 0


def test_a_missing_file_is_no_ids(tmp_path: Path) -> None:
    assert _subordinate(tmp_path / "absent", "carl", 1000) == 0


def test_describe_names_root_and_counts_ids() -> None:
    text = "         0       1000          1\n         1     100000      65536\n"
    assert describe(text) == "namespace: root is uid 1000 outside; 65537 ids mapped"


def test_the_removal_maps_the_same_ids_without_a_mount_namespace() -> None:
    assert user_argv("/u", subordinate=True) == ["/u", "--user", "--map-root-user", "--map-auto"]
    assert unshare_argv("/u", root=False, subordinate=True)[:4] == user_argv("/u", subordinate=True)
