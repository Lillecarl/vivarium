"""What a guest is made of, for each kind of machine.

:class:`Machine` owns a guest's lifetime and its console, and everything it
does above this file is the same whichever backend it has.  A backend's
whole job is to answer one question: what do I exec, with which fds, and
what else has to be running alongside it.

The host-side plumbing is shared, not reimplemented.  Both backends take:

    the agent socketpair    a fd carrying arpyc, raw
    the segment fd          a fd from :mod:`uml_runner.net`, raw frames
    passt                   started from :mod:`uml_runner.forward`'s specs

UML takes the segment fd as ``vec1:transport=fd``; QEMU takes the same fd
as ``dgram,local.type=fd``.  Both are plain ``send``/``recv`` on the fd
with no framing, which is why one segment can hold guests of either kind.

The difference is passt.  UML's vector transport has no length prefix and
passt has one, so a UML guest runs behind ``uml-passt-bridge``, which
forks passt and translates.  QEMU speaks passt's protocol itself, so the
bridge is not in the picture and the runner starts passt directly.
"""

from __future__ import annotations

import json
import mmap
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import container, forward, mconsole, qmp


class BackendError(Exception):
    """A guest could not be assembled: a helper that would not start."""


PR_SET_PDEATHSIG = 1
"""``prctl`` option number, which Python's ``signal`` module does not
carry.  From ``include/uapi/linux/prctl.h``; the number is ABI."""


def die_with_parent() -> None:
    """Ask the kernel to SIGKILL this child when the runner dies.

    Every other guarantee here is cleanup code, and cleanup code does not
    run when the runner is killed with SIGKILL, dies on a fault, or is
    stopped by a build that gave up.  A guest that outlives its runner is
    then a UML kernel or a QEMU spinning on a core with nothing left to
    report to -- measured, one such guest used a whole core for 31 hours.

    ``PR_SET_PDEATHSIG`` is the only thing that covers that case: the
    kernel sends the signal, so nothing has to be running to send it.

    Called after ``setsid``, which does not clear it.  The ``getppid``
    check closes the race where the runner dies between the fork and this
    call, which would otherwise leave the child holding a death signal
    that can no longer arrive.
    """
    import ctypes

    parent = os.getppid()
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    if os.getppid() != parent:
        os._exit(1)


def _tail(path: Path, lines: int = 20) -> str:
    """The end of *path*, for a helper that has already exited.

    The end and not the start: passt names the port it could not bind
    once per port and says why it gave up on the last line.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError as error:
        return f"({path} unreadable: {error})"
    return "\n".join(text.splitlines()[-lines:]) or f"({path} is empty)"


ARTIFACTS_ENV = "UML_ARTIFACTS"
"""What the UML guest's /init reads the host directory from.  Not the
host-side ``UML_TEST_ARTIFACTS``: that one names the root of a run, this
one names one guest's subdirectory of it."""

STORE_ENV = "UML_STORE"
"""What the UML guest's /init mounts as its /nix: the guest's store view,
or the host's /nix when it has none."""

ARTIFACTS_TAG = "artifacts"
"""The virtiofs tag QEMU serves the same directory under.
``modules/qemu.nix`` mounts it by this name."""

_VECTOR_DEPTH = 64
"""Frames per ``sendmmsg``/``recvmmsg``, and NAPI's poll weight, for UML.

Also how many receive buffers the driver keeps allocated per interface,
each one MTU-sized -- so at a jumbo MTU this is megabytes of the guest's
RAM.  There is no point going deep: AF_UNIX lets about ten frames sit in
a socketpair, so nothing beyond that is ever in flight."""

PHYSMEM_DIRS = ("/dev/shm", "/tmp")
"""Where a UML guest's RAM may live, best first.

UML's "physical" memory is an unlinked file it mmaps ``MAP_SHARED``, put
in ``TMPDIR`` when that is set and in ``/dev/shm`` or ``/tmp`` when it is
not.  Nix sets ``TMPDIR`` to the build directory in every sandbox, and
UML takes it even though it is on the builder's disk -- it only warns,
``Warning: tempdir /build is not on tmpfs``.  Every page the guest
dirties is then a dirty page of a disk file, so the host writes the
guest's RAM out under ``vm.dirty_ratio``.  Measured on a guest that
dirtied 468 MiB of its own RAM and exited: 84-90 MiB reached the disk
with ``TMPDIR`` on btrfs, 12-13 MiB with it on tmpfs."""


