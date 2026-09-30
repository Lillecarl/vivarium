"""Does `exec` reach a run that is running, not paused?

Starts `vivarium run`, with no breakpoint, and one phase, `live`, that
holds until /tmp/go exists in its guest. Nothing in the run makes that
file: only an `exec` sent while the phase runs does, so a passing run is
the proof. `run`, `pytest` and `continue` must still be refused while
nothing is paused.

    live-exec VIVARIUM SPEC WORKDIR
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path


def until(check: Callable[[], bool], seconds: float, step: float = 0.1) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(step)
    return check()


def main() -> None:
    vivarium, spec, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    os.environ.setdefault("HOME", str(work))
    out = work / "run"
    log = work / "run.log"

    def ctl(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [vivarium, "ctl", "--out", str(out), *args], capture_output=True, text=True, timeout=60
        )

    with log.open("w") as sink:
        runner = subprocess.Popen(
            [vivarium, "run", "--spec", spec, "--out", str(out)],
            stdout=sink,
            stderr=subprocess.STDOUT,
        )

    def fail(why: str) -> None:
        runner.kill()
        sys.exit(f"{why}\n--- run.log\n{log.read_text()}")

    def started() -> bool:
        if runner.poll() is not None:
            fail("the runner ended first")
        return "[phase] live" in log.read_text() and (out / "control.sock").exists()

    if not until(started, 300, 0.2):
        fail("the phase never started")

    state = ctl("state")
    if state.stdout.splitlines()[:1] != ["running"] or "live\trunning" not in state.stdout:
        fail(f"not running live:\n{state.stdout}{state.stderr}")
    print("ok: live is running and the run is not paused")

    for args in (["run", "live"], ["pytest", str(work)], ["continue"]):
        refused = ctl(*args)
        if refused.returncode == 0 or "only while paused" not in refused.stderr:
            fail(f"{args[0]} was not refused while running: {refused.stdout}{refused.stderr}")
    print("ok: run, pytest and continue are refused while running")

    reply = ctl(
        "exec",
        'await anyio.sleep(2); print("from-exec"); await one.succeed("touch /tmp/go")',
    )
    if reply.returncode != 0 or "from-exec" not in reply.stdout:
        fail(f"exec failed while running:\n{reply.stdout}{reply.stderr}")
    print("ok: exec ran against the guest while live ran")

    try:
        status = runner.wait(timeout=120)
    except subprocess.TimeoutExpired:
        fail("the run did not end after exec made /tmp/go")
    if status != 0:
        fail(f"the run exited {status}")
    phases = json.loads((out / "phases.json").read_text())["phases"]
    if [p["state"] for p in phases if p["name"] == "live"] != ["passed"]:
        fail(f"live did not pass: {phases}")
    print("ok: live passed because exec released it")

    events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
    sent = next(e["at"] for e in events if e.get("data", {}).get("op") == "exec")
    printed = [e for e in events if e["kind"] == "output" and e["text"] == "from-exec"]
    if [e.get("phase") for e in printed] != ["exec"]:
        fail(f"exec's print is not one event of phase exec: {printed}")
    ticks = [e for e in events if e["kind"] == "output" and e["text"] == "[live] tick"]
    if any(e.get("phase") != "live" for e in ticks):
        fail("a print of live was attributed to something else")
    # The phase printed while exec held its own output: neither took
    # `sys.stdout` from the other.
    if not any(sent < e["at"] < printed[0]["at"] for e in ticks):
        fail(f"live printed nothing between exec's start at {sent} and its print")
    print("ok: exec's prints and live's prints each went to their own")


if __name__ == "__main__":
    main()
