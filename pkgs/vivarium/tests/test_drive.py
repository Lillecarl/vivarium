"""What the driver does when things go wrong.

These need no guest, because the cases worth pinning are the ones a
working guest never reaches: a boot that fails partway, and a phase that
raises. Both are about whether the guests get stopped afterwards, and a
guest that is not stopped is a UML kernel spinning on a core until
somebody notices.
"""

import contextlib
import io
import tempfile
from pathlib import Path

import anyio
import pytest
from vivarium_runner import MachineError

from vivarium.cli import drive
from vivarium.control import SOCKET, Op, request
from vivarium.phases import PhaseState
from vivarium.spec import PhaseSpec, Spec


class FakeSession:
    """Enough of a session to drive, and a record of what was called."""

    def __init__(
        self, *, boot_error: Exception | None = None, out: Path | None = None
    ) -> None:
        self.spec = Spec(machines=[], phases=[PhaseSpec(name="one", script=Path("x"))])
        self.state: dict[str, PhaseState] = {"one": PhaseState.PENDING}
        # Somewhere real: every drive serves its control socket there.
        self.out = out or Path(tempfile.mkdtemp(prefix="fake-session-"))
        self.vms = None
        self.errors: dict[str, str] = {}
        self.ran: list[str] = []
        self.boot_error = boot_error
        self.booted = False
        self.torn_down = False
        self.wrote = False
        self.said: list[str] = []
        self.following: bool | None = None

    def emit(self, kind, text: str, **_kwargs) -> None:
        self.said.append(f"{kind}:{text}")

    def _replay(self, lines: int = 20) -> None:
        self.said.append("replay")

    def _capture(self, phase: str | None = None, copy: io.StringIO | None = None):
        return contextlib.redirect_stdout(copy) if copy is not None else contextlib.nullcontext()

    async def boot(self) -> None:
        self.booted = True
        if self.boot_error is not None:
            raise self.boot_error

    def pending(self) -> list[PhaseSpec]:
        return [
            phase
            for phase in self.spec.phases
            if self.state[phase.name] is PhaseState.PENDING
        ]

    async def run(self, phase: PhaseSpec) -> PhaseState:
        self.ran.append(phase.name)
        self.state[phase.name] = PhaseState.PASSED
        return PhaseState.PASSED

    def write_output(self) -> None:
        self.wrote = True
        self.said.append("write")

    async def teardown(self) -> None:
        self.torn_down = True

    async def drain(self) -> None:
        self.said.append("drain")

    async def follow(self) -> None:
        self.following = True
        try:
            await anyio.sleep_forever()
        finally:
            self.following = False


@pytest.mark.anyio
class TestTeardownAlwaysHappens:
    async def test_a_good_run_tears_down(self):
        session = FakeSession()
        await drive(session)
        assert session.torn_down

    async def test_a_failed_boot_still_tears_down(self):
        """The one that leaked.

        `boot` lets every guest settle before it reports, so a failure
        can leave others running. With the boot outside the `try` nothing
        ever stopped them, and the process exited leaving kernels behind.
        """
        session = FakeSession(boot_error=MachineError("no"))
        await drive(session)
        assert session.torn_down, "a failed boot left the guests running"

    async def test_a_failed_boot_is_not_a_traceback(self):
        session = FakeSession(boot_error=MachineError("no"))
        await drive(session)
        assert session.state["one"] is PhaseState.PENDING

    async def test_the_evidence_is_written_even_when_the_boot_failed(self):
        """A run that got nowhere is still a run somebody has to read."""
        session = FakeSession(boot_error=MachineError("no"))
        await drive(session)
        assert session.wrote

    async def test_a_failed_boot_replays_the_consoles(self):
        """The only thing that says *why* it would not boot.

        A guest that never answers cannot be asked anything, so its last
        console lines are the whole of the evidence.
        """
        session = FakeSession(boot_error=MachineError("no"))
        await drive(session)
        assert "replay" in session.said


@pytest.mark.anyio
class TestTheJournalFollower:
    async def test_it_stops_when_the_drive_ends(self):
        """`follow` never returns by itself, so a drive that forgot to
        cancel it would never return either."""
        session = FakeSession()
        with anyio.fail_after(5):
            await drive(session)
        assert session.following is False

    async def test_it_stops_when_the_boot_failed(self):
        session = FakeSession(boot_error=MachineError("no"))
        with anyio.fail_after(5):
            await drive(session)
        assert session.following is False

    async def test_the_last_entries_come_before_the_verdict(self):
        """A reader of `events.jsonl` stops at `run_finished`. A guest's
        last words after it are words nobody reads."""
        session = FakeSession()
        await drive(session)
        assert session.said.index("drain") < session.said.index("write")


async def until_paused(socket: Path) -> None:
    with anyio.fail_after(5):
        while True:
            # The file exists from bind(), a moment before listen(): a
            # connect in between is refused. Measured on a loaded host.
            with contextlib.suppress(ConnectionRefusedError):
                if socket.exists() and (await request(socket, Op.STATE)).result == "paused":
                    return
            await anyio.sleep(0.01)


