"""Does each guest see only its own closure, and can it still add paths?

Each guest's system is on the host, since the run built both, but only
in its own guest's store. So each guest must see its own system and not
the other's: the other's exists on the host, which is what makes its
absence mean something. Then a guest adds a path and Nix accepts it.

Last, each guest takes the others' closures while it runs, as a switch
to a configuration built after boot does: the files arrive, not empty
mount points, and Nix in the guest holds the paths valid.
"""

from pathlib import Path

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    systems = {
        name: (await vm.succeed("readlink -f /run/current-system")).strip()
        for name, vm in vms.items()
    }
    for name, vm in vms.items():
        for other, system in systems.items():
            if other == name:
                await vm.succeed(f"test -e {system}")
                continue
            if not Path(system).exists():
                raise AssertionError(f"{other}'s system {system} is not on the host")
            rc, _ = await vm.execute(f"test -e {system}")
            if rc == 0:
                raise AssertionError(f"{name} sees {other}'s system {system}")
        count = (await vm.succeed("ls /nix/store | wc -l")).strip()
        print(f"[test] {name}: {count} store entries; its own system, not the other's")

    for name, vm in vms.items():
        added = (await vm.succeed(f"echo {name} > /tmp/f && nix-store --add /tmp/f")).strip()
        await vm.succeed(f"nix-store --verify-path {added}")
        print(f"[test] {name} added {added} to its store")

    for name, vm in vms.items():
        for other, peer in vms.items():
            if other == name:
                continue
            system = systems[other]
            await vm.fail(f"nix-store --check-validity {system}")
            assert peer.spec.store_paths is not None
            added = await vm.add_closure(str(peer.spec.store_paths.parent))
            if system not in added:
                raise AssertionError(f"{name} was not given {other}'s system: {added}")
            await vm.succeed(f"test -x {system}/init")
            await vm.succeed(f"nix-store --verify-path {system}")
            print(f"[test] {name} took {other}'s closure while running: {len(added)} paths")
