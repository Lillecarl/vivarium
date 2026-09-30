"""Does a failure skip what depends on it, and nothing else?

Reads the attempt's `phases.json` and `events.jsonl`; the session
derivation fails when a phase fails, so the check reads the attempt.

    phase_rules ATTEMPT
"""

import json
import sys
from pathlib import Path

WANT = {
    "boot": "passed",
    "cluster": "failed",
    # The rule. Skipped, not failed: nothing ran it.
    "check": "skipped",
    # The other half of the rule, and the one nixpkgs cannot do.
    "independent": "passed",
}


def main() -> None:
    attempt = Path(sys.argv[1])
    report = json.loads((attempt / "phases.json").read_text())
    states = {phase["name"]: phase["state"] for phase in report["phases"]}
    for name, want in WANT.items():
        if states.get(name) != want:
            sys.exit(f"phase {name} is {states.get(name)!r}, expected {want!r}")
        print(f"ok: {name} is {want}")
    if report["passed"]:
        sys.exit("a run holding a failure and a skip reported itself passed")
    print("ok: the run failed, as a run with unanswered phases must")

    # The failing command's event says how it ended, so a reader of
    # events.jsonl need not rebuild that from the phase's error text.
    events = [json.loads(line) for line in (attempt / "events.jsonl").read_text().splitlines()]
    failing = [
        e for e in events if e["kind"] == "rpc" and e.get("phase") == "cluster" and e["text"] == "exit 1"
    ]
    if not failing or failing[0].get("data", {}).get("exit") != 1:
        sys.exit(f"the failing command's event does not carry exit 1: {failing}")
    passing = [e for e in events if e["kind"] == "rpc" and e.get("data", {}).get("exit") == 0]
    if not passing:
        sys.exit("no command event carries exit 0")
    print("ok: command events carry their exit code")


if __name__ == "__main__":
    main()
