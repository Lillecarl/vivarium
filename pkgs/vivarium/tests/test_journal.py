"""Reading a journal that is still being written."""

import json
from pathlib import Path

import pytest

from vivarium.events import Event, Kind, Level, render
from vivarium.journal import Entry, Tail, level, parse, split


def line(**fields: object) -> str:
    return json.dumps(fields)


class TestParse:
    def test_the_fields_a_reader_filters_on(self):
        entry = parse(
            line(
                MESSAGE="started",
                PRIORITY="6",
                _SYSTEMD_UNIT="kubelet.service",
                SYSLOG_IDENTIFIER="kubelet",
                _PID="412",
            )
        )
        assert entry == Entry(
            message="started",
            priority=6,
            unit="kubelet.service",
            identifier="kubelet",
            pid=412,
        )

    def test_a_message_that_is_not_utf8(self):
        """journald writes those as a list of byte values, not a string."""
        entry = parse(line(MESSAGE=[104, 105, 255], PRIORITY="6"))
        assert entry is not None
        assert entry.message == "hi�"

    def test_a_kernel_line_has_no_unit(self):
        entry = parse(line(MESSAGE="oops", PRIORITY="3", _TRANSPORT="kernel"))
        assert entry is not None
        assert entry.unit is None
        assert "unit" not in entry.data()

    def test_no_priority_is_journalds_default(self):
        entry = parse(line(MESSAGE="x"))
        assert entry is not None
        assert entry.priority == 6

    def test_emerg_is_not_mistaken_for_missing(self):
        entry = parse(line(MESSAGE="x", PRIORITY="0"))
        assert entry is not None
        assert entry.priority == 0

    @pytest.mark.parametrize("text", ["", "not json", "[1, 2]", '{"MESSAGE": '])
    def test_what_is_not_an_entry(self, text: str):
        assert parse(text) is None


class TestLevel:
    def test_errors_are_seen_at_detail(self):
        assert level(Entry(message="x", priority=3)) is Level.DETAIL

    def test_the_rest_sits_with_the_console(self):
        assert level(Entry(message="x", priority=4)) is Level.CONSOLE


class TestSplit:
    def test_a_partial_line_waits(self):
        assert split(b'{"a":1}\n{"b"') == (['{"a":1}'], b'{"b"')

    def test_a_complete_buffer_leaves_nothing(self):
        assert split(b"one\ntwo\n") == (["one", "two"], b"")


@pytest.mark.anyio
class TestTail:
    async def test_a_file_that_is_not_there_yet(self, tmp_path: Path):
        assert await Tail(tmp_path / "journal.jsonl").read() == []

    async def test_each_line_once_as_the_file_grows(self, tmp_path: Path):
        path = tmp_path / "journal.jsonl"
        tail = Tail(path)
        path.write_text('{"n":1}\n{"n"')
        assert await tail.read() == ['{"n":1}']
        with path.open("a") as handle:
            handle.write(':2}\n')
        assert await tail.read() == ['{"n":2}']
        assert await tail.read() == []

    async def test_a_restarted_stream_is_read_from_the_start(self, tmp_path: Path):
        """`truncate:` in the unit: a guest that starts the stream again
        starts the file again, and an offset past its end reads nothing
        forever."""
        path = tmp_path / "journal.jsonl"
        tail = Tail(path)
        path.write_text('{"n":1}\n{"n":2}\n')
        await tail.read()
        path.write_text('{"n":3}\n')
        assert await tail.read() == ['{"n":3}']


class TestRender:
    def test_the_unit_names_the_source(self):
        event = Event(
            at=0,
            kind=Kind.JOURNAL,
            level=Level.CONSOLE,
            text="Started",
            machine="cp",
            data={"unit": "kubelet.service", "identifier": "kubelet"},
        )
        assert render(event) == "[cp] kubelet.service: Started"

    def test_the_identifier_when_there_is_no_unit(self):
        event = Event(
            at=0,
            kind=Kind.JOURNAL,
            level=Level.CONSOLE,
            text="oops",
            machine="cp",
            data={"identifier": "kernel"},
        )
        assert render(event) == "[cp] kernel: oops"
