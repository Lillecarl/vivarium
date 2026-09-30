"""`vivarium run`: one linear drive of one session.

Thin on purpose. Everything here is ordering and reporting; the session
holds the work. That is what lets an MCP server be the same object
driven slowly -- written as a `main` with the logic inside, none of it
would be reusable.

**One program, run two ways.** A build runs it with `--out $out`, and a
developer runs the same binary with `--out somewhere`. Nothing branches
on which, so the two cannot drift -- and drift is where every asymmetry
in the old pair came from: no log by hand, no report by hand, a `$@` that
was documented and rejected.
"""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import sys
from collections.abc import Collection
from pathlib import Path


import anyio
from vivarium_runner import MachineError

from . import monitor, namespace, repl, runroot
from .control import SOCKET, Controller, Op, request
from .events import Kind, Level
from .phases import PhaseState, launchable, ready, summarise
from .sinks import Broadcast, ConsoleFiles, JsonLines, Junit, Log, Terminal
from .session import Session, SessionError
from .spec import PhaseSpec, Spec, SpecError





def parse(argv: list[str] | None = None) -> argparse.Namespace:
    """The command line, and whatever follows `--` for pytest.

    `vivarium run --spec s --out o -- -k hostname -x` hands `-k hostname -x`
    to every pytest phase. Split here rather than left to argparse, whose
    REMAINDER takes the first unknown flag as the start of it.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    extra: list[str] = []
    if "--" in argv:
        at = argv.index("--")
        argv, extra = argv[:at], argv[at + 1 :]
    parser = argparse.ArgumentParser(
        prog="vivarium",
        description="Run NixOS guests",
        epilog="Arguments after -- go to every pytest phase.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="boot the guests and run every phase")
    run.add_argument("--spec", type=Path, required=True, help="the spec Nix wrote")
    run.add_argument(
        "--out",
        type=Path,
        required=True,
        help="where the run's evidence goes: artifacts, log, report, status",
    )
    run.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="PHASE",
        help="run only this phase; repeatable",
    )
    run.add_argument(
        "--break",
        dest="breaks",
        action="append",
        default=[],
        metavar="PHASE",
        help="pause before this phase, with the guests up; repeatable. `vivarium ctl` reaches in",
    )
    run.add_argument(
        "--kernel",
        type=Path,
        metavar="PATH",
        help=(
            "boot this kernel instead of the one Nix built: `linux` from a UML"
            " tree, a bzImage for QEMU (virtio built in). For iterating on a"
            " kernel without a Nix build per change; never the check's kernel"
        ),
    )
    run.add_argument(
        "--break-on-start",
        action="store_true",
        help="pause before the first phase, with the guests up",
    )
    run.add_argument(
        "--interactive",
        action="store_true",
        help=(
            "a Python REPL on this terminal, as nixos-test's driverInteractive:"
            " pause before the first phase, after a failure and after the last"
            " phase; ^D tears the run down"
        ),
    )
    run.add_argument(
        "--break-on-failure",
        action="store_true",
        help="pause when a phase fails, with the guests up and the state intact",
    )
    run.add_argument(
        "--serial",
        action="store_true",
        help=(
            "one phase at a time, in the order Nix sorted them, even where"
            " `nodes` would let phases run at once"
        ),
    )
    run.add_argument(
        "--offline",
        action="store_true",
        help=(
            "give the guests no way off this host, the way a sandboxed"
            " check has none"
        ),
    )
    run.add_argument(
        "--keep",
        action="store_true",
        help="remove nothing at the end: guest disks, sockets, the run directory",
    )
    run.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help=(
            "-v shows every command sent to a guest, -vv adds the guests'"
            " consoles. Both are written to the output directory either way"
        ),
    )
    run.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="only failures and the verdict",
    )

    phases = sub.add_parser("phases", help="list the phases and exit")
    phases.add_argument("--spec", type=Path, required=True)

    ctl = sub.add_parser("ctl", help="reach into a run that is paused at a breakpoint")
    ctl.add_argument("--out", type=Path, required=True, help="the run's --out")
    ctl.add_argument("op", choices=[str(op) for op in Op])
    ctl.add_argument(
        "arg",
        nargs="?",
        default="",
        help=(
            "exec: Python, or - for stdin. inject: a file with `test(vms)`."
            " pytest: a test file or directory, pytest's arguments after --."
            " run: a phase"
        ),
    )
    watch = sub.add_parser(
        "monitor",
        help="print a run's events, one line each, until its verdict",
        description=(
            "Follow a run that vivarium-mcp started. Exit 0 if it passed, 1 if it"
            " failed, 2 if it exited without a verdict, 3 if the stream ended early."
        ),
    )
    watch.add_argument("target", help="the run's --out directory, or its id")
    watch.add_argument("--json", action="store_true", help="each event as a JSON object")
    watch.add_argument(
        "--quiet",
        action="store_true",
        help="only pauses, failures and the verdict, not each phase that starts or passes",
    )
    args = parser.parse_args(argv)
    args.pytest_args = extra
    return args


def announce(session: Session) -> None:
    """Every knob, its value and where the value came from.

    Before anything boots, and always. A misspelled variable is invisible
    otherwise: the run quietly does the whole suite instead of the one
    case that was asked for, and nothing says why.
    """
    for name, knob in sorted(session.spec.knobs.items()):
        session.emit(
            Kind.KNOB,
            f"knob {name}={knob.value!r} ({knob.source})",
            knob=name,
            value=knob.value,
            source=knob.source,
            env=knob.env,
        )


def terminal_level(args: argparse.Namespace) -> Level:
    """What the person watching asked to see.

    The default leaves the guests' consoles out. Every one of them is
    written to `console/<guest>.log` regardless, and the tail of each is
    replayed when a phase fails -- so nothing is lost by the quiet
    default, and the case that matters is louder than it was.
    """
    if args.quiet:
        return Level.ERROR
    if args.verbose >= 2:
        return Level.CONSOLE
    if args.verbose == 1:
        return Level.DETAIL
    return Level.INFO


def sinks_for(args: argparse.Namespace, name: str) -> Broadcast:
    """Everything that wants the events.

    One stream, four readers: the person, the machine, the guests' own
    consoles, and whatever CI reads JUnit with. Adding a fifth -- an MCP
    server -- is another entry here and nothing else.
    """
    return Broadcast(
        [
            Terminal(terminal_level(args)),
            # Unfiltered on purpose. The terminal is a view; this is the
            # record, and a record that only kept what somebody thought
            # was interesting at the time is not one.
            Log(args.out / "log"),
            JsonLines(args.out / "events.jsonl"),
            ConsoleFiles(args.out / "console"),
            Junit(args.out / "junit.xml", name),
        ]
    )


async def run(args: argparse.Namespace) -> int:
    spec = Spec.read(args.spec)
    sink = sinks_for(args, spec.name)
    session = Session(
        spec,
        args.out,
        offline=args.offline,
        sink=sink,
        pytest_args=args.pytest_args,
        kernel=args.kernel.resolve() if args.kernel else None,
    )
    session.emit(Kind.RUN_STARTED, f"output in {args.out}")
    session.emit(Kind.NOTE, namespace.describe(Path("/proc/self/uid_map").read_text()))
    announce(session)

    if args.only:
        missing = set(args.only) - {phase.name for phase in session.spec.phases}
        if missing:
            raise SessionError(
                f"no such phase: {', '.join(sorted(missing))};"
                f" have {', '.join(p.name for p in session.spec.phases)}"
            )
        # Deselected, not skipped. The two read the same in a list and
        # mean opposite things: one says nobody knows the answer, the
        # other says nobody wanted it.
        for phase in session.spec.phases:
            if phase.name not in args.only:
                session.state[phase.name] = PhaseState.DESELECTED
                session.emit(
                    Kind.PHASE_FINISHED,
                    f"{phase.name} deselected",
                    level=Level.DETAIL,
                    phase=phase.name,
                    state=str(PhaseState.DESELECTED),
                )

    unknown = set(args.breaks) - {phase.name for phase in session.spec.phases}
    if unknown:
        # A misspelled breakpoint is a run that never stops.
        raise SessionError(f"no such phase to break before: {', '.join(sorted(unknown))}")

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(_stop_on_signal, session, group.cancel_scope)
            await drive(
                session,
                breaks=args.breaks,
                break_on_failure=args.break_on_failure or args.interactive,
                break_on_start=args.break_on_start or args.interactive,
                interactive=args.interactive,
                serial=args.serial,
            )
            group.cancel_scope.cancel()
        if session.stopped_by is not None:
            return 128 + session.stopped_by
        for line in session.report.summary().splitlines():
            session.emit(Kind.NOTE, line.removeprefix("[time] "))
        session.emit(Kind.NOTE, summarise(session.state))
        return 0 if session.passed else 1
    finally:
        sink.close()


async def _stop_on_signal(session: Session, scope: anyio.CancelScope) -> None:
    """SIGTERM or SIGHUP ends the run the way ^C does: the drive is
    cancelled, and its shielded teardown powers each guest off, then
    signals each guest's process group, SIGTERM and then SIGKILL."""
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGHUP) as signals:
        async for received in signals:
            session.stopped_by = int(received)
            session.emit(Kind.NOTE, f"{signal.Signals(received).name}: tearing the guests down")
            scope.cancel()
            return


