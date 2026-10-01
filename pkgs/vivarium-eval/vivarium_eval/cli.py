"""`vivarium-eval`: name a run, and it is evaluated, built and run.

    vivarium-eval run pytest-phase --out ./out -- -k hostname
    vivarium-eval phases recipes --file ~/Code/myproject
    vivarium-eval switch switch --out ./out --node one --module ./more.nix

`nix run --file . <attr>.run` with the evaluation and the build moved
inside it: nanopynix evaluates `--file`, selects the attribute, builds
its `.run` (which pulls in the spec and the phase type check) and execs
it. There is no `nix build` first and no store path to paste.

**A separate package from `vivarium`, on purpose.** nanopynix links Nix, and
`vivarium` is what every sandboxed check runs. A sandboxed check must not
evaluate -- everything is decided by the time it runs -- so it never
needs this, and a consumer of `mkTest` never builds it.

Evaluation is impure, as `nix build --file` is, so a knob reads the
environment the same way here as there.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import anyio
import nanopynix
from nanopynix.exceptions import NixError
from vivarium import control

if TYPE_CHECKING:
    from nanopynix.rpc import EvalSession, ValueProxy


@dataclass(frozen=True)
class Request:
    command: str
    file: Path
    attr: list[str]
    rest: list[str]
    """Everything else, for `vivarium` itself: its options, and `--` with
    pytest's arguments after it."""


def split_attr(text: str) -> list[str]:
    """`lan.qemu` to `["lan", "qemu"]`. No quoting: no attribute here
    has a dot in its name."""
    parts = text.split(".")
    if not all(parts):
        raise ValueError(f"not an attribute path: {text!r}")
    return parts


def entry(file: Path) -> Path:
    """The file to evaluate. A directory means its `default.nix`, as
    `nix build --file` takes it."""
    return file / "default.nix" if file.is_dir() else file


def parse(argv: list[str]) -> Request:
    """Our two options, and the rest untouched for `vivarium`.

    `--` is found by hand: argparse drops it, and `vivarium` needs it to know
    where pytest's arguments begin.
    """
    head, tail = argv, []
    if "--" in argv:
        at = argv.index("--")
        head, tail = argv[:at], argv[at:]
    parser = argparse.ArgumentParser(
        prog="vivarium-eval",
        description="Evaluate a run, build it and run it",
        epilog="Anything else goes to `vivarium`: `vivarium-eval run x --out o -v -- -k name`.",
    )
    parser.add_argument("command", choices=["run", "phases"])
    parser.add_argument("attr", help="the attribute to run, such as `lan` or `lan.qemu`")
    parser.add_argument(
        "--file",
        "-f",
        type=Path,
        default=Path("."),
        help="the file or directory to evaluate, as `nix build --file` takes it",
    )
    args, rest = parser.parse_known_args(head)
    try:
        attr = split_attr(args.attr)
    except ValueError as error:
        parser.error(str(error))
    return Request(args.command, args.file, attr, [*rest, *tail])


