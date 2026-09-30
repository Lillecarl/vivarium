"""Everything a run has to say, as data.

One stream, several sinks. A sink renders; it does not decide. That is
the difference from nixpkgs' driver, where `TerminalLogger`,
`JunitXMLLogger` and `XMLLogger` each carry their own `_log_level` and
each re-implement the same three comparisons -- so a fourth consumer
means a fourth copy, and a filter fixed in one is still wrong in the
others.

Here the filter is one pure function over an event, the renderers are
pure functions from an event to text, and a sink is the small piece that
writes. An MCP server becomes another sink over the same events rather
than something that greps a log.

**An event is not a log line.** It carries the machine, the phase and the
numbers as fields, so `events.jsonl` answers "how long did every command
on `cp` take" without anybody parsing a prefix out of a string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


class Level(IntEnum):
    """How much a reader wants to see.

    Numbers, because the only thing ever asked of them is `>=`.
    """

    CONSOLE = 10
    """A guest's own output. Thousands of lines, nearly all of it a
    kernel and systemd saying ordinary things."""

    DETAIL = 20
    """Each command sent to a guest, and each wait."""

    INFO = 30
    """What a phase did, and what the run decided. The default."""

    ERROR = 40
    """A failure, and the context that explains it."""


class Kind(StrEnum):
    """What happened. Crosses into JSON, so the values are words."""

    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    KNOB = "knob"
    BOOT = "boot"
    CONSOLE = "console"
    RPC = "rpc"
    PHASE_STARTED = "phase_started"
    PHASE_FINISHED = "phase_finished"

    OUTPUT = "output"
    """What a phase printed. Rendered exactly as written, because a
    script prefixes its own lines -- `[test] ...` by convention here --
    and `[uml] [test] ...` helps nobody. `grep '[test]'` keeps working,
    which AGENTS.md has told people to do since the beginning."""

    CASE = "case"
    """One pytest test's outcome, inside a pytest phase. `text` is the
    node id; `data` carries `outcome`, `when`, and `error` or `reason`."""

    JOURNAL = "journal"
    """One journal entry from a guest, streamed while it runs. `data`
    carries `unit`, `identifier`, `priority` and `pid`, so one service
    on one machine is a `jq` select, not a regex."""

    NOTE = "note"
    """The runner talking about itself."""

    ERROR = "error"


@dataclass(frozen=True)
class Event:
    """One thing that happened, with its context as fields.

    Frozen: a sink must not be able to change what a later sink sees.
    """

    at: float
    kind: Kind
    level: Level
    text: str
    machine: str | None = None
    phase: str | None = None
    seconds: float | None = None
    data: dict = field(default_factory=dict)

    def as_json(self) -> str:
        body = {
            "at": round(self.at, 3),
            "kind": str(self.kind),
            "level": int(self.level),
            "text": self.text,
        }
        if self.machine is not None:
            body["machine"] = self.machine
        if self.phase is not None:
            body["phase"] = self.phase
        if self.seconds is not None:
            body["seconds"] = round(self.seconds, 3)
        if self.data:
            body["data"] = self.data
        return json.dumps(body)


def wanted(event: Event, level: Level) -> bool:
    """Should a reader asking for *level* see this?

    The whole filter, in one place, used by every sink that filters. In
    nixpkgs this comparison exists once per logger class.
    """
    return event.level >= level


def render(event: Event) -> str:
    """An event as a line for a person.

    Pure, so the terminal's output is tested without a terminal. The
    prefix is the machine when there is one and the kind when there is
    not, which keeps `grep '\\[cp\\]'` working the way it always has.
    """
    if event.kind is Kind.CONSOLE and event.machine:
        return f"[{event.machine}] {event.text}"
    if event.kind is Kind.JOURNAL and event.machine:
        source = event.data.get("unit") or event.data.get("identifier") or "journal"
        return f"[{event.machine}] {source}: {event.text}"
    if event.kind in (Kind.PHASE_STARTED, Kind.PHASE_FINISHED):
        return f"[phase] {event.text}"
    if event.kind is Kind.CASE:
        outcome = str(event.data.get("outcome", "")).upper()
        took = f" ({event.seconds:.2f}s)" if event.seconds else ""
        return f"[case] {outcome} {event.text}{took}"
    if event.kind is Kind.RPC and event.machine:
        return f"[{event.machine}] $ {event.text}"
    if event.kind is Kind.ERROR:
        return f"[error] {event.text}"
    if event.kind is Kind.OUTPUT:
        return event.text
    return f"[uml] {event.text}"


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def junit(events: Iterable[Event], name: str = "uml") -> str:
    """The finished phases as a JUnit document.

    Built from the same events as everything else rather than recorded
    separately, so it cannot disagree with `phases.json`. CI tools read
    this and nothing else, which is the only reason it exists.

    A skipped phase is `<skipped>` and a deselected one is left out
    entirely: a report that lists phases nobody asked for as "skipped"
    makes every selective run look half-broken in a CI dashboard.

    A pytest phase is its tests, one `<testcase>` each, classed under
    the phase. The phase's own row is left out unless it failed with no
    failing test to show for it -- a collection that found nothing.
    """
    # A pytest run sent to a paused session by hand is exploring, and the
    # run's verdict does not count it. Its failures here would contradict
    # `status`.
    kept = [
        e
        for e in events
        if e.kind in (Kind.PHASE_FINISHED, Kind.CASE) and not e.data.get("by_hand")
    ]
    with_cases = {e.phase for e in kept if e.kind is Kind.CASE}
    failing_cases = {
        e.phase
        for e in kept
        if e.kind is Kind.CASE and e.data.get("outcome") in ("failed", "error")
    }
    cases: list[str] = []
    failures = 0
    skipped = 0
    seconds = 0.0
    for event in kept:
        if event.kind is Kind.CASE:
            outcome = event.data.get("outcome", "")
            state = "failed" if outcome in ("failed", "error") else outcome
            classname = f"{name}.{event.phase}"
            case_name = event.text
        else:
            state = event.data.get("state", "")
            if state == "deselected":
                continue
            if state == "interrupted":
                # A dashboard has no third outcome, and "passed" would lie.
                state = "failed"
            if event.phase in with_cases and not (
                state == "failed" and event.phase not in failing_cases
            ):
                continue
            classname = name
            case_name = event.phase or ""
        took = event.seconds or 0.0
        seconds += took
        body = ""
        if state == "failed":
            failures += 1
            error = event.data.get("error", "failed")
            # The first line in the attribute and the whole thing in the
            # body. A dashboard shows the attribute in a table cell, and
            # a traceback in a table cell helps nobody.
            summary = event.data.get("message") or (
                error.splitlines()[0] if error else "failed"
            )
            body = (
                f'<failure message="{_xml_escape(summary)}">'
                f"{_xml_escape(error)}</failure>"
            )
        elif state == "skipped":
            skipped += 1
            body = (
                "<skipped message="
                f'"{_xml_escape(event.data.get("reason", "a phase it needs failed"))}"/>'
            )
        cases.append(
            f'  <testcase classname="{_xml_escape(classname)}"'
            f' name="{_xml_escape(case_name)}"'
            f' time="{took:.3f}">{body}</testcase>'
        )

    head = (
        f'<testsuite name="{_xml_escape(name)}" tests="{len(cases)}"'
        f' failures="{failures}" skipped="{skipped}" time="{seconds:.3f}">'
    )
    return "\n".join(['<?xml version="1.0" encoding="UTF-8"?>', head, *cases, "</testsuite>", ""])
