"""containerd's overlay snapshotter tests, as root, where they do not skip.

`fixedTests` is the fix with its updated test, and must pass. `controlTests`
is the same test against the unfixed code, and must fail: otherwise the
test does not tell the two apart.
"""

from vivarium_runner import MachineError, Machines

# -test.root, or containerd's testutil skips every test that needs root,
# and a skip passes: measured, both binaries said PASS without it.
RUN = "-test.root -test.run '^TestOverlay$' -test.v -test.count=1"


async def test(vms: Machines) -> None:
    (_, vm), = vms.items()
    settings = vms.settings
    results = {}
    for which in ("fixedTests", "controlTests"):
        rc, out = await vm.execute(f"cd /tmp && {settings[which]} {RUN} 2>&1", timeout=900)
        lines = out.strip().splitlines()
        print(f"[{which}] exit {rc}, {len(lines)} lines; the end of it:")
        for line in lines[-25:]:
            print(f"[{which}] {line}")
        results[which] = rc
    if results["fixedTests"] != 0:
        raise MachineError("the fix does not pass its own test")
    if results["controlTests"] == 0:
        raise MachineError("the updated test passes on the unfixed code, so it tells nothing apart")
    print("[unit] the fix passes; the unfixed code fails the same test")