def memory_bytes(memory: str) -> int:
    """``mem=`` as a number, the way UML's ``memparse`` reads it."""
    scale = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if memory[-1:].lower() in scale:
        return int(memory[:-1]) * scale[memory[-1].lower()]
    return int(memory)


def _fstype(path: str) -> str | None:
    """What kind of filesystem *path* is on, from ``/proc/self/mountinfo``.

    The longest mount point that is a prefix of *path* wins, and the last
    such line wins a tie -- a later mount over the same directory hides
    the earlier one.
    """
    found: str | None = None
    longest = -1
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        before, _, after = line.partition(" - ")
        fields, rest = before.split(), after.split()
        if len(fields) < 5 or not rest:
            continue
        point = fields[4]
        if path != point and not path.startswith(point.rstrip("/") + "/"):
            continue
        if len(point) >= longest:
            longest, found = len(point), rest[0]
    return found


def _fits_guest_ram(directory: str, size: int) -> bool:
    """Can *size* bytes of guest RAM live in *directory*?

    Three questions, each one UML asks itself at boot and two of which it
    answers by carrying on anyway.  ``statfs`` for the kind, ``statvfs``
    for the room, and a ``PROT_EXEC`` mapping of a file in it -- that
    last one is ``check_tmpexec``, and a ``noexec`` tmpfs makes the guest
    ``exit(1)`` before it prints anything else.

    The room asked for is the whole of ``mem=``, although the file is
    sparse and a guest rarely touches all of it.  Being wrong in this
    direction costs the disk-backed behaviour we already have; being
    wrong the other way costs a guest killed part way through a test.
    """
    if _fstype(directory) != "tmpfs":
        return False
    try:
        stat = os.statvfs(directory)
    except OSError:
        return False
    if stat.f_bavail * stat.f_frsize < size:
        return False
    try:
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"\0" * mmap.PAGESIZE)
            probe.flush()
            with mmap.mmap(
                probe.fileno(),
                mmap.PAGESIZE,
                flags=mmap.MAP_PRIVATE,
                prot=mmap.PROT_READ | mmap.PROT_EXEC,
            ):
                return True
    except (OSError, ValueError):
        return False


def physmem_dir(memory: str) -> str | None:
    """Where this guest's RAM should live, or ``None`` to leave it to UML.

    ``None`` is the honest answer on a host with no usable tmpfs: UML
    then does what it does today, which is slower but works.
    """
    size = memory_bytes(memory)
    for directory in PHYSMEM_DIRS:
        if _fits_guest_ram(directory, size):
            return directory
    return None


def socket_dir(rundir: Path, name: str, fallback: str | None) -> tuple[Path, list[Path]]:
    """A directory to put *name* in where the kernel will accept the path.

    *rundir* where it fits, and a directory of its own where it does not:
    a unix socket's path goes in a ``sockaddr_un``, which holds 108 bytes,
    and a caller whose TMPDIR is long -- an agent's scratch directory is
    often 90 characters on its own -- would otherwise lose the control
    channel with nothing but a log line about it.

    Returns the directory and whatever has to be removed afterwards.
    """
    if len(str(rundir / name).encode()) < mconsole.UNIX_PATH_MAX:
        return rundir, []
    made = Path(tempfile.mkdtemp(prefix="uml-", dir=fallback or "/tmp"))
    return made, [made]


@dataclass
class Launch:
    """One guest's process, and whatever has to outlive its start."""

    argv: list[str]
    pass_fds: tuple[int, ...]
    env: dict[str, str] = field(default_factory=dict)
    helpers: list[subprocess.Popen] = field(default_factory=list)
    """Side processes the guest needs -- virtiofsd, passt -- for the
    backends that do not hide them behind something else.  Killed when the
    guest is torn down, in this order."""
    memory: Path | None = None
    """Where this guest answers a request to change its memory: UML's
    management console, QEMU's monitor. Which of the two it is follows
    from the backend, and :class:`Machine` asks the backend rather than
    the path."""
    pid_file: Path | None = None
    """Where the guest wrote its own pid, for a backend whose process is
    not the one the runner spawned. UML forks the kernel from the passt
    bridge, so the runner's own pid is the bridge's."""
    cleanup: list[Path] = field(default_factory=list)
    """Directories the backend made outside the run directory, removed when
    the guest is torn down."""
    agent_path: Path | None = None
    """A socket to connect to for the agent, once it says it is ready, for
    a backend with no serial line to carry the socketpair."""


