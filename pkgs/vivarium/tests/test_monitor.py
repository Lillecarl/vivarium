"""`vivarium monitor`: one line per event, and the verdict as the exit status."""

import contextlib
import json
from pathlib import Path

import anyio
import pytest

from vivarium.monitor import LIVE, PAUSED, SOCKET, follow, follow_file, line, locate, paused_now, status


class TestLine:
    def test_plain(self):
        event = {"run": "r1", "event": "failed", "phase": "check", "text": "phase check failed: boom"}
        assert line(event) == "r1 failed check: phase check failed: boom"

    def test_a_message_of_several_lines_stays_on_one(self):
        event = {"run": "r1", "event": "exited", "text": "run exited 1 without a verdict:\nTraceback\n  boom\n"}
        assert "\n" not in line(event)
        assert line(event) == "r1 exited: run exited 1 without a verdict: | Traceback | boom"

    def test_json_is_one_object_per_line(self):
        event = {"run": "r1", "event": "progress", "text": "a\nb"}
        assert json.loads(line(event, as_json=True)) == event
        assert "\n" not in line(event, as_json=True)


class TestStatus:
    @pytest.mark.parametrize(
        ("event", "code"),
        [
            ({"event": "finished", "passed": "true"}, 0),
            ({"event": "finished", "passed": "false"}, 1),
            ({"event": "exited"}, 2),
            ({"event": "paused"}, None),
            ({"event": "progress"}, None),
        ],
    )
    def test_the_verdict_is_the_exit_status(self, event, code):
        assert status(event) == code


PAUSE = {"run": "r", "event": "paused", "text": "paused after boot failed"}
RESUMED = {"run": "r", "event": "resumed", "text": "resumed, paused after boot failed"}
FAILED = {"run": "r", "event": "finished", "passed": "false", "text": "run failed"}


@pytest.mark.parametrize(
    ("events", "paused"),
    [
        ([], False),
        ([PAUSE], True),
        ([PAUSE, RESUMED], False),
        ([PAUSE, RESUMED, PAUSE], True),
        ([PAUSE, {"event": "progress"}], True),
        ([PAUSE, FAILED], False),
    ],
)
def test_paused_now_is_the_last_pause_left_open(events, paused):
    assert paused_now(events) == paused


def test_locate_takes_a_directory_or_an_id(tmp_path: Path):
    assert locate(str(tmp_path)) == tmp_path / SOCKET
    assert locate("vivarium-x-abc").name == SOCKET
    assert locate("vivarium-x-abc").parent.name == "vivarium-x-abc"


async def _serve(path: Path, events: list[dict]) -> None:
    """One client, these events, then close."""
    async with await anyio.create_unix_listener(str(path)) as listener:
        async with await listener.accept() as stream:
            # A client that exits at a pause leaves before the rest.
            with contextlib.suppress(anyio.BrokenResourceError):
                for event in events:
                    await stream.send(json.dumps(event).encode() + b"\n")


@pytest.mark.anyio
async def test_follow_prints_each_event_and_exits_with_the_verdict(tmp_path: Path, capsys):
    socket = tmp_path / SOCKET
    events = [
        {"run": "r", "event": "progress", "phase": "boot", "text": "phase boot started"},
        {"run": "r", "event": "finished", "passed": "false", "text": "run failed: boot failed"},
    ]
    async with anyio.create_task_group() as group:
        group.start_soon(_serve, socket, events)
        while not socket.exists():
            await anyio.sleep(0.01)
        with anyio.fail_after(5):
            code = await follow(socket)
    assert code == 1
    assert capsys.readouterr().out.splitlines() == [
        "r progress boot: phase boot started",
        "r finished: run failed: boot failed",
    ]


@pytest.mark.anyio
async def test_quiet_leaves_out_progress(tmp_path: Path, capsys):
    socket = tmp_path / SOCKET
    events = [
        {"run": "r", "event": "progress", "phase": "boot", "text": "phase boot passed"},
        {"run": "r", "event": "paused", "text": "after boot failed"},
        {"run": "r", "event": "finished", "passed": "false", "text": "run failed"},
    ]
    async with anyio.create_task_group() as group:
        group.start_soon(_serve, socket, events)
        while not socket.exists():
            await anyio.sleep(0.01)
        with anyio.fail_after(5):
            code = await follow(socket, quiet=True)
    assert code == 1
    assert capsys.readouterr().out.splitlines() == [
        "r paused: after boot failed",
        "r finished: run failed",
    ]


