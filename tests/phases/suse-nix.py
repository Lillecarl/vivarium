"""openSUSE with a writable /nix; see `suse-nix` in default.nix.

Nix in the guest opens a local store, knows the paths the guest booted
with, and adds a path of its own. A collection deletes that unrooted
path and keeps the agent: the agent answers after it.
"""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    suse = vms.suse
    # No nixbld group on the image, so no build users.
    nix = f"NIX_CONFIG='build-users-group =' {vms.settings['nix']}"
    nix_path = vms.settings["nix"]
    pid = (await suse.succeed("systemctl show -p MainPID --value vivarium-agent")).strip()
    agent = (await suse.succeed(f"readlink -f /proc/{pid}/exe")).strip()
    assert agent.startswith("/nix/store/"), agent

    valid = await suse.succeed(f"{nix}/bin/nix-store --check-validity {nix_path} && echo valid")
    assert valid.strip() == "valid", valid
    print("[test] Nix on openSUSE opens a local store that knows its own closure")

    added = (await suse.succeed(f"echo garbage > /tmp/g && {nix}/bin/nix-store --add /tmp/g")).strip()
    await suse.succeed(f"test -f {added}")
    print(f"[test] the guest adds {added} to its store")

    # The agent's units are the second root, and no process holds them
    # open, so only their root keeps them: drop it, and they go.
    roots = "/nix/var/nix/gcroots/vivarium"
    units = (await suse.succeed(f"readlink {roots}/2")).strip()
    await suse.succeed(f"rm {roots}/2")
    await suse.succeed(f"{nix}/bin/nix-store --gc")
    await suse.fail(f"test -e {added}")
    await suse.fail(f"test -e {units}")
    await suse.succeed(f"for r in {roots}/*; do test -e $(readlink $r); done")
    await suse.succeed(f"test -e {agent}")
    release = await suse.succeed(". /etc/os-release && echo $ID")
    assert release.strip() == "opensuse-leap", release
    print("[test] a collection deleted the unrooted paths, kept every rooted one, "
          "and the agent still answers")
