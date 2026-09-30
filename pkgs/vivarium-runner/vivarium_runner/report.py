"""Where a run spent its time.

A test here is minutes of waiting and seconds of work, and which minutes
is not guessable: booting, `kubeadm init`, a unit that is slow to settle,
and a poll loop that sleeps longer than the thing it waits for all look
the same from outside.  So every round trip to a guest and every wait is
recorded, and the run writes one JSON file at the end.

Two kinds of step, and the difference matters when reading the totals:

    rpc     one round trip to a guest's agent.
    wait    a poll loop, which is many `rpc` steps and the sleeps between
            them.  Its seconds therefore *contain* those of the `rpc`
            steps inside it -- do not add the two together.

Written on failure as well as on success.  The run you most want the
timings for is the one that timed out.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# Enough to recognise a command, short enough that a few thousand of them
# stay a file somebody will open.
_WIDTH = 200

# The environment variable that names where to write. `mkTest` sets it to
# a file in `$out`, so a sandboxed build always records; a run by hand
# records when it is asked to.
ENV = "UML_TEST_REPORT"


@dataclass
class Step:
    at: float
    machine: str
    kind: str
    what: str
    seconds: float


@dataclass
class Report:
    started: float = field(default_factory=time.monotonic)
    machines: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)

    def booted(self, name: str, seconds: float, facts: dict) -> None:
        self.machines[name] = dict(facts, boot_seconds=round(seconds, 3))
        # A step as well, so booting counts as time the run accounted
        # for. Guests boot together, and two overlapping steps are
        # counted once, so this is wall clock and not the sum.
        self.step(name, "boot", f"boot {name}", seconds)

    @contextmanager
    def waiting(self, machine: str, what: str):
        """Time a poll loop as one step, whatever it does inside."""
        started = time.monotonic()
        try:
            yield
        finally:
            self.step(machine, "wait", what, time.monotonic() - started)

    def step(self, machine: str, kind: str, what: str, seconds: float) -> None:
        self.steps.append(
            Step(
                at=round(time.monotonic() - self.started - seconds, 3),
                machine=machine,
                kind=kind,
                what=what[:_WIDTH],
                seconds=round(seconds, 3),
            )
        )

    def _by_command(self) -> list[dict]:
        """Round trips grouped by the program they ran.

        The first two words, because `kubectl get pod ...` and `kubectl
        get node ...` are different questions while a thousand distinct
        pod names are not.
        """
        totals: dict[str, list] = {}
        for s in self.steps:
            if s.kind != "rpc":
                continue
            key = " ".join(s.what.split()[:2])
            entry = totals.setdefault(key, [0, 0.0])
            entry[0] += 1
            entry[1] += s.seconds
        rows = [
            {"command": k, "calls": n, "seconds": round(t, 3)}
            for k, (n, t) in totals.items()
        ]
        return sorted(rows, key=lambda r: -r["seconds"])[:30]

    def write(self, path: Path, passed: bool, error: str | None = None) -> None:
        total = time.monotonic() - self.started
        # Waits contain the round trips inside them, so the two are
        # reported apart rather than summed.
        waited = sum(s.seconds for s in self.steps if s.kind == "wait")
        # Waits nest, and round trips nest inside them, so neither list
        # can be summed for a total. The top-level steps can: what is left
        # over is time the run spent somewhere nothing recorded, which on a
        # long test is a test's own poll loops. That number is the point --
        # it says how much of the run is still invisible.
        accounted = _toplevel(self.steps)
        body = {
            "passed": passed,
            "error": error,
            "total_seconds": round(total, 3),
            "recorded_seconds": round(accounted, 3),
            "unrecorded_seconds": round(max(total - accounted, 0.0), 3),
            "boot_seconds": round(
                max((m["boot_seconds"] for m in self.machines.values()), default=0.0), 3
            ),
            "waiting_seconds": round(waited, 3),
            "machines": self.machines,
            "by_command": self._by_command(),
            "slowest": [
                vars(s)
                for s in sorted(self.steps, key=lambda s: -s.seconds)[:20]
            ],
            "steps": [vars(s) for s in self.steps],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2) + "\n")

    def summary(self, slowest: int = 5) -> str:
        """The same numbers as the JSON, short enough for a CI log.

        The file is only written when `$UML_TEST_REPORT` names one, which
        a sandboxed build always does and `nix run` never does -- so
        without this a CI job that boots a guest leaves no trace of where
        its twenty minutes went.

        `recorded` is the honest part of it: the share of the run that any
        step covers at all.  A low figure means the time went somewhere
        nothing here is watching, and the answer is another `waiting`,
        not a bigger deadline.
        """
        total = time.monotonic() - self.started
        accounted = _toplevel(self.steps)
        share = round(100 * accounted / total) if total else 0
        boot = max((m["boot_seconds"] for m in self.machines.values()), default=0.0)
        lines = [
            f"[time] total {total:.1f}s, recorded {share}%, boot {boot:.1f}s"
        ]
        for s in sorted(self.steps, key=lambda s: -s.seconds)[:slowest]:
            lines.append(f"[time]   {s.seconds:7.1f}s {s.kind:5} {s.what} ({s.machine})")
        return "\n".join(lines)

    def write_if_asked(self, passed: bool, error: str | None = None) -> Path | None:
        where = os.environ.get(ENV)
        if not where:
            return None
        path = Path(where)
        self.write(path, passed, error)
        return path


def _toplevel(steps: list) -> float:
    """Seconds covered by the steps, counting nesting once.

    Steps are appended when they finish, so a wait lands after everything
    inside it. Sorting by start and skipping anything that begins before
    the last one ended leaves the outermost of each nest.

    Longest first on a tie, or a round trip that started in the same
    instant as the wait around it would be taken for the outer one and
    the wait discarded -- which reports *less* time the more a test
    records.
    """
    total = 0.0
    end = -1.0
    for s in sorted(steps, key=lambda s: (s.at, -s.seconds)):
        if s.at >= end:
            total += s.seconds
            end = s.at + s.seconds
    return total


RUN = Report()
"""The run in progress.

A module-level value because `run_test` is the only entry point and a
test never makes a second run in one process -- passing a recorder
through `machines()` into every `Machine` would be four signatures
changed to say the same thing.
"""
