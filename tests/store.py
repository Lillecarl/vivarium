#!/usr/bin/env python3
"""One guest whose Nix can see the whole host store.

`vivarium.hostStore` puts the host's store under the guest's own writable
layer as a Nix local-overlay store.  This checks the two halves of that:

  * a path the guest was never told about, and that is not in its closure,
    is valid in there -- that comes from the lower store's database;
  * a derivation built inside the guest lands in the upper layer and is
    valid too.

Outside the build sandbox only.  A sandbox `/nix` holds `store` and nothing
else, so there is no host database to be the lower layer:

    nix run --file . store.run        # and store.qemu.run
"""

import json
import os
import subprocess

from vivarium_runner import Machines

# The same view of the host's store that the guest gets, used here to pick
# a path that view actually has.
HOST_STORE = "local?root=/&read-only=true"

# The agent hands back stdout and stderr together, and Nix narrates a
# build on stderr, so keep the path the only thing left.
PROBE = (
    "nix build --impure --no-link --print-out-paths --expr "
    "'derivation { name = \"host-store-probe\"; "
    'system = builtins.currentSystem; builder = "/bin/sh"; '
    "args = [ \"-c\" \"echo probe > $out\" ]; }' 2>/dev/null"
)


def host_paths() -> list[str]:
    """Every path the guest's lower store can see, asked of it directly.

    Not `realpath("/run/current-system")`, which is the obvious choice and
    is wrong: `read-only=true` opens the database with SQLite's `immutable`
    parameter, which ignores the write-ahead log, so a path registered on
    the host in the last few megabytes of writes is invisible to the guest
    however valid it is.  Measured -- a `nixos-rebuild` between two runs of
    this test was enough to hide the running system.

    Asking the read-only view what it has cannot go stale that way.
    """
    return _nix("--all").split()


def with_references(candidates: list[str]) -> str:
    """One of `candidates` that refers to something else.

    A path with no references proves only that the lower store has a row
    for it.  One with references proves the lower store served the
    metadata too, which is the half that a registration would otherwise
    explain -- and `--all` is in database order, so the first path is as
    likely as not to be a lone script.
    """
    # Not a .drv: one is a store path like any other, so it would pass,
    # but a build output is the thing a guest would actually want.
    wanted = [p for p in candidates if not p.endswith(".drv")]
    for batch in range(0, min(len(wanted), 400), 100):
        window = wanted[batch : batch + 100]
        info = json.loads(_nix("--json", *window))
        for path in window:
            if info.get(path, {}).get("references"):
                return path
    raise AssertionError("no path in the lower store's first 400 has references")


def _nix(*args: str) -> str:
    seen = subprocess.run(
        [
            "nix",
            "--extra-experimental-features",
            "read-only-local-store",
            "path-info",
            "--store",
            HOST_STORE,
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return seen.stdout


async def test(vms: Machines) -> None:
    node = vms.node
    await node.wait_for_unit("uml-host-store.service")

    # What `settings` handed over, registered by the session because it is in
    # `settings` and for no other reason.
    probe = vms.settings["probe"]
    await node.succeed(f"nix path-info {probe}")
    await node.succeed(f"test $(cat {probe}) = settings")
    print(f"[test] the guest knows the path settings gave it: {probe}")

    closure = set((await node.succeed("nix-store --query --requisites /run/current-system")).split())
    outside = [p for p in host_paths() if p not in closure]
    assert outside, "the host's store holds nothing the guest is not already told about"
    path = with_references(outside)

    print(f"[test] the guest was never told about {path}")
    print("[test] and says:", await node.succeed(f"nix path-info {path}"))

    refs = await node.succeed(f"nix-store --query --references {path}")
    assert refs.strip(), "the lower store knows the path but serves no references for it"
    print(f"[test] and serves {len(refs.split())} references for it")

    built = (await node.succeed(PROBE)).strip()
    print(f"[test] the guest built {built}")
    await node.succeed(f"test $(cat {built}) = probe")
    await node.succeed(f"nix path-info {built}")
    await node.succeed(f"test -e /.nix-upper/store/{os.path.basename(built)}")
    print("[test] and it landed in the upper layer")

    # `--all` reads the upper database alone, so this counts the
    # registration and what the guest built -- not the lower store, which
    # answers about a path but cannot be listed. Measured, and the reason
    # nothing above asserts on this number.
    total = (await node.succeed("nix path-info --all | wc -l")).strip()
    print(f"[test] the guest's upper database holds {total} paths")