@pytest.mark.anyio
async def test_a_stream_that_ends_before_the_verdict_is_3(tmp_path: Path):
    socket = tmp_path / SOCKET
    async with anyio.create_task_group() as group:
        group.start_soon(_serve, socket, [{"run": "r", "event": "paused", "text": "paused"}])
        while not socket.exists():
            await anyio.sleep(0.01)
        with anyio.fail_after(5):
            assert await follow(socket) == 3


async def _follow(tmp_path: Path, events: list[dict], **options) -> int:
    socket = tmp_path / SOCKET
    async with anyio.create_task_group() as group:
        group.start_soon(_serve, socket, events)
        while not socket.exists():
            await anyio.sleep(0.01)
        with anyio.fail_after(5):
            return await follow(socket, **options)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("events", "until_pause", "code"),
    [
        # A live pause: only --until-pause stops there.
        ([LIVE, PAUSE, RESUMED, FAILED], True, PAUSED),
        ([LIVE, PAUSE, RESUMED, FAILED], False, 1),
        # Armed while paused: the replayed pause is still open.
        ([PAUSE, LIVE], True, PAUSED),
        ([PAUSE, LIVE], False, 3),
        # Armed after a resume: the replayed pause is closed.
        ([PAUSE, RESUMED, LIVE, FAILED], True, 1),
        # The replay alone decides nothing until it ends.
        ([PAUSE, RESUMED], True, 3),
    ],
)
async def test_until_pause_exits_at_an_open_pause(tmp_path: Path, events, until_pause, code):
    assert await _follow(tmp_path, events, until_pause=until_pause) == code


@pytest.mark.anyio
async def test_live_is_not_printed(tmp_path: Path, capsys):
    await _follow(tmp_path, [PAUSE, LIVE], until_pause=True, quiet=True)
    assert capsys.readouterr().out.splitlines() == ["r paused: paused after boot failed"]


def _write(out: Path, *events: dict) -> None:
    with (out / "events.jsonl").open("a") as file:
        for event in events:
            file.write(json.dumps(event) + "\n")


STARTED = {"kind": "phase_started", "phase": "boot"}
NOTE_PAUSED = {"kind": "note", "data": {"reason": "after boot failed"}}
NOTE_RESUMED = {"kind": "note", "data": {"resumed": "after boot failed"}}
VERDICT = {"kind": "run_finished", "data": {"passed": False, "states": {"boot": "failed"}}}


class TestFollowFile:
    """A run that no vivarium-mcp started: the monitor reads events.jsonl."""

    @pytest.mark.anyio
    async def test_a_finished_run_replays_to_its_verdict(self, tmp_path: Path, capsys):
        _write(tmp_path, STARTED, VERDICT)
        with anyio.fail_after(5):
            assert await follow_file(tmp_path, grace=0) == 1
        assert capsys.readouterr().out.splitlines() == [
            f"{tmp_path.name} progress boot: phase boot started",
            f"{tmp_path.name} finished: run failed: boot failed",
        ]

    @pytest.mark.anyio
    async def test_a_run_gone_without_a_verdict_is_2(self, tmp_path: Path):
        _write(tmp_path, STARTED)
        with anyio.fail_after(5):
            assert await follow_file(tmp_path, grace=0.3) == 2

    @pytest.mark.anyio
    async def test_it_waits_while_the_run_is_up(self, tmp_path: Path):
        (tmp_path / "control.sock").touch()
        _write(tmp_path, STARTED, NOTE_PAUSED)
        async with anyio.create_task_group() as group:
            codes = []

            async def watch() -> None:
                codes.append(await follow_file(tmp_path, grace=0))

            group.start_soon(watch)
            await anyio.sleep(0.8)
            # The pause alone ends nothing without --until-pause.
            assert codes == []
            _write(tmp_path, NOTE_RESUMED, VERDICT)
            (tmp_path / "control.sock").unlink()
            with anyio.fail_after(5):
                while not codes:
                    await anyio.sleep(0.05)
        assert codes == [1]

    @pytest.mark.anyio
    async def test_until_pause_exits_at_an_open_pause(self, tmp_path: Path):
        (tmp_path / "control.sock").touch()
        _write(tmp_path, STARTED, NOTE_PAUSED)
        with anyio.fail_after(5):
            assert await follow_file(tmp_path, grace=0, until_pause=True) == PAUSED

    @pytest.mark.anyio
    async def test_until_pause_runs_on_past_a_closed_pause(self, tmp_path: Path):
        (tmp_path / "control.sock").touch()
        _write(tmp_path, STARTED, NOTE_PAUSED, NOTE_RESUMED)
        with anyio.move_on_after(0.8) as scope:
            await follow_file(tmp_path, grace=0, until_pause=True)
        assert scope.cancelled_caught
