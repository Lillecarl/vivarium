"""A guest as a rootless OCI container, run by crun.

No kernel boot: the guest's systemd runs as PID 1 in a user, PID, mount,
cgroup and network namespace of its own, as the user who started the run.
``docs/design/history/running-anywhere.md`` (Area 8) has the measurements this is
built on.

Three pieces, each for a fact measured before it was written:

* :func:`oci_config`, the bundle's ``config.json``. Pure, so it is tested
  without a container.
* :func:`probe`, what the host must have, tried rather than read.
* :func:`main`, the launcher. crun gives systemd a console only with
  ``terminal: true``, and then hands the pty master over a socket instead
  of relaying it -- to a pipe it says "tcgetattr: Inappropriate ioctl for
  device" and exits. The launcher takes the master and copies it to its own
  stdout, which is what :class:`~vivarium_runner.machine.Machine` reads.
"""

from __future__ import annotations

import json
import os
import pwd
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

AGENT_DIR = "/run/host/agent"
"""Where the guest sees the host's agent directory."""

AGENT_SOCKET = f"{AGENT_DIR}/sock"

SUBORDINATE_IDS = 65536
"""How many ids beyond root the guest gets. 65534 is ``nobody``, so fewer
leaves a service that runs as it unable to start."""

CAPABILITIES = [
    "CAP_" + name
    for name in (
        "CHOWN DAC_OVERRIDE DAC_READ_SEARCH FOWNER FSETID KILL SETGID SETUID "
        "SETPCAP LINUX_IMMUTABLE NET_BIND_SERVICE NET_BROADCAST NET_ADMIN "
        "NET_RAW IPC_LOCK IPC_OWNER SYS_MODULE SYS_RAWIO SYS_CHROOT "
        "SYS_PTRACE SYS_PACCT SYS_ADMIN SYS_BOOT SYS_NICE SYS_RESOURCE "
        "SYS_TIME SYS_TTY_CONFIG MKNOD LEASE AUDIT_WRITE AUDIT_CONTROL "
        "SETFCAP MAC_OVERRIDE MAC_ADMIN SYSLOG WAKE_ALARM BLOCK_SUSPEND "
        "AUDIT_READ PERFMON BPF CHECKPOINT_RESTORE"
    ).split()
]
"""Every capability, in the guest's own user namespace only. An empty set
is what crun gives when the field is left out, and systemd then fails
every ``User=`` service at the GROUP step (measured)."""


@dataclass(frozen=True)
class Range:
    """A run of subordinate ids from ``/etc/subuid`` or ``/etc/subgid``."""

    start: int
    count: int


