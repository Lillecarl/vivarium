"""Does `.driverInteractive` behave like nixos-test's?

Input goes in on a pipe, as a person would type it. The run must pause
before any phase has run, carry what `interactive` merged in, run a
declared phase when asked, and tear down when the input ends.

    interactive DRIVER WORKDIR
"""

import subprocess
import sys
from pathlib import Path

INPUT = """\
print("states:", sorted(set(map(str, session.state.values()))))
print("merged:", (await one.succeed("cat /etc/vivarium-interactive")).strip())
print("hello:", await run("hello"))
"""


def main() -> None:
    driver, work = sys.argv[1], Path(sys.argv[2])
    done = subprocess.run(
        [driver, "--out", str(work / "out")],
        input=INPUT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    said = done.stdout + done.stderr
    print(said)
    expect = {
        "states: ['pending']": "the run did not pause before its first phase",
        "merged: yes": "`interactive` was not merged in",
        "hello: passed": "run('hello') did not run the phase",
    }
    for line, why in expect.items():
        if line not in said:
            sys.exit(f"missing {line!r}: {why}")
    # The end of the input stops the run the way SIGTERM does.
    if done.returncode != 143:
        sys.exit(f"exit {done.returncode}, not 143")
    print("ok: paused before any phase, merged `interactive`, ran a phase, stopped at ^D")


if __name__ == "__main__":
    main()