async def drive(
    session: Session,
    *,
    breaks: Collection[str] = (),
    break_on_failure: bool = False,
    break_on_start: bool = False,
    interactive: bool = False,
    serial: bool = False,
) -> None:
    """Boot, run what is pending, write the evidence, put the guests down.

    Separate from `run` so it can be driven with something other than a
    command line -- which is what an MCP server does, and what the test
    for the teardown path does.

    The guests' journals stream beside it for the whole drive, a pause
    included: a guest left up after a failure keeps logging, and that is
    often what explains the failure. The control socket exists only when
    a breakpoint was asked for.
    """
    wanted = breaks or break_on_failure or break_on_start or interactive
    control = Controller(session) if wanted else None
    async with anyio.create_task_group() as group:
        group.start_soon(session.follow)
        if control is not None:
            await group.start(control.serve)
            if interactive:
                group.start_soon(repl.serve, control)
        try:
            await _sequence(
                session,
                control,
                set(breaks),
                break_on_failure,
                serial,
                start=break_on_start,
                end=interactive,
            )
        finally:
            group.cancel_scope.cancel()


async def _sequence(
    session: Session,
    control: Controller | None,
    breaks: set[str],
    break_on_failure: bool,
    serial: bool = False,
    *,
    start: bool = False,
    end: bool = False,
) -> None:
    try:
        # Inside the `try`, not before it. `_start_all` lets every guest
        # settle before reporting, so a failed boot can leave others
        # running -- and outside this block nothing would ever stop them.
        await session.boot()
        if start and control is not None:
            await control.pause("before the first phase")
        await _schedule(session, control, breaks, break_on_failure, serial)
        if end and control is not None:
            await control.pause("after the last phase")
    except MachineError as error:
        # A guest that would not boot. Every phase stays pending, so the
        # run fails on its own account below; this only keeps a traceback
        # about sockets out of the way of the message that matters.
        session.emit(Kind.ERROR, f"no guests: {error}", level=Level.ERROR)
        session._replay()
    finally:
        # So `events.jsonl` has the guests' last words before its verdict.
        # Shielded, so a ^C at a breakpoint still writes the evidence
        # and puts the guests down rather than leaving it to
        # `die_with_parent`.
        with anyio.CancelScope(shield=True):
            await session.drain()
            session.write_output()
            await session.teardown()


