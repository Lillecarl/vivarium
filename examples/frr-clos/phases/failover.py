"""A leaf loses one spine, then both, then gets them back.

With one uplink the fabric carries on over the other spine. With none
the leaf is cut off. With both back it spreads over two spines again.

A link is cut with nftables, not `ip link set down`: the link stays up
and BGP finds out from its hold timer, as in a real failure. Under UML a
link once down never comes up again: the vector driver closes the
segment fd on down (measured: "Network is unreachable" on up).
"""

from fabric import established, eventually, loopbacks, never, nexthops, ping
from vivarium_runner import Machine, Machines


async def cut(vm: Machine, interface: str) -> None:
    await vm.succeed(
        "nft add table inet cut; "
        + "; ".join(
            f"nft add chain inet cut {hook} '{{ type filter hook {hook} priority 0; }}'"
            for hook in ("input", "output", "forward")
        )
    )
    for hook, match in (("input", "iifname"), ("output", "oifname"), ("forward", "iifname"), ("forward", "oifname")):
        await vm.succeed(f"nft add rule inet cut {hook} {match} {interface} drop")


async def mend(vm: Machine) -> None:
    await vm.succeed("nft delete table inet cut")


async def test(vms: Machines) -> None:
    clos = vms.settings["clos"]
    spines = list(clos["spines"])
    leaves = list(clos["leaves"])
    leaf, other = vms[leaves[0]], leaves[-1]
    addresses = loopbacks(vms)
    target, source = addresses[other], addresses[leaf.name]
    servers = clos["servers"]
    tenant = next(name for name, server in servers.items() if server["leaf"] == leaf.name)
    remote = next(server["address"] for server in servers.values() if server["leaf"] == other)

    await cut(leaf, spines[0])
    await eventually(leaf, f"test $(ip route show {target} | grep -c nexthop) = 0")
    route = await leaf.succeed(f"ip route show {target}")
    if nexthops(route) != 1:
        raise AssertionError(f"{leaf.name} without {spines[0]}: {route}")
    await eventually(leaf, ping(target, source))
    await eventually(vms[tenant], ping(remote))
    print(f"[test] without {spines[0]}, {leaf.name} reaches {other} over {spines[1]}, and so does {tenant}")

    for spine in spines[1:]:
        await cut(leaf, spine)
    await never(leaf, ping(target, source))
    await never(vms[tenant], ping(remote))
    print(f"[test] with every uplink down, {leaf.name} and {tenant} reach nothing")

    await mend(leaf)
    await established(leaf)
    await eventually(leaf, f"test $(ip route show {target} | grep -c nexthop) = {len(spines)}")
    await eventually(vms[tenant], ping(remote))
    print(f"[test] with the uplinks back, {leaf.name} spreads over {len(spines)} spines again")
