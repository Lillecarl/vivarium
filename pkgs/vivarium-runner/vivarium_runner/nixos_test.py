"""nixos-test's test-script API, over this runner's guests.

A nixos-test `testScript` is synchronous Python: `machine.succeed(...)`
blocks. Here it runs in a worker thread, and each call is handed back to
the event loop that owns the guests. The names and signatures follow
`nixos/lib/test-driver` in nixpkgs, so a script runs unchanged; see
`nixos-test.nix` for the half that maps the Nix.

The screen calls -- screenshots, OCR, key presses -- go to
:class:`Machine`'s own. What has no equivalent, such as a raw QEMU
monitor, raises NotImplementedError naming the call, rather than doing
something else.
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
import time
import unittest
from collections.abc import Callable, Iterator
from typing import Any

import anyio
import anyio.from_thread
import anyio.to_thread

from . import display
from .machine import Machine as Guest

DEFAULT_TIMEOUT = 900
"""nixos-test's default for every wait, in seconds."""

POLL = 1.0


def pythonize(name: str) -> str:
    """nixos-test's `pythonize_name`: a node name as a Python identifier."""
    return name.replace("-", "_")


def _seconds(timeout: Any) -> float:
    """A timeout as nixos-test takes it: seconds, or a timedelta."""
    if timeout is None:
        return DEFAULT_TIMEOUT
    return timeout.total_seconds() if hasattr(timeout, "total_seconds") else float(timeout)


def _delay(delay: Any) -> float:
    """A key delay as nixos-test takes it: seconds, a timedelta, or None."""
    if delay is None:
        return 0.0
    return delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)


class Unsupported(NotImplementedError, AttributeError):
    """A nixos-test call with no equivalent here. An AttributeError too, so
    `hasattr` still answers."""


