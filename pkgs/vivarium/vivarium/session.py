"""A run something else drives.

`run_test` did the whole sequence in one call and tore the guests down in
a `finally`, so there was no point at which anything outside could speak.
Everything the design wants needs that point: holding a failed run open,
stopping before a phase, running one phase later, an MCP server driving
the same object slowly.

So the session is the program and the CLI is one drive of it:

    session = Session(spec, out)
    await session.boot()
    for phase in session.pending():
        await session.run(phase)
    await session.teardown()

**Teardown is never automatic.** That is the whole point, and it is also
the risk: a caller that forgets leaves a UML kernel spinning on a core.
`vivarium_runner.backend.die_with_parent` covers the parent dying, which
covers the CLI. It does not cover an MCP server that stays up, so
whatever owns sessions has to reap them.

The mechanism underneath is `vivarium_runner`, unchanged. This module owns the
sequence, not the guests.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import time
import traceback
from collections import defaultdict
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from anyio.from_thread import BlockingPortal
from vivarium_runner import Machine, MachineError, Machines, MachineSpec, Toolchain
from vivarium_runner.net import build_lans
from vivarium_runner.report import Report

from . import journal, junit_in
from .events import Event, Kind, Level
from .phases import PhaseState, passed, runnable, skipped_by
from .pytest_plugin import Plugin, arguments, machine_fixtures

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection

    from .sinks import Sink
    from .spec import PhaseSpec, PytestSpec, Spec


class SessionError(RuntimeError):
    """The session could not do what was asked."""


_PRINTING: ContextVar[str | None] = ContextVar("uml_printing", default=None)
"""The phase whose task is printing. See `Session._capture`."""


class _Lines:
    """`sys.stdout` while phases run: each line an event of its phase."""

    def __init__(self, emit: Callable[..., None]) -> None:
        self.emit = emit

    def write(self, text: str) -> int:
        phase = _PRINTING.get()
        for line in text.splitlines():
            if line.strip():
                self.emit(Kind.OUTPUT, line, phase=phase)
        return len(text)

    def flush(self) -> None:
        pass


class CasesFailed(RuntimeError):
    """A pytest phase whose tests failed. Each one is its own event."""


def _pytest_main(args: list[str], plugins: list[object]) -> int:
    # The tests are in the store, which is read-only.
    sys.dont_write_bytecode = True
    return int(pytest.main(args, plugins=plugins))


def _forget(names: set[str], under: Path) -> None:
    """Drop the modules among `names` that were loaded from `under`."""
    root = under.resolve()
    for name in names:
        file = getattr(sys.modules.get(name), "__file__", None)
        if file is not None and Path(file).resolve().is_relative_to(root):
            del sys.modules[name]


def load_phase(script: Path) -> Callable[[Machines], Awaitable[None]]:
    """The `test` coroutine a phase's script exports.

    Imported, not `exec`'d. nixpkgs' driver runs `exec(tests, symbols)`
    with its methods injected as globals, and pays for it three ways: a
    script cannot import anything, nothing type checks it, and every
    frame is named `<string>` -- so the driver carries a traceback filter
    to make an assertion readable. A module has none of those problems.
    """
    spec = importlib.util.spec_from_file_location(f"uml_phase_{script.stem}", script)
    if spec is None or spec.loader is None:
        raise SessionError(f"cannot import a phase from {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    test = getattr(module, "test", None)
    if test is None:
        raise SessionError(f"{script} exports no `test`")
    if not callable(test):
        raise SessionError(f"`test` in {script} is not callable")
    return test


def with_kernel(
    toolchain: dict[str, str], machines: list[dict[str, Any]], kernel: Path
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """The toolchain and machines, booting `kernel` instead of Nix's.

    UML reads its kernel from the toolchain; QEMU from each machine's
    `boot`. Both are swapped, so the caller need not know which backend
    it has. Pure, and only ever reached from `--kernel`, by hand: the
    check keeps the kernel Nix built.
    """
    swapped = {**toolchain, "kernel": str(kernel)} if "kernel" in toolchain else dict(toolchain)
    return swapped, [
        {**machine, "boot": {**machine["boot"], "kernel": str(kernel)}}
        if isinstance(machine.get("boot"), dict)
        else machine
        for machine in machines
    ]


class Session:
    """One run, from evaluation to teardown, driven a step at a time."""

    def __init__(
        self,
        spec: Spec,
        out: Path,
        *,
        offline: bool = False,
        sink: Sink | None = None,
        pytest_args: list[str] | None = None,
        kernel: Path | None = None,
    ) -> None:
        self.spec = spec
        # A kernel from a working tree, by hand. See `with_kernel`.
        self.kernel = kernel
        self.out = out
        self.offline = offline
        self.sink = sink
        # Added to every pytest phase's own `args`: `vivarium run ... -- -k x`.
        self.pytest_args = pytest_args or []
        # The pytest test running now, so a command or a journal entry
        # names the test that caused it and not only the phase.
        self.case: str | None = None
        self._started = time.monotonic()
        self.artifacts = out / "artifacts"
        self.state: dict[str, PhaseState] = {
            phase.name: PhaseState.PENDING for phase in spec.phases
        }
        self.errors: dict[str, str] = {}
        self.stopped_by: int | None = None
        """The signal that ended the run early, if one did."""
        # Which phase holds each guest, so a command or a journal entry
        # carries the phase it belonged to. By guest because two phases
        # on disjoint guests run at once, and a guest is only ever held by
        # one. "What did `cluster` spend its time on" is then a question
        # `events.jsonl` answers on its own.
        self.running: dict[str, str] = {}
        self._captures = 0
        self._stdout: Any = None
        self.vms: Machines | None = None
        # Its own, not `report.RUN`. A process may hold several sessions
        # and their timings are not one run's.
        self.report = Report()
        self._lans: list[Any] = []
        self._booted = False
        self._journals: dict[str, journal.Tail] = {}
        # `follow` and a phase boundary both drain, and a Tail read twice
        # at once hands the same lines to both.
        self._draining = anyio.Lock()
        self._settled: set[str] = set()
        self._junit_seen: set[tuple[Path, int]] = set()
        # Process-wide, like `_capture`: one session per process, which
        # the CLI and the MCP server's child processes both are.
        for path in reversed(spec.pythonPath):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))

    # ── events ─────────────────────────────────────────────────────

    def emit(
        self,
        kind: Kind,
        text: str,
        *,
        level: Level = Level.INFO,
        machine: str | None = None,
        phase: str | None = None,
        seconds: float | None = None,
        **data: object,
    ) -> None:
        """Say something, once, to every sink that wants it."""
        if self.sink is None:
            return
        self.sink.emit(
            Event(
                at=time.monotonic() - self._started,
                kind=kind,
                level=level,
                text=text,
                machine=machine,
                phase=phase,
                seconds=seconds,
                data=dict(data),
            )
        )

    def _console(self, machine: str, line: str) -> None:
        self.emit(Kind.CONSOLE, line, level=Level.CONSOLE, machine=machine)

    def _command(self, machine: str, what: str, seconds: float) -> None:
        self.emit(
            Kind.RPC,
            what,
            level=Level.DETAIL,
            machine=machine,
            seconds=seconds,
            phase=self.running.get(machine),
            **self._in_case(),
        )

    @contextlib.contextmanager
    def _capture(self, phase: str | None = None):
        """Turn what a phase prints into events.

        A phase script says things with `print`, which is right -- asking
        a test author to learn a logging API to say "the cluster came up"
        is how a framework stops being used. But a bare print reaches the
        terminal and nothing else, so the log file and `events.jsonl`
        would be missing the one thing a reader most wants.

        Captured here instead, so each line becomes a NOTE carrying the
        phase it came from. That is more than the old `tee` managed: the
        line is attributed, not just kept.

        **`sys.stdout` is process-wide**, and phases on disjoint guests
        run at once. So the first capture installs one writer and the
        last one out restores the real stdout, and the writer asks a
        context variable which phase is printing: each phase is its own
        task, and a task has its own context. `redirect_stdout` per phase
        would restore the terminal when the first of two phases ended,
        under the other one still printing.
        """
        token = _PRINTING.set(phase)
        if self._captures == 0:
            self._stdout = sys.stdout
            sys.stdout = _Lines(self.emit)
        self._captures += 1
        try:
            yield
        finally:
            self._captures -= 1
            if self._captures == 0:
                sys.stdout = self._stdout
            _PRINTING.reset(token)

    def _replay(self, lines: int = 20, nodes: Collection[str] | None = None) -> None:
        """The end of each guest's console, at error level.

        This is what makes a quiet default safe. The console is off the
        terminal while things work, and the moment a phase fails the
        last lines of every guest arrive without anybody going to look
        for a file -- which is the context that usually explains it.

        nixpkgs has the switch (`print_serial_logs`) and not this: there
        the choice is all of it all the time, or none of it including
        when it would have helped.
        """
        if self.vms is None:
            return
        for name, vm in self.vms.items():
            if nodes is not None and name not in nodes:
                continue
            tail = [line for line in list(vm._history)[-lines:] if line]
            if not tail:
                continue
            self.emit(
                Kind.ERROR,
                f"the last {len(tail)} console lines from {name}:",
                level=Level.ERROR,
                machine=name,
            )
            for line in tail:
                self.emit(
                    Kind.ERROR, f"  {line}", level=Level.ERROR, machine=name
                )

    # ── the operations ─────────────────────────────────────────────

    async def boot(self) -> Machines:
        """Bring every guest up and wait for its agent."""
        if self._booted:
            raise SessionError("already booted")
        toolchain, machines = self.spec.toolchain(), self.spec.machines
        if self.kernel is not None:
            toolchain, machines = with_kernel(toolchain, machines, self.kernel)
            self.emit(
                Kind.NOTE,
                f"booting {self.kernel}, not the kernel Nix built; this is not the check",
                level=Level.ERROR,
                kernel=str(self.kernel),
            )
        tools = Toolchain.from_json(toolchain)
        specs = [MachineSpec.from_json(m) for m in machines]

        segments: dict[str, list[str]] = defaultdict(list)
        for one in specs:
            if one.network:
                segments[one.network].append(one.name)
        self._lans = build_lans(segments)
        lan_fd = {
            name: fd for lan in self._lans for name, fd in lan.fds.items()
        }

        self.emit(Kind.BOOT, f"booting {', '.join(one.name for one in specs)}")
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self._journals = {}
        for one in specs:
            path = self._guest_artifacts(one.name) / journal.FILE
            # A second run into the same `--out` would otherwise follow the
            # last run's journal as if this guest had written it.
            path.unlink(missing_ok=True)
            self._journals[one.name] = journal.Tail(path)
        vms = Machines(
            (
                one.name,
                Machine(
                    one,
                    tools,
                    lan_fd=lan_fd.get(one.name),
                    artifacts=self._guest_artifacts(one.name),
                    recorder=self.report,
                    offline=self.offline,
                    on_console=self._console,
                    on_command=self._command,
                ),
            )
            for one in specs
        )
        vms.settings = self.spec.settings
        vms.artifacts = self.artifacts
        # A phase reads `vms.knobs["name"]` and gets the value, not the
        # record: a script branching on a steer should not have to know
        # where the steer came from.
        vms.knobs = {name: knob.value for name, knob in self.spec.knobs.items()}
        # One phase's findings for a later one -- a process census taken
        # before the suites, read by the phase that looks for leaks.
        vms.shared = {}
        vms.phase = None

        # Serially, and before anything spawns: picking a free host
        # address means binding a port and letting go of it again, so two
        # guests doing it at once would both be told the same address is
        # free. **This set is per session**, which is right for one CLI
        # run and wrong for an MCP server holding several -- see the
        # design, area 0d.
        taken: set[str] = set()
        for machine in vms.values():
            machine.resolve_forward(taken)

        for lan in self._lans:
            lan.start()
        self.vms = vms
        # Captured too: passt and the forward resolver say useful things
        # on the way up, and they say them with `print`.
        with self._capture():
            failures = await _start_all(vms)
        for lan in self._lans:
            lan.detach()
        if failures:
            raise failures[0]
        self._booted = True
        return vms

    async def drain(self) -> None:
        """Emit every journal entry the guests have written since last asked.

        Attributed to the running phase. That is exact because both phase
        boundaries wait for the stream: a drain as the phase starts, and
        `settle` before it finishes.
        """
        async with self._draining:
            for name, tail in self._journals.items():
                for line in await tail.read():
                    entry = journal.parse(line)
                    if entry is None:
                        continue
                    if entry.identifier == journal.SETTLE:
                        self._settled.add(entry.message)
                        continue
                    self.emit(
                        Kind.JOURNAL,
                        entry.message,
                        level=journal.level(entry),
                        machine=name,
                        phase=self.running.get(name),
                        **entry.data(),
                        **self._in_case(),
                    )

    async def settle(
        self, timeout: float = 2.0, nodes: Collection[str] | None = None
    ) -> None:
        """Wait until each guest's journal has reached the host.

        Only `nodes` when given: a phase settles its own guests, and a
        token sent to a guest another phase holds is a command in the
        middle of that phase.

        A test that logs and returns is done before journald has handed
        the line on, and a teardown straight after loses it: measured,
        `echo` from a unit that `systemd-run --wait` had finished never
        reached the file. So each live guest logs a token, and this
        drains until every token has arrived -- everything logged before
        it has arrived too.

        Bounded, and skipped for a guest that is dead or not streaming:
        evidence is worth two seconds, never a hung run.
        """
        if self.vms is None:
            return
        tokens: set[str] = set()
        with anyio.move_on_after(timeout):
            for name, tail in self._journals.items():
                if nodes is not None and name not in nodes:
                    continue
                vm = self.vms.get(name)
                if vm is None or not tail.streaming or not vm.alive():
                    continue
                token = f"{name}-{time.monotonic_ns()}"
                try:
                    await vm.succeed(
                        f"echo {token} | systemd-cat --identifier={journal.SETTLE}",
                        timeout=timeout,
                    )
                except MachineError:
                    continue
                tokens.add(token)
            while not tokens <= self._settled:
                await self.drain()
                await anyio.sleep(0.02)
        await self.drain()

    def _cases_from_guests(self, phase: str, nodes: Collection[str] | None = None) -> None:
        """Every JUnit file a guest wrote during this phase, as cases.

        See `junit_in`. Keyed on the file and its mtime, so a suite that
        rewrites `unit.xml` in a later phase is read again, and a file
        read once is not read twice. Only `nodes` when given, so a file
        another phase is still writing is not read half-written.
        """
        for path in sorted(self.artifacts.glob(f"*/{junit_in.DIRECTORY}/*.xml")):
            if nodes is not None and path.parent.parent.name not in nodes:
                continue
            try:
                key = (path, path.stat().st_mtime_ns)
            except OSError:
                continue
            if key in self._junit_seen:
                continue
            self._junit_seen.add(key)
            machine = path.parent.parent.name
            try:
                cases = junit_in.parse(path.read_text(errors="replace"))
            except ValueError as error:
                self.emit(
                    Kind.ERROR,
                    f"{path.name} from {machine}: {error}",
                    level=Level.ERROR,
                    machine=machine,
                    phase=phase,
                )
                continue
            for case in cases:
                bad = case.outcome in ("failed", "error")
                fields = {
                    key: value
                    for key, value in (
                        ("message", case.message),
                        ("error", case.error),
                        ("reason", case.reason),
                    )
                    if value is not None
                }
                self.emit(
                    Kind.CASE,
                    case.name,
                    level=Level.ERROR if bad else Level.DETAIL,
                    machine=machine,
                    phase=phase,
                    seconds=case.seconds,
                    outcome=case.outcome,
                    when="guest",
                    file=path.name,
                    **fields,
                )

    def _in_case(self) -> dict[str, str]:
        return {"case": self.case} if self.case is not None else {}

    async def follow(self, interval: float = 0.25) -> None:
        """Stream the guests' journals into the events, until cancelled.

        Run beside the phases by whatever drives the session -- `drive`
        in a task group, an MCP server in a task of its own. The session
        holds no task of its own, so it has no lifetime to get wrong.
        """
        try:
            while True:
                await self.drain()
                await anyio.sleep(interval)
        finally:
            # What the guests wrote on the way down.
            with anyio.CancelScope(shield=True):
                await self.drain()

    async def begin_case(self, nodeid: str) -> None:
        """A pytest test starts. What happens until `end_case` is its.

        Exact for a command. A journal entry is attributed when it
        arrives, and one logged in a test's last milliseconds can arrive
        after the test ends: a barrier per test would cost a command per
        guest per test. The phase has one, see `settle`.
        """
        await self.drain()
        self.case = nodeid

    async def end_case(self) -> None:
        await self.drain()
        self.case = None

    async def _pytest(
        self, name: str, spec: PytestSpec, vms: Machines, *, by_hand: bool = False
    ) -> str:
        """One pytest run, in a worker thread, against these guests.

        `by_hand` is a run sent to a paused session, not a phase: it takes
        only its own arguments, and its cases stay out of junit.xml.
        """
        # `--import-mode=importlib` puts nothing on `sys.path`, so a test
        # could not import a helper module beside it without this.
        here = spec.tests if spec.tests.is_dir() else spec.tests.parent
        sys.path.insert(0, str(here))
        imported = set(sys.modules)
        try:
            async with BlockingPortal() as portal:
                plugin = Plugin(self, name, portal, by_hand=by_hand)
                plugins = [plugin, machine_fixtures(vms)]
                extra = spec.args if by_hand else [*spec.args, *self.pytest_args]
                args = arguments(str(spec.tests), extra)
                code = await anyio.to_thread.run_sync(_pytest_main, args, plugins)
        finally:
            sys.path.remove(str(here))
            # pytest's importlib mode hands back a module already in
            # `sys.modules`, and so does a plain import of a helper. So
            # a second run of an edited file would be the first run's code.
            _forget(set(sys.modules) - imported, here)
        if code == pytest.ExitCode.NO_TESTS_COLLECTED:
            # A phase that tested nothing is a selection that matched
            # nothing, and a green run would hide the typo.
            raise CasesFailed("no tests were collected")
        if code == pytest.ExitCode.USAGE_ERROR:
            # pytest says why on its own stderr, not through any hook.
            raise CasesFailed(f"pytest rejected its arguments: {args[1:]}")
        if code != pytest.ExitCode.OK:
            raise CasesFailed(
                plugin.summary() if plugin.outcomes else f"pytest exited {code}"
            )
        self.emit(Kind.NOTE, plugin.summary(), phase=name)
        return plugin.summary()

    def view(self, phase: PhaseSpec) -> Machines:
        """The guests `phase` declared, as the `vms` its script is given.

        Its own object, because two phases run at once and each has its
        own `phase`. Everything else is shared by reference: `shared` is
        how one phase leaves a finding for the next. A phase that reaches
        a guest it did not declare fails on the name, which is the whole
        check that `nodes` is true.
        """
        if self.vms is None:
            raise SessionError("run before boot")
        wanted = phase.nodes or list(self.vms)
        missing = [name for name in wanted if name not in self.vms]
        if missing:
            # A node whose `networking.hostName` is not its attribute name
            # boots under the hostname.
            raise SessionError(
                f"phase {phase.name} names {', '.join(missing)} in `nodes`;"
                f" the guests are {', '.join(self.vms)}"
            )
        vms = Machines((name, self.vms[name]) for name in wanted)
        vms.settings = self.vms.settings
        vms.artifacts = self.vms.artifacts
        vms.knobs = self.vms.knobs
        vms.shared = self.vms.shared
        # One script may serve several phases, told apart by this.
        vms.phase = phase.name
        return vms

    async def run(self, phase: PhaseSpec) -> PhaseState:
        """Run one phase, and record what its outcome means for the rest.

        Reentrant for phases on disjoint guests: everything it touches
        per phase is keyed by the phase or by its guests. Which phases
        may overlap is `phases.launchable`'s answer, not this method's.
        """
        if self.vms is None:
            raise SessionError("run before boot")
        nodes = phase.nodes or list(self.vms)
        await self.drain()
        self.state[phase.name] = PhaseState.RUNNING
        for name in nodes:
            self.running[name] = phase.name
        self.emit(Kind.PHASE_STARTED, phase.name, phase=phase.name, nodes=nodes)
        started = time.monotonic()
        try:
            vms = self.view(phase)
            if phase.pytest is not None:
                await self._pytest(phase.name, phase.pytest, vms)
            elif phase.script is not None:
                # Loaded inside the `try`: a script that does not import
                # is this phase failing. Outside it, an ImportError took
                # the whole drive down with no failed phase and no pause.
                test = load_phase(phase.script)
                with self._capture(phase.name):
                    await test(vms)
        except Exception as error:
            took = time.monotonic() - started
            await self.settle(nodes=nodes)
            self._cases_from_guests(phase.name, nodes)
            self._record(phase, started)
            self.state[phase.name] = PhaseState.FAILED
            self.errors[phase.name] = f"{type(error).__name__}: {error}"
            self.emit(
                Kind.PHASE_FINISHED,
                f"{phase.name} FAILED: {error}",
                level=Level.ERROR,
                phase=phase.name,
                seconds=took,
                state=str(PhaseState.FAILED),
                error=self.errors[phase.name],
            )
            # A failing test has already said why, with pytest's own
            # rewritten assertion. The runner's traceback would only add
            # the frames of the runner.
            if not isinstance(error, CasesFailed):
                self.emit(
                    Kind.ERROR,
                    traceback.format_exc().rstrip(),
                    level=Level.ERROR,
                    phase=phase.name,
                )
            self._replay(nodes=nodes)
            for name in skipped_by(phase.name, self.spec.phases):
                if self.state.get(name) is PhaseState.PENDING:
                    self.state[name] = PhaseState.SKIPPED
                    self.emit(
                        Kind.PHASE_FINISHED,
                        f"{name} skipped, it needs {phase.name}",
                        phase=name,
                        state=str(PhaseState.SKIPPED),
                        reason=f"{phase.name} failed",
                    )
            self._release(nodes)
            return PhaseState.FAILED
        except BaseException:
            # Cancelled: the run is being stopped. Recorded, then passed
            # on, because a stop must not be swallowed here.
            self.state[phase.name] = PhaseState.INTERRUPTED
            self.emit(
                Kind.PHASE_FINISHED,
                f"{phase.name} interrupted",
                level=Level.ERROR,
                phase=phase.name,
                seconds=time.monotonic() - started,
                state=str(PhaseState.INTERRUPTED),
                error="the run was stopped while this phase ran",
            )
            self._release(nodes)
            raise
        took = time.monotonic() - started
        await self.settle(nodes=nodes)
        self._cases_from_guests(phase.name, nodes)
        self._release(nodes)
        self._record(phase, started)
        self.state[phase.name] = PhaseState.PASSED
        self.emit(
            Kind.PHASE_FINISHED,
            f"{phase.name} passed in {took:.1f}s",
            phase=phase.name,
            seconds=took,
            state=str(PhaseState.PASSED),
        )
        return PhaseState.PASSED

    def _release(self, nodes: list[str]) -> None:
        for name in nodes:
            self.running.pop(name, None)

    def _record(self, phase: PhaseSpec, started: float) -> None:
        """A phase is a span in the timings as well as a row in the state.

        The same unit everywhere -- report, event stream, breakpoint name,
        MCP tool -- rather than three names for one thing.
        """
        self.report.step("-", "phase", phase.name, time.monotonic() - started)

    def write_output(self) -> None:
        """Everything a reader needs, in the directory the caller named.

        The same four things whether or not this ran in a sandbox, which
        is the point: `.run` and the check used to be written separately,
        and every asymmetry between them was a bug somebody met later.

            status       0 or 1, as text
            report.json  where the time went
            phases.json  what each phase did, and why it was skipped
            artifacts/   what the guests wrote to /artifacts
        """
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "status").write_text("0\n" if self.passed else "1\n")
        (self.out / "phases.json").write_text(
            json.dumps(
                {
                    "passed": self.passed,
                    "phases": [
                        {
                            "name": name,
                            "state": str(state),
                            "error": self.errors.get(name),
                        }
                        for name, state in self.state.items()
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        first = next(iter(self.errors.values()), None)
        self.report.write(self.out / "report.json", self.passed, first)
        self.emit(
            Kind.RUN_FINISHED,
            "passed" if self.passed else "failed",
            level=Level.ERROR if not self.passed else Level.INFO,
            passed=self.passed,
            states={name: str(state) for name, state in self.state.items()},
        )

    def pending(self) -> list[PhaseSpec]:
        """The phases still worth running, in the order Nix sorted them."""
        return runnable(self.spec.phases, self.state)

    @property
    def passed(self) -> bool:
        return passed(self.state)

    async def teardown(self) -> None:
        """Guests down. Safe to call twice; never called for you."""
        if self.vms is not None:
            async with anyio.create_task_group() as group:
                for machine in self.vms.values():
                    group.start_soon(_shutdown, machine)
            self.vms = None
        for lan in self._lans:
            lan.close()
        self._lans = []
        self._booted = False

    # ── helpers ────────────────────────────────────────────────────

    def _guest_artifacts(self, name: str) -> Path:
        """One guest's own directory, made before it boots.

        Per guest, because three nodes writing `pytest.log` into one
        directory is two lost files. Made here and not in the guest:
        hostfs and virtiofs both serve a directory that exists.

        Mode 1777, as /tmp: the runner is root of its own namespace, so
        without it only a guest's root could write here.
        """
        path = self.artifacts / name
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o1777)
        return path


async def _start_all(vms: Machines) -> list[BaseException]:
    """Start every guest, and let each settle even if another fails.

    A task group cancels its siblings when one raises, which would leave
    a guest half-spawned for the teardown to trip over. So each failure
    is caught where it happens and reported after all of them are done.
    """
    failures: list[BaseException] = []

    async def start(machine: Machine) -> None:
        try:
            await machine.start()
        except BaseException as error:  # noqa: BLE001 -- recorded, then re-raised by the caller
            failures.append(error)

    async with anyio.create_task_group() as group:
        for machine in vms.values():
            group.start_soon(start, machine)
    return failures


async def _shutdown(machine: Machine) -> None:
    """Teardown must not fail: a guest that will not stop cleanly is not
    a reason to leave the others running."""
    try:
        await machine.shutdown()
    except Exception as error:  # noqa: BLE001
        print(f"[vivarium] {machine.name} did not shut down cleanly: {error}", flush=True)
