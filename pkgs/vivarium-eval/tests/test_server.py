import json
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from vivarium_eval.cli import explain
from vivarium_eval.server import _monitor, channel_event, run_argv, select, why_it_exited


class TestAnEvaluationError:
    # What Nix printed for an `after` naming a phase that does not exist,
    # measured through `vivarium-mcp`: the point was the last of 60 lines.
    NIX = (
        "\x1b[31;1merror:\x1b[0m\n"
        "       … while calling the 'derivationStrict' builtin\n"
        "         at /nix/store/x/lib/customisation.nix:405:12:\n"
        "       \x1b[31;1merror:\x1b[0m uml: these phases are named in an `after`"
        " and do not exist: bot."
    )

    def test_the_point_comes_first_without_colour(self):
        text = explain(self.NIX)
        assert text.startswith("error: uml: these phases")
        assert "\x1b" not in text
        assert "customisation.nix" in text, "the trace is kept, after the point"

    def test_the_channel_shows_the_point_not_the_trace(self):
        output = "[uml] evaluating broken\n[uml] evaluation failed: " + explain(self.NIX) + "\n"
        assert "do not exist: bot" in why_it_exited(output)

    def test_any_other_death_shows_the_end(self):
        output = "\n".join(f"line {n}" for n in range(40))
        assert why_it_exited(output).splitlines()[-1] == "line 39"


def event(kind: str, **fields: object) -> dict:
    return {"kind": kind, **fields}


class TestChannelEvent:
    def test_a_pause(self):
        content, meta = channel_event(
            event("note", text="paused", data={"reason": "after cases failed"}), "r1"
        ) or ("", {})
        assert meta == {"run": "r1", "event": "paused", "reason": "after cases failed"}
        assert "guests are up" in content

    def test_a_failed_phase(self):
        pushed = channel_event(
            event("phase_finished", phase="cases", data={"state": "failed", "error": "boom"}), "r1"
        )
        assert pushed == ("phase cases failed: boom", {"run": "r1", "event": "failed", "phase": "cases"})

    def test_the_verdict(self):
        pushed = channel_event(
            event("run_finished", data={"passed": False, "states": {"boot": "passed", "cases": "failed"}}),
            "r1",
        )
        assert pushed == (
            "run failed: boot passed, cases failed",
            {"run": "r1", "event": "finished", "passed": "false"},
        )

    def test_progress_at_each_phase_boundary(self):
        assert channel_event(event("phase_started", phase="deploy"), "r1") == (
            "phase deploy started",
            {"run": "r1", "event": "progress", "phase": "deploy", "state": "started"},
        )
        assert channel_event(
            event("phase_finished", phase="deploy", seconds=12.4, data={"state": "passed"}), "r1"
        ) == (
            "phase deploy passed in 12s",
            {"run": "r1", "event": "progress", "phase": "deploy", "state": "passed"},
        )

    @pytest.mark.parametrize(
        "quiet",
        [
            event("journal", text="Started"),
            event("phase_finished", data={"state": "skipped"}),
            event("note", text="control: exec", data={"op": "exec"}),
        ],
    )
    def test_the_rest_stays_in_the_file(self, quiet: dict):
        assert channel_event(quiet, "r1") is None

    def test_every_meta_key_is_an_identifier(self):
        """Claude Code drops a key with anything but letters, digits and _."""
        for pushed in (
            channel_event(event("note", data={"reason": "x"}), "r"),
            channel_event(event("phase_finished", phase="p", data={"state": "failed"}), "r"),
            channel_event(event("run_finished", data={"passed": True}), "r"),
        ):
            assert pushed is not None
            assert all(key.isidentifier() for key in pushed[1])


LINES = [
    json.dumps(e)
    for e in [
        event("journal", machine="cp", text="a", data={"unit": "kubelet.service"}),
        event("journal", machine="cp", text="b", data={"unit": "etcd.service"}),
        event("journal", machine="w1", text="c", data={"unit": "kubelet.service"}),
        event("case", phase="cases", text="t.py::test_x", data={"outcome": "failed"}),
        event("rpc", machine="cp", phase="cases", text="hostname", data={"case": "t.py::test_x"}),
    ]
] + ["not json"]


class TestSelect:
    def test_one_service_on_one_machine(self):
        found = select(LINES, kind="journal", machine="cp", unit="kubelet.service")
        assert [e["text"] for e in found] == ["a"]

    def test_by_case(self):
        assert [e["text"] for e in select(LINES, case="test_x")] == ["hostname"]

    def test_the_last_n(self):
        assert [e["text"] for e in select(LINES, kind="journal", limit=2)] == ["b", "c"]

    def test_contains(self):
        assert [e["kind"] for e in select(LINES, contains="t.py")] == ["case"]


