"""`vivarium-mcp`: runs as tools, and what happens to them as channel events.

An MCP server over stdio. `start` launches a run as a child process with
`--break-on-failure` on; the other tools are the control socket's
operations and a query over `events.jsonl`. When the run pauses, fails a
phase or finishes, a `notifications/claude/channel` event is pushed, so
Claude Code hears about it without polling:

    <channel source="vivarium" run="vivarium-pytest-x1" event="paused" reason="after cases failed">...

**The run is a child process, never this one.** MCP's stdio transport
is this process's stdout, and a session writes to stdout -- the terminal
sink, and a phase's captured `print`. One stray line there corrupts the
protocol. So a run is `vivarium-eval run ...` (or `python -m vivarium.cli run`
for a spec) with its output in `<out>/terminal.log`, and this process
is one more client of `<out>/control.sock`, as `vivarium ctl` is.

Channels are a Claude Code research preview. The server declares the
`claude/channel` capability, and until it is on the allowlist Claude
Code takes its events only with
`claude --dangerously-load-development-channels server:uml`. Without it
the tools work and the events are dropped, so `state` and `events`
still answer.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification
from vivarium.control import SOCKET, Op, Reply, reachable, request
from vivarium.journal import Tail
from vivarium.monitor import LIVE, SOCKET as MONITOR_SOCKET, TERMINAL, paused_now

from .cli import split_attr

if TYPE_CHECKING:
    from anyio.abc import Process, SocketStream, TaskGroup
    from anyio.streams.memory import MemoryObjectSendStream

CHANNEL: Final = "notifications/claude/channel"

INSTRUCTIONS: Final = """\
Runs NixOS guests under vivarium and lets you reach into them.

`start` launches a run in the background and returns its id at once; it
pauses on the first failing phase with the guests still up. `exec` runs
Python against the live guests (top-level await; `vms`, `session` and
each guest by name are in scope, and names persist between calls), and
`inject` runs a local file's `async def test(vms)`; both work while
phases run, not only while paused. While paused, `run_pytest` runs local
pytest tests, `run_phase` runs a declared phase, and `resume`
continues. `inject` and `run_pytest` read the file each time, so the loop
for a failing test is: edit it, send it again, against the same guests.
`events` queries the run's event stream: filter by kind (journal, case,
phase_finished, rpc, output, error), machine, unit, phase or case.

Events arrive as <channel source="vivarium" run="..." event="progress|paused|resumed|failed|finished|exited" ...>.
A `progress` or `resumed` event marks the run moving on; say one line
about it so the person watching sees the run move, and do nothing else.
Without channels, run the `monitor` command that `start` returns in a
Monitor: it prints pauses, failures and the verdict, one line each, and
exits with the verdict (0 passed, 1 failed, 2 exited without one).
`monitor_all` prints every event, progress included.
If your harness wakes you only when a background command exits, run
`monitor_pause` in the background instead: it also exits 4 at a pause.
Run it again after each `resume`.
On `paused`, look with `events` and `exec` before you `resume` -- the
guests go down when the run ends. `stop` ends a run early and still
tears the guests down.
"""


# ── pure ────────────────────────────────────────────────────────────


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


def select(
    lines: list[str],
    *,
    kind: str | None = None,
    machine: str | None = None,
    unit: str | None = None,
    phase: str | None = None,
    case: str | None = None,
    contains: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The last `limit` events that match every filter given."""
    found: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        data = event.get("data") or {}
        if kind is not None and event.get("kind") != kind:
            continue
        if machine is not None and event.get("machine") != machine:
            continue
        if phase is not None and event.get("phase") != phase:
            continue
        if unit is not None and data.get("unit") != unit:
            continue
        if case is not None and case not in str(data.get("case", "")):
            continue
        if contains is not None and contains not in str(event.get("text", "")):
            continue
        found.append(event)
    return found[-limit:] if limit > 0 else found


