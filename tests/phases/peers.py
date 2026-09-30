"""Does every guest get `defaults`, and know its peers by name?

`defaults` writes /etc/vivarium-defaults into every guest. Each guest must
resolve the other by hostname to its `vec1` address, from /etc/hosts
alone, and reach it by that name.
"""

from vivarium_runner import Machines

ADDRESSES = {"server": "192.168.99.2", "client": "192.168.99.3"}


async def test(vms: Machines) -> None:
    for name, vm in (("server", vms.server), ("client", vms.client)):
        await vm.succeed("grep -qx from-defaults /etc/vivarium-defaults")
        peer = "client" if name == "server" else "server"
        resolved = (await vm.succeed(f"getent hosts {peer}")).split()[0]
        if resolved != ADDRESSES[peer]:
            raise AssertionError(f"{name} resolves {peer} to {resolved}, not {ADDRESSES[peer]}")
        await vm.succeed(f"ping -c 1 -W 2 {peer}")
        print(f"[test] {name}: defaults applied; {peer} is {resolved} and answers")
