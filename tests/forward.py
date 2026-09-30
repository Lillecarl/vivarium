#!/usr/bin/env python3
"""One guest, reached from the host.

Everything else here drives guests over the serial line, which works
before networking exists and says nothing about whether anyone outside
can reach a service inside.  This is the test that connects the other
way: the host opens a TCP connection to an address passt is listening on
and expects a process in the guest to answer it.

The three things worth guarding, in order of how quietly they break:

  * a port bound in the guest after boot is reachable anyway, which is
    the whole reason `ports = "all"` exists -- passt cannot be told
    about a forward once it is running;
  * a privileged port is reachable at its offset, because nothing here
    may bind below `net.ipv4.ip_unprivileged_port_start`;
  * a port in the host's ephemeral range is *not* reachable, and says so
    rather than appearing to work.
"""

import asyncio
import urllib.error
import urllib.request

from vivarium_runner import Machine, Machines
from vivarium_runner.forward import ephemeral_range

PAGE = "hello-from-inside-the-guest"
DOC_ROOT = "/tmp/www"

PLAIN = 8080
"""An ordinary port, bound long after passt stopped taking arguments."""

PRIVILEGED = 80
"""Bound in the guest as root; the host cannot listen this low, so the
runner is expected to have moved it up by `privilegedOffset`."""


async def serve(vm: Machine, port: int) -> None:
    """Start a web server in the guest on *port* and leave it running."""
    await vm.succeed(
        f"mkdir -p {DOC_ROOT} && echo {PAGE} > {DOC_ROOT}/probe"
    )
    await vm.succeed(
        f"systemd-run --unit=probe-{port} --collect"
        f" --working-directory={DOC_ROOT}"
        f" python3 -m http.server {port}"
    )


async def fetch(where: str, timeout: float = 30) -> str:
    """GET /probe from the host side, giving the guest time to bind."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            return await asyncio.to_thread(
                lambda: urllib.request.urlopen(
                    f"http://{where}/probe", timeout=5
                )
                .read()
                .decode()
                .strip()
            )
        except (urllib.error.URLError, OSError) as error:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"nothing answered on {where}: {error}")
            await asyncio.sleep(0.5)


async def test(vms: Machines) -> None:
    vm = vms.node

    # `Rule.address` is None until `resolve_forward` fills it in.
    address = vm.forward[0].address
    print(f"[test] guest was given {address}")
    assert address is not None, "the guest booted with an unresolved forward"
    assert address.startswith("127.0.0."), f"expected a loopback address, got {address}"

    await vm.wait_for_unit("sshd.service")
    assert vm.reachable(vm.spec.ssh_port), "sshd's port is not forwarded"

    # Nothing has bound either of these yet, and passt has been running
    # since before the guest booted.
    for port in (PLAIN, PRIVILEGED):
        await serve(vm, port)

    where = vm.reachable(PLAIN)
    assert where == [f"{address}:{PLAIN}"], where
    assert await fetch(where[0]) == PAGE
    print(f"[test] port bound after boot answers on {where[0]}")

    where = vm.reachable(PRIVILEGED)
    assert where == [f"{address}:{PRIVILEGED + 10000}"], where
    assert await fetch(where[0]) == PAGE
    print(f"[test] privileged {PRIVILEGED} answers on {where[0]}")

    listening = await vm.listening()
    for port in (PLAIN, PRIVILEGED, vm.spec.ssh_port):
        assert port in listening, f"{port} missing from {listening}"
    print(f"[test] guest reports listening on {', '.join(map(str, listening))}")

    # passt skips the host's ephemeral range, and saying so is the point:
    # a service there comes up fine and is simply unreachable.
    low, _ = ephemeral_range()
    assert vm.reachable(low) == [], f"{low} should not be forwarded"
    print(f"[test] ephemeral {low} correctly reports as not forwarded")

