#!/usr/bin/env python3
"""A guest gives its memory back, by itself and on demand.

Two mechanisms, and this checks each where it is visible. Both backends,
and the script cannot tell which one it is on -- which is the point: UML
reports free pages through `madvise(MADV_REMOVE)` on the file its memory
lives in, QEMU through virtio-balloon on the memfd its memory lives in,
and `host_memory_kib()` is one number either way.

- Free page reporting, so what the host pays falls on its own within
  seconds of the guest freeing anything. Measured from the host, because
  no number inside the guest can see it.
- The balloon, which takes pages out of the guest on demand. Measured
  inside the guest, because after reporting has run there is little left
  for it to return to the host -- what it still does is squeeze the
  guest and let go again.
"""

import asyncio

from uml_runner import Machine, Machines

#: What the guest reads: every distinct file of its own system closure.
#: Measured at 362 MiB over 16002 files, and 17 seconds for the run.
#:
#: Distinct is the whole of it. `sw` is a directory of aliases -- 5944
#: entries resolving to a few dozen store paths -- so reading it by name
#: reads the same inodes over and over and caches almost nothing. An
#: earlier version of this test did that and read 424 MiB to cache 52 MB.
#:
#: `|| true` because `xargs` exits 123 when any `cat` did, and one of
#: 16002 paths being unreadable says nothing about the memory this is
#: measuring. What the read achieved is asserted, not assumed.
READ = (
    "find -L /run/current-system -type f 2>/dev/null "
    "| xargs -r readlink -f 2>/dev/null | sort -u "
    "| xargs -r cat 2>/dev/null > /dev/null || true"
)

#: How much page cache the read has to produce for the rest to mean
#: anything, in kibibytes. Measured: 79 MB before, 441 MB after.
GREW_KIB = 200 * 1024

#: How much of what the read cost the host has to come back, as a
#: fraction. Measured: UML returns all of it, QEMU 82% -- its reporting
#: works in whole blocks the guest may not have free in one piece.
#:
#: A fraction rather than a distance from the boot figure, because the
#: two backends start from different places and a fixed margin that fits
#: both is either flaky on one or blind on the other.
SETTLED_SHARE = 2 / 3

#: Long enough to be a failure rather than a slow host. The framework
#: waits two seconds before it starts a cycle and reports an idle guest
#: in about thirty; this took three.
REPORT_TIMEOUT = 60

#: What the balloon is asked for. Smaller than what the cache held,
#: because it takes pages that are already free -- under UML it allocates
#: GFP_ATOMIC and cannot reclaim to make more.
SHRINK = "256M"
SHRINK_KIB = 256 * 1024

#: How much of that it has to get. Not all of it: UML takes the whole
#: amount, QEMU has measured between 189M and 220M, because its guest
#: inflates asynchronously and stops when it runs out of free pages.
SHRINK_SHARE = 0.5


async def settle(vm: Machine, target: int) -> int:
    """Wait for what the host pays to fall to *target* kibibytes.

    Polled from the host, because the guest is not told to do anything
    and has nothing to report when it has.
    """
    paying = vm.host_memory_kib()
    with vm.waiting("the host to stop paying for freed pages"):
        for _ in range(REPORT_TIMEOUT):
            paying = vm.host_memory_kib()
            if paying <= target:
                return paying
            await asyncio.sleep(1)
    raise AssertionError(
        f"the host still pays {paying // 1024}M after {REPORT_TIMEOUT}s, "
        f"wanted {target // 1024}M: free page reporting is not running, or "
        "the host does not support MADV_REMOVE where the guest's memory is"
    )


async def test(vms: Machines) -> None:
    vm = vms.node

    booted = vm.host_memory_kib()
    print(f"[test] at boot: host {booted // 1024}M")

    await vm.succeed(READ, timeout=300)
    cached = (await vm.meminfo())["Cached"]
    filled = vm.host_memory_kib()
    print(f"[test] after reading: guest cached {cached // 1024}M, host {filled // 1024}M")
    assert cached > GREW_KIB, (
        f"the guest cached {cached}kB, so the read did not land in its page "
        "cache and nothing after this measures anything"
    )
    assert filled - booted > GREW_KIB, (
        "the host did not start paying for that cache, so there is nothing "
        "for the rest of this test to give back"
    )

    await vm.drop_caches()
    dropped = (await vm.meminfo())["Cached"]
    assert dropped < cached // 2, "drop_caches freed nothing in the guest"

    settled = await settle(vm, filled - int((filled - booted) * SETTLED_SHARE))
    print(
        f"[test] the host stopped paying by itself: {filled // 1024}M -> "
        f"{settled // 1024}M, of {(filled - booted) // 1024}M the read cost it"
    )

    # The console, measured in the guest. Nothing is asked of the host
    # here: reporting has already taken those pages, and asking again
    # would be a race with it rather than a check of anything.
    free_before = (await vm.meminfo())["MemFree"]
    await vm.shrink(SHRINK)
    ballooned = (await vm.meminfo())["MemFree"]
    print(f"[test] after shrink {SHRINK}: guest free {free_before // 1024}M -> {ballooned // 1024}M")
    assert free_before - ballooned > SHRINK_KIB * SHRINK_SHARE, (
        f"asked the balloon for {SHRINK} and the guest only gave up "
        f"{(free_before - ballooned) // 1024}M"
    )

    await vm.grow(SHRINK)
    returned = (await vm.meminfo())["MemFree"]
    print(f"[test] after grow {SHRINK}: guest free {returned // 1024}M")
    # Most of what `shrink` took, not all of it: some of what comes back
    # lands in page cache before this reads `MemFree`, and the balloon
    # keeps its own bookkeeping pages. Measured, of 256M asked for: UML
    # gave back all of it, QEMU 165M of the 220M it had taken.
    assert returned - ballooned > (free_before - ballooned) // 2, (
        f"grow returned {(returned - ballooned) // 1024}M of the "
        f"{(free_before - ballooned) // 1024}M shrink took"
    )