def subordinate(path: Path, user: str, uid: int) -> Range | None:
    """The first range *path* gives *user*, by name or by number."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.strip().split(":")
        if len(fields) == 3 and fields[0] in (user, str(uid)):
            return Range(int(fields[1]), int(fields[2]))
    return None


def _mapping(host: int, extra: Range) -> list[dict]:
    return [
        {"containerID": 0, "hostID": host, "size": 1},
        {"containerID": 1, "hostID": extra.start, "size": SUBORDINATE_IDS},
    ]


def _bind(source: str, destination: str, *options: str) -> dict:
    return {
        "destination": destination,
        "type": "bind",
        "source": source,
        "options": ["rbind", *options],
    }


def _fs(kind: str, destination: str, *options: str) -> dict:
    return {
        "destination": destination,
        "type": kind,
        "source": kind,
        "options": list(options),
    }


def oci_config(
    *,
    hostname: str,
    init: str,
    setpriv: str,
    rootfs: Path,
    store: str,
    agent_dir: Path,
    artifacts: Path | None,
    uid: int,
    gid: int,
    subuid: Range | None,
    subgid: Range | None,
    writable_store: bool = True,
    store_view: bool = False,
) -> dict:
    """The ``config.json`` for one guest.

    The mounts are in the order crun applies them, and the order matters:
    the agent's bind lands on the ``/run`` tmpfs, so it comes after it.

    With no subordinate ids the guest shares the runner's user namespace.
    That is the `uid-range` sandbox, where the build is already root with
    65536 ids, as nixpkgs' nspawn tests run with ``--private-users=no``.

    Without ``writable_store``, ``/nix/store`` is the host's, bound
    read-only and recursively: the sandbox's store is one bind mount per
    input, and an overlay does not see a lower's submounts.

    With ``store_view``, *store* is the guest's writable store view
    (``storeview.build``), bound in as it is: it holds its own binds.
    """
    mounts = [
        _fs("proc", "/proc", "nosuid", "noexec", "nodev"),
        _fs("tmpfs", "/dev", "nosuid", "strictatime", "mode=755", "size=65536k"),
        # gid 3 is NixOS' `tty`. NixOS' own devpts mount asks for it, and
        # says "Invalid gid '3'" where the guest has only one id.
        _fs(
            "devpts",
            "/dev/pts",
            "nosuid",
            "noexec",
            "newinstance",
            "ptmxmode=0666",
            "mode=0620",
            "gid=3",
        ),
        _fs("tmpfs", "/dev/shm", "nosuid", "noexec", "nodev", "mode=1777"),
        _fs("mqueue", "/dev/mqueue", "nosuid", "noexec", "nodev"),
        # Read-only, which is what keeps udevd from starting: its unit has
        # `ConditionPathIsReadWrite=/sys`. A udevd in a user namespace gets
        # no uevents, so every link stayed "pending" and networkd never
        # configured vec0 (measured).
        _fs("sysfs", "/sys", "nosuid", "noexec", "nodev", "ro"),
        # `cgroup2`, a plain mount, and not OCI's `cgroup`: crun reads what
        # that means from the host's /sys/fs/cgroup, which a uid-range
        # build does not mount, and says "invalid file system type".
        _fs("cgroup2", "/sys/fs/cgroup", "nosuid", "noexec", "nodev", "rw"),
        _fs("tmpfs", "/run", "nosuid", "nodev", "mode=755"),
        _fs("tmpfs", "/tmp", "nosuid", "nodev", "mode=1777"),
        # The host's store below, the guest's writes above. `userxattr`
        # because an unprivileged overlay may not use trusted.* xattrs.
        #
        # /nix/store and not all of /nix, as the other backends have it.
        # Unprivileged, `lowerdir=/nix` fails with EINVAL where the host's
        # /nix/store is a mount of its own, and `lowerdir=/nix/store`
        # works (measured with unshare). One mount for all of /nix is for
        # a kubelet `subPath`, and a container guest runs no kubelet.
        {
            "destination": "/nix/store",
            "type": "overlay",
            "source": "overlay",
            "options": [
                f"lowerdir={store}/store",
                f"upperdir={rootfs}/.nix-upper",
                f"workdir={rootfs}/.nix-work",
                "userxattr",
            ],
        }
        if writable_store and not store_view
        else _bind(f"{store}/store", "/nix/store", "rw" if store_view else "ro"),
        _bind(str(agent_dir), AGENT_DIR, "rw"),
    ]
    if artifacts is not None:
        mounts.append(_bind(str(artifacts), "/artifacts", "rw"))

    return {
        "ociVersion": "1.0.2",
        "hostname": hostname,
        "root": {"path": str(rootfs), "readonly": False},
        "process": {
            "terminal": True,
            "user": {"uid": 0, "gid": 0},
            # The init dies with crun, and the PID namespace with it. crun
            # does not arrange that: SIGKILLed, it left the guest running.
            "args": [setpriv, "--pdeathsig", "KILL", "--", init],
            "env": [
                "PATH=/run/current-system/sw/bin",
                "container=crun",
                "TERM=dumb",
            ],
            "cwd": "/",
            "capabilities": {
                "bounding": CAPABILITIES,
                "effective": CAPABILITIES,
                "permitted": CAPABILITIES,
            },
            "noNewPrivileges": False,
        },
        "mounts": mounts,
        "linux": {
            "namespaces": [
                {"type": kind}
                for kind in ("pid", "ipc", "uts", "mount", "cgroup", "network")
            ]
            + ([{"type": "user"}] if subuid is not None else []),
            "maskedPaths": [],
            "readonlyPaths": [],
        }
        | (
            {"uidMappings": _mapping(uid, subuid), "gidMappings": _mapping(gid, subgid)}
            if subuid is not None and subgid is not None
            else {}
        ),
    }


# ── what the host must have ─────────────────────────────────────────


@dataclass(frozen=True)
class Missing:
    """One thing the host lacks, and what gives it."""

    what: str
    why: str
    remedy: str

    def __str__(self) -> str:
        return f"{self.what}: {self.why}\n    fix: {self.remedy}"


def _unshare_user() -> None:
    os.unshare(os.CLONE_NEWUSER)


def probe(
    user: str | None = None, uid: int | None = None, *, tun: bool = False
) -> list[Missing]:
    """What this host lacks for a container guest; empty when nothing.

    Each check does the thing once. Reading configuration is not enough:
    an AppArmor profile or a seccomp filter shows only as a failed attempt.

    ``tun`` adds what a LAN needs: a tap device made in a network namespace
    of its own.
    """
    uid = os.getuid() if uid is None else uid
    user = user or pwd.getpwuid(uid).pw_name
    missing: list[Missing] = []

    if tun:
        why = tap_fails()
        if why:
            missing.append(
                Missing(
                    "a tap device",
                    why,
                    "in a Nix build, put /dev/net in extra-sandbox-paths "
                    "(ghanix: nix.install.devNet = true)",
                )
            )

    try:
        subprocess.run([sys.executable, "-c", ""], preexec_fn=_unshare_user, check=True)
        userns = True
    except (OSError, subprocess.SubprocessError):
        userns = False
    if not userns:
        missing.append(
            Missing(
                "a user namespace",
                "unshare(CLONE_NEWUSER) failed",
                "allow unprivileged user namespaces: user.max_user_namespaces > 0, "
                "and on Ubuntu kernel.apparmor_restrict_unprivileged_userns=0",
            )
        )

    # In the uid-range sandbox the runner is root already, with the ids a
    # guest needs, and there is no /etc/subuid or newuidmap to ask.
    if not owns_ids():
        _probe_ranges(user, uid, userns, missing)

    if not cgroup_works() and scope() is None:
        missing.append(
            Missing(
                "a cgroup systemd can write in",
                "a cgroup2 mounted in a new cgroup namespace was not writable, "
                "and systemd-run --user could not make a delegated scope",
                "run under a user systemd, or in a cgroup delegated to you; in "
                "a Nix build, ask for the uid-range system feature",
            )
        )
    return missing


def owns_ids() -> bool:
    """Root in a user namespace that maps at least a guest's worth of ids:
    a `uid-range` build."""
    if os.getuid() != 0:
        return False
    try:
        lines = Path("/proc/self/uid_map").read_text().splitlines()
    except OSError:
        return False
    return sum(int(line.split()[2]) for line in lines) >= SUBORDINATE_IDS


NO_SETUID = "no-setuid"
"""A file in the agent directory when setuid bits cannot be set: a Nix
build's seccomp filter refuses them, for the container too ("chmod: ...
Operation not permitted" from suid-sgid-wrappers, measured).
modules/container.nix skips that unit on it rather than failing the boot.
A file and not PID 1's environment: NixOS' stage 2 starts systemd without
the variable, and a `ConditionEnvironment` on it never held (measured)."""


def setuid_allowed(directory: Path) -> bool:
    """Whether a file in *directory* can be made setuid. Tried, not read."""
    probe_file = directory / "setuid-probe"
    probe_file.write_bytes(b"")
    try:
        os.chmod(probe_file, 0o4755)
        return bool(os.stat(probe_file).st_mode & 0o4000)
    except PermissionError:
        return False
    finally:
        probe_file.unlink()


def store_is_one_mount(store: str) -> bool:
    """Whether *store*/store has no mounts under it. A sandbox's store has
    one bind per input, which an overlay lower does not show."""
    prefix = f"{store}/store/"
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return False
    return not any(line.split()[4].startswith(prefix) for line in lines)


_CGROUP_PROBE = """
import ctypes, os, sys, tempfile
uid, gid = os.getuid(), os.getgid()
os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS | os.CLONE_NEWCGROUP)
open("/proc/self/setgroups", "w").write("deny")
open("/proc/self/uid_map", "w").write(f"0 {uid} 1")
open("/proc/self/gid_map", "w").write(f"0 {gid} 1")
target = tempfile.mkdtemp()
libc = ctypes.CDLL(None, use_errno=True)
if libc.mount(b"none", target.encode(), b"cgroup2", 0, None) != 0:
    sys.exit("mount: " + os.strerror(ctypes.get_errno()))
