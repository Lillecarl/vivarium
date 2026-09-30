"""One stream, several readers, and the filter in one place.

nixpkgs' driver has `TerminalLogger`, `JunitXMLLogger` and `XMLLogger`,
each with its own `_log_level` and its own copy of the same comparisons.
These tests pin the thing that replaces all three: events are data, the
filter is one function, and a renderer is pure.
"""

import json

from vivarium.events import Event, Kind, Level, junit, render, wanted
from vivarium.sinks import Broadcast, ConsoleFiles, JsonLines, Junit, Log, Terminal


def event(kind: Kind, text: str = "x", **kwargs) -> Event:
    kwargs.setdefault("level", Level.INFO)
    kwargs.setdefault("at", 1.0)
    return Event(kind=kind, text=text, **kwargs)


class TestTheFilterIsOneFunction:
    def test_a_reader_at_info_does_not_get_the_console(self):
        console = event(Kind.CONSOLE, level=Level.CONSOLE)
        assert not wanted(console, Level.INFO)

    def test_a_reader_at_console_gets_everything(self):
        for kind, level in (
            (Kind.CONSOLE, Level.CONSOLE),
            (Kind.RPC, Level.DETAIL),
            (Kind.NOTE, Level.INFO),
            (Kind.ERROR, Level.ERROR),
        ):
            assert wanted(event(kind, level=level), Level.CONSOLE)

    def test_quiet_still_gets_errors(self):
        """The one thing no level may hide."""
        assert wanted(event(Kind.ERROR, level=Level.ERROR), Level.ERROR)

    def test_quiet_drops_an_ordinary_note(self):
        assert not wanted(event(Kind.NOTE, level=Level.INFO), Level.ERROR)


class TestRenderIsPure:
    """Rendered without a terminal, which is why it can be tested at all."""

    def test_a_console_line_keeps_its_machine_prefix(self):
        """`grep '[cp]'` has worked since the beginning and must keep
        working; the stream changed, not the log's shape."""
        line = render(event(Kind.CONSOLE, "systemd: ready", machine="cp"))
        assert line == "[cp] systemd: ready"

    def test_a_phase_reads_as_a_phase(self):
        assert render(event(Kind.PHASE_STARTED, "cluster")) == "[phase] cluster"

    def test_a_command_is_marked_as_one(self):
        line = render(event(Kind.RPC, "kubectl get nodes", machine="cp"))
        assert line == "[cp] $ kubectl get nodes"

    def test_anything_else_is_the_runner_talking(self):
        assert render(event(Kind.NOTE, "output in /tmp/x")) == "[vivarium] output in /tmp/x"


class TestAnEventIsNotALogLine:
    """The point of the rework: fields, not a string to parse."""

    def test_the_machine_and_the_seconds_survive_as_fields(self):
        body = json.loads(
            event(Kind.RPC, "uptime", machine="cp", seconds=1.25).as_json()
        )
        assert body["machine"] == "cp"
        assert body["seconds"] == 1.25

    def test_nothing_absent_is_written(self):
        """A null-filled record is worse to read and bigger to keep."""
        body = json.loads(event(Kind.NOTE, "hello").as_json())
        assert "machine" not in body and "phase" not in body

    def test_an_event_cannot_be_changed_by_a_sink(self):
        one = event(Kind.NOTE)
        try:
            one.text = "tampered"  # ty: ignore[invalid-assignment]
        except Exception:
            return
        raise AssertionError("an event was mutable, so one sink can lie to the next")


class TestJunit:
    FINISHED = [
        Event(
            at=1.0,
            kind=Kind.PHASE_FINISHED,
            level=Level.INFO,
            text="boot passed",
            phase="boot",
            seconds=6.9,
            data={"state": "passed"},
        ),
        Event(
            at=8.0,
            kind=Kind.PHASE_FINISHED,
            level=Level.ERROR,
            text="cluster failed",
            phase="cluster",
            seconds=2.0,
            data={"state": "failed", "error": "MachineError: exit 1"},
        ),
        Event(
            at=10.0,
            kind=Kind.PHASE_FINISHED,
            level=Level.INFO,
            text="check skipped",
            phase="check",
            data={"state": "skipped", "reason": "cluster failed"},
        ),
    ]

    def test_it_counts_what_happened(self):
        out = junit(self.FINISHED, "mine")
        assert 'tests="3"' in out
        assert 'failures="1"' in out
        assert 'skipped="1"' in out

    def test_a_failure_carries_its_reason(self):
        assert "MachineError: exit 1" in junit(self.FINISHED)

    def test_a_multi_line_error_does_not_reach_the_attribute(self):
        """A dashboard puts the attribute in a table cell.

        The whole traceback belongs in the body, where something will
        show it on request.
        """
        multi = Event(
            at=1.0,
            kind=Kind.PHASE_FINISHED,
            level=Level.ERROR,
            text="x",
            phase="a",
            data={"state": "failed", "error": "first line\nsecond line"},
        )
        out = junit([multi])
        assert 'message="first line"' in out
        assert "second line" in out

    def test_a_deselected_phase_is_left_out_entirely(self):
        """Not reported as skipped.

        A selective run would otherwise look half-broken in a dashboard,
        every time, although nothing went wrong.
        """
        deselected = Event(
            at=1.0,
            kind=Kind.PHASE_FINISHED,
            level=Level.DETAIL,
            text="other deselected",
            phase="other",
            data={"state": "deselected"},
        )
        out = junit([*self.FINISHED, deselected])
        assert 'tests="3"' in out
        assert "other" not in out

    def test_it_escapes_what_would_break_the_document(self):
        nasty = Event(
            at=1.0,
            kind=Kind.PHASE_FINISHED,
            level=Level.ERROR,
            text="x",
            phase="a<b>c",
            data={"state": "failed", "error": 'he said "&" <here>'},
        )
        out = junit([nasty])
        assert "a&lt;b&gt;c" in out
        assert "&amp;" in out
        assert "<here>" not in out


