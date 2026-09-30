"""The run's own user and mount namespace, entered before anything else.

Every run depends on user namespaces: passt makes one for itself, and a
guest's store view is bind mounts, which need root of a mount namespace.
So `uml run` checks for them first and fails in seconds, naming the fix,
rather than after the guests boot.

The runner re-executes itself as root of a new user namespace with a
private mount namespace. Root there is the caller outside; the caller's
subordinate ids are mapped too when the host has them, because container
guests need them (``uml_runner.container.owns_ids``). A `uid-range` build
is root with its ids already, so it takes a mount namespace only.

util-linux's `unshare` does the mapping: ``--map-auto`` calls the host's
setuid newuidmap, which must run outside the new namespace.
"""

from __future__ import annotations

import os
import pwd
import resource
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import runroot

ENTERED = "UML_NAMESPACE"
"""Set in the re-executed runner, so it does not enter a second time."""

SUBORDINATE_IDS = 65536
"""What a container guest needs, and what `--map-auto` maps."""

FIX = (
    "allow unprivileged user namespaces: user.max_user_namespaces > 0,"
    " and on Ubuntu kernel.apparmor_restrict_unprivileged_userns=0"
)


class NamespaceError(RuntimeError):
    pass


def _subordinate(path: Path, user: str, uid: int) -> int:
    """How many ids /etc/subuid or /etc/subgid gives this user."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return 0
    total = 0
    for line in lines:
        fields = line.split(":")
        if len(fields) == 3 and fields[0] in (user, str(uid)):
            total += int(fields[2])
    return total


def has_subordinate_ids(user: str, uid: int, root: Path = Path("/")) -> bool:
    """Whether `--map-auto` can map a guest's worth of ids."""
    return all(
        _subordinate(root / "etc" / name, user, uid) >= SUBORDINATE_IDS
        for name in ("subuid", "subgid")
    ) and all(shutil.which(tool) for tool in ("newuidmap", "newgidmap"))


def user_argv(unshare: str, *, subordinate: bool) -> list[str]:
    """`unshare` as root of a new user namespace, with the caller's
    subordinate ids when it has them."""
    auto = ["--map-auto"] if subordinate else []
    return [unshare, "--user", "--map-root-user", *auto]


def unshare_argv(unshare: str, *, root: bool, subordinate: bool) -> list[str]:
    """The `unshare` command line that puts the runner in its namespace."""
    mount = ["--mount", "--propagation", "private", "--"]
    if root:
        return [unshare, *mount]
    return [*user_argv(unshare, subordinate=subordinate), *mount]


def describe(uid_map: str) -> str:
    """One line for the log: who root is outside, and how many ids."""
    rows = [line.split() for line in uid_map.splitlines() if line.strip()]
    total = sum(int(row[2]) for row in rows)
    outside = next((row[1] for row in rows if row[0] == "0"), None)
    root = f"root is uid {outside} outside" if outside is not None else "no root mapped"
    return f"namespace: {root}; {total} ids mapped"


def userns_works() -> str | None:
    """None when this process can make a user namespace; else why not.

    A child makes it, so the refusal's errno reaches the message; a
    preexec_fn that raises says only "Exception occurred in preexec_fn".
    """
    child = subprocess.run(
        [sys.executable, "-c", "import os; os.unshare(os.CLONE_NEWUSER)"],
        capture_output=True,
        text=True,
    )
    if child.returncode == 0:
        return None
    lines = child.stderr.strip().splitlines()
    return lines[-1] if lines else f"exit {child.returncode}"


def raise_fd_limit() -> None:
    """Soft to hard. UML fails at 1024 (EMFILE in start_userspace), which
    many shells and every `systemd-run --user` unit give."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < hard:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))


def enter(unshare: Path | None) -> None:
    """Re-execute this process in the run's namespace; return inside it.

    Call it before any thread starts: the kernel refuses a user namespace
    to a threaded process.
    """
    if os.environ.get(ENTERED):
        return
    if unshare is None:
        raise NamespaceError("the spec names no `unshare`; an older user-mode-nixos wrote it")
    why = userns_works()
    if why is not None:
        raise NamespaceError(f"this run needs a user namespace, and making one failed ({why}); {FIX}")
    raise_fd_limit()
    uid = os.getuid()
    subordinate = uid != 0 and has_subordinate_ids(pwd.getpwuid(uid).pw_name, uid)
    shape = {"root": uid == 0, "subordinate": subordinate}

    # The same mapping as the run, so the removal may delete what a
    # container guest's subordinate ids own.
    remove = [sys.executable, "-c", runroot.REMOVE]
    if uid != 0:
        remove = [*user_argv(str(unshare), subordinate=subordinate), "--", *remove]
    where = runroot.base()
    runroot.reap(where, remove)
    root = Path(tempfile.mkdtemp(prefix=runroot.PREFIX, dir=where))
    cleaner = runroot.spawn_cleaner(root, remove)
    (root / runroot.OWNERS).write_text(runroot.owners(os.getpid(), cleaner))

    os.environ[ENTERED] = "1"
    os.environ[runroot.ENV] = str(root)
    os.environ["TMPDIR"] = str(root)
    argv = unshare_argv(str(unshare), **shape)
    os.execv(argv[0], [*argv, sys.executable, *sys.orig_argv[1:]])