os.mkdir(target + "/probe")
os.rmdir(target + "/probe")
"""
"""What systemd in the guest will do: mount a cgroup2 in its own cgroup
namespace and make a group in it. Not a check of the runner's cgroup
directory: a `uid-range` build has no /sys/fs/cgroup mounted at all, and
this still works there (measured)."""


_TAP_PROBE = """
import os
from vivarium_runner.container import _open_tap
uid, gid = os.getuid(), os.getgid()
os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)
open("/proc/self/setgroups", "w").write("deny")
open("/proc/self/uid_map", "w").write(f"0 {uid} 1")
open("/proc/self/gid_map", "w").write(f"0 {gid} 1")
os.close(_open_tap("probe0", 1500, "52:54:00:00:00:01"))
"""
"""What the LAN relay does, in a namespace of its own: open /dev/net/tun
and make a tap. The file existing is not the answer; the device cgroup or
a missing node both show only here."""


def tap_fails() -> str | None:
    """Why no tap can be made here, or ``None`` when one can."""
    done = subprocess.run(
        [sys.executable, "-c", _TAP_PROBE],
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, sys.path))),
    )
    if done.returncode == 0:
        return None
    return (done.stderr.strip().splitlines() or ["failed"])[-1]


def cgroup_works(prefix: list[str] | None = None) -> bool:
    done = subprocess.run(
        [*(prefix or []), sys.executable, "-c", _CGROUP_PROBE], capture_output=True
    )
    return done.returncode == 0


def _probe_ranges(user: str, uid: int, userns: bool, missing: list[Missing]) -> None:
    ranges = {}
    for name, path in (("subuid", Path("/etc/subuid")), ("subgid", Path("/etc/subgid"))):
        found = subordinate(path, user, uid)
        if found is None or found.count < SUBORDINATE_IDS:
            missing.append(
                Missing(
                    f"{SUBORDINATE_IDS} subordinate ids in {path}",
                    f"{user} has {found.count if found else 'none'}",
                    f"NixOS: users.users.{user}.autoSubUidGidRange = true; "
                    f"elsewhere: usermod --add-{name}s 100000-165535 {user}",
                )
            )
        else:
            ranges[name] = found
    if userns and len(ranges) == 2:
        for helper, host, extra in (
            ("newuidmap", uid, ranges["subuid"]),
            ("newgidmap", os.getgid(), ranges["subgid"]),
        ):
            why = _try_map(helper, host, extra)
            if why:
                missing.append(
                    Missing(
                        f"a working {helper}",
                        why,
                        "install shadow's uidmap tools, setuid or with "
                        "cap_setuid/cap_setgid; NixOS has them in /run/wrappers/bin",
                    )
                )


SCOPE = ["--user", "--scope", "--quiet", "--collect", "-p", "Delegate=yes"]


def scope() -> list[str] | None:
    """A command prefix that runs its command in a delegated cgroup, or
    ``None`` when the user's systemd will not make one.

    ``systemd-run --scope`` execs the command in its own process rather
    than forking it, so the parent-death signal the runner set survives.
    The host's systemd-run, not one from the store: it talks to the host's
    user manager, and the two must agree.
    """
    path = _which("systemd-run")
    if path is None:
        return None
    prefix = [path, *SCOPE]
    return prefix if cgroup_works(prefix) else None


def needs_scope() -> bool:
    return not cgroup_works()


def _try_map(helper: str, host: int, extra: Range) -> str | None:
    """Map one subordinate id into a fresh user namespace with *helper*.

    Tried, not read: NixOS' wrappers carry file capabilities and no setuid
    bit, so a mode check refuses a helper that works (measured).
    """
    path = _which(helper)
    if path is None:
        return "not on PATH"
    # A child in a user namespace of its own, alive until its stdin closes.
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        preexec_fn=_unshare_user,
    )
    try:
        done = subprocess.run(
            [path, str(child.pid), "0", str(host), "1", "1", str(extra.start), "1"],
            capture_output=True,
            text=True,
        )
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait()
    if done.returncode != 0:
        return f"{path} failed: {(done.stderr or done.stdout).strip()}"
    return None


def _which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(":"):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# ── the launcher ─────────────────────────────────────────────────────


def _die_with_parent() -> None:
    # Imported here: backend imports this module.
    from .backend import die_with_parent

    die_with_parent()


def _relay(master: int, crun: subprocess.Popen) -> None:
    """Copy the guest's console to stdout until the guest is gone."""
    out = sys.stdout.buffer
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)
    while True:
        if not selector.select(timeout=0.5):
            if crun.poll() is not None:
                return
            continue
        try:
            data = os.read(master, 65536)
        except OSError:
            # EIO: no slave end is open at this moment. That is not the
            # end of the guest: systemd hangs the console up as it starts
            # and opens it again, and a relay that stopped here lost every
            # line after "starting systemd..." (measured). crun's exit is
            # the end.
            if crun.poll() is not None:
                return
            time.sleep(0.05)
            continue
        if not data:
            return
        out.write(data)
        out.flush()


