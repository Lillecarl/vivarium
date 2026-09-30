"""`vivarium monitor`: one line per event, and the verdict as the exit status."""

import json
from pathlib import Path

import anyio
import pytest

from vivarium.monitor import SOCKET, follow, line, locate, status


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


def test_locate_takes_a_directory_or_an_id(tmp_path: Path):
    assert locate(str(tmp_path)) == tmp_path / SOCKET
    assert locate("vivarium-x-abc").name == SOCKET
    assert locate("vivarium-x-abc").parent.name == "vivarium-x-abc"


async def _serve(path: Path, events: list[dict]) -> None:
    """One client, these events, then close."""
    async with await anyio.create_unix_listener(str(path)) as listener:
        async with await listener.accept() as stream:
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
