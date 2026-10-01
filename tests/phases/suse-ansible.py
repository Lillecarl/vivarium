"""Ansible on a NixOS guest configures openSUSE over a segment."""

from vivarium_runner import Machines

ANSIBLE = (
    "ANSIBLE_HOST_KEY_CHECKING=False ANSIBLE_LOCAL_TEMP=/tmp/ansible "
    "ansible-playbook --private-key /tmp/key -u ansible -i {inventory} {playbook}"
)


async def test(vms: Machines) -> None:
    suse, ctl = vms.suse, vms.ctl
    lab = (await suse.succeed("ip -br -4 addr show lab")).split()
    if "10.9.0.2/24" not in lab:
        raise AssertionError(f"SUSE's lab interface is {lab}")
    print(f"[test] SUSE named its segment link `lab` and holds {lab[2]}, from the seed's network-config")

    await ctl.succeed(f"install -m 0600 {vms.settings['key']} /tmp/key")
    playbook = vms.settings["playbook"]

    status, out = await ctl.execute(
        ANSIBLE.format(inventory="'suse,' -e ansible_host=10.9.0.99", playbook=playbook), timeout=120
    )
    if status == 0 or "UNREACHABLE" not in out:
        raise AssertionError(f"ansible against nothing exited {status}:\n{out}")
    print("[test] against an address nobody holds, ansible fails UNREACHABLE")

    out = await ctl.succeed(
        ANSIBLE.format(inventory="'suse,' -e ansible_host=10.9.0.2", playbook=playbook), timeout=300
    )
    recap = next((line for line in out.splitlines() if line.startswith("suse")), out)
    said = (await suse.succeed("cat /etc/vivarium-ansible")).strip()
    if said != "openSUSE Leap 16.0":
        raise AssertionError(f"the playbook wrote {said!r}:\n{out}")
    await suse.succeed("id deploy")
    print(f"[test] the playbook ran on {said}: {' '.join(recap.split())}")
