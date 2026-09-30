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

A run that no `vivarium-mcp` started has no `monitor.sock`, and a server
that is gone leaves one nobody answers. Then the monitor reads
`events.jsonl` and makes the same events from it with `channel_event`.
The run is up while `control.sock` exists; the run writes its verdict
before it removes the socket, so a run gone with no verdict exits 2.

`--quiet` leaves out `progress` and `resumed`, so an agent that runs this
in a Monitor wakes for a pause, a failure and the verdict, and not for
every phase that passed.

`--until-pause` also exits, with 4, when the run pauses. It is for a
harness that wakes an agent only when a background command ends: arm it,
and the agent comes back at the pause. After `resume`, arm it again. A
pause in the replayed events counts only when no `resumed` follows it,
and the server sends `LIVE` after the replay, so a monitor armed while
the run is paused exits at once, and one armed after a resume does not.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Final

import anyio

from .control import SOCKET as CONTROL_SOCKET, reachable
from .journal import Tail

SOCKET: Final = "monitor.sock"

TERMINAL: Final = frozenset({"finished", "exited"})
"""The events after which a run sends nothing more."""

QUIET_SKIPS: Final = frozenset({"progress", "resumed"})
"""What `--quiet` leaves out: nobody needs to wake for these."""

LIVE: Final = {"event": "live"}
"""What the server sends after the replayed events. It is not an event and
is not printed."""

PAUSED: Final = 4
"""The exit status of `--until-pause` at a pause."""

GRACE: Final = 10.0
"""Seconds a file monitor waits for `control.sock` to appear before it takes
the run as gone. The run binds it moments after `events.jsonl` starts."""


def channel_event(event: dict[str, Any], run: str) -> tuple[str, dict[str, str]] | None:
    """What of the run's event stream is worth interrupting Claude for.

    A pause, a failed phase and the verdict. Everything else stays in
    `events.jsonl`, where `events` can ask for it: a channel event is
    context in the conversation, and a journal line each is a flood.
    Meta keys are identifiers only; Claude Code drops any other key.
    """
    kind = event.get("kind")
    data = event.get("data") or {}
    meta = {"run": run}
    # Facts only. Claude Code frames channel content as untrusted and
    # tells the model not to act on imperative language in it, so what
    # to do next belongs in the server's `instructions`, which it trusts.
    if kind == "note" and "resumed" in data:
        meta |= {"event": "resumed", "reason": str(data["resumed"])}
        return f"resumed, paused {data['resumed']}", meta
    if kind == "note" and "reason" in data:
        meta |= {"event": "paused", "reason": str(data["reason"])}
        return f"paused {data['reason']}; the guests are up until the run is resumed or stopped", meta
    # Progress, so a run of many minutes is not silent until it ends:
    # measured, a nixkube run showed the user nothing for its first
    # quarter of an hour. One event per phase boundary, not per line.
    if kind == "phase_started":
        phase = str(event.get("phase", ""))
        meta |= {"event": "progress", "phase": phase, "state": "started"}
        return f"phase {phase} started", meta
    if kind == "phase_finished" and data.get("state") == "passed":
        phase = str(event.get("phase", ""))
        meta |= {"event": "progress", "phase": phase, "state": "passed"}
        return f"phase {phase} passed in {event.get('seconds', 0):.0f}s", meta
    if kind == "phase_finished" and data.get("state") == "failed":
        phase = str(event.get("phase", ""))
        meta |= {"event": "failed", "phase": phase}
        return f"phase {phase} failed: {data.get('error', '')}".strip(), meta
    if kind == "run_finished":
        passed = bool(data.get("passed"))
        meta |= {"event": "finished", "passed": "true" if passed else "false"}
        states = data.get("states") or {}
        summary = ", ".join(f"{name} {state}" for name, state in states.items())
        return f"run {'passed' if passed else 'failed'}: {summary}", meta
    return None


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


def paused_now(events: list[dict[str, Any]]) -> bool:
    """Whether the run is paused after these events: a pause with no
    `resumed` or verdict after it."""
    for event in reversed(events):
        if event.get("event") == "paused":
            return True
        if event.get("event") in {"resumed", *TERMINAL}:
            return False
    return False


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


class Watch:
    """Prints each event and says when to stop: the verdict, or with
    `until_pause` an open pause once the replay is over."""

    def __init__(self, *, as_json: bool = False, quiet: bool = False, until_pause: bool = False) -> None:
        self.as_json = as_json
        self.quiet = quiet
        self.until_pause = until_pause
        self.seen: list[dict[str, Any]] = []
        self.live = False

    def take(self, event: dict[str, Any]) -> int | None:
        if event == LIVE:
            self.live = True
        else:
            self.seen.append(event)
            if shown(event, quiet=self.quiet):
                print(line(event, as_json=self.as_json), flush=True)
            code = status(event)
            if code is not None:
                return code
        if self.until_pause and self.live and paused_now(self.seen):
            return PAUSED
        return None


async def follow(socket: Path, **options: bool) -> int:
    """Print the run's events until its verdict, or with `until_pause`
    until it is paused; answer the exit status."""
    watch = Watch(**options)
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
                code = watch.take(json.loads(raw))
                if code is not None:
                    return code


async def follow_file(out: Path, *, grace: float = GRACE, **options: bool) -> int:
    """`follow`, from `<out>/events.jsonl`, for a run with no monitor socket."""
    watch = Watch(**options)
    tail = Tail(out / "events.jsonl")
    control = anyio.Path(out / CONTROL_SOCKET)
    started = anyio.current_time()
    was_up = False
    while True:
        # Before the read: a socket gone now means the verdict, if any, is
        # already in the file.
        up = await control.exists()
        was_up = was_up or up
        for raw in await tail.read():
            try:
                pushed = channel_event(json.loads(raw), out.name)
            except json.JSONDecodeError:
                continue
            if pushed is not None:
                code = watch.take({**pushed[1], "text": pushed[0]})
                if code is not None:
                    return code
        if not watch.live:
            code = watch.take(LIVE)
            if code is not None:
                return code
        if not up and (was_up or anyio.current_time() - started > grace):
            return 2
        await anyio.sleep(0.25)
