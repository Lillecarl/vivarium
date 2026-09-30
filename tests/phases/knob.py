#!/usr/bin/env python3
"""What a run was told from outside, and what a check is told instead.

The property this holds: a sandboxed run always takes the declared
default. `builtins.getEnv` answers `""` under a pure evaluation and an
unset variable answers `""` too, so the check and a flake consumer land
on the same value with no special case -- and CI cannot accidentally run
something other than the check because a variable was exported.
"""

from vivarium_runner import Machines

DEFAULT = "every-case"
"""Declared in default.nix. What the sandbox must see."""


async def test(vms: Machines) -> None:
    assert "selection" in vms.knobs, (
        f"a declared knob is missing: {sorted(vms.knobs)}"
    )
    value = vms.knobs["selection"]
    print(f"[test] selection={value!r}")

    # The guest gets it only because this phase hands it over. A knob is
    # not ambient: nothing in the guest's environment carries it, which
    # is what keeps a guest's behaviour a function of its configuration.
    out = await vms.one.succeed("echo $SELECTION")
    assert out.strip() == "", "a knob leaked into the guest without being passed"

    out = await vms.one.succeed("echo $SELECTION", env={"SELECTION": value})
    assert out.strip() == value, f"the guest saw {out.strip()!r}, not {value!r}"
    print("[test] and it reaches the guest when a phase passes it")