class TestSinksWrite:
    def test_the_log_keeps_what_the_terminal_filtered_out(self, tmp_path):
        """`--quiet` changes what you watch, never what you can go back to."""
        log = Log(tmp_path / "log")
        log.emit(event(Kind.CONSOLE, "kernel says things", machine="cp"))
        log.close()
        assert "kernel says things" in (tmp_path / "log").read_text()

    def test_each_guest_gets_its_own_console_file(self, tmp_path):
        files = ConsoleFiles(tmp_path)
        files.emit(event(Kind.CONSOLE, "from cp", machine="cp"))
        files.emit(event(Kind.CONSOLE, "from worker", machine="worker"))
        files.emit(event(Kind.NOTE, "not a console line"))
        files.close()
        assert (tmp_path / "cp.log").read_text() == "from cp\n"
        assert (tmp_path / "worker.log").read_text() == "from worker\n"
        assert not (tmp_path / "uml.log").exists()

    def test_events_are_written_as_they_happen(self, tmp_path):
        """A killed run keeps everything up to the moment it died, which
        a document built in memory and written on close cannot do."""
        lines = JsonLines(tmp_path / "events.jsonl")
        lines.emit(event(Kind.NOTE, "one"))
        lines.emit(event(Kind.NOTE, "two"))
        # Deliberately not closed.
        written = (tmp_path / "events.jsonl").read_text().splitlines()
        assert len(written) == 2
        assert json.loads(written[0])["text"] == "one"


class TestBroadcastNeverFailsTheRun:
    class Broken:
        def emit(self, event):
            raise OSError("disk full")

        def close(self):
            raise OSError("still full")

    def test_a_broken_sink_does_not_raise(self, tmp_path, capsys):
        good = Log(tmp_path / "log")
        out = Broadcast([self.Broken(), good])
        out.emit(event(Kind.NOTE, "kept"))
        out.close()
        assert "kept" in (tmp_path / "log").read_text(), (
            "one sink failing stopped the others"
        )

    def test_a_broken_sink_is_dropped_rather_than_retried(self, tmp_path):
        broken = self.Broken()
        out = Broadcast([broken])
        out.emit(event(Kind.NOTE, "one"))
        out.emit(event(Kind.NOTE, "two"))
        # No exception is the assertion: a sink that raises every time
        # would otherwise print once per event for the rest of the run.

    def test_closing_a_broken_sink_does_not_raise(self, tmp_path):
        Broadcast([self.Broken()]).close()


class TestTerminal:
    def test_it_writes_where_it_is_told(self, tmp_path):
        import io

        stream = io.StringIO()
        Terminal(Level.INFO, stream).emit(event(Kind.NOTE, "hello"))
        assert stream.getvalue() == "[vivarium] hello\n"

    def test_it_obeys_the_level(self):
        import io

        stream = io.StringIO()
        term = Terminal(Level.INFO, stream)
        term.emit(event(Kind.CONSOLE, "noise", level=Level.CONSOLE, machine="cp"))
        assert stream.getvalue() == "", "the console reached a terminal at INFO"


class TestJunitSink:
    def test_it_writes_on_close(self, tmp_path):
        sink = Junit(tmp_path / "junit.xml", "mine")
        for one in TestJunit.FINISHED:
            sink.emit(one)
        sink.emit(event(Kind.CONSOLE, "ignored", machine="cp"))
        sink.close()
        out = (tmp_path / "junit.xml").read_text()
        assert 'tests="3"' in out and "ignored" not in out


class TestAPhaseTalkingIsNotTheRunnerTalking:
    """A script prefixes its own lines; `[vivarium] [test] x` helps nobody.

    AGENTS.md has told people to `grep '[test]'` since the beginning, so
    captured output is rendered exactly as it was written.
    """

    def test_captured_output_keeps_its_own_prefix_and_gains_none(self):
        line = render(event(Kind.OUTPUT, "[test] the guest answers", phase="boot"))
        assert line == "[test] the guest answers"

    def test_the_runner_still_marks_its_own(self):
        assert render(event(Kind.NOTE, "booting one")) == "[vivarium] booting one"