def _shares_userns(pid: int) -> bool:
    """Whether *pid* is in this process's user namespace. Joining the one
    you are in is EINVAL."""
    return os.readlink(f"/proc/{pid}/ns/user") == os.readlink("/proc/self/ns/user")


def _init_pid(crun: list[str], name: str) -> int:
    return json.loads(
        subprocess.run([*crun, "state", name], capture_output=True, text=True, check=True).stdout
    )["pid"]


def _uplink(pid: int, log: Path, pasta: list[str]) -> subprocess.Popen:
    """Start pasta in the guest's namespaces: ``vec0``, as passt gives the
    other backends, with its DHCP, its DNS and its forwards.

    It fails at start or not at all -- a port it cannot bind is fatal --
    so a dead pasta a moment later is reported with its log.
    """
    # By pid, pasta joins the init's user namespace as well. Where the guest
    # shares the runner's -- the uid-range sandbox -- that join is EINVAL,
    # "Couldn't enter user namespace" (measured), so name the network
    # namespace alone, which pasta then joins by itself.
    target = [f"--netns=/proc/{pid}/ns/net"] if _shares_userns(pid) else [str(pid)]
    with log.open("wb") as handle:
        proc = subprocess.Popen(
            [*pasta, *target],
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=_die_with_parent,
        )
    time.sleep(0.05)
    if proc.poll() is not None:
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:])
        raise RuntimeError(f"pasta exited ({proc.returncode}) instead of serving the uplink:\n{tail}")
    return proc


