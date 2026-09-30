"""A single NixOS guest, whatever kind of machine it turns out to be.

Three fds make a guest, and they are the same three for every backend:

    vec0  passt         -- outbound NAT plus the host's way in
    vec1  an fd from :mod:`uml_runner.net` -- L2 between guests
    a socketpair        -- arpyc to the guest's agent, on a serial line

Commands go over the socketpair, so nothing here depends on guest
networking having come up, and everything works inside a Nix build
sandbox.  :mod:`uml_runner.backend` turns those three into an argv; this
file does not know which kind of machine it started.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess as sync_subprocess
import tempfile
import time
from asyncio import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .agent import AGENT_READY
from .arpyc import AsyncConnection, connect
from . import backend as backends
from . import forward
from . import mconsole
from . import qmp
from . import report
from . import storeview

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CONSOLE_HISTORY = 2000

_SYSTEMD_TIMEOUT = 60
"""How long to wait for one `systemctl` round trip.  The agent gives the
command itself 30s, so anything past this is the guest, not systemd."""


class MachineError(Exception):
    """A guest failed to boot, or a command in it did not do as told."""


@dataclass(frozen=True)
class Toolchain:
    """Host-side binaries shared by every guest in a run.

    Only passt is wanted by both backends.  The rest is per backend, and
    absent rather than empty when the run does not use that backend -- a
    QEMU run must not name the UML kernel, because naming it is what makes
    Nix spend half an hour building it.
    """

    passt: Path
    kernel: Path | None = None
    bridge: Path | None = None
    qemu: Path | None = None
    qemu_img: Path | None = None
    virtiofsd: Path | None = None
    crun: Path | None = None
    setpriv: Path | None = None

    @classmethod
    def from_json(cls, data: dict) -> Toolchain:
        def maybe(key: str) -> Path | None:
            value = data.get(key)
            return Path(value) if value else None

        return cls(
            passt=Path(data["passt"]),
            kernel=maybe("kernel"),
            bridge=maybe("bridge"),
            qemu=maybe("qemu"),
            qemu_img=maybe("qemuImg"),
            virtiofsd=maybe("virtiofsd"),
            crun=maybe("crun"),
            setpriv=maybe("setpriv"),
        )


VIEWED = frozenset({"uml", "qemu"})
"""Backends whose guests get a store view. A container guest overlays its
store on the host, and an overlay does not see the binds a view is made
of."""


@dataclass(frozen=True)
class MachineSpec:
    """What Nix knows about a guest; see ``mkTest`` in flake.nix."""

    name: str
    backend: str = "uml"
    index: int = 0
    image: Path | None = None
    memory: str = "128M"
    seccomp: str = "auto"
    cpus: int = 1
    ssh_port: int = 4325
    mtu: int = 65000
    network: str | None = None
    address: str | None = None
    store: str = "/nix"
    """The directory a guest gets as its `/nix`. The runner points it at
    the guest's store view when the spec names `storePaths`."""
    store_paths: Path | None = None
    """A closureInfo's `store-paths`: the guest's closure, and all its view
    holds."""
    boot: dict = field(default_factory=dict)
    """What a QEMU guest boots: kernel, initrd, toplevel and cmdline, as
    ``modules/qemu.nix`` worked them out.  Empty under UML, which boots the
    root image instead."""
    forward: tuple[forward.Rule, ...] = ()
    """Host-side port forwards.  Addresses in these are still None until
    :func:`uml_runner.forward.resolve` has run over every machine in the
    run at once -- see :func:`uml_runner.harness.machines`."""

    @classmethod
    def from_json(cls, data: dict) -> MachineSpec:
        image = data.get("image")
        return cls(
            name=data["name"],
            backend=data.get("backend", "uml"),
            index=data.get("index", 0),
            image=Path(image) if image else None,
            memory=data.get("memory", "128M"),
            seccomp=data.get("seccomp", "auto"),
            cpus=data.get("cpus", 1),
            ssh_port=data.get("sshPort", 4325),
            mtu=data.get("mtu", 65000),
            network=data.get("network"),
            address=data.get("address"),
            store=data.get("store", "/nix"),
            store_paths=Path(data["storePaths"]) if data.get("storePaths") else None,
            boot=data.get("boot", {}),
            forward=tuple(
                forward.Rule.from_json(rule) for rule in data.get("forward", [])
            ),
        )

    @property
    def ip(self) -> str | None:
        """The ``vec1`` address without its prefix length."""
        return self.address.split("/")[0] if self.address else None

    def mac(self, nic: int) -> str:
        """This guest's address on ``vecN``.

        It carries the machine's index, because two guests on one segment
        sharing a MAC is not a segment.  ``modules/qemu.nix`` matches these
        to name the interfaces, so the two must agree.
        """
        return f"52:54:00:12:{nic:02x}:{self.index:02x}"


