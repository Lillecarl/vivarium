"""Breakpoints and injected Python, without a guest."""

import ast
import shlex
from pathlib import Path

import pytest

from vivarium.control import Console, Op, commands, parse_request, split_last_expression


class TestSplitLastExpression:
    def test_a_trailing_expression_is_the_value(self):
        body, last = split_last_expression("x = 1\nx + 1")
        assert len(body.body) == 1
        assert last is not None
        assert isinstance(last.body, ast.BinOp)

    def test_a_trailing_statement_has_no_value(self):
        _, last = split_last_expression("x = 1")
        assert last is None

    def test_empty(self):
        body, last = split_last_expression("")
        assert body.body == []
        assert last is None


class TestParseRequest:
    def test_a_request(self):
        assert parse_request(b'{"op": "exec", "arg": "1"}') == (Op.EXEC, "1")

    def test_state_needs_no_arg(self):
        assert parse_request(b'{"op": "state"}') == (Op.STATE, "")

    @pytest.mark.parametrize(
        ("line", "says"),
        [
            (b"nope", "not JSON"),
            (b"[1]", "JSON object"),
            (b'{"op": "rm"}', "no such op"),
            (b'{"op": "exec"}', "needs an arg"),
            (b'{"op": "exec", "arg": 1}', "is a string"),
        ],
    )
    def test_what_is_refused(self, line: bytes, says: str):
        with pytest.raises(ValueError, match=says):
            parse_request(line)


@pytest.mark.anyio
class TestConsole:
    async def test_top_level_await_and_its_value(self):
        async def hostname() -> str:
            return "one"

        console = Console({"hostname": hostname})
        reply = await console.execute("await hostname()")
        assert reply.ok
        assert reply.result == "'one'"

    async def test_a_name_survives_to_the_next_call(self):
        console = Console({})
        await console.execute("pid = 42")
        reply = await console.execute("pid + 1")
        assert reply.result == "43"

    async def test_print_is_the_output(self):
        reply = await Console({}).execute("print('hello')")
        assert reply.output == "hello\n"
        assert reply.result is None

    async def test_an_exception_is_the_reply_not_a_crash(self):
        reply = await Console({}).execute("1 / 0")
        assert not reply.ok
        assert reply.error is not None
        assert "ZeroDivisionError" in reply.error

    async def test_a_syntax_error_is_the_reply(self):
        reply = await Console({}).execute("def (")
        assert not reply.ok
        assert "SyntaxError" in (reply.error or "")


def test_each_printed_command_reaches_the_run_it_names():
    out = Path("/tmp/a run")
    lines = dict(commands("/nix/store/x-vivarium/bin/vivarium", out))
    argvs = [shlex.split(line) for line in lines.values()]
    assert all(argv[0] == "/nix/store/x-vivarium/bin/vivarium" for argv in argvs)
    assert all(str(out) in argv for argv in argvs)
    watch = next(argv for argv in argvs if argv[1] == "monitor")
    assert watch[2:] == [str(out), "--quiet", "--until-pause"]
    assert {argv[4] for argv in argvs if argv[1] == "ctl"} == {"exec", "inject", "state", "continue"}
