"""`vivarium monitor`: a run's events, one line each, for a process that watches stdout.

`vivarium-mcp` serves `<out>/monitor.sock` for each run it starts, and writes
there the same events it pushes over its Claude channel: progress, a
pause, a failed phase, the verdict. Channels need a development flag;
a command whose every stdout line is an event needs nothing, and Claude
Code's Monitor tool shows it as a running shell. So the socket carries
the channel to whoever is watching, flag or not.

A client connecting late gets the run's earlier events first, then the
live ones. The server closes the stream after the verdict, and the exit
status says what the verdict was.

`--quiet` leaves out `progress`, so an agent that runs this in a Monitor
wakes for a pause, a failure and the verdict, and not for every phase
that passed.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Final

import anyio

from .control import reachable

SOCKET: Final = "monitor.sock"

TERMINAL: Final = frozenset({"finished", "exited"})
"""The events after which a run sends nothing more."""

QUIET_SKIPS: Final = frozenset({"progress"})
"""What `--quiet` leaves out: a phase starting or passing needs no one."""


def shown(event: dict[str, Any], *, quiet: bool) -> bool:
    return not (quiet and event.get("event") in QUIET_SKIPS)


def status(event: dict[str, Any]) -> int | None:
    """The exit status a terminal event means, or `None` for any other.

    0 passed, 1 failed, 2 exited with no verdict: a crash or an
    evaluation error.
    """
    if event.get("event") == "finished":
        return 0 if event.get("passed") == "true" else 1
    if event.get("event") == "exited":
        return 2
    return None


def line(event: dict[str, Any], *, as_json: bool = False) -> str:
    """One event as one line. A watcher takes each line as one event, so a
    message of several lines, such as a traceback's tail, is joined."""
    if as_json:
        return json.dumps(event, separators=(",", ":"))
    head = " ".join(str(event[key]) for key in ("run", "event", "phase") if event.get(key))
    text = " | ".join(part.strip() for part in str(event.get("text", "")).splitlines() if part.strip())
    return f"{head}: {text}" if text else head


def locate(target: str) -> Path:
    """The socket of a run, named by its `--out` directory or by its id.

    `vivarium-mcp` makes each run's directory under the temporary directory and
    names the run after it, so an id alone is enough.
    """
    path = Path(target)
    if not path.is_dir():
        path = Path(tempfile.gettempdir()) / target
    return path / SOCKET


async def follow(socket: Path, *, as_json: bool = False, quiet: bool = False) -> int:
    """Print the run's events until its verdict; answer the exit status."""
    with reachable(socket) as name:
        stream = await anyio.connect_unix(name)
    async with stream:
        buffer = b""
        while True:
            try:
                buffer += await stream.receive()
            except (anyio.EndOfStream, anyio.BrokenResourceError):
                return 3
            *lines, buffer = buffer.split(b"\n")
            for raw in lines:
                event = json.loads(raw)
                if shown(event, quiet=quiet):
                    print(line(event, as_json=as_json), flush=True)
                code = status(event)
                if code is not None:
                    return code