TUNSETIFF = 0x400454CA
SIOCSIFMTU = 0x8922
SIOCSIFHWADDR = 0x8924
IFF_TAP = 0x0002
IFF_NO_PI = 0x1000
ARPHRD_ETHER = 1


def _open_tap(name: str, mtu: int, mac: str) -> int:
    import fcntl
    import struct

    tap = os.open("/dev/net/tun", os.O_RDWR)
    fcntl.ioctl(tap, TUNSETIFF, struct.pack("16sH22x", name.encode(), IFF_TAP | IFF_NO_PI))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        fcntl.ioctl(control, SIOCSIFMTU, struct.pack("16si20x", name.encode(), mtu))
        fcntl.ioctl(
            control,
            SIOCSIFHWADDR,
            struct.pack("16sH6s16x", name.encode(), ARPHRD_ETHER, bytes.fromhex(mac.replace(":", ""))),
        )
    return tap


def tap_relay(pid: int, lan: int, name: str, mtu: int, mac: str) -> int:
    """Be the guest's ``vec1``: a tap in its namespaces, one frame each way.

    A segment fd carries one raw frame per datagram, which is what UML's
    ``transport=fd`` and QEMU's ``dgram`` take; a tap reads and writes one
    frame per call. So the relay is a copy, and one segment holds guests
    of all three kinds.

    Joins the guest's user namespace first, which makes this root there:
    creating a tap in the guest's network namespace needs CAP_NET_ADMIN in
    the namespace that owns it. A process must be single-threaded to join
    a user namespace, which is why this is a process of its own.
    """
    joins = [("net", os.CLONE_NEWNET)]
    if not _shares_userns(pid):
        joins.insert(0, ("user", os.CLONE_NEWUSER))
    for kind, flag in joins:
        ns = os.open(f"/proc/{pid}/ns/{kind}", os.O_RDONLY)
        os.setns(ns, flag)
        os.close(ns)
    tap = _open_tap(name, mtu, mac)
    selector = selectors.DefaultSelector()
    selector.register(tap, selectors.EVENT_READ, lan)
    selector.register(lan, selectors.EVENT_READ, tap)
    while True:
        for key, _ in selector.select():
            try:
                frame = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                return 0
            if not frame:
                return 0
            try:
                os.write(key.data, frame)
            except (BlockingIOError, OSError):
                # A full queue drops the frame, as a wire would.
                pass


