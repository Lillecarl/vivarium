"""A run's own directory, and the cleaner that removes it however the run
ends.

Everything a run writes outside `--out` goes under one run root: `uml
run` points `TMPDIR` at it, and the runner's short-path fallbacks for
sockets use it too (`VIVARIUM_RUN_ROOT`). A normal end removes it. So does a
SIGKILL of the runner: before it enters its namespace, the runner forks
a cleaner in a session of its own, which waits on the runner's pidfd and
then removes the root. A killed process group does not take the cleaner
with it. A cleaner killed too is the last gap, and the next run's reaper
closes it: it removes every run root whose runner and cleaner are both
gone.

Measured before this: a SIGKILLed runner left one directory per guest
and one for crun, and 158 had built up in /tmp. Its processes and its
store views were already gone, the processes by parent-death signals and
the views with the mount namespace.
"""

from __future__ import annotations

import os
import select
import shutil
import tempfile
from pathlib import Path

ENV = "VIVARIUM_RUN_ROOT"
KEEP = "VIVARIUM_KEEP"
"""Set, nothing is removed: not by the runner, not by the cleaner."""
PREFIX = "vivarium-run-"
OWNERS = "owners"
"""`<runner pid> <start> <cleaner pid> <start>`: who may still use a root.
A start time as well as a pid, so a reused pid is not taken for the run."""

REMOVE = "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)"
"""Run as root of a user namespace that maps the run's ids: a container
guest's files belong to subordinate ids, which the caller alone may not
delete."""


def base() -> Path:
    """/tmp when writable, for short socket paths; else TMPDIR."""
    tmp = Path("/tmp")
    if tmp.is_dir() and os.access(tmp, os.W_OK):
        return tmp
    return Path(tempfile.gettempdir())


def start_time(pid: int) -> int | None:
    """The process's start in clock ticks since boot, or None if it is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Field 22; the name before it is in parentheses and may hold spaces.
    return int(stat.rsplit(")", 1)[1].split()[19])


def owners(runner: int, cleaner: int) -> str:
    return f"{runner} {start_time(runner)} {cleaner} {start_time(cleaner)}\n"


def stale(text: str, started: dict[int, int | None]) -> bool:
    """Whether no owner named in *text* is still the same process.

    *started* maps a pid to its start time now, None when it is gone.
    """
    fields = text.split()
    pairs = zip(fields[0::2], fields[1::2], strict=True)
    return not any(
        start != "None" and started.get(int(pid)) == int(start) for pid, start in pairs
    )


def reap(where: Path, remove_argv: list[str]) -> list[Path]:
    """Remove run roots whose runner and cleaner are both gone."""
    removed = []
    for root in where.glob(f"{PREFIX}*"):
        try:
            text = (root / OWNERS).read_text()
        except OSError:
            # No owners yet: a run between creating its root and writing
            # the file. Never someone else's to take.
            continue
        pids = [int(pid) for pid in text.split()[0::2]]
        if stale(text, {pid: start_time(pid) for pid in pids}):
            os.spawnv(os.P_WAIT, remove_argv[0], [*remove_argv, str(root)])
            removed.append(root)
    return removed


def remove_own() -> None:
    """The runner's own removal at its end. Inside the namespace, as root,
    so every file a guest left is ours to delete."""
    root = os.environ.get(ENV)
    if root and not os.environ.get(KEEP):
        shutil.rmtree(root, ignore_errors=True)


def spawn_cleaner(root: Path, remove_argv: list[str]) -> int:
    """Fork the cleaner; return its pid. The caller then execs the runner,
    which keeps the caller's pid: that is what the cleaner watches."""
    runner = os.getpid()
    child = os.fork()
    if child:
        return child
    try:
        os.setsid()
        watch = os.pidfd_open(runner)
        # Nothing the runner holds stays open here: a pipe from its
        # stdout would give its reader no EOF until the cleaner exits.
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.closerange(3, watch)
        os.closerange(watch + 1, os.sysconf("SC_OPEN_MAX"))
        # The pidfd proves the runner was alive when it was taken only if
        # the parent is still the runner afterwards.
        if os.getppid() == runner:
            select.select([watch], [], [])
        if not os.environ.get(KEEP) and root.exists():
            os.execv(remove_argv[0], [*remove_argv, str(root)])
    finally:
        os._exit(0)