def run_argv(
    *,
    out: Path,
    attr: str | None,
    spec: str | None,
    file: str,
    breaks: list[str],
    break_on_failure: bool,
    only: list[str],
    offline: bool,
    pytest_args: list[str],
    runner: Path | None = None,
    kernel: str | None = None,
) -> list[str]:
    """The child's command line. By attribute through `vivarium-eval`, which
    evaluates first; by spec straight to the `vivarium` the spec names."""
    if (attr is None) == (spec is None):
        raise ValueError("give exactly one of attr and spec")
    bin_dir = Path(sys.executable).parent
    if attr is not None:
        split_attr(attr)
        head = [str(bin_dir / "vivarium-eval"), "run", attr, "--file", file]
    elif runner is not None:
        # The spec's own `vivarium`, which knows every field in it.
        head = [str(runner / "bin" / "vivarium"), "run", "--spec", str(spec)]
    else:
        head = [sys.executable, "-m", "vivarium.cli", "run", "--spec", str(spec)]
    argv = [*head, "--out", str(out)]
    for name in breaks:
        argv += ["--break", name]
    for name in only:
        argv += ["--only", name]
    if break_on_failure:
        argv.append("--break-on-failure")
    if offline:
        argv.append("--offline")
    if kernel is not None:
        argv += ["--kernel", kernel]
    if pytest_args:
        argv += ["--", *pytest_args]
    return argv


# ── runs ────────────────────────────────────────────────────────────


