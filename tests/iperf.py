#!/usr/bin/env python3
"""Throughput over the segment between two guests.

Runs iperf3 in both directions against the server unit each guest is
already running, and prints the summary lines.
"""

import json

from uml_runner import Machine, Machines

DURATION = 5


async def measure(source: Machine, target: Machine) -> float:
    """Run iperf3 from *source* to *target*; returns bits/second."""
    report = json.loads(
        await source.succeed(
            f"iperf3 --client {target.ip} --time {DURATION} --json",
            timeout=DURATION + 30,
        )
    )
    return report["end"]["sum_received"]["bits_per_second"]


async def test(vms: Machines) -> None:
    server, client = vms.server, vms.client

    for vm in (server, client):
        await vm.wait_for_unit("iperf3-server.service")
    await server.succeed(f"ping -c2 {client.ip}")

    for source, target in ((server, client), (client, server)):
        rate = await measure(source, target)
        print(
            f"[test] {source.name} -> {target.name}: {rate / 1e9:.2f} Gbit/s "
            f"at mtu {source.spec.mtu}"
        )