class TestRunArgv:
    def test_by_attribute_through_uml_eval(self, tmp_path: Path):
        argv = run_argv(
            out=tmp_path,
            attr="pytest-phase",
            spec=None,
            file="/src",
            breaks=["cases"],
            break_on_failure=True,
            only=[],
            offline=True,
            pytest_args=["-k", "x"],
        )
        assert argv[0].endswith("/vivarium-eval")
        assert argv[1:5] == ["run", "pytest-phase", "--file", "/src"]
        assert argv[-7:] == ["--break", "cases", "--break-on-failure", "--offline", "--", "-k", "x"]

    def test_by_spec_straight_to_uml(self, tmp_path: Path):
        argv = run_argv(
            out=tmp_path,
            attr=None,
            spec="/nix/store/x-spec.json",
            file=".",
            breaks=[],
            break_on_failure=False,
            only=["cases"],
            offline=False,
            pytest_args=[],
        )
        assert argv[:5] == [sys.executable, "-m", "vivarium.cli", "run", "--spec"]
        assert argv[-2:] == ["--only", "cases"]

    def test_by_spec_with_the_runner_it_names(self, tmp_path: Path):
        """A spec from a newer lib.nix, run with this package's `uml`,
        lost its `pythonPath` without a word. Measured on nixkube."""
        argv = run_argv(
            out=tmp_path,
            attr=None,
            spec="/nix/store/x-spec.json",
            file=".",
            breaks=[],
            break_on_failure=False,
            only=[],
            offline=False,
            pytest_args=[],
            runner=Path("/nix/store/y-uml"),
        )
        assert argv[:4] == ["/nix/store/y-uml/bin/vivarium", "run", "--spec", "/nix/store/x-spec.json"]

    def test_a_kernel_from_a_working_tree(self, tmp_path: Path):
        argv = run_argv(
            out=tmp_path,
            attr="x",
            spec=None,
            file=".",
            breaks=[],
            break_on_failure=True,
            only=[],
            offline=False,
            pytest_args=["-k", "y"],
            kernel="/home/me/linux/linux",
        )
        # Before `--`: after it, the flag would go to pytest.
        assert argv[argv.index("--kernel") + 1] == "/home/me/linux/linux"
        assert argv.index("--kernel") < argv.index("--")

    @pytest.mark.parametrize(("attr", "spec"), [(None, None), ("a", "b")])
    def test_exactly_one_of_attr_and_spec(self, attr, spec, tmp_path: Path):
        with pytest.raises(ValueError):
            run_argv(
                out=tmp_path,
                attr=attr,
                spec=spec,
                file=".",
                breaks=[],
                break_on_failure=True,
                only=[],
                offline=False,
                pytest_args=[],
            )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _read_lines(stream) -> list[dict]:
    buffer = b""
    while True:
        try:
            buffer += await stream.receive()
        except anyio.EndOfStream:
            return [json.loads(raw) for raw in buffer.splitlines()]


@pytest.mark.anyio
async def test_a_monitor_gets_the_backlog_then_each_event_until_the_verdict(tmp_path: Path):
    run = SimpleNamespace(
        backlog=[{"run": "r", "event": "progress", "text": "phase boot started"}],
        watchers=[],
    )
    path = str(tmp_path / "monitor.sock")
    async with await anyio.create_unix_listener(path) as listener, anyio.create_task_group() as group:
        group.start_soon(listener.serve, lambda stream: _monitor(run, stream))
        with anyio.fail_after(5):
            client = await anyio.connect_unix(path)
            while not run.watchers:
                await anyio.sleep(0.01)
            live = {"run": "r", "event": "finished", "passed": "true", "text": "run passed"}
            run.backlog.append(live)
            run.watchers[0].send_nowait(live)
            async with client:
                lines = await _read_lines(client)
        group.cancel_scope.cancel()
    assert [event["event"] for event in lines] == ["progress", "finished"]
    assert run.watchers == []


@pytest.mark.anyio
async def test_a_monitor_of_a_finished_run_gets_the_backlog_and_an_end(tmp_path: Path):
    run = SimpleNamespace(
        backlog=[{"run": "r", "event": "exited", "text": "run exited 1 without a verdict:"}],
        watchers=[],
    )
    path = str(tmp_path / "monitor.sock")
    async with await anyio.create_unix_listener(path) as listener, anyio.create_task_group() as group:
        group.start_soon(listener.serve, lambda stream: _monitor(run, stream))
        with anyio.fail_after(5):
            async with await anyio.connect_unix(path) as client:
                lines = await _read_lines(client)
        group.cancel_scope.cancel()
    assert [event["event"] for event in lines] == ["exited"]
