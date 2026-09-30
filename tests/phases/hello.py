"""A phase that does one small thing; see `interactive`."""

from uml_runner import Machines


async def test(vms: Machines) -> None:
    print(f"[test] {(await vms.one.succeed('hostname')).strip()} says hello")