async def _schedule(
    session: Session,
    control: Controller | None,
    breaks: set[str],
    break_on_failure: bool,
    serial: bool,
) -> None:
    """Start every phase that may start, and again each time one ends.

    Not in waves: a wave waits for its slowest phase before the next
    starts, which gives the gain back on any graph deeper than one level.

    What is ready is asked again every time, never snapshotted. A phase
    that failed marks its dependents skipped *while this runs*, and a list
    taken before would still hold them -- which once ran `check` against a
    cluster that had already failed.

    **A pause waits for quiet.** A breakpoint or a failure stops new
    phases, and the pause begins when the running ones have ended. So
    paused always means nothing is running, and `exec` never races a phase
    on the same guest.
    """
    every = frozenset(machine["name"] for machine in session.spec.machines)
    running: dict[str, PhaseSpec] = {}
    failed: list[str] = []
    paused_before: set[str] = set()
    changed = anyio.Event()

    async def one(phase: PhaseSpec) -> None:
        try:
            if await session.run(phase) is PhaseState.FAILED:
                failed.append(phase.name)
        finally:
            del running[phase.name]
            changed.set()

    async with anyio.create_task_group() as group:
        while True:
            candidates = [p for p in ready(session.spec.phases, session.state) if p.name not in running]
            reason = None
            if control is not None:
                if break_on_failure and failed:
                    reason = f"after {', '.join(failed)} failed"
                elif stop := [p.name for p in candidates if p.name in breaks - paused_before]:
                    reason = f"before {stop[0]}"
            if reason is not None and control is not None:
                if not running:
                    if reason.startswith("before "):
                        paused_before.add(reason.removeprefix("before "))
                    failed.clear()
                    # Asked again afterwards: a phase run by hand while
                    # paused changed what is ready.
                    await control.pause(reason)
                    continue
                todo = []
            else:
                todo = launchable(candidates, running.values(), every)
                if serial:
                    todo = todo[:1] if not running else []
            for phase in todo:
                running[phase.name] = phase
                group.start_soon(one, phase, name=f"phase {phase.name}")
            if not running:
                break
            await changed.wait()
            changed = anyio.Event()
    for name, state in session.state.items():
        if state is PhaseState.PENDING:
            # Only a spec not from Nix can do this: Nix asserts every
            # `after` names a phase.
            session.emit(
                Kind.ERROR,
                f"{name} never ran: something in its `after` never finished",
                level=Level.ERROR,
                phase=name,
            )


