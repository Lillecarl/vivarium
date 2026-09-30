"""`vivarium run --interactive`: a REPL on the terminal, as nixos-test's
`driverInteractive` has one.

The run pauses before its first phase, with the guests up, and nothing
runs until asked. Input goes to the same console as `vivarium ctl exec`: one
namespace, top-level `await`, each guest by name. `run("phase")` runs a
declared phase, `resume()` lets the rest run, and the run pauses again
after its last phase, so the guests stay up to look at. End the input
(^D) to tear the run down.

Lines are read in a daemon thread of its own: an anyio worker thread is
not a daemon, and one blocked in `input()` kept the process alive after
the run had ended.
"""

from __future__ import annotations

import codeop
import os
import signal
import sys
import threading
from typing import TYPE_CHECKING

import anyio
import anyio.from_thread
import anyio.lowlevel

if TYPE_CHECKING:
    from .control import Controller

BANNER = (
    "uml interactive: guests are up, no phase has run.\n"
    "  vms, session and each guest by name; top-level await works\n"
    "  await run('phase')  run one declared phase\n"
    "  resume()            let the remaining phases run\n"
    "  ^D                  tear the run down\n"
)


def complete(lines: list[str]) -> bool:
    """Whether *lines* are a whole statement; a syntax error is whole too,
    so that it is reported rather than waited on."""
    try:
        return codeop.compile_command("\n".join(lines), "<uml>", "exec") is not None
    except SyntaxError:
        return True


def _reader(send: anyio.abc.ObjectSendStream[str | None], token: object) -> None:
    # Its own buffer, for the prompt only: the loop keeps another.
    lines: list[str] = []
    while True:
        try:
            line = input("... " if lines else ">>> ")
        except EOFError:
            anyio.from_thread.run_sync(send.send_nowait, None, token=token)
            return
        lines.append(line)
        if complete(lines):
            lines = []
        anyio.from_thread.run_sync(send.send_nowait, line, token=token)


async def _until_paused(control: Controller) -> None:
    while not control.paused:
        await anyio.sleep(0.1)


async def serve(control: Controller) -> None:
    """Read, run, print, until the input ends; then stop the run."""
    namespace = control.console.namespace
    namespace["run"] = control.run_phase
    namespace["resume"] = control.resume
    await _until_paused(control)
    print(BANNER, flush=True)
    send, receive = anyio.create_memory_object_stream[str | None](16)
    threading.Thread(
        target=_reader, args=(send, anyio.lowlevel.current_token()), daemon=True
    ).start()
    lines: list[str] = []
    async with receive:
        async for line in receive:
            if line is None:
                break
            lines.append(line)
            if not complete(lines):
                continue
            source, lines = "\n".join(lines), []
            if not source.strip():
                continue
            # Typed while phases run, it waits for the next pause: after
            # a failure, or after the last phase.
            await _until_paused(control)
            reply = await control.execute(source)
            if reply.output:
                print(reply.output, end="" if reply.output.endswith("\n") else "\n", flush=True)
            if reply.result is not None:
                print(reply.result, flush=True)
            if reply.error:
                print(reply.error, end="", file=sys.stderr, flush=True)
    # ^D: the same teardown a SIGTERM gets.
    os.kill(os.getpid(), signal.SIGTERM)
