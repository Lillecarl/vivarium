"""What every phase of frr-clos asks of the fabric."""

from __future__ import annotations

import json
import time

import anyio

from vivarium_runner import Machine, Machines

CONVERGE = 90.0
"""Seconds for BGP to settle after boot or a link change."""


def switches(vms: Machines) -> dict[str, Machine]:
    clos = vms.settings["clos"]
    return {name: vms[name] for name in [*clos["spines"], *clos["leaves"]]}


def loopbacks(vms: Machines) -> dict[str, str]:
    clos = vms.settings["clos"]
    return {name: one["loopback"] for name, one in {**clos["spines"], **clos["leaves"]}.items()}


async def vtysh(vm: Machine, command: str) -> dict:
    return json.loads(await vm.succeed(f"vtysh -c '{command} json'"))


async def established(vm: Machine, timeout: float = CONVERGE) -> None:
    """Every BGP peer of *vm* up, for IPv4 and for EVPN."""
    deadline = time.monotonic() + timeout
    while True:
        summary = await vtysh(vm, "show bgp summary")
        down = [
            f"{family}/{peer}: {state['state']}"
            for family in ("ipv4Unicast", "l2VpnEvpn")
            for peer, state in summary.get(family, {}).get("peers", {}).items()
            if state["state"] != "Established"
        ]
        families = [family for family in ("ipv4Unicast", "l2VpnEvpn") if summary.get(family, {}).get("peers")]
        if not down and len(families) == 2:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"{vm.name}: BGP not established: {down or summary}")
        await anyio.sleep(1)


async def eventually(vm: Machine, command: str, timeout: float = CONVERGE) -> str:
    """*command* succeeds within *timeout*; returns its output."""
    deadline = time.monotonic() + timeout
    while True:
        rc, out = await vm.execute(command)
        if rc == 0:
            return out
        if time.monotonic() > deadline:
            raise AssertionError(f"{vm.name}: still failing after {timeout:.0f}s: {command}\n{out}")
        await anyio.sleep(1)


async def never(vm: Machine, command: str, timeout: float = CONVERGE) -> None:
    """*command* fails within *timeout*, as the fabric withdraws a route."""
    deadline = time.monotonic() + timeout
    while True:
        rc, out = await vm.execute(command)
        if rc != 0:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"{vm.name}: still succeeding after {timeout:.0f}s: {command}\n{out}")
        await anyio.sleep(1)


def nexthops(route: str) -> int:
    """Paths in `ip route show <prefix>` output: ECMP lists each as `nexthop`."""
    many = route.count("nexthop")
    return many if many else (1 if route.strip() else 0)


def ping(address: str, source: str | None = None) -> str:
    return f"ping -c 1 -W 2 {f'-I {source} ' if source else ''}{address}"