def _lan(pid: int, fd: int, mtu: int, mac: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable, "-m", "vivarium_runner.crun_launch", "tap",
            "--pid", str(pid), "--fd", str(fd), "--mtu", str(mtu), "--mac", mac,
        ],
        pass_fds=(fd,),
        start_new_session=True,
        preexec_fn=_die_with_parent,
    )


MS_REC = 0x4000
MS_PRIVATE = 1 << 18


def _cgroup2_mounted() -> bool:
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, _, after = line.partition(" - ")
        if before.split()[4] == "/sys/fs/cgroup" and after.split()[0] == "cgroup2":
            return True
    return False


def _ensure_cgroup2() -> None:
    """Give crun the cgroup2 at /sys/fs/cgroup it insists on.

    crun statfs()es /sys/fs/cgroup before anything else and refuses what
    is not cgroup2 or tmpfs: "invalid file system type". A uid-range build
    mounts nothing there. As root in its own user namespace, the launcher
    can make a mount and cgroup namespace of its own and mount one: the
    build's cgroup, which Nix delegated to it.
    """
    if _cgroup2_mounted() or os.getuid() != 0:
        return
    import ctypes

    os.unshare(os.CLONE_NEWNS | os.CLONE_NEWCGROUP)
    libc = ctypes.CDLL(None, use_errno=True)
    for args in (
        (b"none", b"/", None, MS_REC | MS_PRIVATE, None),
        (b"none", b"/sys/fs/cgroup", b"cgroup2", 0, None),
    ):
        if libc.mount(*args) != 0:
            raise OSError(ctypes.get_errno(), f"mount {args[1].decode()}")


def _report(tun: bool) -> int:
    """The probe as a program: what a derivation runs to fail early."""
    missing = probe(tun=tun)
    for item in missing:
        print(f"missing {item}", flush=True)
    if missing:
        print(f"this host cannot run a container guest: {len(missing)} missing", flush=True)
        return 1
    ids = "the build's own (uid-range)" if owns_ids() else "subordinate ids"
    store = "overlay" if store_is_one_mount("/nix") else "read-only bind"
    uplink = "yes" if tap_fails() is None else "no (no tap device)"
    print(f"ok: user namespace, cgroup, ids: {ids}, store: {store}, uplink: {uplink}")
    return 0


