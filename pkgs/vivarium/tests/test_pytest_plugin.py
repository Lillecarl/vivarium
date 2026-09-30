"""pytest as a phase, without a guest.

The real `Session._pytest`: pytest in a worker thread, the machine on the
session's loop, every call across the portal. Only the machine is fake,
and it is fake in the one way that matters -- it only works on the loop
that owns it, the way a real one does.
"""

import asyncio
import json
import shlex
import sys
from pathlib import Path
from textwrap import dedent

import anyio
import pytest
from vivarium_runner import Machines

from vivarium.control import Controller
from vivarium.events import Event, Kind, junit
from vivarium.phases import PhaseState
from vivarium.session import CasesFailed, Session
from vivarium.spec import PhaseSpec, PytestSpec, Spec


class Machine:
    """Answers only on the loop it was made on."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.loop = asyncio.get_running_loop()
        self.said: list[str] = []
        # What a failed phase replays; a real Machine keeps its console here.
        self._history: list[str] = []

    async def succeed(self, command: str) -> str:
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("called from a loop that does not own this machine")
        self.said.append(command)
        return self.name


def fake_vms(*names: str) -> Machines:
    """What `Session.boot` would have set, on fake machines."""
    vms = Machines((name, Machine(name)) for name in names)  # ty: ignore[invalid-argument-type]
    vms.settings = {}
    vms.artifacts = Path("/nonexistent")
    vms.knobs = {}
    vms.shared = {}
    vms.phase = None
    return vms


class Collect:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass

    def of(self, kind: Kind) -> list[Event]:
        return [e for e in self.events if e.kind is kind]


def session_for(tmp_path: Path, sink: Collect, *pytest_args: str) -> Session:
    session = Session(
        Spec(machines=[], phases=[]),
        tmp_path / "out",
        sink=sink,
        pytest_args=list(pytest_args),
    )
    session.vms = fake_vms("one")
    return session


def write_tests(tmp_path: Path, source: str) -> Path:
    directory = tmp_path / "guest"
    directory.mkdir()
    (directory / "test_guest.py").write_text(dedent(source))
    return directory


GOOD = """
    import pytest

    @pytest.fixture
    async def greeting(one):
        return await one.succeed("echo from a fixture")

    @pytest.fixture
    async def around(one):
        await one.succeed("setup")
        yield "inside"
        await one.succeed("teardown")

    async def test_a_guest_is_a_fixture(one):
        assert await one.succeed("hostname") == "one"

    async def test_async_fixtures(greeting, around):
        assert greeting == "one"
        assert around == "inside"

    def test_a_plain_test_still_runs():
        assert True

    @pytest.mark.parametrize("n", [1, 2])
    async def test_parametrized(one, n):
        print(f"case {n} says hello")
        await one.succeed(f"echo {n}")

    def test_skipped():
        pytest.skip("not today")
"""


@pytest.mark.anyio
class TestAGoodRun:
    async def test_every_case_is_an_event(self, tmp_path: Path):
        sink = Collect()
        session = session_for(tmp_path, sink)
        await session._pytest("cases", PytestSpec(tests=write_tests(tmp_path, GOOD)), session.vms)
        outcomes = {e.text.split("::")[-1]: e.data["outcome"] for e in sink.of(Kind.CASE)}
        assert outcomes == {
            "test_a_guest_is_a_fixture": "passed",
            "test_async_fixtures": "passed",
            "test_a_plain_test_still_runs": "passed",
            "test_parametrized[1]": "passed",
            "test_parametrized[2]": "passed",
            "test_skipped": "skipped",
        }

    async def test_the_machine_was_used_on_its_own_loop(self, tmp_path: Path):
        sink = Collect()
        session = session_for(tmp_path, sink)
        await session._pytest("cases", PytestSpec(tests=write_tests(tmp_path, GOOD)), session.vms)
        said = session.vms["one"].said  # ty: ignore[unresolved-attribute, possibly-unbound-attribute]
        assert "hostname" in said
        # The async generator fixture's teardown ran, after its setup.
        assert said.index("setup") < said.index("teardown")

    async def test_a_print_is_output_naming_its_case(self, tmp_path: Path):
        sink = Collect()
        session = session_for(tmp_path, sink)
        await session._pytest("cases", PytestSpec(tests=write_tests(tmp_path, GOOD)), session.vms)
        said = [e for e in sink.of(Kind.OUTPUT) if "says hello" in e.text]
        assert [e.data["case"].split("::")[-1] for e in said] == [
            "test_parametrized[1]",
            "test_parametrized[2]",
        ]

    async def test_no_test_is_left_running(self, tmp_path: Path):
        sink = Collect()
        session = session_for(tmp_path, sink)
        await session._pytest("cases", PytestSpec(tests=write_tests(tmp_path, GOOD)), session.vms)
        assert session.case is None

    async def test_junit_has_one_case_per_test_and_no_phase_row(self, tmp_path: Path):
        sink = Collect()
        session = session_for(tmp_path, sink)
        session.running = {"one": "cases"}
        await session._pytest("cases", PytestSpec(tests=write_tests(tmp_path, GOOD)), session.vms)
        finished = Event(
            at=0, kind=Kind.PHASE_FINISHED, level=30, text="", phase="cases",  # ty: ignore[invalid-argument-type]
            data={"state": "passed"},
        )
        document = junit([*sink.events, finished], "run")
        assert document.count("<testcase ") == 6
        assert 'classname="run.cases"' in document
        assert 'name="cases"' not in document


BAD = """
    async def test_arithmetic(one):
        assert 1 + 1 == 3

    async def test_fine(one):
        pass
