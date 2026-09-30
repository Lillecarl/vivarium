"""Reaching into a live session: breakpoints and injected Python.

Changing a phase in Nix costs an evaluation and a new store path, and
changing a guest costs a new image. So the fast loop does neither: the
run pauses at a breakpoint with the guests up, and Python is sent into
it. `exec` runs code with top-level `await`, `inject` runs a file from
the working tree, `pytest` runs tests from it, `run` runs a declared
phase, `continue` resumes. `exec` and `inject` also reach a run that is
not paused, beside the phases that are running.

    uml run --spec s --out o --break check
    uml ctl --out o exec 'await one.succeed("systemctl --failed")'
    uml ctl --out o inject ./scratch.py
    uml ctl --out o pytest ./tests/chaos -- -k etcd
    uml ctl --out o continue

The operations are the MCP server's tools as well; it is one more
client of `<out>/control.sock`.

**The socket runs arbitrary code as the user who started the run.** It
is created mode 0600 in the output directory.

Every operation is an event, code included, so `events.jsonl` says
what was done to the guests by hand and not only what the phases did.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import json
import os
import shlex
import traceback
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import anyio

from .events import Kind, Level
from .phases import PhaseState
from .session import CasesFailed, load_phase
from .spec import PytestSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from anyio.abc import ByteStream, TaskStatus

    from .session import Session

SOCKET: Final = "control.sock"


class Op(StrEnum):
    STATE = "state"
    EXEC = "exec"
    INJECT = "inject"
    RUN = "run"
    PYTEST = "pytest"
    CONTINUE = "continue"


@dataclass
class Reply:
    ok: bool
    output: str = ""
    result: str | None = None
    error: str | None = None
    state: dict[str, str] | None = None

    def encode(self) -> bytes:
        return json.dumps(asdict(self)).encode() + b"\n"


# ── pure ────────────────────────────────────────────────────────────


def split_last_expression(source: str) -> tuple[ast.Module, ast.Expression | None]:
    """The statements, and the final expression if the code ends in one.

    What a REPL does: `await one.succeed("hostname")` should answer with
    the hostname, not with nothing, so a trailing expression is split off
    and evaluated for its value.
    """
    tree = ast.parse(source, mode="exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body.pop()
        return tree, ast.Expression(body=last.value)  # ty: ignore[unresolved-attribute]
    return tree, None


def parse_request(line: bytes) -> tuple[Op, str]:
    """One request: `{"op": ..., "arg": ...}`. Raises ValueError."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError(f"not JSON: {error}") from None
    if not isinstance(raw, dict):
        raise ValueError("a request is a JSON object")
    try:
        op = Op(raw.get("op"))
    except ValueError:
        raise ValueError(f"no such op {raw.get('op')!r}; have {', '.join(Op)}") from None
    arg = raw.get("arg", "")
    if not isinstance(arg, str):
        raise ValueError("arg is a string")
    if op in (Op.EXEC, Op.INJECT, Op.RUN, Op.PYTEST) and not arg:
        raise ValueError(f"{op} needs an arg")
    return op, arg


# ── the console ─────────────────────────────────────────────────────


class Console:
    """Python run against the session, one namespace for all of it.

    A name bound by one `exec` is there for the next, the way a REPL
    keeps its variables: finding a pid in one call and killing it in the
    next is the ordinary shape of poking at a failure.
    """

    def __init__(
        self,
        namespace: dict[str, Any],
        capture: Callable[[io.StringIO], AbstractContextManager[object]] = contextlib.redirect_stdout,
    ) -> None:
        self.namespace = namespace
        # What sends a print to the reply. The session's, in a run: see
        # `Session._capture`.
        self.capture = capture

    async def execute(self, source: str) -> Reply:
        output = io.StringIO()
        flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        try:
            body, last = split_last_expression(source)
            with self.capture(output):
                await _run(compile(body, "<uml ctl>", "exec", flags=flags), self.namespace)
                result = None
                if last is not None:
                    result = await _run(
                        compile(last, "<uml ctl>", "eval", flags=flags), self.namespace
                    )
        except Exception:  # noqa: BLE001 -- the caller's code; its failure is the reply
            return Reply(ok=False, output=output.getvalue(), error=traceback.format_exc())
        return Reply(
            ok=True,
            output=output.getvalue(),
            result=None if result is None else repr(result),
        )


