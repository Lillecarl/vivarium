"""L3 over EVPN: symmetric routing between VLANs in VRF RED.

A server on VLAN 10 reaches one on VLAN 20, through its leaf's anycast
gateway and the L3 VNI. A leaf advertises a host route only once it has
heard from the host: a silent server is not routed until it speaks. The
route is in the tenant's VRF only: the underlay's own table never
carries a tenant prefix.
"""

from fabric import eventually, ping
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    servers = vms.settings["clos"]["servers"]
    source = next(name for name, server in servers.items() if server["vlan"] == "10")
    target = next(name for name, server in servers.items() if server["vlan"] == "20")
    leaf = servers[source]["leaf"]
    address = servers[target]["address"]

    silent = await vms[leaf].succeed(f"ip route show vrf RED {address}")
    if silent.strip():
        raise AssertionError(f"{leaf} routes {target} before it said anything: {silent}")
    for name, server in servers.items():
        gateway = vms.settings["clos"]["gateways"][server["vlan"]]
        await eventually(vms[name], ping(gateway))
    print(f"[test] {leaf} had no route to {target} until each server greeted its gateway")

    await eventually(vms[source], ping(address))
    print(f"[test] {source} on VLAN 10 reaches {target} on VLAN 20, routed")

    tenant = await eventually(vms[leaf], f"ip route show vrf RED {address} | grep .")
    underlay = await vms[leaf].succeed(f"ip route show table main {address}; ip route show table main 10.1.20.0/24")
    if underlay.strip():
        raise AssertionError(f"{leaf}'s main table carries a tenant route: {underlay}")
    print(f"[test] {leaf} routes {address} in VRF RED ({tenant.strip()}) and not in its main table")
