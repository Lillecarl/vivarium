"""L2 over EVPN: one VLAN on two leaves, joined by its VNI.

Two servers in one subnet on different leaves reach each other. The
first leaf learns the far server's MAC from BGP, not from flooding. With
the far leaf's VXLAN device down, the stretch is gone.
"""

from fabric import eventually, never, ping, vtysh
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    servers = vms.settings["clos"]["servers"]
    stretched = [name for name, server in servers.items() if server["vlan"] == "10"]
    near, far = stretched[0], stretched[1]
    near_leaf, far_leaf = servers[near]["leaf"], servers[far]["leaf"]
    vni = servers[far]["vni"]

    await eventually(vms[near], ping(servers[far]["address"]))
    print(f"[test] {near} on {near_leaf} reaches {far} on {far_leaf} in one subnet")

    mac = vms[far].interface(f"{far_leaf}-{far}").mac
    macs = (await vtysh(vms[near_leaf], f"show evpn mac vni {vni}")).get("macs", {})
    if macs.get(mac, {}).get("type") != "remote":
        raise AssertionError(f"{near_leaf} has no remote EVPN MAC {mac} on VNI {vni}: {macs}")
    print(f"[test] {near_leaf} learned {far}'s MAC {mac} over EVPN, as remote")

    await vms[far_leaf].succeed(f"ip link set vni{vni} down")
    await never(vms[near], ping(servers[far]["address"]))
    print(f"[test] with vni{vni} down on {far_leaf}, {near} no longer reaches {far}")
    await vms[far_leaf].succeed(f"ip link set vni{vni} up")
    await eventually(vms[near], ping(servers[far]["address"]))
    print(f"[test] and again once it is up")
