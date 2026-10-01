"""Switch a running guest between its configurations; see `switch` in
default.nix."""

from vivarium_runner import Machines, MachineError


async def agent_pid(vms: Machines) -> str:
    return (await vms.one.unit_info("vivarium-agent.service"))["MainPID"]


async def current(vms: Machines) -> str:
    return (await vms.one.succeed("readlink /run/current-system")).strip()


async def test(vms: Machines) -> None:
    one = vms.one
    two = one.spec.configurations["two"]
    booted = one.spec.toplevel
    assert await current(vms) == booted
    assert (await one.succeed("cat /etc/vivarium-config")).strip() == "one"
    assert await one.unit_state("only-in-two.service") == "inactive"
    agent = await agent_pid(vms)

    try:
        await one.switch_to("nope")
    except MachineError as error:
        assert "two" in str(error), error
    else:
        raise AssertionError("an unknown configuration switched")

    # `test` activates without touching the profile, as nixos-rebuild test.
    await one.switch_to("two", "test")
    assert await current(vms) == two
    assert await one.unit_state("only-in-two.service") == "active"
    await one.fail("test -e /nix/var/nix/profiles/system")

    await one.switch_to("two")
    profile = await one.succeed("readlink -f /nix/var/nix/profiles/system")
    assert profile.strip() == two, profile
    assert (await one.succeed("cat /etc/vivarium-config")).strip() == "two"

    await one.switch_to()
    assert await current(vms) == booted
    assert (await one.succeed("cat /etc/vivarium-config")).strip() == "one"
    assert await one.unit_state("only-in-two.service") == "inactive"
    profile = await one.succeed("readlink -f /nix/var/nix/profiles/system")
    assert profile.strip() == booted, profile
    generations = await one.succeed(
        "nix-env --profile /nix/var/nix/profiles/system --list-generations"
    )
    assert len(generations.strip().splitlines()) == 2, generations

    # `two` changes the agent's unit, so only restartIfChanged kept it.
    assert await agent_pid(vms) == agent, "a switch restarted the agent"
    print(f"[test] agent {agent} survived three switches")