class Uml:
    """A guest as an ordinary Linux process.

    Asks nothing of the host: no ``/dev/kvm``, no root, no tap device. One
    processor, and a trap into the host kernel for every guest syscall.
    """

    name = "uml"

    def launch(self, machine, rundir: Path, agent_fd: int, lan_fd: int | None) -> Launch:
        spec, tools = machine.spec, machine.tools
        ram = physmem_dir(spec.memory)
        uml_dir, cleanup = socket_dir(rundir, f"{mconsole.UMID}/mconsole", ram)
        argv = [
            str(tools.bridge),
            "--vec",
            self._vec(0, 3, spec.mtu),
            # The bridge forks passt, so the uplink's addressing reaches
            # it one argument at a time rather than directly.
            *(
                arg
                for value in forward.uplink_args(machine.offline)
                for arg in ("--passt", value)
            ),
            *forward.to_args(machine.forward),
            str(tools.kernel),
            f"ubd0={rundir}/cow,{spec.image}",
            "root=/dev/ubda",
            "rw",
            "init=/init",
            f"mem={spec.memory}",
            f"ssl0=fd:{agent_fd}",
            # Catch the guest's syscalls with a seccomp filter instead of
            # ptrace: fewer context switches per trap and per page fault,
            # which measures a few percent on throughput and about five
            # seconds off a boot.  "auto" falls back to ptrace where the
            # host will not let us install a filter, rather than
            # refusing to boot the way "on" does.  `boot.uml.seccomp` sets
            # it; "off" is the ptrace userspace -- see issue #8.
            f"seccomp={spec.seccomp}",
            # The management console, which is how the host asks this
            # guest to give memory back.  `uml_dir` defaults to ~/.uml,
            # and a build sandbox has no writable HOME; the run directory
            # is per guest, so one fixed umid in it collides with nothing.
            f"uml_dir={uml_dir}",
            f"umid={mconsole.UMID}",
        ]
        if machine.artifacts is not None:
            # The kernel does not know this one, so it hands it to /init
            # as an environment variable -- "will be passed to user space"
            # in the boot log. modules/image.nix mounts it there, with
            # busybox, and says why it cannot be a mount unit.
            argv.append(f"{ARTIFACTS_ENV}={machine.artifacts}")
        # What /init mounts as the guest's /nix; see modules/image.nix.
        argv.append(f"{STORE_ENV}={spec.store}")
        if lan_fd is not None:
            argv.append(self._vec(1, lan_fd, spec.mtu))

        pass_fds = tuple(fd for fd in (agent_fd, lan_fd) if fd is not None)
        # The bridge finds passt on PATH.
        env = dict(os.environ, PATH=f"{tools.passt.parent}:{os.environ['PATH']}")
        # The guest's RAM, and nothing else: the runner's own temporary
        # files -- the cow file above among them -- keep the caller's
        # TMPDIR, because a guest's disk belongs on a disk.  See
        # PHYSMEM_DIRS.
        if ram is not None:
            env["TMPDIR"] = ram
        return Launch(
            argv=argv,
            pass_fds=pass_fds,
            env=env,
            memory=mconsole.socket_path(uml_dir),
            pid_file=uml_dir / mconsole.UMID / "pid",
            cleanup=cleanup,
        )

    @staticmethod
    def memory_control(path: Path) -> mconsole.Mconsole:
        """The client for `Launch.memory` under this backend.

        The reply comes back with `sendto` to the address the request came
        from, so the client needs a bound path of its own -- beside the
        guest's socket, which is the one directory already known to fit in
        a `sockaddr_un`.
        """
        return mconsole.Mconsole(path, path.with_name("client"))

    @staticmethod
    def _vec(unit: int, fd: int, mtu: int) -> str:
        """A ``vecN=`` device on *fd*.

        ``mtu`` is only settable here: the driver leaves ``max_mtu`` at
        ``ether_setup``'s 1500, so ``ip link set mtu`` cannot raise it
        afterwards.

        No ``gro=1``: all it does is fix the receive buffers at 64K so
        that a transport with virtio-net headers can deliver a segment
        larger than the MTU.  ``fd`` has no such headers -- nothing ever
        arrives bigger than a frame -- so it would only mean allocating
        64K per frame and throwing most of it away.
        """
        return f"vec{unit}:transport=fd,fd={fd},depth={_VECTOR_DEPTH},mtu={mtu}"


