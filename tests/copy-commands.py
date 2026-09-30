"""Do the command lines a run prints work as they are, copied from its output?

Starts `vivarium run --break later` and reads nothing but run.log: each
command comes from the line the run printed, split and run unchanged.

- the monitor armed before the pause exits 4 at the pause;
- the printed `exec` reaches the guest;
- a monitor armed while paused exits 4 at once;
- after the printed `continue`, the same monitor runs on to the verdict,
  0, and does not stop at the pause it replays.

    copy-commands VIVARIUM SPEC WORKDIR
"""

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

WATCH = "wait for a pause or the verdict (exits 4 at a pause): "
EXEC = "run Python against the guests: "
RESUME = "resume a paused run: "


def main() -> None:
    vivarium, spec, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    os.environ.setdefault("HOME", str(work))
    out = work / "run"
    log = work / "run.log"
    with log.open("w") as sink:
        runner = subprocess.Popen(
            [vivarium, "run", "--spec", spec, "--out", str(out), "--break", "later"],
            stdout=sink,
            stderr=subprocess.STDOUT,
        )

    def fail(why: str) -> None:
        runner.kill()
        sys.exit(f"{why}\n--- run.log\n{log.read_text()}")

    def printed(label: str, count: int = 1) -> list[str]:
        """The argv of the command after `label`, from the `count`th line
        that carries it, once the run has printed that many."""
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            lines = [line for line in log.read_text().splitlines() if label in line]
            if len(lines) >= count:
                return shlex.split(lines[count - 1].split(label, 1)[1])
            if runner.poll() is not None:
                fail(f"the run ended before it printed {label!r} {count} times")
            time.sleep(0.2)
        fail(f"the run never printed {label!r} {count} times")
        raise AssertionError

    watch = printed(WATCH)
    print(f"ok: the run printed its monitor: {shlex.join(watch)}")
    armed = subprocess.Popen(watch, stdout=subprocess.PIPE, text=True)
    try:
        output, _ = armed.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        armed.kill()
        fail("the monitor did not exit at the pause")
    if armed.returncode != 4 or "paused" not in output:
        fail(f"the monitor exited {armed.returncode} at the pause, with {output!r}")
    print(f"ok: the monitor exited 4 at the pause: {output.strip().splitlines()[-1]}")

    reply = subprocess.run(printed(EXEC, 2), capture_output=True, text=True, timeout=60)
    if reply.returncode != 0 or "one" not in reply.stdout:
        fail(f"the printed exec did not reach the guest: {reply.stdout}{reply.stderr}")
    print(f"ok: the printed exec reached the guest: {reply.stdout.strip()}")

    again = subprocess.run(watch, capture_output=True, text=True, timeout=30)
    if again.returncode != 4:
        fail(f"a monitor armed while paused exited {again.returncode}, not 4")
    print("ok: a monitor armed while paused exited 4 at once")

    resumed = subprocess.run(printed(RESUME, 2), capture_output=True, text=True, timeout=60)
    if resumed.returncode != 0:
        fail(f"the printed continue failed: {resumed.stdout}{resumed.stderr}")
    after = subprocess.run(watch, capture_output=True, text=True, timeout=300)
    if after.returncode != 0:
        fail(f"the monitor armed after continue exited {after.returncode}, not 0:\n{after.stdout}")
    print("ok: armed after continue, the monitor ran on to the verdict")

    if runner.wait(timeout=120) != 0:
        fail(f"the run exited {runner.returncode}")
    print("ok: the run passed")


if __name__ == "__main__":
    main()
