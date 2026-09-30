#!/usr/bin/env python3
"""A guest as a rootless container: systemd up, and a service as nobody.

`systemd-detect-virt` answers `container-other` under crun, which proves
this is not a UML or a QEMU guest. The `nobody` service is the part that
needs a range of ids: with root alone mapped it fails at the GROUP step.

The same script runs by hand and in the `uid-range` sandbox. The sandbox
has no /dev/net/tun, so no uplink, and its store is one bind per input,
so the guest's store is read-only there. Each part says when it is
skipped, so a sandboxed pass never reads as a full one.
"""

import socket
from pathlib import Path

from vivarium_runner import Machine, Machines
from vivarium_runner.container import store_is_one_mount, tap_fails


async def test(vms: Machines) -> None:
    one = vms.one
    kind = (await one.execute("systemd-detect-virt --container"))[1].strip()
    print(f"[test] systemd-detect-virt: {kind}")
    if kind in ("none", ""):
        raise AssertionError(f"expected a container, got {kind!r}")

    await one.succeed("test $(hostname) = one")
    await one.wait_for_unit("multi-user.target")
    failed = await one.succeed("systemctl --failed --no-legend --plain")
    if failed.strip():
        raise AssertionError(f"failed units:\n{failed}")

    who = await one.succeed("runuser -u nobody -- id -u")
    if who.strip() != "65534":
        raise AssertionError(f"nobody runs as {who.strip()}")
    print("[test] systemd is up, nothing failed, and nobody is 65534")

    # What a login shell gets, as on the other backends. `daemon` here made
    # a user's own `nix daemon` connect to itself.
    remote = (await one.succeed("bash -lc 'echo $NIX_REMOTE'")).strip()
    if remote != "auto":
        raise AssertionError(f"a login shell has NIX_REMOTE={remote!r}, expected 'auto'")
    print("[test] NIX_REMOTE is auto, as on the other backends")

    # A user's ssh reads the system config, and refuses an included file
    # owned by neither root nor that user. The store reads as uid 65534 in
    # a rootless container, so nothing it includes may come from the store.
    # Not `nobody`: 65534 is nobody, the one user the store belongs to.
    rc, out = await one.execute("runuser -u sshd -- ssh -G localhost >/dev/null")
    if rc != 0:
        raise AssertionError(f"a user's ssh refuses the system config:\n{out}")
    print("[test] a user's ssh reads the system config")

    # What the host pays, as for the other backends: the processes' PSS,
    # since a container has no memory file.
    kib = one.host_memory_kib()
    if kib <= 0:
        raise AssertionError(f"the host pays {kib} KiB for a running guest")
    print(f"[test] the host pays {kib // 1024} MiB for this guest")

    if store_is_one_mount("/nix"):
        await _writable_store(one)
    else:
        print("[test] skipped: a writable store (this store is one bind per input)")

    # The runner's own answer, so the phase and the launcher cannot disagree.
    if tap_fails() is None:
        await _uplink(one)
    else:
        print("[test] skipped: the uplink and forwards (no tap device here)")


async def _writable_store(one: Machine) -> None:
    # The guest adds a path, and the host's store does not get it.
    added = (await one.succeed("echo from-the-guest > /tmp/f && nix-store --add /tmp/f 2>/dev/null")).strip()
    await one.succeed(f"test -e {added} && nix-store --verify-path {added}")
    if Path(added).exists():
        raise AssertionError(f"{added} reached the host's store")
    print(f"[test] the guest added {added} to its own store")

    # A build in the guest's own Nix sandbox: a user namespace inside the
    # container's, which the host allows (measured before building this).
    built = (
        await one.succeed(
            "nix-build --no-out-link --option sandbox true -E "
            "'derivation { name = \"in-the-guest\"; system = builtins.currentSystem;"
            " builder = \"/bin/sh\"; args = [ \"-c\" \"echo built > $out\" ]; }'"
            # The agent returns stderr after stdout; the path is stdout.
            " 2>/dev/null"
        )
    ).strip()
    if (await one.succeed(f"cat {built}")).strip() != "built":
        raise AssertionError(f"{built} does not hold what the builder wrote")
    print(f"[test] the guest built {built} in its own sandbox")


async def _uplink(one: Machine) -> None:
    # pasta's DHCP gives vec0 the address passt gives the other backends,
    # and its DNS forwarder answers.
    await one.succeed(
        "for i in $(seq 50); do ip -4 -o addr show vec0 | grep -q inet && exit 0; sleep 0.2; done; exit 1"
    )
    print(f"[test] vec0: {(await one.succeed('ip -4 -o addr show vec0')).split()[3]}")
    await one.succeed("getent hosts localhost")

    # A forward: the host reaches the guest's sshd through pasta.
    host, _, port = one.reachable(4325)[0].rpartition(":")
    with socket.create_connection((host, int(port)), timeout=10) as conn:
        banner = conn.recv(64)
    if not banner.startswith(b"SSH-"):
        raise AssertionError(f"{host}:{port} answered {banner!r}, not sshd")
    print(f"[test] the host reaches sshd at {host}:{port}")
