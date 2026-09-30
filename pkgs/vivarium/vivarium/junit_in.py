"""JUnit a guest wrote, read back as cases.

A suite that has to run *inside* a guest -- one that starts daemons, or
builds into stores it makes -- runs there and writes JUnit XML to
`/artifacts/junit/`. At the end of the phase the session reads every new
file there and emits each test as a `case` event, attributed to the
phase and the machine. So those tests are first-class: one JUnit case
each in the run's own `junit.xml`, and `events kind=case` in the MCP
server, the same as a pytest phase run on the host.

Any runner that writes JUnit works: pytest's `--junitxml`, `go test`
through go-junit-report, cargo-nextest. The phase's verdict is still
its script's; the cases are evidence.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from typing import Final

DIRECTORY: Final = "junit"
"""Under each guest's `/artifacts`, so `artifacts/<machine>/junit/`."""


@dataclass(frozen=True)
class Case:
    name: str
    outcome: str
    seconds: float
    message: str | None = None
    error: str | None = None
    reason: str | None = None


def parse(text: str) -> list[Case]:
    """Every `<testcase>` in a JUnit document, in order.

    Raises ValueError on a document that is not XML, so the caller can
    say which file it was rather than lose the phase.
    """
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise ValueError(f"not JUnit XML: {error}") from None
    return [_case(element) for element in root.iter("testcase")]


def _case(element: ElementTree.Element) -> Case:
    classname = element.get("classname", "")
    name = element.get("name", "")
    full = f"{classname}::{name}" if classname else name
    try:
        seconds = float(element.get("time", "0") or 0)
    except ValueError:
        seconds = 0.0
    for tag, outcome in (("failure", "failed"), ("error", "error")):
        found = element.find(tag)
        if found is not None:
            return Case(
                full,
                outcome,
                seconds,
                message=found.get("message") or None,
                error=(found.text or found.get("message") or "").strip() or None,
            )
    skipped = element.find("skipped")
    if skipped is not None:
        return Case(full, "skipped", seconds, reason=skipped.get("message") or None)
    return Case(full, "passed", seconds)