def _killpg(pid: int, sig: int) -> None:
    """Signal *pid*'s whole process group, and tolerate it being gone.

    The group and not the process: a guest is a tree -- the UML bridge
    starts passt and the kernel under it, QEMU starts nothing but is
    itself one of several -- and signalling only the leader leaves the
    rest running with nothing to report to.
    """
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def _running(pid: int) -> bool:
    """*pid* exists and is not a zombie waiting to be reaped."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # The state follows the command name, which may itself hold ") ".
    return stat.rpartition(")")[2].split()[0] not in ("Z", "X")


class Machine:
    """Boots a guest and drives it, in the style of a NixOS test node."""

    artifacts: Path | None
    """A host directory this guest sees at ``/artifacts``.  What the guest
    writes there is on the host the moment it is written, so it survives a
    guest that never answers again."""

    def __init__(
        self,
        spec: MachineSpec,
        tools: Toolchain,
        *,
        lan_fd: int | None = None,
        artifacts: Path | None = None,
        boot_timeout: float = 180,
        # Generous, because several guests on a loaded builder are slow
        # in a way that looks exactly like a hang.
        command_timeout: float = 120,
        recorder: report.Report | None = None,
        offline: bool = False,
        on_console: Callable[[str, str], None] | None = None,
        on_command: Callable[[str, str, float], None] | None = None,
    ) -> None:
        self.spec = spec
        self.tools = tools
        self.backend = backends.get(spec.backend)
        self.lan_fd = lan_fd
        self.artifacts = artifacts
        # `report.RUN` is the process-wide one, which is right while a
        # process holds a single run. A session passes its own, because
        # an MCP server holds several at once and their timings are not
        # one run's. See uml/session.py.
        self.recorder = recorder if recorder is not None else report.RUN
        self.boot_timeout = boot_timeout
        self.command_timeout = command_timeout
        # A run-time choice, not a property of the built guest: the same
        # image runs either way. A sandboxed run is offline whatever this
        # says, because the sandbox has no network for passt to use.
        self.offline = offline
        self.on_console = on_console
        self.on_command = on_command
        self.forward: list[forward.Rule] = list(spec.forward)

        self._rundir: Path | None = None
        self._process: subprocess.Process | None = None
        self._monitor: asyncio.Task | None = None
        self._console: asyncio.Queue[str] = asyncio.Queue()
        self._history: collections.deque[str] = collections.deque(
            maxlen=_CONSOLE_HISTORY
        )
        self._agent_sock: socket.socket | None = None
        self._guest_sock: socket.socket | None = None
        self._conn: AsyncConnection | None = None
        self._helpers: list[sync_subprocess.Popen] = []
        self._spare_fds: list[int] = []
        self._memory: mconsole.Mconsole | qmp.Qmp | None = None
        self._pid_file: Path | None = None
        self._cleanup: list[Path] = []

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def ip(self) -> str | None:
        return self.spec.ip

    def __repr__(self) -> str:
        return f"<Machine {self.name}>"

    # ── lifecycle ──────────────────────────────────────────────────

    def resolve_forward(self, taken: set[str]) -> None:
        """Settle this guest's forwards, before anything is spawned.

        Every guest in a run must go through here before any of them
        starts, or two of them pick the same address: the check is a
        bind that is released again immediately, so it only means
        anything while nothing else is racing it.  *taken* carries what
        earlier guests were given and is added to here.
        """
        self.forward, notes = forward.resolve(self.forward, taken=taken)
        for note in notes:
            self._log(f"warning: {note}")
        forward.probe(self.forward)
        for rule in self.forward:
            what = "all ports" if rule.wide else ", ".join(
                str(port) for port in rule.ports
            )
            self._log(f"forwarding {what} on {rule.address}")

    async def start(self) -> None:
        """Boot the guest and connect to its agent."""
        if self._process is not None:
            return

        self._rundir = Path(tempfile.mkdtemp(prefix=f"uml-{self.name}-"))
        if self.spec.store_paths is not None and self.spec.backend in VIEWED:
            view = storeview.build(
                self._rundir / "nix", storeview.read_paths(self.spec.store_paths)
            )
            self.spec = replace(self.spec, store=str(view))
        self._agent_sock, self._guest_sock = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_STREAM
        )

        try:
            launch = self.backend.launch(
                self, self._rundir, self._guest_sock.fileno(), self.lan_fd
            )
        except backends.BackendError as error:
            # A host that lacks something is a reason, not a crash: said
            # the way a guest that did not boot is said.
            raise MachineError(f"[{self.name}] {error}") from None
        self._helpers = launch.helpers
        self._cleanup = launch.cleanup
        self._pid_file = launch.pid_file
        if launch.memory is not None:
            self._memory = self.backend.memory_control(launch.memory)
        # Fds the backend opened for the child and no longer needs here.
        self._spare_fds = [
            fd
            for fd in launch.pass_fds
            if fd not in (self._guest_sock.fileno(), self.lan_fd)
        ]
        self._log(f"exec: {' '.join(launch.argv)}")

        self._process = await subprocess.create_subprocess_exec(
            *launch.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=launch.env or None,
            # Own process group, so everything the backend started under
            # it dies together when we signal it.
            start_new_session=True,
            # And the kernel kills it if we never get to signal anything.
            preexec_fn=backends.die_with_parent,
            pass_fds=launch.pass_fds,
        )
        for fd in self._spare_fds:
            os.close(fd)
        self._spare_fds = []
        self._monitor = asyncio.ensure_future(self._pump_console())

        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await asyncio.wait_for(
                self._wait_for_line(re.compile(re.escape(AGENT_READY))),
                timeout=self.boot_timeout,
            )
            elapsed = loop.time() - started
            self._log(f"up in {elapsed:.1f}s")
            self.recorder.booted(
                self.name,
                elapsed,
                {
                    "backend": self.spec.backend,
                    "cpus": self.spec.cpus,
                    "memory": self.spec.memory,
                },
            )
        except asyncio.TimeoutError:
            raise MachineError(
                f"[{self.name}] agent did not come up within "
                f"{self.boot_timeout:g}s"
            ) from None

        if launch.agent_path is not None:
            # No serial line to carry the socketpair: the agent listens on
            # a socket the host bound in, and says so before it accepts.
            self._agent_sock.close()
            self._agent_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._agent_sock.connect(str(launch.agent_path))
        self._conn = connect(self._agent_sock.detach())
        self._agent_sock = None
        # Drop our copy of the guest's end, so the connection reports EOF
        # when the guest goes away rather than hanging on our own fd.
        self._guest_sock.close()
        self._guest_sock = None


    async def shutdown(self) -> None:
        """Ask the guest to power off, then make sure nothing is left."""
        # `alive` first: a dead guest's agent does not refuse, it hangs,
        # and the request below waited out its full timeout after `crash`.
        if self._conn is not None and not self._conn.closed and self.alive():
            try:
                await asyncio.wait_for(self.execute("systemctl poweroff"), timeout=15)
            except (MachineError, OSError, EOFError, asyncio.TimeoutError):
                pass
            self._conn.close()
            self._conn = None

        for sock in (self._agent_sock, self._guest_sock):
            if sock is not None:
                sock.close()
        self._agent_sock = self._guest_sock = None

        await self._reap()

        # virtiofsd and passt, where the backend started them itself.
        # Under UML they are children of the bridge and went with it.
        #
        # By group and not by pid: each one leads its own session, and a
        # helper that forked -- passt does -- leaves the child behind when
        # only the leader is killed.
        for helper in self._helpers:
            if helper.poll() is None:
                _killpg(helper.pid, signal.SIGKILL)
                helper.wait()
        self._helpers = []

        if self._monitor is not None:
            self._monitor.cancel()
            try:
                await self._monitor
            except asyncio.CancelledError:
                pass
            self._monitor = None

        if self._memory is not None:
            memory, self._memory = self._memory, None
            try:
                await memory.close()
            except (OSError, EOFError, mconsole.MconsoleError, qmp.QmpError):
                # The guest is already gone by here in the ordinary case,
                # and a monitor that cannot be said goodbye to must not
                # stop the rest of the teardown.
                pass

        for directory in self._cleanup:
            shutil.rmtree(directory, ignore_errors=True)
        self._cleanup = []

        if self._rundir is not None:
            storeview.remove(self._rundir / "nix")
            shutil.rmtree(self._rundir, ignore_errors=True)
            self._rundir = None

    async def _reap(self) -> None:
        if self._process is None or self._process.returncode is not None:
            return
        for sig, grace in ((signal.SIGTERM, 30), (signal.SIGKILL, 5)):
            self._signal(sig)
            try:
                await asyncio.wait_for(self._process.wait(), timeout=grace)
                return
            except asyncio.TimeoutError:
                continue
        self._log("process would not die")

    def _signal(self, sig: int) -> None:
        if self._process is None or self._process.returncode is not None:
            return
        _killpg(self._process.pid, sig)

    def _guest_pid(self) -> int | None:
        """The guest's own process: the kernel under UML, QEMU itself."""
        if self._process is None:
            return None
        if self._pid_file is None:
            return self._process.pid
        try:
            return int(self._pid_file.read_text().strip())
        except (OSError, ValueError):
            return None

    def alive(self) -> bool:
        """Is the guest's process still running? Asks the host, not the
        agent: a dead guest's agent does not answer, it hangs."""
        pid = self._guest_pid()
        return pid is not None and _running(pid)

    async def crash(self) -> None:
        """Kill the guest outright, the way a power cut would.

        SIGKILL: no shutdown, nothing flushed, no chance to say goodbye.
        For a test of what survives a dead node. `shutdown` afterwards is
        still safe and still needed.

        The kernel's pid as well as the group, because under UML the
        spawned process is the bridge, and killing its group alone left
        the kernel answering RPC in one run of two.
        """
        if self._process is None:
            raise MachineError(f"[{self.name}] not started")
        pid = self._guest_pid()
        self._signal(signal.SIGKILL)
        if pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        await self._process.wait()
        # Polled: under UML the kernel is not our child, so there is
        # nothing to wait on.
        for _ in range(50):
            if not self.alive():
                return
            await asyncio.sleep(0.1)
        raise MachineError(f"[{self.name}] pid {pid} survived SIGKILL")

    async def wait(self, timeout: float | None = 90) -> int:
        """Wait for the UML process to exit; returns its exit code."""
        if self._process is None:
            raise MachineError(f"[{self.name}] not started")
        try:
            await asyncio.wait_for(self._process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._signal(signal.SIGTERM)
            return -1
        return self._process.returncode or 0

    # ── console ────────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        """One console line, to whatever is listening.

        `on_console` is how a caller takes this stream somewhere other
        than stdout -- a file per guest, a filter, an event queue. Left
        unset it prints, which is what `mkTest` has always done.
        """
        if self.on_console is not None:
            self.on_console(self.name, message)
            return
        print(f"[{self.name}] {message}", flush=True)

    async def _pump_console(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        while True:
            try:
                raw = await self._process.stdout.readline()
            except ValueError:
                # Line longer than the stream limit; skip it rather than die.
                continue
            if not raw:
                return
            line = _ANSI.sub("", raw.decode(errors="replace").rstrip())
            if not line:
                continue
            self._log(line)
            self._history.append(line)
            self._console.put_nowait(line)

    async def _wait_for_line(self, pattern: re.Pattern) -> str:
        """Match *pattern* against console output, past or future."""
        for line in self._history:
            if pattern.search(line):
                return line
        while True:
            try:
                line = await asyncio.wait_for(self._console.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if self._process is not None and self._process.returncode is not None:
                    raise MachineError(
                        f"[{self.name}] guest exited while waiting for "
                        f"{pattern.pattern!r}"
                    ) from None
                continue
            if pattern.search(line):
                return line

    async def wait_for_console_text(
        self, pattern: str, timeout: float | None = None
    ) -> str:
        """Wait for a regex to appear on the guest's console."""
        return await asyncio.wait_for(
            self._wait_for_line(re.compile(pattern)),
            timeout=timeout or self.command_timeout,
        )

    # ── commands ───────────────────────────────────────────────────

    @property
    def _agent(self):
        if self._conn is None:
            raise MachineError(f"[{self.name}] agent is not connected")
        return self._conn.root

    async def _ask(self, what: str, call, timeout: float):
        """Await one agent call, turning silence into a real error.

        Every call goes through here.  A guest that has wedged or run out
        of memory simply stops replying, and without a deadline the test
        would sit on the future until the whole run is killed -- with no
        clue as to which machine, or what it was asked.

        Every call is also timed here, for the same reason: this is the
        one place all of them pass through.  See report.py.
        """
        started = time.monotonic()
        try:
            return await asyncio.wait_for(call, timeout=timeout)
        except asyncio.TimeoutError:
            raise MachineError(
                f"[{self.name}] guest stopped answering during: {what}\n"
                f"{self._console_tail()}"
            ) from None
        except EOFError:
            raise MachineError(
                f"[{self.name}] guest went away during: {what}\n"
                f"{self._console_tail()}"
            ) from None
        finally:
            took = time.monotonic() - started
            self.recorder.step(self.name, "rpc", what, took)
            if self.on_command is not None:
                # The same choke point the timings use, so a new kind of
                # call is reported without touching this.
                self.on_command(self.name, what, took)

    def _console_tail(self, lines: int = 15) -> str:
        """The last thing the guest said, for an error that has no other
        evidence to offer -- a wedged guest cannot be asked anything."""
        tail = list(self._history)[-lines:]
        return "\n".join(f"    | {line}" for line in tail) or "    | (silent)"

    async def execute(
        self,
        command: str,
        timeout: float | None = None,
        label: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Run a shell command in the guest; returns (exit code, output).

        *label* is what the timing report calls this step.  A command
        carrying shell plumbing -- a redirect kept so that a deadline has
        something to read -- is unreadable as a report line and says
        nothing the program name does not.

        *env* is added to this one command's environment, and is how a
        declared impurity reaches the guest -- ``vms.env`` on the host,
        chosen by the script, and never the whole host environment.
        """
        timeout = timeout or self.command_timeout
        # The guest kills the command at `timeout`; give the round trip
        # longer, so its error is what we report, not ours.
        return await self._ask(
            label or command,
            self._agent.run(command, timeout=timeout, env=env),
            timeout + 10,
        )

    async def succeed(
        self,
        command: str,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Run a command that must succeed; returns its output."""
        rc, out = await self.execute(command, timeout=timeout, env=env)
        if rc != 0:
            raise MachineError(
                f"[{self.name}] command failed (exit {rc}): {command}\n{out}"
            )
        return out

    async def fail(
        self,
        command: str,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Run a command that must fail; returns its output."""
        rc, out = await self.execute(command, timeout=timeout, env=env)
        if rc == 0:
            raise MachineError(
                f"[{self.name}] command unexpectedly succeeded: {command}\n{out}"
            )
        return out

    # ── systemd ────────────────────────────────────────────────────

    async def unit_state(self, unit: str) -> str:
        """ActiveState of *unit* (``active``, ``failed``, ...)."""
        return await self._ask(
            f"unit_state {unit}", self._agent.unit_state(unit), _SYSTEMD_TIMEOUT
        )

    async def unit_info(self, unit: str) -> dict[str, str]:
        """Every property ``systemctl show`` reports for *unit*."""
        return await self._ask(
            f"unit_info {unit}", self._agent.unit_info(unit), _SYSTEMD_TIMEOUT
        )

    async def list_units(self, pattern: str = "*") -> list[dict]:
        """Units matching *pattern*, as dicts of the systemctl columns."""
        return await self._ask(
            f"list_units {pattern}",
            self._agent.list_units(pattern),
            _SYSTEMD_TIMEOUT,
        )

    async def listening(self) -> list[int]:
        """Guest ports with something listening on them."""
        return await self._ask("listening", self._agent.listening(), _SYSTEMD_TIMEOUT)

    def reachable(self, guest_port: int) -> list[str]:
        """Where *guest_port* answers from the host, as ``address:port``.

        Empty when nothing forwards it -- which is the answer worth
        having, since it cannot be fixed without rebooting the guest.
        """
        return forward.reachable(
            self.forward, guest_port, forward.unprivileged_start()
        )

    async def processes(self) -> list[dict]:
        """Every process in the guest: pid, ppid, name and cmdline.

        For proving that the thing under test left nothing running.  A
        guest is thrown away at poweroff, so it is where a leak can be
        counted with no machine to clean up afterwards.
        """
        return await self._ask("processes", self._agent.processes(), _SYSTEMD_TIMEOUT)

    async def count_processes(self, pattern: str) -> int:
        """How many processes have *pattern* in their name or command.

        Both fields, because ``/proc/<pid>/stat`` truncates a name at 15
        characters -- ``nix-daemon`` survives that and a longer name does
        not.
        """
        return sum(
            1
            for p in await self.processes()
            if pattern in p["name"] or pattern in p["cmdline"]
        )

    async def journal(self, unit: str | None = None, lines: int = 50) -> str:
        """Tail of the guest journal, optionally for one unit."""
        return await self._ask(
            f"journal {unit or 'all'}",
            self._agent.journal(unit, lines),
            _SYSTEMD_TIMEOUT,
        )

    # ── memory ─────────────────────────────────────────────────────

    async def meminfo(self) -> dict[str, int]:
        """``/proc/meminfo``, in kibibytes.

        The way to see what :meth:`shrink` achieved: the balloon holds its
        pages as ordinary allocations, so ``MemFree`` falls by what it
        took and ``MemTotal`` does not move.
        """
        text = await self.succeed("cat /proc/meminfo", timeout=30)
        values: dict[str, int] = {}
        for line in text.splitlines():
            name, _, rest = line.partition(":")
            fields = rest.split()
            if fields and fields[0].isdigit():
                values[name] = int(fields[0])
        return values

    def host_memory_kib(self) -> int:
        """Kibibytes of host memory this guest's RAM costs right now.

        A guest's memory is one sparse file on both backends -- UML maps
        an unlinked temporary file, QEMU a ``memory-backend-memfd`` -- so
        the host pays for the blocks that file has allocated and nothing
        else. A guest starts near zero however large its memory is, grows
        towards it as it touches pages, and falls again as it reports the
        pages it has freed.

        The measurement no guest-side number gives: the guest's own
        ``MemFree`` counts a page it has freed, and the host may still be
        paying for that page.
        """
        if self._process is None:
            raise MachineError(f"[{self.name}] is not running")
        if self._pid_file is None:
            pid = str(self._process.pid)
        else:
            # UML forks the kernel from the passt bridge, so the process
            # the runner spawned is the bridge and its pid is the wrong
            # one. The kernel writes its own beside the console socket.
            try:
                pid = self._pid_file.read_text().strip()
            except OSError as error:
                raise MachineError(f"[{self.name}] no pid file: {error}") from error
        want = backends.memory_bytes(self.spec.memory)
        # Unlinked and exactly the guest's memory long. QEMU also holds
        # its scratch disk by fd and unlinked, so a `memfd:` target wins
        # over a bare size match -- a qcow2 that happened to be exactly
        # this long would otherwise be read as the guest's memory.
        found: int | None = None
        for entry in (Path("/proc") / pid / "fd").iterdir():
            try:
                target = os.readlink(entry)
                if not target.endswith("(deleted)"):
                    continue
                stat = os.stat(entry)
            except OSError:
                continue
            if stat.st_size != want:
                continue
            if "memfd:" in target:
                return stat.st_blocks // 2
            found = stat.st_blocks // 2
        if found is not None:
            return found
        raise MachineError(f"[{self.name}] found no memory file under /proc/{pid}/fd")

    async def drop_caches(self) -> None:
        """Free the guest's page cache.

        Worth far more here than on a real machine, and worth it before
        every :meth:`shrink`.  The guest's cache of the store is a second
        copy of pages the host already holds, and a miss on it is a host
        ``read()`` that hits the host's own cache -- measured at 1.3-1.5
        GB/s against 5.1-6.5 GB/s for a hit, so dropping it costs a memcpy
        and not a disk read.  See issue #12.
        """
        await self.succeed("sync; echo 3 > /proc/sys/vm/drop_caches", timeout=60)

    async def shrink(self, amount: str) -> None:
        """Take *amount* of memory away from this guest.

        ``amount`` is written the way ``boot.uml.memory`` is: ``"256M"``.

        A balloon, so **the pages come from what is already free**, and
        how much it gets is worth measuring rather than assuming --
        :meth:`meminfo` is where it shows, as ``MemFree`` falling. A guest
        holding a page cache gives up almost nothing until
        :meth:`drop_caches` has run.

        Both backends have a limit here and they are not the same one.
        UML's console allocates ``GFP_ATOMIC``, which cannot reclaim, and
        stops at the first page it cannot get while still reporting
        success. QEMU's balloon asks the guest, which will reclaim to
        answer and takes its time about it.

        Not the way to make the *host* stop paying: free page reporting
        already does that, on both backends, within seconds and without
        being asked. This is for squeezing a guest on purpose.
        """
        await self._balloon(-backends.memory_bytes(amount))

    async def grow(self, amount: str) -> None:
        """Give this guest back up to *amount* of what :meth:`shrink` took.

        Never past the memory it booted with. Both backends clamp there
        rather than failing.
        """
        await self._balloon(backends.memory_bytes(amount))

    async def _balloon(self, delta: int) -> None:
        if self._memory is None:
            raise MachineError(
                f"[{self.name}] has no control channel for its memory, so it "
                "cannot be resized"
            )
        with self.recorder.waiting(self.name, f"balloon {delta // 1024 // 1024}M"):
            try:
                await self._memory.balloon(delta)
            except (mconsole.MconsoleError, qmp.QmpError) as error:
                raise MachineError(f"[{self.name}] {error}") from error

    def waiting(self, what: str):
        """Record a wait of the test's own as one step.

        `wait_for_unit` and friends are timed already.  A loop a test
        writes itself is not, and on a long test that is most of the run:
        measured on nixkube's nine scenarios, 632 of 786 seconds were in
        settle loops the runner could not see.  Wrap one and it appears::

            with cp.waiting("the node's state to settle"):
                await settle(cp)
        """
        return self.recorder.waiting(self.name, what)

    async def wait_for_unit(self, unit: str, timeout: float = 120) -> None:
        """Wait until *unit* is active, failing fast if it dies first."""
        with self.recorder.waiting(self.name, f"unit {unit}"):
            await self._wait_for_unit(unit, timeout)

    async def _wait_for_unit(self, unit: str, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            state = await self.unit_state(unit)
            if state == "active":
                return
            if state == "failed":
                raise MachineError(
                    f"[{self.name}] unit {unit} failed:\n"
                    f"{await self.journal(unit)}"
                )
            if asyncio.get_running_loop().time() > deadline:
                # With the journal, like the `failed` branch above: an
                # `inactive` unit says only that nothing happened, and why
                # is in the log of whatever was to pull it in.
                raise MachineError(
                    f"[{self.name}] timed out waiting for unit {unit} "
                    f"(state: {state})\n{await self.journal(unit)}"
                )
            await asyncio.sleep(0.5)