class Machine:
    """One guest, with nixos-test's `Machine` methods."""

    def __init__(self, guest: Guest) -> None:
        self._guest = guest
        self.name = guest.name

    def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return anyio.from_thread.run(functools.partial(fn, *args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        raise Unsupported(f"nixos-test's Machine.{name} has no equivalent here")

    # The runner boots every guest before the first phase.
    def start(self, allow_reboot: bool = False) -> None:
        pass

    def is_up(self) -> bool:
        return self._guest.alive()

    def log(self, msg: str) -> None:
        print(f"[{self.name}] {msg}", flush=True)

    def execute(
        self,
        command: str,
        check_return: bool = True,
        check_output: bool = True,
        timeout: Any = DEFAULT_TIMEOUT,
    ) -> tuple[int, str]:
        # nixos-test's backdoor shell: root's login environment, so root's
        # per-user packages are on PATH (simple-container's `hello` needs
        # it), then strict mode, which a script relies on to fail a broken
        # pipeline or an unset variable. The profile before strict mode:
        # it reads variables that may be unset. stderr goes to the console,
        # as the backdoor's does: a script parses what `succeed` returns,
        # and `nix build --print-out-paths` prints its progress on stderr.
        wrapped = (
            "export USER=root HOME=/root; source /etc/profile >/dev/null 2>&1; "
            f"exec 2>/dev/console; set -euo pipefail; {command}"
        )
        return self._call(self._guest.execute, wrapped, timeout=_seconds(timeout))

    def succeed(self, *commands: str, timeout: Any = None) -> str:
        output = ""
        for command in commands:
            status, out = self.execute(command, timeout=_seconds(timeout))
            if status != 0:
                raise Exception(f"command `{command}` failed (exit code {status}):\n{out}")
            output += out
        return output

    def fail(self, *commands: str, timeout: Any = None) -> str:
        output = ""
        for command in commands:
            status, out = self.execute(command, timeout=_seconds(timeout))
            if status == 0:
                raise Exception(f"command `{command}` unexpectedly succeeded")
            output += out
        return output

    def _until(self, what: str, check: Callable[[], bool], timeout: Any) -> None:
        deadline = time.monotonic() + _seconds(timeout)
        while not check():
            if time.monotonic() > deadline:
                raise Exception(f"timed out waiting for {what} on {self.name}")
            time.sleep(POLL)

    def wait_until_succeeds(self, command: str, timeout: Any = DEFAULT_TIMEOUT) -> str:
        result: list[str] = []

        def ok() -> bool:
            status, out = self.execute(command)
            result[:] = [out]
            return status == 0

        self._until(f"`{command}` to succeed", ok, timeout)
        return result[0]

    def wait_until_fails(self, command: str, timeout: Any = DEFAULT_TIMEOUT) -> str:
        result: list[str] = []

        def failed() -> bool:
            status, out = self.execute(command)
            result[:] = [out]
            return status != 0

        self._until(f"`{command}` to fail", failed, timeout)
        return result[0]

    def systemctl(self, q: str, user: str | None = None) -> tuple[int, str]:
        if user is None:
            return self.execute(f"systemctl {q}")
        return self.execute(f"su -l {user} --shell /bin/sh -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user {q}'")

    def get_unit_info(self, unit: str, user: str | None = None) -> dict[str, str]:
        status, out = self.systemctl(f'--no-pager show "{unit}"', user)
        if status != 0:
            raise Exception(f'retrieving systemctl info for unit "{unit}" failed with exit code {status}')
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def get_unit_property(self, unit: str, property: str, user: str | None = None) -> str:
        return self.get_unit_info(unit, user).get(property, "")

    def require_unit_state(self, unit: str, require_state: str = "active") -> None:
        state = self.get_unit_info(unit).get("ActiveState")
        if state != require_state:
            raise Exception(f"Expected unit '{unit}' to be in state '{require_state}' but it is in state '{state}'")

    def wait_for_unit(self, unit: str, user: str | None = None, timeout: Any = DEFAULT_TIMEOUT) -> None:
        def active() -> bool:
            info = self.get_unit_info(unit, user)
            state = info.get("ActiveState")
            if state == "failed":
                raise Exception(f'unit "{unit}" reached state "{state}"')
            if state == "inactive" and self.systemctl("list-jobs --full 2>&1", user)[1].strip() == "No jobs running.":
                raise Exception(f'unit "{unit}" is inactive and there are no pending jobs')
            return state == "active"

        self._until(f"unit {unit}", active, timeout)

    def wait_for_file(self, filename: str, timeout: Any = DEFAULT_TIMEOUT) -> None:
        self._until(f"file {filename}", lambda: self.execute(f"test -e {filename}")[0] == 0, timeout)

    def _port(self, port: int, addr: str) -> bool:
        return self.execute(f"bash -c '</dev/tcp/{addr}/{port}' 2>/dev/null")[0] == 0

    def wait_for_open_port(self, port: int, addr: str = "localhost", timeout: Any = DEFAULT_TIMEOUT) -> None:
        self._until(f"port {addr}:{port} to open", lambda: self._port(port, addr), timeout)

    def wait_for_closed_port(self, port: int, addr: str = "localhost", timeout: Any = DEFAULT_TIMEOUT) -> None:
        self._until(f"port {addr}:{port} to close", lambda: not self._port(port, addr), timeout)

    def wait_for_open_unix_socket(self, addr: str, is_datagram: bool = False, timeout: Any = DEFAULT_TIMEOUT) -> None:
        self._until(f"socket {addr}", lambda: self.execute(f"test -S {addr}")[0] == 0, timeout)

    def start_job(self, jobname: str, user: str | None = None) -> tuple[int, str]:
        return self.systemctl(f"start {jobname}", user)

    def stop_job(self, jobname: str, user: str | None = None) -> tuple[int, str]:
        return self.systemctl(f"stop {jobname}", user)

    def sleep(self, secs: float) -> None:
        time.sleep(secs)

    def wait_for_console_text(self, regex: str, timeout: Any = None) -> None:
        self._call(self._guest.wait_for_console_text, regex, timeout=_seconds(timeout))

    # The screen: a guest needs `vivarium.display`, which nixos-test.nix
    # sets for a test that uses one.

    def screenshot(self, filename: str) -> None:
        print(f"[{self.name}] screenshot {self._call(self._guest.screenshot, filename)}", flush=True)

    def send_key(self, key: str, delay: Any = 0.01, log: bool = True) -> None:
        self._call(self._guest.send_key, display.CHAR_TO_KEY.get(key, key), delay=_delay(delay))

    def send_chars(self, chars: str, delay: Any = 0.01) -> None:
        self._call(self._guest.send_chars, chars, delay=_delay(delay))

    def get_screen_text_variants(self) -> list[str]:
        return [screen.text for screen in self._call(self._guest.read_screen)]

    def get_screen_text(self) -> str:
        return self._call(self._guest.screen_text)

    def wait_for_text(self, regex: str, timeout: Any = DEFAULT_TIMEOUT) -> None:
        # nixos-test searches the whole text, across lines; find_text
        # matches within one, for a box. So poll the text.
        pattern = re.compile(regex)
        deadline = time.monotonic() + _seconds(timeout)
        while True:
            read = self.get_screen_text_variants()
            if any(pattern.search(text) for text in read):
                return
            if time.monotonic() > deadline:
                shot = self._call(self._guest.screenshot, None)
                raise Exception(
                    f"timed out waiting for {regex} on the screen of {self.name} ({shot}); OCR read:\n"
                    + "\n---\n".join(read)
                )
            time.sleep(POLL)

    def wait_for_x(self, timeout: Any = DEFAULT_TIMEOUT) -> None:
        self._until(
            "the X11 server",
            lambda: self.execute(
                "journalctl -b SYSLOG_IDENTIFIER=systemd | grep -q 'Reached target Current graphical'"
                " && [ -e /tmp/.X11-unix/X0 ]"
            )[0]
            == 0,
            timeout,
        )

    def get_window_names(self) -> list[str]:
        return self.succeed(
            r"xwininfo -root -tree | sed 's/.*0x[0-9a-f]* \"\([^\"]*\)\".*/\1/; t; d'"
        ).splitlines()

    def wait_for_window(self, regexp: str, timeout: Any = DEFAULT_TIMEOUT) -> None:
        pattern = re.compile(regexp)
        self._until(
            f"a window {regexp}",
            lambda: any(pattern.search(name) for name in self.get_window_names()),
            timeout,
        )

    def shutdown(self) -> None:
        self._call(self._guest.shutdown)

    def crash(self) -> None:
        self._call(self._guest.crash)

    def wait_for_shutdown(self) -> None:
        self._call(self._guest.wait)


class _Log:
    """nixos-test's `log`: the few methods scripts call on it."""

    def info(self, msg: str) -> None:
        print(msg, flush=True)

    warning = error = log = info

    @contextlib.contextmanager
    def nested(self, msg: str, attrs: dict[str, str] | None = None) -> Iterator[None]:
        print(msg, flush=True)
        yield


def symbols(guests: dict[str, Guest]) -> dict[str, Any]:
    """What a nixos-test script sees: its globals."""
    machines = {pythonize(name): Machine(guest) for name, guest in guests.items()}

    @contextlib.contextmanager
    def subtest(name: str) -> Iterator[None]:
        print(f"[subtest] {name}", flush=True)
        yield

    def retry(fn: Callable[[bool], bool], timeout_seconds: int = DEFAULT_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout_seconds
        while not fn(False):
            if time.monotonic() > deadline:
                if fn(True):
                    return
                raise Exception(f"action timed out after {timeout_seconds} seconds")
            time.sleep(POLL)

    def unsupported(name: str) -> Callable[..., None]:
        def call(*_: Any, **__: Any) -> None:
            raise Unsupported(f"nixos-test's {name} has no equivalent here")

        return call

    names: dict[str, Any] = dict(
        start_all=lambda: None,
        join_all=lambda: None,
        machines=list(machines.values()),
        subtest=subtest,
        retry=retry,
        log=_Log(),
        os=os,
        t=unittest.TestCase(),
        serial_stdout_on=lambda: None,
        serial_stdout_off=lambda: None,
        driver=None,
        polling_condition=unsupported("polling_condition"),
        create_machine=unsupported("create_machine"),
    )
    names.update(machines)
    if len(machines) == 1 and "machine" not in machines:
        names["machine"] = next(iter(machines.values()))
    return names


async def run(guests: dict[str, Guest], script: str) -> None:
    """Run a nixos-test `testScript` against *guests*."""
    code = compile(script, "testScript", "exec")
    namespace = symbols(guests)
    await anyio.to_thread.run_sync(exec, code, namespace)
