"""Several segments per guest, each interface named as declared.

`r` (UML) is on `a` as `left` and on `b` as `right`. `q` (QEMU) is on `a`
as `up`, `c` (a container) on `b` as `down`, and the two share `ll` with
link-local addresses only. Every name is checked against its MAC and its
addresses, and every link against the segment it claims: a ping out of
the wrong interface must fail, and nothing crosses from `a` to `b`.
"""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    for name, vm in vms.items():
        for nic in vm.spec.interfaces:
            mac = (await vm.succeed(f"cat /sys/class/net/{nic.name}/address")).strip()
            if mac != nic.mac:
                raise AssertionError(f"{name}: {nic.name} has MAC {mac}, expected {nic.mac}")
            shown = await vm.succeed(f"ip -br addr show dev {nic.name}")
            for address in nic.addresses:
                if address not in shown:
                    raise AssertionError(f"{name}: {nic.name} lacks {address}: {shown}")
        names = ", ".join(nic.name for nic in vm.spec.interfaces)
        print(f"[test] {name}: {names}, each with its MAC and addresses")

    r, q, c = vms.r, vms.q, vms.c
    await q.succeed("ping -c 2 -W 5 r.a")
    await q.succeed("ping -6 -c 2 -W 5 fd70:1::1")
    await c.succeed("ping -c 2 -W 5 r.b")
    print("[test] q reaches r on a, over IPv4 and IPv6, and c reaches r on b")

    # The name maps to the segment it claims: out of the other one, no reply.
    await r.succeed(f"ping -c 1 -W 5 -I {r.interface('a').name} 10.70.1.2")
    await r.fail(f"ping -c 1 -W 2 -I {r.interface('b').name} 10.70.1.2")
    await q.fail("ping -c 1 -W 2 10.70.2.2")
    print("[test] r answers on a only through left, and nothing crosses from a to b")

    side = c.interface("ll").name
    local = (await c.succeed(f"ip -6 -br addr show dev {side} scope link")).split()[2]
    link_local = local.split("/")[0]
    await q.succeed(f"ping -6 -c 2 -W 5 {link_local}%{q.interface('ll').name}")
    await q.fail(f"ping -6 -c 1 -W 2 {link_local}%{q.interface('a').name}")
    print(f"[test] q reaches c's {link_local} over ll, and not over a")