"""


@pytest.mark.anyio
class TestAFailingRun:
    async def test_the_phase_fails_with_a_summary(self, tmp_path: Path):
        sink = Collect()
        session = session_for(tmp_path, sink)
        with pytest.raises(CasesFailed, match="1 failed, 1 passed"):
            await session._pytest("cases", PytestSpec(tests=write_tests(tmp_path, BAD)), session.vms)

    async def test_the_assertion_is_rewritten(self, tmp_path: Path):
        """What pytest is for: the values, not `AssertionError`."""
        sink = Collect()
        session = session_for(tmp_path, sink)
        with pytest.raises(CasesFailed):
            await session._pytest("cases", PytestSpec(tests=write_tests(tmp_path, BAD)), session.vms)
        failed = [e for e in sink.of(Kind.CASE) if e.data["outcome"] == "failed"]
        assert failed[0].data["message"] == "assert (1 + 1) == 3"

    async def test_selection_reaches_pytest(self, tmp_path: Path):
        sink = Collect()
        session = session_for(tmp_path, sink, "-k", "fine")
        await session._pytest("cases", PytestSpec(tests=write_tests(tmp_path, BAD)), session.vms)
        assert [e.data["outcome"] for e in sink.of(Kind.CASE)] == ["passed"]

    async def test_a_selection_that_matches_nothing_fails(self, tmp_path: Path):
        """A typo in `-k` is a green run that tested nothing, otherwise."""
        sink = Collect()
        session = session_for(tmp_path, sink, "-k", "no_such_test")
        with pytest.raises(CasesFailed, match="no tests were collected"):
            await session._pytest("cases", PytestSpec(tests=write_tests(tmp_path, BAD)), session.vms)

    async def test_arguments_pytest_rejects_are_named(self, tmp_path: Path):
        sink = Collect()
        session = session_for(tmp_path, sink, "-k", "bad((")
        with pytest.raises(CasesFailed, match="rejected its arguments.*bad"):
            await session._pytest("cases", PytestSpec(tests=write_tests(tmp_path, BAD)), session.vms)

    async def test_a_module_that_does_not_import(self, tmp_path: Path):
        sink = Collect()
        session = session_for(tmp_path, sink)
        tests = write_tests(tmp_path, "import no_such_module\n")
        with pytest.raises(CasesFailed):
            await session._pytest("cases", PytestSpec(tests=tests), session.vms)
        [case] = sink.of(Kind.CASE)
        assert case.data["outcome"] == "error"
        assert "no_such_module" in case.data["error"]


@pytest.mark.anyio
class TestImports:
    async def test_a_test_imports_a_helper_beside_it(self, tmp_path: Path):
        """`--import-mode=importlib` puts nothing on sys.path by itself."""
        sink = Collect()
        session = session_for(tmp_path, sink)
        tests = write_tests(tmp_path, "from kube_helpers import ANSWER\n\ndef test_it():\n    assert ANSWER == 42\n")
        (tests / "kube_helpers.py").write_text("ANSWER = 42\n")
        await session._pytest("cases", PytestSpec(tests=tests), session.vms)
        assert [e.data["outcome"] for e in sink.of(Kind.CASE)] == ["passed"]

    async def test_a_phase_script_imports_from_python_path(self, tmp_path: Path):
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "shared_helpers.py").write_text("def greet() -> str:\n    return 'hi'\n")
        script = tmp_path / "phase.py"
        script.write_text("from shared_helpers import greet\nasync def test(vms):\n    print(greet())\n")
        sink = Collect()
        session = Session(
            Spec(machines=[], phases=[], pythonPath=[lib]), tmp_path / "out", sink=sink
        )
        session.vms = fake_vms("one")
        session.state = {"phase": PhaseState.PENDING}
        assert await session.run(PhaseSpec(name="phase", script=script)) is PhaseState.PASSED
        assert [e.text for e in sink.of(Kind.OUTPUT)] == ["hi"]


@pytest.mark.anyio
class TestAScriptThatDoesNotImport:
    async def test_is_a_failed_phase_not_a_crash(self, tmp_path: Path):
        """Measured on nixkube: a missing helper module escaped `run` as
        an ExceptionGroup, and the drive died with every later phase
        still pending."""
        script = tmp_path / "phase.py"
        script.write_text("import no_such_helper\nasync def test(vms):\n    pass\n")
        sink = Collect()
        session = session_for(tmp_path, sink)
        session.state = {"phase": PhaseState.PENDING}
        assert await session.run(PhaseSpec(name="phase", script=script)) is PhaseState.FAILED
        assert "no_such_helper" in session.errors["phase"]


@pytest.mark.anyio
class TestRunningAgain:
    """"Edit a test, run it again" against the same guests, in one process."""

    async def test_an_edit_is_what_runs(self, tmp_path: Path):
        """pytest's importlib mode hands back a module already in
        `sys.modules` under the same name, so the second run would be the
        first run's code."""
        tests = write_tests(tmp_path, "def test_it():\n    assert 1 == 1\n")
        session = session_for(tmp_path, Collect())
        await session._pytest("cases", PytestSpec(tests=tests), session.vms)
        (tests / "test_guest.py").write_text("def test_it():\n    assert 1 == 2\n")
        with pytest.raises(CasesFailed):
            await session._pytest("cases", PytestSpec(tests=tests), session.vms)

    async def test_a_same_named_file_elsewhere_is_not_the_first(self, tmp_path: Path):
        """The store's `test_chaos.py` ran as the phase; the working tree's
        `test_chaos.py` is what the edit is in."""
        store, tree = tmp_path / "store", tmp_path / "tree"
        store.mkdir()
        tree.mkdir()
        (store / "test_chaos.py").write_text("def test_it():\n    assert True\n")
        (tree / "test_chaos.py").write_text("def test_it():\n    assert False, 'the edit'\n")
        session = session_for(tmp_path, Collect())
        await session._pytest("chaos", PytestSpec(tests=store), session.vms)
        with pytest.raises(CasesFailed):
            await session._pytest("chaos", PytestSpec(tests=tree), session.vms)

    async def test_a_helper_beside_the_tests_is_reread(self, tmp_path: Path):
        tests = write_tests(tmp_path, "from scenario_helpers import ANSWER\n\ndef test_it():\n    assert ANSWER == 1\n")
        (tests / "scenario_helpers.py").write_text("ANSWER = 1\n")
        session = session_for(tmp_path, Collect())
        await session._pytest("cases", PytestSpec(tests=tests), session.vms)
        (tests / "scenario_helpers.py").write_text("ANSWER = 2\n")
        with pytest.raises(CasesFailed):
            await session._pytest("cases", PytestSpec(tests=tests), session.vms)