def _parse(argv: list[str]):
    import argparse

    parser = argparse.ArgumentParser(prog="vivarium_runner.crun_launch")
    sub = parser.add_subparsers(dest="mode", required=True)
    run = sub.add_parser("run")
    run.add_argument("--crun", required=True)
    run.add_argument("--state", required=True)
    run.add_argument("--bundle", required=True)
    run.add_argument("--name", required=True)
    run.add_argument("--lan-fd", type=int)
    run.add_argument("--mtu", type=int, default=1500)
    run.add_argument("--mac")
    run.add_argument("--pasta-log")
    run.add_argument("pasta", nargs=argparse.REMAINDER)
    check = sub.add_parser("probe", help="say what this host lacks, and exit 1 if anything")
    check.add_argument("--tun", action="store_true", help="a LAN will be asked for")
    tap = sub.add_parser("tap")
    tap.add_argument("--pid", type=int, required=True)
    tap.add_argument("--fd", type=int, required=True)
    tap.add_argument("--mtu", type=int, required=True)
    tap.add_argument("--mac", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """``python -m vivarium_runner.crun_launch run|tap ...``.

    ``run`` starts the guest and relays its console; with ``--pasta-log``
    and a pasta command line after ``--`` it gets an uplink, and with
    ``--lan-fd`` a ``vec1`` on that segment. ``tap`` is the ``vec1`` relay.
    """
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.mode == "tap":
        return tap_relay(args.pid, args.fd, "vec1", args.mtu, args.mac)
    if args.mode == "probe":
        return _report(args.tun)
    _ensure_cgroup2()

    name = args.name
    bundle = args.bundle
    uplink = [args.pasta_log, *[a for a in args.pasta if a != "--"]] if args.pasta_log else []
    crun = [args.crun, "--root", args.state, "--cgroup-manager=disabled"]

    # A short directory: a sockaddr_un holds 108 bytes, and a bundle under
    # a long TMPDIR does not fit.
    sockets = Path(tempfile.mkdtemp(prefix="vivarium-crun-", dir=os.environ.get("VIVARIUM_RUN_ROOT") or "/tmp"))
    console = sockets / "console"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(console))
    listener.listen(1)

    proc = subprocess.Popen(
        [*crun, "run", "--bundle", bundle, "--console-socket", str(console), name],
        stdin=subprocess.DEVNULL,
        # Its own group, so a signal to the runner's group reaches this
        # process and not crun: crun would pass SIGTERM to systemd, which
        # reads it as "re-execute".
        start_new_session=True,
        preexec_fn=_die_with_parent,
    )

    def stop(signum: int, _frame) -> None:
        subprocess.run([*crun, "kill", name, "KILL"], capture_output=True)

    signal.signal(signal.SIGTERM, stop)

    listener.settimeout(0.5)
    master: int | None = None
    helpers: list[subprocess.Popen] = []
    try:
        while master is None and proc.poll() is None:
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            _, fds, _, _ = socket.recv_fds(conn, 1024, 1)
            conn.close()
            if not fds:
                # crun connected and failed before it had a pty to send.
                # Its own error is already on stdout; its exit is the
                # status.
                continue
            master = fds[0]
        if master is not None and (uplink or args.lan_fd is not None):
            try:
                pid = _init_pid(crun, name)
                if uplink:
                    helpers.append(_uplink(pid, Path(uplink[0]), uplink[1:]))
                if args.lan_fd is not None:
                    helpers.append(_lan(pid, args.lan_fd, args.mtu, args.mac))
            except (RuntimeError, subprocess.CalledProcessError) as error:
                # On the console, which is where Machine looks for why a
                # guest did not come up.
                print(f"vivarium-crun: {error}", flush=True)
                return 1
        if master is not None:
            _relay(master, proc)
        return proc.wait()
    finally:
        listener.close()
        console.unlink(missing_ok=True)
        sockets.rmdir()
        if proc.poll() is None:
            stop(signal.SIGTERM, None)
            proc.wait()
        for helper in helpers:
            if helper.poll() is None:
                helper.kill()
                helper.wait()
        subprocess.run([*crun, "delete", "--force", name], capture_output=True)