async def resolve(file: Path, attr: list[str], command: str) -> str:
    """Evaluate, build the attribute's `.run` or `.phases`, and return
    the program in it.

    That program, and not this package's `vivarium`, is what runs: it carries
    the `vivarium` of the library that was evaluated, which knows every field
    of the spec. Building `.run` also runs the type check of every phase
    script, exactly as `nix run --file . <attr>.run` does.
    """
    async with (
        nanopynix.rpc.Session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        target = await session_of(evaluator, file, attr)
        # `vivarium-eval run` runs the session's `.driver`; `phases` its `.phases`.
        built = Path(await target.attr({"run": "driver"}.get(command, command)).realise_string())
    [program] = sorted((built / "bin").iterdir())
    return str(program)


async def session_of(evaluator: EvalSession, file: Path, attr: list[str]) -> ValueProxy:
    target = await (await evaluator.file(str(entry(file).resolve()))).auto_call()
    for name in attr:
        target = target.attr(name)
    # A check that asserts on a session carries it as `.session`, so
    # the name of the check runs the session it checks.
    if not await target.has_attr("spec") and await target.has_attr("session"):
        target = target.attr("session")
    return target


# The session again with one more module on one node: its peers see the
# change, as they would had the module been there from the start. An
# unknown node is refused first: `nodes.<name>` would declare a new guest.
EXTENDED: Final = """session: node: module:
  let
    names = builtins.attrNames session.nodes;
    extended = session.extend { modules = [ { nodes.${node}.imports = [ (/. + module) ]; } ]; };
    build = extended.nodes.${node}.system.build;
  in
  if !(builtins.elem node names) then
    throw "no guest ${node} in this session; it has: ${builtins.concatStringsSep ", " names}"
  else
    { system = build.toplevel; registration = build.vivariumNixRegistration; }
"""

Action = Literal["switch", "boot", "test", "dry-activate"]


async def build_extended(file: Path, attr: list[str], node: str, module: Path) -> tuple[str, str]:
    """The node's system with *module* added, and its closure's registration."""
    async with (
        nanopynix.rpc.Session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        target = await session_of(evaluator, file, attr)
        extended = await (await target.apply(EXTENDED)).call(node, str(module.resolve()))
        system = await extended.attr("system").realise_string()
        registration = await extended.attr("registration").realise_string()
    return system, registration


def switch_code(node: str, system: str, registration: str, action: Action) -> str:
    """What the run executes: the closure into the guest, then the switch."""
    return (
        f"_guest = vms[{node!r}]\n"
        f"await _guest.add_closure({registration!r})\n"
        f"print((await _guest.switch_to({system!r}, {action!r}))[1], end='')\n"
    )


def parse_switch(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vivarium-eval switch",
        description="Add a module to one guest of a running session, build it on the host and switch to it",
    )
    parser.add_argument("attr", help="the session the run was started from, such as `switch`")
    parser.add_argument("--file", "-f", type=Path, default=Path("."))
    parser.add_argument("--out", type=Path, required=True, help="the running session's --out")
    parser.add_argument("--node", required=True, help="the guest to change")
    parser.add_argument("--module", type=Path, required=True, help="a NixOS module file")
    parser.add_argument("--action", choices=["switch", "boot", "test", "dry-activate"], default="switch")
    return parser.parse_args(argv)


def switch(argv: list[str]) -> int:
    args = parse_switch(argv)
    try:
        attr = split_attr(args.attr)
    except ValueError as error:
        say(str(error))
        return 2
    started = time.monotonic()
    say(f"evaluating {args.attr} with {args.module} on {args.node}")
    try:
        system, registration = anyio.run(build_extended, args.file, attr, args.node, args.module)
    except NixError as error:
        say(f"evaluation failed: {explain(str(error))}")
        return 1
    say(f"built {system} in {time.monotonic() - started:.1f}s")
    code = switch_code(args.node, system, registration, args.action)
    reply = anyio.run(control.request, args.out / control.SOCKET, control.Op.EXEC, code)
    if reply.output:
        print(reply.output, end="" if reply.output.endswith("\n") else "\n")
    if reply.error:
        print(reply.error.rstrip(), file=sys.stderr)
    return 0 if reply.ok else 1


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def explain(error: str) -> str:
    """A Nix error with its point first.

    Nix prints the stack from the outside in, so the line that says what
    is wrong comes last, under a trace of `writeTextFile` and `//`. A
    reader -- a person, or an agent reading a channel event that shows
    the last lines -- wants it first. Colour codes go too: this text
    lands in files and events, not only on a terminal.
    """
    plain = ANSI.sub("", error).rstrip()
    lines = plain.splitlines()
    last = max((i for i, line in enumerate(lines) if line.lstrip().startswith("error:")), default=None)
    if last is None:
        return plain
    point = "\n".join(lines[last:]).strip()
    trace = "\n".join(lines[:last]).rstrip()
    return f"{point}\n\n{trace}" if trace else point


def say(text: str) -> None:
    # Before a session exists, so there is no `emit` to go through yet.
    print(f"[vivarium] {text}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["switch"]:
        raise SystemExit(switch(argv[1:]))
    request = parse(argv)
    started = time.monotonic()
    say(f"evaluating {'.'.join(request.attr)} from {entry(request.file)}")
    try:
        program = anyio.run(resolve, request.file, request.attr, request.command)
    except NixError as error:
        say(f"evaluation failed: {explain(str(error))}")
        raise SystemExit(1) from None
    say(f"evaluated and built in {time.monotonic() - started:.1f}s")
    # Exec'd, not called: the program is the one the evaluated library
    # built, with its own `vivarium`. Calling this package's `vivarium` instead ran
    # a spec from a newer lib.nix with an older runner, and a phase failed
    # on an import the newer field existed to make work.
    os.execv(program, [program, *request.rest])


if __name__ == "__main__":
    main()