def paused(session: Session) -> Controller:
    control = Controller(session)
    control._resume = anyio.Event()
    return control


def ask(op: str, arg: str) -> bytes:
    return json.dumps({"op": op, "arg": arg}).encode()


@pytest.mark.anyio
class TestPytestByHand:
    """`vivarium ctl pytest`: a working tree's tests against a paused run."""

    async def test_edit_and_send_again(self, tmp_path: Path):
        tests = write_tests(tmp_path, "async def test_it(one):\n    assert await one.succeed('x') == 'one'\n")
        sink = Collect()
        control = paused(session_for(tmp_path, sink))
        reply = await control.handle(ask("pytest", str(tests)))
        assert (reply.ok, reply.result) == (True, "1 passed")
        (tests / "test_guest.py").write_text("def test_it():\n    assert 'edited' == 'x'\n")
        reply = await control.handle(ask("pytest", shlex.join([str(tests), "-k", "test_it"])))
        assert not reply.ok
        assert reply.result == "failed"

    async def test_its_cases_stay_out_of_the_verdict(self, tmp_path: Path):
        tests = write_tests(tmp_path, "def test_no():\n    assert False\n")
        sink = Collect()
        control = paused(session_for(tmp_path, sink))
        await control.handle(ask("pytest", str(tests)))
        [case] = sink.of(Kind.CASE)
        assert case.data["by_hand"] is True
        assert case.phase == "pytest:guest"
        assert "<testcase " not in junit(sink.events, "run"), "an exploration counted in junit.xml"

    async def test_only_its_own_arguments(self, tmp_path: Path):
        """`vivarium run ... -- -k x` selects in the declared phases, not here."""
        tests = write_tests(tmp_path, "def test_a():\n    pass\n")
        control = paused(session_for(tmp_path, Collect(), "-k", "nothing_matches"))
        assert (await control.handle(ask("pytest", str(tests)))).ok

    async def test_a_missing_path_is_named(self, tmp_path: Path):
        control = paused(session_for(tmp_path, Collect()))
        reply = await control.handle(ask("pytest", str(tmp_path / "nope")))
        assert "no tests at" in (reply.error or "")