@dataclass
class Run:
    id: str
    out: Path
    process: Process
    finished: bool = False
    tail: Tail = field(init=False)
    backlog: list[dict[str, str]] = field(default_factory=list)
    """Every event pushed for this run, for a monitor that connects late."""
    watchers: list[MemoryObjectSendStream[dict[str, str]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.tail = Tail(self.out / "events.jsonl")

    @property
    def socket(self) -> Path:
        return self.out / SOCKET


class Runs:
    """Every run this server started, and the push channel for them."""

    def __init__(self, group: TaskGroup, push: MemoryObjectSendStream[SessionMessage]) -> None:
        self.group = group
        self.push_stream = push
        self.runs: dict[str, Run] = {}

    async def push(self, content: str, meta: dict[str, str]) -> None:
        """To the channel, and to every monitor of the run."""
        event = {**meta, "text": content}
        run = self.runs.get(meta["run"])
        if run is not None:
            run.backlog.append(event)
            for watcher in run.watchers:
                watcher.send_nowait(event)
        notification = JSONRPCNotification(
            jsonrpc="2.0", method=CHANNEL, params={"content": content, "meta": meta}
        )
        await self.push_stream.send(SessionMessage(message=JSONRPCMessage(notification)))

    async def start(self, argv: list[str], out: Path, env: dict[str, str]) -> Run:
        log = (out / "terminal.log").open("wb")
        process = await anyio.open_process(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, **env},
        )
        log.close()
        run = Run(id=out.name, out=out, process=process)
        self.runs[run.id] = run
        # Bound before `start` answers, so a monitor started on the reply
        # finds the socket.
        path = out / MONITOR_SOCKET
        with reachable(path) as name:
            listener = await anyio.create_unix_listener(name)
        path.chmod(0o600)
        self.group.start_soon(listener.serve, lambda stream: _monitor(run, stream))
        self.group.start_soon(self._watch, run)
        return run

    async def _watch(self, run: Run) -> None:
        """Follow the run's events and push what matters, until it exits."""
        while True:
            for line in await run.tail.read():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("kind") == "run_finished":
                    run.finished = True
                pushed = channel_event(event, run.id)
                if pushed is not None:
                    await self.push(*pushed)
            if run.process.returncode is not None:
                break
            await anyio.sleep(0.25)
        if not run.finished:
            # Died before a verdict: an evaluation error, a crash. The
            # reason is at the end of what it printed.
            tail = why_it_exited(_read(run.out / "terminal.log"))
            await self.push(
                f"run exited {run.process.returncode} without a verdict:\n{tail}",
                {"run": run.id, "event": "exited"},
            )

    def get(self, run: str) -> Run:
        found = self.runs.get(run)
        if found is None:
            raise ValueError(f"no run {run!r}; have {', '.join(self.runs) or 'none'}")
        return found

    async def stop_all(self) -> None:
        for run in self.runs.values():
            await _stop(run)


async def _monitor(run: Run, stream: SocketStream) -> None:
    """One `vivarium monitor`: the backlog, `LIVE`, then each event, until the verdict."""
    send, receive = anyio.create_memory_object_stream[dict[str, str]](math.inf)
    # No await between the copy and the append, so no event falls between.
    backlog = list(run.backlog)
    run.watchers.append(send)
    try:
        async with stream, receive:
            for event in backlog:
                await stream.send(json.dumps(event).encode() + b"\n")
                if event.get("event") in TERMINAL:
                    return
            await stream.send(json.dumps(LIVE).encode() + b"\n")
            async for event in receive:
                await stream.send(json.dumps(event).encode() + b"\n")
                if event.get("event") in TERMINAL:
                    return
    except (anyio.BrokenResourceError, anyio.ClosedResourceError):
        # The monitor went away first; that ends only its own stream.
        return
    finally:
        run.watchers.remove(send)
        send.close()


async def _stop(run: Run) -> None:
    """SIGINT, which the run answers with its shielded teardown."""
    if run.process.returncode is not None:
        return
    run.process.send_signal(signal.SIGINT)
    with anyio.move_on_after(60):
        await run.process.wait()
        return
    run.process.kill()


def _spec(spec: Path) -> dict[str, Any]:
    """The fields of a spec this server reads: its name, since the file
    name is a store hash, and the `vivarium` that knows the rest."""
    try:
        return json.loads(spec.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def why_it_exited(output: str, lines: int = 15) -> str:
    """The part of a dead run's output that says why.

    An evaluation failure is printed point first by `vivarium-eval`, with the
    Nix trace after it, so its opening lines are the ones to show; the
    last lines would be the trace. Anything else died at the end.
    """
    text = output.splitlines()
    for index, line in enumerate(text):
        if line.startswith("[vivarium] evaluation failed:"):
            return "\n".join(text[index : index + 4])
    return "\n".join(text[-lines:])


def _reply(reply: Reply) -> dict[str, Any]:
    return {key: value for key, value in vars(reply).items() if value not in (None, "")}


# ── the server ──────────────────────────────────────────────────────


def build(runs_holder: list[Runs]) -> FastMCP:
    """The tools, over whichever `Runs` the running server put in the
    holder -- it needs the task group and the write stream, which exist
    only once `serve` is running."""
    server = FastMCP("vivarium", instructions=INSTRUCTIONS)

    def runs() -> Runs:
        return runs_holder[0]

    @server.tool()
    async def start(
        attr: str | None = None,
        spec: str | None = None,
        file: str = ".",
        breaks: list[str] | None = None,
        break_on_failure: bool = True,
        only: list[str] | None = None,
        offline: bool = False,
        pytest_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        kernel: str | None = None,
    ) -> dict[str, str]:
        """Start a run in the background; returns its id at once.

        `attr` is evaluated from `file` (a directory means its default.nix),
        or give `spec`, a spec path. `breaks` pauses before those phases;
        `break_on_failure` pauses on a failed phase. `env` is added to the
        run's environment, which the evaluation reads: a knob's variable,
        or `UMBRELLA_DEV` to build against a working copy.
        `kernel` boots a kernel from a working tree instead of Nix's:
        `linux` from a UML build, or a bzImage with virtio built in.
        Events arrive on the vivarium channel. `monitor` in the reply is a
        command that prints the pauses, failures and verdict, one line
        each; `monitor_all` prints every event; `monitor_pause` is
        `monitor` that also exits 4 at a pause. `state` and `events`
        answer meanwhile."""
        written = _spec(Path(str(spec))) if spec is not None else {}
        name = (attr or str(written.get("name", "run"))).replace(".", "-")
        out = Path(tempfile.mkdtemp(prefix=f"vivarium-{name}-"))
        argv = run_argv(
            out=out,
            attr=attr,
            spec=spec,
            file=os.path.abspath(file),
            breaks=breaks or [],
            break_on_failure=break_on_failure,
            only=only or [],
            offline=offline,
            pytest_args=pytest_args or [],
            runner=Path(written["vivarium"]) if written.get("vivarium") else None,
            kernel=os.path.abspath(kernel) if kernel else None,
        )
        run = await runs().start(argv, out, env or {})
        every = [sys.executable, "-m", "vivarium.cli", "monitor", str(out)]
        return {
            "run": run.id,
            "out": str(out),
            "monitor": shlex.join([*every, "--quiet"]),
            "monitor_all": shlex.join(every),
            "monitor_pause": shlex.join([*every, "--quiet", "--until-pause"]),
        }

    @server.tool()
    async def state(run: str) -> dict[str, Any]:
        """Each phase's state, and whether the run is paused, running or gone."""
        found = runs().get(run)
        if found.process.returncode is not None:
            return {"run": run, "status": f"exited {found.process.returncode}"}
        if not found.socket.exists():
            return {"run": run, "status": "starting or running without a control socket"}
        return _reply(await request(found.socket, Op.STATE))

    @server.tool(name="exec")
    async def exec_(run: str, code: str) -> dict[str, Any]:
        """Run Python in the run, paused or running. Top-level await;
        `vms`, `session` and each guest by name are in scope; names
        persist between calls. The last expression's repr is `result`."""
        return _reply(await request(runs().get(run).socket, Op.EXEC, code))

    @server.tool()
    async def inject(run: str, path: str) -> dict[str, Any]:
        """Run a local file's `async def test(vms)` in the run, paused or
        running, read fresh from disk, so an edit takes effect by
        injecting it again."""
        return _reply(await request(runs().get(run).socket, Op.INJECT, os.path.abspath(path)))

    @server.tool()
    async def run_pytest(run: str, path: str, args: list[str] | None = None) -> dict[str, Any]:
        """Run pytest on a local test file or directory against the paused
        guests, read fresh from disk: edit a test and run it again without
        rebuilding the setup. `args` go to pytest (`["-k", "etcd"]`). The
        cases are events marked by_hand and do not count toward the run's
        verdict."""
        arg = shlex.join([os.path.abspath(path), *(args or [])])
        return _reply(await request(runs().get(run).socket, Op.PYTEST, arg))

    @server.tool()
    async def run_phase(run: str, phase: str) -> dict[str, Any]:
        """Run a declared phase now, in the paused run."""
        return _reply(await request(runs().get(run).socket, Op.RUN, phase))

    @server.tool()
    async def resume(run: str) -> dict[str, Any]:
        """Continue a paused run."""
        found = runs().get(run)
        reply = _reply(await request(found.socket, Op.CONTINUE))
        # Until the pause is resolved in the backlog: a `monitor_pause`
        # armed on this reply would otherwise replay the pause and exit.
        with anyio.move_on_after(10):
            while paused_now(found.backlog) and found.process.returncode is None:
                await anyio.sleep(0.05)
        return reply

    @server.tool()
    async def stop(run: str) -> dict[str, Any]:
        """End a run now. The evidence is written and the guests go down."""
        found = runs().get(run)
        await _stop(found)
        return {"run": run, "status": f"exited {found.process.returncode}"}

    @server.tool()
    async def events(
        run: str,
        kind: str | None = None,
        machine: str | None = None,
        unit: str | None = None,
        phase: str | None = None,
        case: str | None = None,
        contains: str | None = None,
        limit: int = 50,
    ) -> dict[str, list[dict[str, Any]]]:
        """The run's events, newest last, filtered. `kind="journal", machine="cp",
        unit="kubelet.service"` is one service on one guest; `kind="case"` is
        each pytest test; `kind="error"` is every traceback."""
        path = runs().get(run).out / "events.jsonl"
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return {"events": []}
        return {"events": select(
            lines,
            kind=kind,
            machine=machine,
            unit=unit,
            phase=phase,
            case=case,
            contains=contains,
            limit=limit,
        )}

    # Every tool returns an object. FastMCP wraps any other return type
    # as `{"result": ...}`, and `exec`'s own reply has a `result` key, so
    # a client could not tell the two apart.
    @server.tool(name="runs")
    async def list_runs() -> dict[str, list[dict[str, str]]]:
        """Every run this server started."""
        return {"runs": [
            {
                "run": found.id,
                "out": str(found.out),
                "status": "running"
                if found.process.returncode is None
                else f"exited {found.process.returncode}",
            }
            for found in runs().runs.values()
        ]}

    return server


async def serve() -> None:
    holder: list[Runs] = []
    server = build(holder)
    lowlevel = server._mcp_server  # noqa: SLF001 -- FastMCP has no way to declare an experimental capability
    options = lowlevel.create_initialization_options(
        experimental_capabilities={"claude/channel": {}}
    )
    async with stdio_server() as (read_stream, write_stream), anyio.create_task_group() as group:
        runs = Runs(group, write_stream.clone())
        holder.append(runs)
        try:
            await lowlevel.run(read_stream, write_stream, options)
        finally:
            with anyio.CancelScope(shield=True):
                await runs.stop_all()
            group.cancel_scope.cancel()
            # The stdio writer runs until every clone of its stream is
            # closed, so an open one kept the server alive after its
            # client left -- measured, it never exited.
            await runs.push_stream.aclose()


def main() -> None:
    anyio.run(serve)


if __name__ == "__main__":
    main()
