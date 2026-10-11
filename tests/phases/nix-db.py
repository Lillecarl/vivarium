"""A read-only reader of the guest's Nix database, as harmonia's cache is
one; see `nix-db` in default.nix.

The control removes the WAL files the boot unit kept, and the same
reader fails. The unit puts them back.
"""

from vivarium_runner import Machines

READ = (
    "systemd-run -p DynamicUser=yes --quiet --pipe --wait sqlite3 "
    "'file:/nix/var/nix/db/db.sqlite?mode=ro' 'select count(*) from ValidPaths'"
)


async def test(vms: Machines) -> None:
    one = vms.one
    count = (await one.succeed(READ)).strip()
    assert int(count.splitlines()[-1]) > 0, count
    print(f"[test] an unprivileged read-only reader sees {count.splitlines()[-1]} valid paths")

    await one.succeed("rm /nix/var/nix/db/db.sqlite-wal /nix/var/nix/db/db.sqlite-shm")
    code, said = await one.execute(READ)
    assert code != 0 and "unable to open database file" in said, (code, said)
    print("[test] without the WAL files the same reader fails")

    await one.succeed("systemctl restart vivarium-nix-db.service")
    await one.succeed(READ)
    print("[test] the boot unit puts them back, and the reader works again")
