"""Where the events go. Each sink writes; none of them decide.

The decisions -- what a reader at this level should see, what a line
looks like, what a JUnit document says -- are pure functions in
`events.py`. What is left here is files and a terminal, which is why
there is so little of it.

**A sink must never fail a run.** A full disk while writing a log is not
a reason to lose the guests, so `Broadcast` reports and carries on. The
run's verdict comes from the phases, never from whether its evidence was
written.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Protocol

from .events import Event, Kind, Level, junit, render, wanted

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


class Sink(Protocol):
    def emit(self, event: Event) -> None: ...
    def close(self) -> None: ...


class Terminal:
    """What a person watching the run sees.

    The default level leaves a guest's console out, which is the change
    a reader notices most: a two-guest run printed several thousand
    lines of kernel and systemd, and the six lines that said what the
    test did were somewhere in it.

    Nothing is lost by that -- the console is always written to its own
    file, and `Session` replays the tail of it when a phase fails. So
    the quiet case is quiet and the interesting case is louder than it
    used to be.
    """

    def __init__(self, level: Level = Level.INFO, stream=None) -> None:
        self.level = level
        self._stream = stream if stream is not None else sys.stdout

    def emit(self, event: Event) -> None:
        if not wanted(event, self.level):
            return
        print(render(event), file=self._stream, flush=True)

    def close(self) -> None:
        pass


class Log:
    """The whole run, readable, in one file.

    Unfiltered by design. The terminal is a view and this is the record,
    and a record holding only what somebody thought was interesting at
    the time is not a record. So `--quiet` changes what you watch and
    never what you can go back to.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", buffering=1, encoding="utf-8", errors="replace")

    def emit(self, event: Event) -> None:
        self._file.write(render(event) + "\n")

    def close(self) -> None:
        self._file.close()


class JsonLines:
    """Every event, as it happens, one JSON object per line.

    Appended rather than written at the end, so a run that is killed
    still has everything up to the moment it died -- which nixpkgs'
    JUnit logger cannot do, because it builds its document in memory and
    writes it on close.

    This is the file a machine reads: an MCP server, a dashboard, or a
    person with `jq`.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", buffering=1, encoding="utf-8")

    def emit(self, event: Event) -> None:
        self._file.write(event.as_json() + "\n")

    def close(self) -> None:
        self._file.close()


class ConsoleFiles:
    """Each guest's console, in a file of its own.

    Per machine rather than one file with prefixes: reading what one
    guest did is then `cat`, not `grep`, and a guest that printed a
    megabyte does not bury the others.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, object] = {}

    def emit(self, event: Event) -> None:
        if event.kind is not Kind.CONSOLE or not event.machine:
            return
        handle = self._files.get(event.machine)
        if handle is None:
            handle = (self._dir / f"{event.machine}.log").open(
                "w", buffering=1, encoding="utf-8", errors="replace"
            )
            self._files[event.machine] = handle
        handle.write(event.text + "\n")  # ty: ignore[unresolved-attribute]

    def close(self) -> None:
        for handle in self._files.values():
            handle.close()  # ty: ignore[unresolved-attribute]
        self._files.clear()


class Junit:
    """A JUnit document, for the CI tools that read nothing else.

    Holds the finished phases and writes on close, because the format is
    a single document. It is built from the same events as everything
    else, so it cannot disagree with `phases.json` -- in nixpkgs the
    JUnit logger is fed separately and can.
    """

    def __init__(self, path: Path, name: str = "uml") -> None:
        self._path = path
        self._name = name
        self._events: list[Event] = []

    def emit(self, event: Event) -> None:
        if event.kind in (Kind.PHASE_FINISHED, Kind.CASE):
            self._events.append(event)

    def close(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(junit(self._events, self._name))


class Broadcast:
    """One event to every sink, and no sink may take the run down."""

    def __init__(self, sinks: Iterable[Sink]) -> None:
        self._sinks = list(sinks)
        self._broken: set[int] = set()

    def emit(self, event: Event) -> None:
        for index, sink in enumerate(self._sinks):
            if index in self._broken:
                continue
            try:
                sink.emit(event)
            except Exception as error:  # noqa: BLE001 -- see the module docstring
                self._broken.add(index)
                print(
                    f"[uml] {type(sink).__name__} stopped taking events"
                    f" ({error}); the run carries on",
                    file=sys.stderr,
                    flush=True,
                )

    def close(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception as error:  # noqa: BLE001
                print(
                    f"[uml] {type(sink).__name__} did not close cleanly: {error}",
                    file=sys.stderr,
                    flush=True,
                )