@pytest.mark.anyio
class TestBreakpoints:
    async def test_a_break_pauses_before_the_phase_until_continue(self, tmp_path: Path):
        session = FakeSession(out=tmp_path)
        socket = tmp_path / SOCKET
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: drive(session, breaks=["one"]))
            await until_paused(socket)
            assert session.ran == [], "the phase ran through its breakpoint"
            reply = await request(socket, Op.EXEC, "21 * 2")
            assert reply.result == "42"
            assert (await request(socket, Op.CONTINUE)).ok
        assert session.ran == ["one"]
        assert session.torn_down
        assert not socket.exists(), "the socket outlived the run"

    async def test_a_phase_run_while_paused_is_not_run_again(self, tmp_path: Path):
        session = FakeSession(out=tmp_path)
        socket = tmp_path / SOCKET
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: drive(session, breaks=["one"]))
            await until_paused(socket)
            reply = await request(socket, Op.RUN, "one")
            assert reply.result == "passed"
            await request(socket, Op.CONTINUE)
        assert session.ran == ["one"]

    async def test_a_failure_pauses_with_the_state_intact(self, tmp_path: Path):
        session = FakeSession(out=tmp_path)

        async def fail(phase: PhaseSpec) -> PhaseState:
            session.state[phase.name] = PhaseState.FAILED
            return PhaseState.FAILED

        session.run = fail  # ty: ignore[invalid-assignment]
        socket = tmp_path / SOCKET
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: drive(session, break_on_failure=True))
            await until_paused(socket)
            reply = await request(socket, Op.STATE)
            assert reply.state == {"one": "failed"}
            assert not session.torn_down, "the guests went down before anyone looked"
            await request(socket, Op.CONTINUE)
        assert session.torn_down

    async def test_exec_while_running_and_nothing_that_needs_a_pause(self, tmp_path: Path):
        """`exec` runs beside a phase. `run`, `pytest` and `continue` are
        the scheduler's business, and wait for a pause."""
        session = FakeSession(out=tmp_path)
        started = anyio.Event()
        release = anyio.Event()

        async def slow(phase: PhaseSpec) -> PhaseState:
            started.set()
            await release.wait()
            session.state[phase.name] = PhaseState.PASSED
            return PhaseState.PASSED

        session.run = slow  # ty: ignore[invalid-assignment]
        socket = tmp_path / SOCKET
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: drive(session, break_on_failure=True))
            await started.wait()
            reply = await request(socket, Op.EXEC, "print('beside'); 1")
            assert reply.ok, reply.error
            assert (reply.output, reply.result) == ("beside\n", "1")
            for op, arg in ((Op.RUN, "one"), (Op.PYTEST, str(tmp_path)), (Op.CONTINUE, "")):
                reply = await request(socket, op, arg)
                assert not reply.ok
                assert "only while paused" in (reply.error or "")
            release.set()

    async def test_a_deep_output_directory_still_pauses(self, tmp_path: Path):
        """`<out>/control.sock` is longer than `sun_path` under a deep `--out`.

        Measured: a scratch directory of 95 characters failed the drive with
        `OSError: AF_UNIX path too long`, and no breakpoint was reachable.
        """
        deep = tmp_path / ("d" * 60) / ("e" * 60)
        deep.mkdir(parents=True)
        session = FakeSession(out=deep)
        socket = deep / SOCKET
        assert len(str(socket)) > 108
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: drive(session, breaks=["one"]))
            await until_paused(socket)
            assert (await request(socket, Op.CONTINUE)).ok
        assert session.ran == ["one"]

    @pytest.mark.parametrize("control", [True, False])
    async def test_a_socket_without_a_breakpoint_unless_turned_off(
        self, tmp_path: Path, control: bool
    ):
        session = FakeSession(out=tmp_path)
        seen: list[bool] = []

        async def look(phase: PhaseSpec) -> PhaseState:
            seen.append((tmp_path / SOCKET).exists())
            session.state[phase.name] = PhaseState.PASSED
            return PhaseState.PASSED

        session.run = look  # ty: ignore[invalid-assignment]
        await drive(session, control=control)
        assert seen == [control]
        assert not (tmp_path / SOCKET).exists()


def parallel_session(**phases: list[str]) -> FakeSession:
    """Phases on guests `a` and `b`, each on the guests named, all after `boot`."""
    session = FakeSession()
    session.spec = Spec(
        machines=[{"name": "a"}, {"name": "b"}],
        phases=[
            PhaseSpec(name="boot", script=Path("x")),
            *(
                PhaseSpec(name=name, script=Path("x"), nodes=nodes, after=["boot"])
                for name, nodes in phases.items()
            ),
            PhaseSpec(name="end", script=Path("x"), after=list(phases)),
        ],
    )
    session.state = {phase.name: PhaseState.PENDING for phase in session.spec.phases}
    return session