class Qemu:
    """A guest as a virtual machine, with KVM.

    Needs ``/dev/kvm``: ``accel=kvm`` and never ``accel=kvm:tcg``, because
    the fallback is silent and ten times slower, so a builder that lost KVM
    would only look like a slow day.
    """

    name = "qemu"

    def launch(self, machine, rundir: Path, agent_fd: int, lan_fd: int | None) -> Launch:
        helpers: list[subprocess.Popen] = []
        opened: list[int] = []
        try:
            return self._launch(machine, rundir, agent_fd, lan_fd, helpers, opened)
        except BaseException:
            # Nothing owns these until a Launch carries them back, so a
            # failure between the first one and the last would leave a
            # daemon running, or an unlinked disk image alive with no name
            # and no way to find it. A test that fails while booting is
            # exactly when that happens.
            for helper in helpers:
                if helper.poll() is None:
                    helper.kill()
                    helper.wait()
            for fd in opened:
                os.close(fd)
            raise

    def _launch(
        self,
        machine,
        rundir: Path,
        agent_fd: int,
        lan_fd: int | None,
        helpers: list[subprocess.Popen],
        opened: list[int],
    ) -> Launch:
        spec, tools = machine.spec, machine.tools

        # Every unix socket this guest needs, in one directory short
        # enough to name them: virtiofsd's, the artifacts one, and the
        # monitor. `vfs` and `art` are no longer than `qmp`, so one check
        # covers all three.
        sockets, cleanup = socket_dir(rundir, "qmp", None)

        vfs_fd, vfsd = self._virtiofsd(tools, sockets, "vfs", spec.store)
        helpers.append(vfsd)
        opened.append(vfs_fd)

        art_fd: int | None = None
        if machine.artifacts is not None:
            art_fd, artd = self._virtiofsd(
                tools, sockets, "art", str(machine.artifacts)
            )
            helpers.append(artd)
            opened.append(art_fd)

        disk_fds = self._scratch_disk(tools, rundir, spec.image)
        opened.extend(disk_fds)

        passt_fd, passt_proc = self._passt(
            tools, rundir, machine.forward, machine.offline
        )
        helpers.append(passt_proc)
        opened.append(passt_fd)

        boot = spec.boot
        argv = [
            str(tools.qemu),
            "-machine", "q35,accel=kvm,memory-backend=guest-memory",
            # Nested virtualization only when the guest asked: `-cpu host`
            # alone hands every guest the host's vmx or svm, and udev
            # then loads KVM in it. See `nestedVirtualization`.
            "-cpu", "host" if boot.get("nested") else "host,-vmx,-svm",
            "-smp", str(spec.cpus),
            "-m", spec.memory,
            "-nodefaults", "-no-reboot", "-display", "none",
            # vhost-user-fs reads the guest's RAM directly, so the RAM has
            # to be a shared memfd rather than anonymous.  Without this,
            # virtiofsd connects and every read returns nothing.
            "-object",
            f"memory-backend-memfd,id=guest-memory,size={spec.memory},share=on",
            "-kernel", boot["kernel"],
            "-initrd", boot["initrd"],
            "-append", f"{boot['cmdline']} init={boot['toplevel']}/init",
            # The root, as /dev/vda, by fd rather than by name -- see
            # _scratch_disk. Two fds in the set because QEMU opens an
            # image O_RDONLY to probe its format before reopening it
            # O_RDWR, and matches the set on the access mode.
            "-add-fd", f"fd={disk_fds[0]},set=1",
            "-add-fd", f"fd={disk_fds[1]},set=1",
            "-drive", "file=/dev/fdset/1,if=virtio,format=qcow2",
            # The console, read by Machine._pump_console.
            "-serial", "stdio",
            "-chardev", f"socket,id=virtiofs,fd={vfs_fd}",
            "-device", "vhost-user-fs-pci,chardev=virtiofs,tag=nix",
            # The control channel: the same socketpair UML gets on ssl0.
            "-chardev", f"socket,id=agent,fd={agent_fd}",
            "-device", "virtio-serial",
            "-device", "virtconsole,chardev=agent",
            "-netdev", f"stream,id=vec0,addr.type=fd,addr.str={passt_fd}",
            "-device", f"virtio-net-pci,netdev=vec0,mac={spec.mac(0)}",
            # The guest tells QEMU which pages it has freed and QEMU
            # madvises them out of the memfd above, so the host stops
            # paying for a page cache the guest has dropped. Without it a
            # guest drifts towards its whole `-m` and stays there.
            "-device", "virtio-balloon-pci,free-page-reporting=on",
            # And the monitor, which is how the host asks for a size
            # rather than waiting for the guest to volunteer one.
            "-qmp", f"unix:{sockets}/qmp,server=on,wait=off",
        ]
        if art_fd is not None:
            argv += [
                "-chardev", f"socket,id=artifacts,fd={art_fd}",
                "-device",
                f"vhost-user-fs-pci,chardev=artifacts,tag={ARTIFACTS_TAG}",
            ]
        if lan_fd is not None:
            argv += [
                "-netdev", f"dgram,id=vec1,local.type=fd,local.str={lan_fd}",
                "-device",
                f"virtio-net-pci,netdev=vec1,mac={spec.mac(1)},host_mtu={spec.mtu}",
            ]

        pass_fds = tuple(
            fd
            for fd in (agent_fd, passt_fd, lan_fd, vfs_fd, art_fd, *disk_fds)
            if fd is not None
        )
        return Launch(
            argv=argv,
            pass_fds=pass_fds,
            helpers=helpers,
            memory=sockets / "qmp",
            cleanup=cleanup,
        )

    @staticmethod
    def memory_control(path: Path) -> qmp.Qmp:
        """The client for `Launch.memory` under this backend."""
        return qmp.Qmp(path)

    @staticmethod
    def _scratch_disk(tools, rundir: Path, image: Path | None) -> tuple[int, int]:
        """A writable layer over the read-only root image, with no name.

        The same shape as UML's ``ubd0=<cow>,<image>``: the image stays in
        the store, and everything the guest writes goes here instead.

        The file is unlinked before QEMU is started and handed over as open
        file descriptors.  So the only thing keeping those gigabytes alive
        is a process, and the kernel frees them the moment QEMU is gone --
        including when the runner is killed with a signal it cannot catch,
        which no amount of cleanup code covers.

        Two descriptors, read-write and read-only, because QEMU opens an
        image O_RDONLY to probe its format before reopening it O_RDWR, and
        picks from the fd set by access mode.  With one it says
        ``Failed to find file descriptor with matching flags=0x0``.
        """
        if image is None:
            raise BackendError("this guest has no root image")
        scratch = rundir / "root.qcow2"
        done = subprocess.run(
            [
                str(tools.qemu_img), "create",
                "-q",
                "-f", "qcow2",
                # Named, because qemu-img refuses to guess a backing
                # format and a guess would be silently wrong.
                "-F", "raw",
                "-b", str(image),
                str(scratch),
            ],
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            raise BackendError(
                f"could not make a scratch disk over {image}: "
                f"{(done.stderr or done.stdout).strip()}"
            )
        fds = (os.open(scratch, os.O_RDWR), os.open(scratch, os.O_RDONLY))
        scratch.unlink()
        return fds

    @staticmethod
    def _virtiofsd(
        tools, rundir: Path, socket_name: str, shared: str
    ) -> tuple[int, subprocess.Popen]:
        """Serve a host directory, and leave no socket behind.

        *socket_name* keeps the store's and the artifacts' sockets apart
        under *rundir*.

        virtiofsd takes a *listening* socket on ``--fd``, so the path it
        was bound to is only needed for long enough to connect to it once.
        Bind, listen, connect, unlink, and hand one end to virtiofsd and
        the other to QEMU: nothing is left in the filesystem even while the
        guest is running, so nothing can be left after it.


        ``--no-announce-submounts`` is load-bearing, and the failure it
        avoids names nothing useful.  NixOS binds ``/nix/store`` onto
        itself, so ``store`` is a submount of the shared directory.
        Announced, the guest makes it an automount dentry, and overlayfs
        refuses one as a lower layer -- ``ovl_dentry_weird`` rejects
        ``DCACHE_NEED_AUTOMOUNT`` and every lookup under ``/nix/store``
        fails with ``EREMOTE``.  What that reads as, from the initrd, is
        ``Failed to resolve path ... : Object is remote``, on a store the
        guest can list one directory above.

        The cost of turning it off: the guest sees one inode number space
        across what were two filesystems on the host.  Two host
        filesystems sharing an inode number would alias.  That cannot
        happen while ``/nix/store`` is a bind of ``/nix``.
        """
        # Short, because an AF_UNIX path is about 107 bytes and a run
        # directory under a long TMPDIR eats most of that.
        socket_path = rundir / socket_name
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(socket_path))
            listener.listen(1)
            client.connect(str(socket_path))
        except OSError as error:
            listener.close()
            client.close()
            raise BackendError(f"could not make a socket at {socket_path}: {error}")
        finally:
            socket_path.unlink(missing_ok=True)

        proc = subprocess.Popen(
            [
                str(tools.virtiofsd),
                f"--fd={listener.fileno()}",
                f"--shared-dir={shared}",
                # Nothing left to drop: this is already unprivileged, and
                # namespace sandboxing needs privileges a build does not
                # have.
                "--sandbox", "none",
                "--cache", "auto",
                "--no-announce-submounts",
            ],
            pass_fds=(listener.fileno(),),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=die_with_parent,
        )
        listener.close()

        # There is no socket file to wait for any more, so the readiness
        # check is that virtiofsd is still alive a moment later. Without
        # it, a bad argument or an unreadable shared directory would show
        # up as a guest whose initrd cannot mount /nix, which names the
        # wrong thing.
        time.sleep(0.05)
        if proc.poll() is not None:
            client.close()
            raise BackendError(
                f"virtiofsd exited ({proc.returncode}) instead of serving {shared}"
            )

        return client.detach(), proc

    @staticmethod
    def _passt(
        tools, rundir: Path, rules, offline: bool = False
    ) -> tuple[int, subprocess.Popen]:
        """Start passt on one end of a socketpair; return QEMU's end.

        No bridge: passt frames with a 4-byte big-endian length prefix,
        which is QEMU's own socket protocol, so the two talk directly.
        The specifiers are the ones forward.py builds for UML, unchanged.

        **passt keeps a log and is checked for a pulse**, because it used
        to have neither.  It ran `--quiet` with both streams on
        /dev/null and nothing looked at its exit status, so a passt that
        refused to start -- which it does outright when a port it was
        asked for will not bind -- left a guest with no uplink and no
        word said.  What that looks like from inside is a guest whose
        vec0 has a link-local address and nothing else, two minutes
        later, reported as a name that would not resolve.  Measured on a
        GitHub runner; see the `[cp] addresses and routes` of
        user-mode-nixos run 34943651620.
        """
        qemu_end, passt_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        log = rundir / "passt.log"
        handle = log.open("wb")
        proc = subprocess.Popen(
            [
                str(tools.passt),
                "--foreground",
                "--fd", str(passt_end.fileno()),
                *forward.uplink_args(offline),
                *forward.to_args(rules),
            ],
            pass_fds=(passt_end.fileno(),),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=die_with_parent,
        )
        passt_end.close()
        handle.close()

        # The same readiness check as virtiofsd above, and for the same
        # reason: passt fails at startup or not at all.
        time.sleep(0.05)
        if proc.poll() is not None:
            qemu_end.close()
            raise BackendError(
                f"passt exited ({proc.returncode}) instead of serving the "
                f"uplink:\n{_tail(log)}"
            )
        # Kept open until the guest has spawned; Machine closes it after.
        return qemu_end.detach(), proc


