"""pytest as a phase.

A phase declared with `pytest.tests` instead of `script` is a pytest run
against the session's guests, which are already up. What a test author
gets is pytest -- assertion rewriting, `-k`, parametrize, fixtures,
`conftest.py` -- and the same `Machine` API a phase script uses:

    async def test_hostname(one: Machine) -> None:
        assert await one.succeed("hostname") == "one"

**pytest runs in a worker thread, and the guests stay on the session's
loop.** A `Machine` belongs to the loop that started it, so an async
test or fixture is sent back to that loop through a `BlockingPortal`
and awaited there. The loop stays free between calls, which is why the
journal keeps streaming while a test runs.

It is a phase and not the owner of the run. Ordering lives in Nix,
where a failure skips what depends on it; pytest orders nothing and
would run a check against a cluster that was never built.

Each guest is a fixture named after it, and `vms` is all of them.
"""

from __future__ import annotations

import functools
import inspect
import types
from collections import Counter
from typing import TYPE_CHECKING, Any

import pytest

from .events import Kind, Level

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from anyio.from_thread import BlockingPortal
    from vivarium_runner import Machines

    from .session import Session


def machine_fixtures(vms: Machines) -> types.ModuleType:
    """One fixture per guest, named after it.

    A module and not a class: pytest binds a fixture found on an
    instance as a method, and these take no `self`.
    """
    module = types.ModuleType("uml_machines")
    for name in vms:

        def make(name: str = name) -> Any:
            @pytest.fixture(name=name)
            def machine() -> Any:
                return vms[name]

            return machine

        setattr(module, f"_machine_{name}", make())

    @pytest.fixture(name="vms")
    def all_of_them() -> Machines:
        return vms

    module.vms = all_of_them  # ty: ignore[unresolved-attribute]
    return module


def _in_loop(portal: BlockingPortal, func: Callable[..., Any]) -> Callable[..., Any]:
    """A sync stand-in for an async fixture, run on the session's loop.

    An async generator fixture spans two portal calls, one per
    `__anext__`, so its setup and teardown are separate tasks. An anyio
    task group held open across its `yield` does not survive that.
    """
    if inspect.isasyncgenfunction(func):

        def generator(**kwargs: Any) -> Generator[Any]:
            agen = func(**kwargs)
            value = portal.call(agen.__anext__)
            try:
                yield value
            finally:
                try:
                    portal.call(agen.__anext__)
                except StopAsyncIteration:
                    pass
                else:
                    raise RuntimeError(f"fixture {func.__name__} yielded twice")

        return generator

    def call(**kwargs: Any) -> Any:
        return portal.call(functools.partial(func, **kwargs))

    return call


class Plugin:
    """The hooks that tie one pytest run to one session."""

    def __init__(
        self, session: Session, phase: str, portal: BlockingPortal, *, by_hand: bool = False
    ) -> None:
        self.session = session
        self.phase = phase
        self.portal = portal
        self.marks: dict[str, Any] = {"by_hand": True} if by_hand else {}
        self.outcomes: Counter[str] = Counter()

    def _emit(self, kind: Kind, text: str, **fields: Any) -> None:
        # On the loop, never from this thread: the sinks write files and
        # the follower writes the same files from the loop.
        self.portal.call(
            functools.partial(
                self.session.emit, kind, text, phase=self.phase, **self.marks, **fields
            )
        )

    # ── running async code on the session's loop ───────────────────

    @pytest.hookimpl(tryfirst=True)
    def pytest_pyfunc_call(self, pyfuncitem: pytest.Function) -> bool | None:
        test = pyfuncitem.obj
        if not inspect.iscoroutinefunction(test):
            return None
        names = pyfuncitem._fixtureinfo.argnames
        kwargs = {name: pyfuncitem.funcargs[name] for name in names}
        self.portal.call(functools.partial(test, **kwargs))
        return True

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_fixture_setup(
        self, fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest
    ) -> Generator[None]:
        func = fixturedef.func
        if not (inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func)):
            yield
            return
        # Restored afterwards, as anyio's own plugin does: the definition
        # is shared by every test that asks for it.
        fixturedef.func = _in_loop(self.portal, func)  # ty: ignore[invalid-assignment]
        try:
            yield
        finally:
            fixturedef.func = func

    # ── which test is running, for everything else that happens ────

    def pytest_runtest_logstart(self, nodeid: str, location: object) -> None:
        self.portal.call(self.session.begin_case, nodeid)

    def pytest_runtest_logfinish(self, nodeid: str, location: object) -> None:
        self.portal.call(self.session.end_case)

    # ── outcomes ────────────────────────────────────────────────────

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        for title, content in report.sections:
            if title == f"Captured stdout {report.when}":
                for line in content.splitlines():
                    if line.strip():
                        self._emit(Kind.OUTPUT, line, case=report.nodeid)

        if report.when == "call":
            outcome = report.outcome
        elif report.failed:
            # A fixture that failed: pytest calls that an error, not a
            # failure, and so does a JUnit reader.
            outcome = "error"
        elif report.skipped and report.when == "setup":
            outcome = "skipped"
        else:
            return
        self._case(report.nodeid, outcome, report.when, report.duration, report)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self._case(report.nodeid or self.phase, "error", "collect", 0.0, report)

    def _case(
        self,
        nodeid: str,
        outcome: str,
        when: str,
        seconds: float,
        report: pytest.TestReport | pytest.CollectReport,
    ) -> None:
        self.outcomes[outcome] += 1
        fields: dict[str, Any] = {"outcome": outcome, "when": when}
        bad = outcome in ("failed", "error")
        if bad:
            fields["error"] = report.longreprtext
            crash = getattr(report.longrepr, "reprcrash", None)
            if crash is not None:
                fields["message"] = crash.message
        elif outcome == "skipped" and isinstance(report.longrepr, tuple):
            fields["reason"] = str(report.longrepr[2])
        self._emit(
            Kind.CASE,
            nodeid,
            level=Level.ERROR if bad else Level.INFO,
            seconds=seconds,
            **fields,
        )
        if bad:
            self._emit(Kind.ERROR, report.longreprtext, level=Level.ERROR, case=nodeid)

    def summary(self) -> str:
        """`2 passed, 1 failed`, in pytest's own order."""
        order = ("failed", "error", "passed", "skipped")
        return ", ".join(
            f"{self.outcomes[name]} {name}" for name in order if self.outcomes[name]
        ) or "no tests ran"


def arguments(tests: str, extra: list[str]) -> list[str]:
    """What `pytest.main` is given, before the caller's own arguments.

    Every choice here is about sharing a process with a running session:

    - `no:terminal`: every outcome is already an event, and the terminal
      sink prints those. pytest's own report would be the same run twice.
    - `--capture=sys`: `fd` capture swaps file descriptor 1 while a test
      runs, and the loop thread's terminal writes to it at the same time.
    - `no:cacheprovider`: the tests are in the store, which is read-only.
    - `--import-mode=importlib`: nothing is put on `sys.path`, so two
      pytest phases with a `test_basic.py` each do not collide.
    """
    return [
        tests,
        "-p",
        "no:terminal",
        "-p",
        "no:cacheprovider",
        "--capture=sys",
        "--import-mode=importlib",
        *extra,
    ]
