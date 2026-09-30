"""Turns a Nix-generated spec into booted, connected guests.

A test script is just a coroutine over the machines::

    from uml_runner import run_test

    async def test(vms):
        await vms.server.succeed(f"ping -c2 {vms.client.ip}")

    run_test(test)

Everything else -- parsing ``--spec``, wiring the L2 segments, booting in
parallel, tearing down whatever managed to start -- happens here, so
tests contain nothing but the thing they are testing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable

from .forward import ForwardError
from .machine import Machine, MachineError, MachineSpec, Toolchain
from .net import build_lans
from . import report


ARTIFACTS_ENV = "UML_TEST_ARTIFACTS"
"""``mkTest`` sets it to a directory inside the attempt derivation's
output, which is why that derivation must not fail -- see lib.nix."""


class Machines(dict[str, Machine]):
    """The run's machines by name, also reachable as attributes.

    Parameterised because a bare ``dict`` makes ``values()`` and
    ``items()`` Unknown, and a caller's pyright then checks nothing.
    """

    settings: dict
    """Whatever the spec's ``settings`` held -- values a test needs that
    only Nix knows, such as a package version or an image tag.  Empty
    unless ``mkTest`` was given some."""

    artifacts: Path
    """Where this run's evidence goes.  Each guest sees its own
    subdirectory as ``/artifacts``, so a test collects a file by writing
    it in the guest and nothing is copied afterwards."""

    env: dict[str, str]
    """The impurities this test declared, read from the host environment.

    One entry per name in ``mkTest``'s ``impurities``, and every declared
    name is present -- unset reads as ``""``, so a test branches on the
    value and never on a missing key.  Empty inside a build sandbox,
    which has no environment to read; that is what makes the test's
    default the thing CI runs."""

    knobs: dict[str, str]
    """What this run was told from outside, by name.

    Declared in Nix and resolved there, so a knob can change what is
    *built* as well as what a phase does. Every declared name is present;
    one whose variable is unset carries its declared default, which is
    what a sandboxed check always gets. Set by ``mkTest``; empty under
    the old ``run_test`` harness, which has ``env`` instead."""

    shared: dict[str, Any]
    """One phase's findings for a later phase of the same run.

    The phases are separate modules, so a value one computes -- a
    process census taken before the suites -- reaches the phase that
    compares against it only through here. Set by ``mkTest``."""

    phase: str | None
    """The phase running now. One script can serve several phases, told
    apart by name: ``mkTest`` phases generated from one list in Nix.
    ``None`` under the old ``run_test`` harness."""

    argv: list[str]
    """What was left on the command line after ``--spec``.

    ``nix run --file . <test>.run -- -k mytest`` reaches a script here.
    The harness parses none of it."""

    def __getattr__(self, name: str) -> Machine:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(
                f"no machine {name!r} in this test; have {', '.join(self)}"
            ) from None


@asynccontextmanager
async def machines(spec: dict):
    """Boot every machine in *spec*, yield them, then tear them down."""
    tools = Toolchain.from_json(spec)
    specs = [MachineSpec.from_json(m) for m in spec["machines"]]

    segments: dict[str, list[str]] = defaultdict(list)
    for s in specs:
        if s.network:
            segments[s.network].append(s.name)
    lans = build_lans(segments)
    lan_fd = {name: fd for lan in lans for name, fd in lan.fds.items()}

    artifacts = _artifacts_dir()
    vms = Machines(
        (
            s.name,
            Machine(
                s,
                tools,
                lan_fd=lan_fd.get(s.name),
                artifacts=_guest_artifacts(artifacts, s.name),
            ),
        )
        for s in specs
    )
    vms.settings = spec.get("settings", {})
    vms.artifacts = artifacts
    vms.env = _impurities(spec.get("impurities", []))
    vms.argv = list(spec.get("argv", []))
    # Declared, so set: an attribute read before it was assigned is an
    # AttributeError, not an empty value.
    vms.shared = {}
    vms.phase = None
    # Serially, and before anything spawns: picking a free host address
    # means binding a port and letting go of it again, so two guests
    # doing it at once would both be told the same address is free.
    taken: set[str] = set()
    for machine in vms.values():
        machine.resolve_forward(taken)
    try:
        for lan in lans:
            lan.start()
        print(f"[test] booting {', '.join(vms)} ...", flush=True)
        # Let every machine settle even if one fails, so that a guest is
        # never left half-spawned for the teardown below to trip over.
        results = await asyncio.gather(
            *(m.start() for m in vms.values()), return_exceptions=True
        )
        for lan in lans:
            lan.detach()
        for error in results:
            if isinstance(error, BaseException):
                raise error
        yield vms
    finally:
        print("[test] shutting down ...", flush=True)
        await asyncio.gather(
            *(m.shutdown() for m in vms.values()), return_exceptions=True
        )
        for lan in lans:
            lan.close()


def _artifacts_dir() -> Path:
    """Where this run writes what it wants to keep.

    Always somewhere, never nowhere: a guest mounts it unconditionally, so
    a run with no directory would boot differently from a check.  Never
    cleaned up either -- evidence deleted at the end of a run is evidence
    nobody read.
    """
    where = os.environ.get(ARTIFACTS_ENV)
    path = Path(where) if where else Path(tempfile.mkdtemp(prefix="uml-artifacts-"))
    path.mkdir(parents=True, exist_ok=True)
    # Printed at the start, because a run that is killed reaches no end.
    print(f"[test] artifacts in {path}", flush=True)
    return path


def _impurities(names: list[str]) -> dict[str, str]:
    """The declared names, read from the host environment, and announced.

    Nix carries the *names*.  A value never enters the spec, so it never
    enters a store path and the derivation hash does not move with it --
    which is what lets a sandboxed build stay pure while a run by hand is
    steerable.  The sandbox has no environment, so every value is empty
    there and the test takes its own default.

    Printed, always, because that default is the failure this cannot have:
    a misspelled variable changes nothing, the run does the whole suite
    instead of the one case asked for, and nothing says why.
    """
    env = {name: os.environ.get(name, "") for name in names}
    for name, value in env.items():
        said = repr(value) if value else "unset"
        print(f"[test] impurity {name}={said}", flush=True)
    return env


def _guest_artifacts(root: Path, name: str) -> Path:
    """One guest's own subdirectory, made before it boots.

    Per guest, because three nodes writing ``pytest.log`` into one
    directory is two lost files.  Made here and not in the guest: hostfs
    and virtiofs both serve a directory that exists.
    """
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_spec(argv: list[str] | None = None) -> dict:
    """Read the test spec named by ``--spec`` on the command line.

    ``parse_known_args``, and the remainder lands under ``argv`` in the
    returned spec -- so what the run was told and what Nix wrote arrive
    as one thing.  Strict parsing here rejected every flag a test wanted,
    with an exit 2 the caller had no way to read as its own.
    """
    parser = argparse.ArgumentParser(description="Run a UML test")
    parser.add_argument(
        "--spec", type=Path, required=True, help="JSON test spec from Nix"
    )
    args, rest = parser.parse_known_args(argv)
    spec = json.loads(args.spec.read_text())
    spec["argv"] = rest
    return spec


def run_test(test: Callable[[Machines], Awaitable[None]]) -> None:
    """Boot the spec's machines, run *test* against them, and exit."""

    async def main() -> None:
        _unwind_on_signal()
        async with machines(load_spec()) as vms:
            await test(vms)

    # Written whichever way the run ends, and named by `$UML_TEST_REPORT`.
    # A run that timed out is the one whose timings are worth reading, so
    # the failing path must not be the one that skips this. See report.py.
    try:
        asyncio.run(main())
    except (MachineError, ForwardError) as error:
        _record(False, str(error))
        print(f"[test] FAILED: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    except asyncio.CancelledError:
        # A signal got here through `_unwind_on_signal`, so the guests are
        # already down. Exit the way a shell reads an interrupt, and
        # without a traceback that says nothing.
        _record(False, "interrupted by a signal")
        print("[test] interrupted; the guests are down", file=sys.stderr, flush=True)
        raise SystemExit(130) from None
    except BaseException as error:
        _record(False, f"{type(error).__name__}: {error}")
        raise
    _record(True)
    print("[test] passed", flush=True)


def _unwind_on_signal() -> None:
    """Turn a terminating signal into a cancellation.

    Python's default for SIGTERM and SIGHUP is to die where it stands, so
    no ``finally`` runs and every guest is orphaned.  That is how a run
    stopped by hand, by a CI timeout, or by a parent shell leaves a UML
    kernel spinning on a core for days.

    Cancelling the task instead unwinds :func:`machines`, whose teardown
    kills each guest's process group.  SIGKILL still cannot be caught --
    :func:`uml_runner.backend.die_with_parent` is what covers that.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def stop(signame: str) -> None:
        print(f"[test] {signame}, shutting the guests down ...", flush=True)
        if task is not None:
            task.cancel()

    for signame in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, signame)
        try:
            loop.add_signal_handler(sig, stop, signame)
        except (NotImplementedError, RuntimeError):
            # No signal handlers off the main thread; the teardown in
            # `machines` still runs for every ordinary exit.
            pass


def _record(passed: bool, error: str | None = None) -> None:
    # Printed whether or not a file is written: `nix run` names no file,
    # and a CI job that boots a guest should still say where its minutes
    # went. See report.py.
    print(report.RUN.summary(), flush=True)
    where = report.RUN.write_if_asked(passed, error)
    if where:
        print(f"[test] timings in {where}", flush=True)