class Overlap:
    """A `run` that records how many phases were running at once."""

    def __init__(self, session: FakeSession, fail: str | None = None) -> None:
        self.session = session
        self.fail = fail
        self.now: set[str] = set()
        self.most = 0
        self.seen_beside: dict[str, set[str]] = {}

    async def __call__(self, phase: PhaseSpec) -> PhaseState:
        self.now.add(phase.name)
        self.most = max(self.most, len(self.now))
        self.seen_beside[phase.name] = set(self.now) - {phase.name}
        await anyio.sleep(0 if phase.name == self.fail else 0.05)
        self.seen_beside[phase.name] |= self.now - {phase.name}
        self.now.discard(phase.name)
        self.session.ran.append(phase.name)
        state = PhaseState.FAILED if phase.name == self.fail else PhaseState.PASSED
        self.session.state[phase.name] = state
        return state


@pytest.mark.anyio
class TestPhasesAtOnce:
    async def test_disjoint_guests_run_together(self):
        session = parallel_session(left=["a"], right=["b"])
        run = Overlap(session)
        session.run = run  # ty: ignore[invalid-assignment]
        with anyio.fail_after(5):
            await drive(session)
        assert run.seen_beside["left"] == {"right"}
        assert run.most == 2

    async def test_a_dependent_waits_for_every_dependency(self):
        session = parallel_session(left=["a"], right=["b"])
        run = Overlap(session)
        session.run = run  # ty: ignore[invalid-assignment]
        await drive(session)
        assert session.ran[-1] == "end"
        assert run.seen_beside["end"] == set()

    async def test_phases_that_declare_nothing_run_one_at_a_time(self):
        """Every session written before `nodes` existed."""
        session = parallel_session(left=[], right=[])
        run = Overlap(session)
        session.run = run  # ty: ignore[invalid-assignment]
        await drive(session)
        assert run.most == 1

    async def test_a_shared_guest_is_a_queue(self):
        session = parallel_session(left=["a"], right=["a", "b"])
        run = Overlap(session)
        session.run = run  # ty: ignore[invalid-assignment]
        await drive(session)
        assert run.most == 1

    async def test_serial_runs_one_at_a_time(self):
        session = parallel_session(left=["a"], right=["b"])
        run = Overlap(session)
        session.run = run  # ty: ignore[invalid-assignment]
        await drive(session, serial=True)
        assert run.most == 1
        assert session.ran == ["boot", "left", "right", "end"], "not the order Nix sorted"

    async def test_a_failure_pauses_only_once_the_others_have_ended(self, tmp_path: Path):
        """Paused means nothing is running. `exec` against a guest a phase
        is still driving is the race breakpoints exist to avoid."""
        session = parallel_session(left=["a"], right=["b"])
        session.out = tmp_path
        run = Overlap(session, fail="left")
        session.run = run  # ty: ignore[invalid-assignment]
        socket = tmp_path / SOCKET
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: drive(session, break_on_failure=True))
            await until_paused(socket)
            reply = await request(socket, Op.STATE)
            assert reply.state is not None
            assert reply.state["left"] == "failed"
            assert reply.state["right"] == "passed", "paused while `right` still ran"
            assert (await request(socket, Op.CONTINUE)).ok

    async def test_a_breakpoint_holds_back_only_its_phase(self, tmp_path: Path):
        session = parallel_session(left=["a"], right=["b"])
        session.out = tmp_path
        run = Overlap(session)
        session.run = run  # ty: ignore[invalid-assignment]
        socket = tmp_path / SOCKET
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: drive(session, breaks=["end"]))
            await until_paused(socket)
            assert "end" not in session.ran
            assert {"left", "right"} <= set(session.ran)
            await request(socket, Op.CONTINUE)
        assert session.ran[-1] == "end"


@pytest.mark.anyio
class TestPendingIsAskedAgain:
    async def test_a_phase_marked_done_mid_loop_is_not_run(self):
        """The bug the guest test found.

        A list of phases taken once before the loop still holds a phase
        that a failure skipped while the loop was running. Asking again
        each time is what makes the skip mean anything.
        """
        session = FakeSession()
        session.spec = Spec(
            machines=[],
            phases=[
                PhaseSpec(name="a", script=Path("x")),
                PhaseSpec(name="b", script=Path("y"), after=["a"]),
            ],
        )
        session.state = {"a": PhaseState.PENDING, "b": PhaseState.PENDING}
        ran: list[str] = []

        async def run(phase: PhaseSpec) -> PhaseState:
            ran.append(phase.name)
            session.state[phase.name] = PhaseState.FAILED
            # What `Session.run` does on a failure.
            session.state["b"] = PhaseState.SKIPPED
            return PhaseState.FAILED

        session.run = run  # ty: ignore[invalid-assignment]
        await drive(session)
        assert ran == ["a"], f"ran a phase that was skipped mid-loop: {ran}"