class Container:
    """A guest as a rootless container: no kernel boot, the host's own.

    Needs what :func:`uml_runner.container.probe` checks, and says which of
    it is missing before anything starts. The uplink and the forwards are
    pasta's, joined to the guest's namespaces by the launcher. ``vec1`` is
    a tap relayed to the segment fd, so a segment mixes all three kinds.
    No memory control yet.
    """

    name = "container"

    def launch(self, machine, rundir: Path, agent_fd: int, lan_fd: int | None) -> Launch:
        spec, tools = machine.spec, machine.tools
        missing = container.probe(tun=lan_fd is not None)
        if missing:
            raise BackendError(
                "this host cannot run a container guest:\n"
                + "\n".join(f"  {item}" for item in missing)
            )
        if spec.image is None:
            raise BackendError(f"{spec.name}: this guest has no root template")

        rootfs = rundir / "root"
        shutil.copytree(spec.image, rootfs, symlinks=True)
        # The template is in the store, so every copy is read-only.
        for directory, _, _ in os.walk(rootfs):
            os.chmod(directory, 0o755)

        sockets, cleanup = socket_dir(rundir, "agent/sock", None)
        agent_dir = sockets / "agent"
        agent_dir.mkdir()
        if not container.setuid_allowed(rundir):
            (agent_dir / container.NO_SETUID).touch()

        # pasta's tap and the LAN's are made on /dev/net/tun, which a Nix
        # sandbox has only with /dev/net in `extra-sandbox-paths`. With no
        # tun the guest has no uplink; the sandbox has no network anyway.
        # The uplink is taken where a tap can be made and left out where it
        # cannot; the LAN is required, and probe() checked it above.
        tun = lan_fd is not None or container.tap_fails() is None

        uid, gid = os.getuid(), os.getgid()
        if container.owns_ids():
            subuid = subgid = None
        else:
            user = pwd.getpwuid(uid).pw_name
            subuid = container.subordinate(Path("/etc/subuid"), user, uid)
            subgid = container.subordinate(Path("/etc/subgid"), user, uid)
            assert subuid is not None and subgid is not None, "probe() checked both"

        bundle = rundir / "bundle"
        bundle.mkdir()
        (bundle / "config.json").write_text(
            json.dumps(
                container.oci_config(
                    hostname=spec.name,
                    init=f"{spec.boot['toplevel']}/init",
                    setpriv=str(tools.setpriv),
                    rootfs=rootfs,
                    store=spec.store,
                    agent_dir=agent_dir,
                    artifacts=machine.artifacts,
                    uid=uid,
                    gid=gid,
                    subuid=subuid,
                    subgid=subgid,
                    writable_store=container.store_is_one_mount(spec.store),
                )
            )
        )
        state = rundir / "crun"
        state.mkdir()
        lan = (
            ["--lan-fd", str(lan_fd), "--mtu", str(spec.mtu), "--mac", spec.mac(1)]
            if lan_fd is not None
            else []
        )
        # A cgroup systemd in the guest can write in: the one this runs in,
        # or a delegated scope made for the launcher when it is not.
        prefix = (container.scope() or []) if container.needs_scope() else []
        return Launch(
            argv=[
                *prefix,
                sys.executable,
                "-m",
                "uml_runner.crun_launch",
                "run",
                "--crun", str(tools.crun),
                "--state", str(state),
                "--bundle", str(bundle),
                "--name", f"uml-{spec.name}-{os.getpid()}",
                *lan,
                # The uplink: pasta joins the guest's namespaces once crun
                # has an init, with passt's arguments and forwards.
                *(
                    [
                        "--pasta-log", str(rundir / "passt.log"),
                        "--",
                        str(tools.passt.with_name("pasta")),
                        "--foreground",
                        "--ns-ifname",
                        "vec0",
                        *forward.uplink_args(machine.offline),
                        *self._pasta_forwards(forward.to_args(machine.forward)),
                    ]
                    if tun
                    else []
                ),
            ],
            pass_fds=(lan_fd,) if lan_fd is not None else (),
            # A Nix-wrapped program carries its imports in the script, not
            # the environment, so the launcher gets this process's path.
            env=dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, sys.path))),
            agent_path=agent_dir / "sock",
            cleanup=cleanup,
        )


    @staticmethod
    def _pasta_forwards(args: list[str]) -> list[str]:
        """*args*, plus what makes pasta forward only what passt would.

        pasta forwards more by default: UDP ports it scans for, and the
        guest's loopback to the host's. passt does neither, so a guest
        that could reach the host's loopback here could not under UML.
        """
        extra = ["--tcp-ns", "none", "--udp-ns", "none"]
        for flag in ("--tcp-ports", "--udp-ports"):
            if flag not in args:
                extra += [flag, "none"]
        return [*args, *extra]


BACKENDS = {backend.name: backend() for backend in (Uml, Qemu, Container)}


def get(name: str):
    """The backend called *name*, as the spec's ``backend`` field names it."""
    try:
        return BACKENDS[name]
    except KeyError:
        raise ValueError(
            f"no backend {name!r}; have {', '.join(sorted(BACKENDS))}"
        ) from None