async def ctl(args: argparse.Namespace) -> int:
    """One request to a paused run; its output, its value, its error."""
    arg = sys.stdin.read() if args.arg == "-" else args.arg
    if args.op == Op.PYTEST:
        # `vivarium ctl --out o pytest ./tests -- -k x`, as `vivarium run` takes them.
        arg = shlex.join([arg, *args.pytest_args])
    socket = args.out / SOCKET
    try:
        reply = await request(socket, Op(args.op), arg)
    except OSError as error:
        print(f"[vivarium] no run is listening at {socket}: {error}", file=sys.stderr)
        return 1
    if reply.output:
        print(reply.output, end="" if reply.output.endswith("\n") else "\n")
    if reply.result is not None:
        print(reply.result)
    if reply.state:
        for name, state in reply.state.items():
            print(f"{name}\t{state}")
    if reply.error:
        print(reply.error.rstrip(), file=sys.stderr)
    return 0 if reply.ok else 1


async def phases(args: argparse.Namespace) -> int:
    spec = Spec.read(args.spec)
    for phase in spec.phases:
        after = f" after {', '.join(phase.after)}" if phase.after else ""
        print(f"{phase.name}{after}")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = parse(argv)
    if args.command == "ctl":
        raise SystemExit(anyio.run(ctl, args))
    if args.command == "monitor":
        socket = monitor.locate(args.target)
        try:
            raise SystemExit(anyio.run(lambda: monitor.follow(socket, as_json=args.json, quiet=args.quiet)))
        except (FileNotFoundError, ConnectionRefusedError) as error:
            # No socket, or nobody behind it: the server that started the
            # run is gone. What the run wrote is still on the disk.
            print(f"[vivarium] cannot follow {socket}: {error}", file=sys.stderr, flush=True)
            print(f"[vivarium] read {socket.parent / 'events.jsonl'} instead", file=sys.stderr, flush=True)
            raise SystemExit(3) from None
        except KeyboardInterrupt:
            raise SystemExit(130) from None
    try:
        if args.command == "phases":
            raise SystemExit(anyio.run(phases, args))
        if args.keep:
            os.environ[runroot.KEEP] = "1"
        # First, before anything boots or any thread starts.
        namespace.enter(Spec.read(args.spec).unshare)
        args.out.mkdir(parents=True, exist_ok=True)
        try:
            raise SystemExit(anyio.run(run, args))
        finally:
            runroot.remove_own()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (SessionError, SpecError, namespace.NamespaceError) as error:
        print(f"[vivarium] {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