INTERLEAVED = """
import anyio

async def test(vms):
    name = vms.phase
    for n in range(3):
        print(f"{name} {n}")
        await anyio.sleep(0.01)
"""


@pytest.mark.anyio
class TestPhasesAtOnce:
    """Two phases on disjoint guests, in one task group, as `drive` runs them."""

    async def test_each_line_is_filed_under_its_own_phase(self, tmp_path: Path):
        """`redirect_stdout` per phase restored the terminal when the
        first phase ended, under the other one still printing."""
        script = tmp_path / "interleaved.py"
        script.write_text(INTERLEAVED)
        sink = Collect()
        session = session_for(tmp_path, sink)
        session.vms = fake_vms("a", "b")
        a = PhaseSpec(name="first", script=script, nodes=["a"])
        b = PhaseSpec(name="second", script=script, nodes=["b"])
        session.state = {"first": PhaseState.PENDING, "second": PhaseState.PENDING}
        stdout = sys.stdout
        async with anyio.create_task_group() as group:
            group.start_soon(session.run, a)
            group.start_soon(session.run, b)
        said = [(e.phase, e.text) for e in sink.of(Kind.OUTPUT)]
        assert sorted(said) == sorted(
            [("first", f"first {n}") for n in range(3)] + [("second", f"second {n}") for n in range(3)]
        )
        assert [text for _, text in said][:2] != ["first 0", "first 1"], "the two did not overlap"
        assert sys.stdout is stdout
        assert session.running == {}

    async def test_a_phase_sees_only_the_guests_it_declared(self, tmp_path: Path):
        script = tmp_path / "reach.py"
        script.write_text("async def test(vms):\n    vms.shared['saw'] = sorted(vms)\n    vms.b\n")
        sink = Collect()
        session = session_for(tmp_path, sink)
        session.vms = fake_vms("a", "b")
        session.state = {"reach": PhaseState.PENDING}
        state = await session.run(PhaseSpec(name="reach", script=script, nodes=["a"]))
        assert state is PhaseState.FAILED
        assert "no machine 'b'" in session.errors["reach"]
        assert session.vms.shared["saw"] == ["a"], "`shared` is the session's own dict"

    async def test_a_phase_without_nodes_sees_every_guest(self, tmp_path: Path):
        script = tmp_path / "all.py"
        script.write_text("async def test(vms):\n    vms.shared['saw'] = (vms.phase, sorted(vms))\n")
        session = session_for(tmp_path, Collect())
        session.vms = fake_vms("a", "b")
        session.state = {"all": PhaseState.PENDING}
        await session.run(PhaseSpec(name="all", script=script))
        assert session.vms.shared["saw"] == ("all", ["a", "b"])
        assert session.vms.phase is None, "the session's own `vms` names no phase"


@pytest.mark.anyio
class TestAStoppedPhase:
    async def test_it_is_interrupted_not_running(self, tmp_path: Path):
        """Measured through `vivarium-mcp stop`: `phases.json` said `running`
        for a run that had ended."""
        script = tmp_path / "slow.py"
        script.write_text("import anyio\nasync def test(vms):\n    await anyio.sleep_forever()\n")
        sink = Collect()
        session = session_for(tmp_path, sink)
        phase = PhaseSpec(name="slow", script=script)
        session.state = {"slow": PhaseState.PENDING}
        async with anyio.create_task_group() as group:
            group.start_soon(session.run, phase)
            await anyio.sleep(0.1)
            group.cancel_scope.cancel()
        assert session.state["slow"] is PhaseState.INTERRUPTED
        [finished] = sink.of(Kind.PHASE_FINISHED)
        assert finished.data["state"] == "interrupted"