async def _run(code: Any, namespace: dict[str, Any]) -> Any:
    # With top-level await allowed, `eval` hands back a coroutine when the
    # code awaited anything, and the value itself when it did not.
    value = eval(code, namespace)  # noqa: S307 -- running the caller's code is the feature
    if code.co_flags & inspect.CO_COROUTINE:
        value = await value
    return value


@contextlib.contextmanager
def reachable(path: Path) -> Iterator[str]:
    """A name for the socket at `path` that fits in `sun_path`, 108 bytes.

    `--out` can be deeper than that. `/proc/self/fd/<directory>/<name>` is
    the same file under a short name, and is needed only to bind and to
    connect: unlink and chmod take any length.
    """
    if len(os.fsencode(path)) < 100:
        yield str(path)
        return
    directory = os.open(path.parent, os.O_PATH | os.O_DIRECTORY)
    try:
        yield f"/proc/self/fd/{directory}/{path.name}"
    finally:
        os.close(directory)


async def _receive_line(stream: ByteStream) -> bytes:
    buffer = b""
    while b"\n" not in buffer:
        try:
            buffer += await stream.receive()
        except anyio.EndOfStream:
            break
    return buffer.split(b"\n", 1)[0]


# ── the controller ──────────────────────────────────────────────────


class Controller:
    """Serves the socket for a whole drive; pauses when told to.

    `exec` and `inject` run at any time, beside the running phases: the
    caller chose to touch those guests, and prints go to the reply by
    task, not by swapping `sys.stdout`. `run` and `pytest` wait for a
    pause, because the scheduler owns the phases and pytest is not
    reentrant.
    """

    ANY_TIME: Final = frozenset({Op.STATE, Op.EXEC, Op.INJECT})

    def __init__(self, session: Session) -> None:
        self.session = session
        self.path = session.out / SOCKET
        self.console = Console(
            self._namespace(), lambda copy: session._capture("exec", copy)
        )
        self._resume: anyio.Event | None = None
        self._continuing = False

    def _namespace(self) -> dict[str, Any]:
        return {"session": self.session, "anyio": anyio}

    @property
    def paused(self) -> bool:
        return self._resume is not None

    async def serve(self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        """Listen until cancelled. Run beside the drive, like `follow`.

        Started with `task_group.start`, so the socket exists before the
        first guest boots: a client that sees the pause message can
        always connect.
        """
        self.session.out.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        with reachable(self.path) as name:
            listener = await anyio.create_unix_listener(name)
        os.chmod(self.path, 0o600)
        task_status.started()
        try:
            await listener.serve(self._client)
        finally:
            with anyio.CancelScope(shield=True):
                await listener.aclose()
            self.path.unlink(missing_ok=True)

    async def pause(self, reason: str) -> None:
        """Hold the drive here until a client says `continue`."""
        self._resume = anyio.Event()
        self._bind_guests()
        self.session.emit(
            Kind.NOTE,
            f"paused {reason}; `vivarium ctl --out {self.session.out} continue` resumes",
            level=Level.ERROR,
            reason=reason,
            socket=str(self.path),
        )
        try:
            await self._resume.wait()
        finally:
            self._resume = None
        self.session.emit(Kind.NOTE, "resumed")

    def _bind_guests(self) -> None:
        """`vms` and each guest by name, once the guests exist."""
        if self.session.vms is not None:
            self.console.namespace.setdefault("vms", self.session.vms)
            for name, vm in self.session.vms.items():
                self.console.namespace.setdefault(name, vm)

    async def _client(self, stream: ByteStream) -> None:
        async with stream:
            line = await _receive_line(stream)
            if not line:
                return
            reply = await self.handle(line)
            await stream.send(reply.encode())
        # After the reply, not in `handle`: resuming lets the drive
        # finish, and a finished drive cancels this server -- measured,
        # the client of a `continue` got no reply at all.
        if self._continuing and self._resume is not None:
            self._continuing = False
            self._resume.set()

    async def handle(self, line: bytes) -> Reply:
        try:
            op, arg = parse_request(line)
        except ValueError as error:
            return Reply(ok=False, error=str(error))
        self.session.emit(
            Kind.NOTE, f"control: {op} {arg}".rstrip(), level=Level.DETAIL, op=str(op), arg=arg
        )
        if op is Op.STATE:
            return Reply(ok=True, state=self._state(), result="paused" if self.paused else "running")
        if op not in self.ANY_TIME and not self.paused:
            return Reply(ok=False, error=f"{op} only while paused; the run is running")
        if op is Op.INJECT and self.session.vms is None:
            return Reply(ok=False, error="no guests are up yet")
        self._bind_guests()
        if op is Op.CONTINUE:
            self._continuing = True
            return Reply(ok=True)
        if op is Op.EXEC:
            return self._record("exec", await self.console.execute(arg))
        if op is Op.INJECT:
            return self._record(f"inject:{Path(arg).name}", await self._inject(arg))
        if op is Op.PYTEST:
            return await self._pytest(arg)
        return await self._run(arg)

    def _record(self, source: str, reply: Reply) -> Reply:
        """What injected code raised, into the event stream as well as
        the reply: the record of a run includes what was done to it by
        hand. Its prints are events already, through `Session._capture`."""
        if reply.error:
            self.session.emit(Kind.ERROR, reply.error.rstrip(), level=Level.ERROR, phase=source)
        return reply

    async def _inject(self, arg: str) -> Reply:
        """A file's `test(vms)`, read from where it is, not from the store.

        Imported fresh each time, so an edit is picked up by sending it
        again. Its outcome is the reply; the run's verdict is untouched.
        """
        output = io.StringIO()
        try:
            test = load_phase(Path(arg).expanduser().resolve())
            with self.session._capture(f"inject:{Path(arg).name}", output):
                await test(self.session.vms)
        except Exception:  # noqa: BLE001 -- the injected code's failure is the reply
            return Reply(ok=False, output=output.getvalue(), error=traceback.format_exc())
        return Reply(ok=True, output=output.getvalue())

    async def _pytest(self, arg: str) -> Reply:
        """pytest from the working tree against the paused guests.

        What `inject` is for a script: the loop for a pytest phase is
        editing a test and sending it again, against guests a long setup
        already built. `arg` is `PATH [PYTEST ARGS...]`, shell-quoted. The
        cases are events marked `by_hand`, and the run's verdict and
        junit.xml leave them out.
        """
        path, *args = shlex.split(arg)
        tests = Path(path).expanduser().resolve()
        if not tests.exists():
            return Reply(ok=False, error=f"no tests at {tests}")
        if self.session.vms is None:
            return Reply(ok=False, error="no guests are up")
        name = f"pytest:{tests.name}"
        spec = PytestSpec(tests=tests, args=args)
        try:
            summary = await self.session._pytest(name, spec, self.session.vms, by_hand=True)
        except CasesFailed as error:
            return Reply(ok=False, result="failed", error=str(error))
        except Exception:  # noqa: BLE001 -- the tests' failure to load is the reply
            return self._record(name, Reply(ok=False, error=traceback.format_exc()))
        return Reply(ok=True, result=summary)

    async def _run(self, name: str) -> Reply:
        phase = next((p for p in self.session.spec.phases if p.name == name), None)
        if phase is None:
            return Reply(ok=False, error=f"no phase {name!r}")
        state = await self.session.run(phase)
        return Reply(
            ok=state is PhaseState.PASSED,
            result=str(state),
            error=self.session.errors.get(name),
            state=self._state(),
        )

    def _state(self) -> dict[str, str]:
        return {name: str(state) for name, state in self.session.state.items()}

    # ── what `vivarium run --interactive` calls, in the same process ─────

    async def execute(self, source: str) -> Reply:
        """`exec`, from the REPL rather than the socket."""
        self._bind_guests()
        return self._record("exec", await self.console.execute(source))

    async def run_phase(self, name: str) -> str:
        """Run one declared phase; its state. Called from REPL code, which
        runs while the drive is paused."""
        reply = await self._run(name)
        if reply.error:
            print(reply.error)
        return reply.result or ""

    def resume(self) -> None:
        """Let the remaining phases run, once the current input returns."""
        if self._resume is not None:
            self._resume.set()


# ── the client ──────────────────────────────────────────────────────


async def request(socket: Path, op: Op, arg: str = "") -> Reply:
    """One request, one reply. What `vivarium ctl` and a test both use."""
    with reachable(socket) as name:
        connected = await anyio.connect_unix(name)
    async with connected as stream:
        await stream.send(json.dumps({"op": str(op), "arg": arg}).encode() + b"\n")
        line = await _receive_line(stream)
    if not line:
        return Reply(ok=False, error="the run closed the connection without a reply")
    return Reply(**json.loads(line))
