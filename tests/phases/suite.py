#!/usr/bin/env python3
"""One script, two phases: `vms.phase` tells them apart.

`census` leaves a value in `vms.shared` for `suite`. `suite` stands in
for a test runner inside the guest: it writes JUnit to /artifacts/junit,
which the session reads back as cases when the phase ends.
"""

from vivarium_runner import Machines

JUNIT = """<testsuites><testsuite name="inner">
<testcase classname="inner.test_a" name="test_ok" time="0.5"/>
<testcase classname="inner.test_a" name="test_bad" time="0.1"><failure message="assert 1 == 2">boom</failure></testcase>
</testsuite></testsuites>"""


async def test(vms: Machines) -> None:
    vm = vms.one
    if vms.phase == "census":
        vms.shared["before"] = int((await vm.succeed("ls /proc | grep -c '^[0-9]'")).strip())
        print(f"[test] census: {vms.shared['before']} processes")
        return
    if vms.phase != "suite":
        raise AssertionError(f"this script serves census and suite, not {vms.phase!r}")
    if "before" not in vms.shared:
        raise AssertionError("census left nothing in vms.shared")
    await vm.succeed("mkdir -p /artifacts/junit")
    await vm.succeed(f"cat > /artifacts/junit/inner.xml <<'EOF'\n{JUNIT}\nEOF")
    print(f"[test] suite: saw the census of {vms.shared['before']}")
