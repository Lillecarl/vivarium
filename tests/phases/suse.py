"""openSUSE Leap under UML, driven like any other guest.

The agent answers from SUSE's userspace. A Python the host built after
boot is absent, then runs once `add_closure` has put it in the guest's
store view: a cloud image guest takes host tools at run time with no
package manager. `switch_to` refuses, naming why.
"""

from vivarium_runner import MachineError, Machines


async def test(vms: Machines) -> None:
    suse = vms.suse
    release = await suse.succeed(". /etc/os-release && echo $ID $VERSION_ID")
    if release.strip() != "opensuse-leap 16.0":
        raise AssertionError(f"the guest says it is {release!r}")
    print(f"[test] the agent answers from {release.strip()}")

    python = vms.settings["python"]
    if (await suse.execute(f"test -e {python['bin']}"))[0] == 0:
        raise AssertionError(f"{python['bin']} is in the guest before add_closure")
    await suse.add_closure(python["closure"])
    said = await suse.succeed(
        f"{python['bin']} -c 'import platform, requests; print(platform.freedesktop_os_release()[\"ID\"])'"
    )
    if said.strip() != "opensuse-leap":
        raise AssertionError(f"the host's Python says {said!r}")
    print("[test] a Python the host built after boot runs in the guest once add_closure has it")

    try:
        await suse.switch_to()
    except MachineError as error:
        if "vivarium.image" not in str(error):
            raise
    else:
        raise AssertionError("switch_to on a cloud image guest did not refuse")
    print("[test] switch_to refuses, naming vivarium.image")
