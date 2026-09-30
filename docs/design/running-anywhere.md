1 # Running anywhere: a design, not a plan
1
1 **Status: draft. Nothing here is decided.** Carl and Claude write this
1 together. It exists because the next four changes cement the shape of the
1 runner, and some of them replace what is there now.
1
2 **Every line carries its iteration number.** A line that has not changed
2 since the first draft reads `1`. A line that a round of feedback changed
2 or added reads the number of that round. So a reader looks at the highest
2 numbers and sees what is new. The prefixes go away when the design
2 settles.
2
9 ## What has landed
9
9 Built and proved. Each claim below has a check that fails when the claim
9 stops being true, and each was red-proved before being believed.
9
9 | | where | proof |
9 | --- | --- | --- |
9 | A session drives a run a step at a time | `pkgs/uml` | 35 unit tests |
9 | Phases declared and ordered in Nix | `modules/run.nix` | `phase-rules` |
9 | A failure skips its dependents, not the rest | `uml/phases.py` | `phase-rules` |
9 | Knobs, resolved in Nix, printed with their source | `modules/run.nix` | `knobs` |
9 | `--only`, with deselected apart from skipped | `uml/cli.py` | `only-rules` |
9 | One program, sandboxed and by hand | `lib.nix` | both doors write the same five files |
9 | `--offline` | `forward.py` | measured by hand, `uplink` |
9 | Recipes as modules | `modules/recipes` | `recipes` |
9 | `always`, for a phase that collects evidence | `uml/phases.py` | `recipes` |
10 | One event stream, five sinks | `uml/events.py`, `uml/sinks.py` | 63 unit tests |
10 | A terminal worth reading, with replay on failure | `uml/cli.py` | measured: 350 lines → 53 |
11 | Every guest's journal streams to the host while it runs | `uml/journal.py`, `modules/guest.nix` | `stream`: survives SIGKILL, UML and QEMU |
11 | pytest as a phase | `uml/pytest_plugin.py` | `pytest-phase`, 10 unit tests |
12 | `uml-eval`: a run by name, evaluated by nanopynix | `pkgs/uml-eval` | by hand: phases, a knob from the environment, `-- -k`; 10 unit tests |
13 | Breakpoints, and Python injected into a paused run | `uml/control.py` | `breakpoint`, 20 unit tests |
9
9 Four bugs were found by building it, and three of them only by running
9 against a real guest:
9
9 - The CLI took the phase list once before the loop, so a dependent
9   skipped mid-loop still ran. `skipped_by` was right and the driver
9   ignored it — no pure test could see that.
9 - A failed boot tore nothing down. `boot` lets every guest settle before
9   reporting, so a failure could leave others running with nothing left
9   to stop them.
9 - The by-hand door did not type check its phase scripts, which is the
9   door where a type error gets written.
9 - `typeCheck` copied scripts by basename, so two sharing one collided.
9
10 Area 3 is done, and it went further than the area describes. Events
10 carry the machine, the phase and the seconds as fields rather than
10 being formatted lines, so `events.jsonl` answers a timing question with
10 `jq` and the MCP server becomes another sink rather than a log parser.
10 nixpkgs' three loggers each carry their own level and their own copy of
10 the same comparisons; here the filter is one function and a renderer is
10 pure.
10
10 Two things beyond it, both because the console left the terminal: a
10 failed phase replays the last 20 lines from every guest, which nixpkgs
10 cannot do (it has the switch and not the replay); and a phase's own
10 `print` is captured and attributed to that phase rather than merely
10 kept.
10
11 Streaming out of a guest (area 5) goes through `/artifacts`, not the
11 agent's RPC. hostfs and virtiofs are write-through, measured on both
11 backends, so a journal entry is on the host's disk as soon as journald
11 has it, and no Python process holds it in between. That also avoided
11 the agent's one-command-at-a-time limit. Each entry is a `journal`
11 event carrying the machine, the unit, the phase and the pytest test.
11 A phase ends with `settle`: each guest logs a token and the session
11 waits until the token arrives. Without it, a line logged as a test
11 returned was lost to the teardown (measured).
11
11 pytest is a phase and not the entrypoint. The ordering stays in Nix,
11 where a failure skips its dependents; pytest would run a check
11 against a cluster that was never built. pytest runs in a worker
11 thread, and async tests and fixtures run on the session's loop
11 through a portal, so a test uses the same `Machine` as a script.
11 pytest as the entrypoint (`pytest --uml-spec`) is the same plugin
11 with the thread's owner swapped. It is not built yet.
11
12 The evaluator (area 0b) is a separate front door, `uml-eval`, and not
12 part of `uml`. nanopynix links Nix, and `uml` is what every sandboxed
12 check runs. A check must not evaluate, so a consumer of `mkSession`
12 never builds nanopynix. `uml-eval` builds the attribute's `.run`, so the
12 phase type check still runs, and then hands the spec to `uml run`.
12 Measured by hand: 6.8 s to 8.0 s to evaluate and build, warm. It
12 serves the CLI and a future MCP server equally.
12
13 It evaluates once. Re-evaluating after an edit is not the loop to
13 build: a phase edit is a new store path and a guest edit is a new
13 image. The loop is injection instead. `--break PHASE` and
13 `--break-on-failure` pause the drive with the guests up, and
13 `<out>/control.sock` takes `exec` (Python with top-level await, one
13 namespace for the whole pause), `inject` (a file's `test(vms)` from
13 the working tree, never the store), `run`, `state` and `continue`.
13 `uml ctl` is the client. The MCP server is another client of the same
13 five operations, not a second implementation. Everything done by hand
13 is an event, so the record of a run includes it.
13
13 `--hold` is gone. It slept on failure until ^C and relied on the
13 kernel to kill the guests; a pause now ends in the same shielded
13 teardown as every other run.
13
14 The MCP server has landed too, and dogfooding it on pynixd and
14 nixkube found and fixed eight things: a Nix error with its point last,
14 a stopped phase left `running`, JUnit from inside a guest, `vms.phase`
14 and `vms.shared`, `pythonPath`, an import error that crashed the
14 drive, a spec run by an older runner that dropped its fields, and a
14 UML kernel whose death nobody noticed. `--kernel` boots a kernel from
14 a working tree.
14
15 Phases run at once where their guests allow it. A phase declares
15 `nodes`; one without holds every guest, so a session that declares
15 nothing runs one phase at a time as before. Parallelism is a property
15 of the graph and the declarations, not a flag to ask for. A pause
15 waits until nothing runs, and every command, journal entry and print
15 keeps its phase. `parallel` proves the overlap and the attribution,
15 and fails under `--serial`. pynixd, a guest per suite, same build,
15 QEMU on 16 cores: suites 75.9s to 40.7s, whole run 87.8s to 53.3s.
15
15 A paused run takes pytest from the working tree: `uml ctl pytest PATH
15 -- ARGS`, and `run_pytest` over MCP. Editing a chaos test and sending
15 it again reuses nixkube's five minutes of setup. Its cases are events
15 marked `by_hand`, outside the verdict and junit.xml. Building it found
15 that a second pytest run in one process ran the first run's modules:
15 pytest's importlib mode reuses `sys.modules`. Each run now drops what
15 it loaded from its tests directory.
15
15 The kernel keeps its objects: `umlKernelCcache`, built by `pynix
15 --namespaced --sandbox-path /ccache=...`, rebuilds in 31s after a
15 one-line change against 122s cold. It needed a nanopynix fix:
15 `--sandbox-path` replaced nix.conf's `sandbox-paths` and took
15 `/bin/sh` with it. A QEMU guest runs VMs of its own only when it sets
15 `nestedVirtualization`; before, `-cpu host` gave every guest KVM.
15
15 One run holds UML and QEMU guests together (`mixed`). pynixd is the
15 first consumer: `tests.daemon` puts pynixd in place of a guest's Nix
15 daemon, on UML, and runs a local build, `ssh-ng://` and `--builders`
15 against it and against nix-daemon on QEMU, comparing every answer.
15 Written against paused guests over MCP. UML runs pynixd's suites with
15 no #8 panic, for 1253 MiB of host memory against QEMU's 2256.
15
14 ## Suggestions, ranked
14
14 Not built yet. Each came from a run, not from a list.
14
15 1. **pytest phases beside others.** A pytest phase runs alone: pytest
15    is not reentrant, and `--capture=sys` is the process's stdout. A
15    pytest per phase in a child process, driving the guests over the
15    portal's wire instead of a thread, lifts both.
15 2. **Shard a suite across guests.** pynixd's `unit` is now the
15    critical path, 40.7s of 40.7s. Phases generated per shard, each
15    on its own guest, split it the way the suites are split now.
15 3. **OCI images from a Dockerfile** (issue #16). Built in a guest
15    with an uplink, since a Dockerfile fetches; outside the sandbox.
15 4. **KTAP to cases.** kselftest and KUnit write KTAP, not JUnit.
14    Reading it the way `junit_in` reads JUnit makes each a case.
14 5. **Evidence out of CI.** nixkube's `test-qemu` now writes `--out
14    ./uml-out`; an `upload-artifact` step keeps junit.xml, events.jsonl
14    and every console on a failure, and a JUnit reporter shows the
14    chaos scenarios as tests.
14 6. **gdb as a tool.** A UML kernel is a process: the pid is known
14    (`Machine._guest_pid`). For QEMU, `-s` behind an option. An MCP
14    tool that answers the attach command.
14 7. **Exact per-test journal attribution, opt-in.** `settle` per
14    pytest test costs one command per guest per test; worth it for a
14    suite where "which test logged this" is the question.
14 8. **Retire `mkTest`.** This repository's own tests and nixkube's
14    `ciTest` still use it. Every feature above is session-only.
18    Decided in round 18, as "one door". See area 9.
14
14 Still open from before: pytest as the entrypoint, and why pynixd's
14 suites panic a UML guest in `munmap` (issue #8).
9
1 ## The goal
1
1 > Everything that is not a push to a registry or a cache must run on any
1 > machine, through user-mode-nixos.
1
1 CI then stops being the place where the answer lives. It becomes one more
1 caller of a thing a developer runs the same way.
1
1 Four things must be easy for that to be true:
1
1 1. Tell a run what to do from outside. Environment variables first, so a
1    run does one case of a suite instead of all of it.
1 2. Get the output back. A known directory on the host, easy to grep.
1 3. Maybe: filter what reaches the terminal. The same stream also goes to
1    the output directory.
1 4. Later: an MCP server. An agent starts a run, sets a breakpoint, gets a
1    notification, and runs Python inside the guest.
1
2 Two more, from the first round of feedback:
2
2 5. Copy and stream things out of a guest, easily.
2 6. Turn the guests' internet off in an unsandboxed run, so that a test
2    written for the sandbox can be iterated on outside it.
1
1 ## What exists today
1
1 Facts, with the file that holds them.
1
1 - A test is a derivation. `mkTest` builds `attempt`, which never fails, and
1   a second derivation reads its `status` file (`lib.nix:353`).
1 - The test script is the program. The derivation runs
1   `python3 ${script} --spec ${spec}`, and the script ends with
1   `run_test(test)` at module level (`tests/artifacts.py`).
1 - `run_test` boots the guests, runs the coroutine, and tears the guests
1   down in a `finally` (`harness.py:113`). Every run ends the same way.
1 - There are two entry points, not one. `run-uml` boots one guest from
1   command-line arguments (`cli.py`). `run_test` boots a spec.
1 - `/artifacts` is a host directory, one per guest, on both backends
1   (`harness.py:142`, `modules/image.nix:60`, `modules/qemu.nix:140`).
1 - A sandboxed run writes `log`, `report.json`, `status` and `artifacts/`
1   into `$out`. A run by hand writes the artifacts to a new temporary
1   directory and keeps nothing else (`harness.py:135`, `lib.nix:330`).
1 - Everything the run prints goes to one stream, with a prefix: `[test]`,
1   `[uml]`, `[time]`, and `[<machine>]` for a guest's console
1   (`machine.py:398`).
1 - The agent takes a method call over a serial line, and the dispatch is
1   generic: `exposed_<name>` (`arpyc.py:29`).
2 - **The agent serves one command at a time, and each one runs to
2   completion** (`agent.py:10`, `subprocess.run`). So nothing streams out
2   of a guest today, and a long command blocks every other question.
2 - A guest reaches the internet through passt, which the runner always
2   starts (`backend.py`). There is no switch that turns it off.
3 - **The runner never invokes Nix.** Not the CLI, not a library. Nix writes
3   a JSON spec and builds the images; the runner reads the file
3   (`harness.py:load_spec`). Checked: no `nix` subprocess anywhere in
3   `uml_runner`. So evaluating Nix would be a new power, not a replacement
3   for a shell-out.
4 - **What is built depends on the spec, and it must not.** Measured: adding
4   one plain string to `settings` moves seven derivations, including
4   `uml-root-image`. The cause is `lib.nix:241` — the settings file is
4   registered in the guest's Nix database, so the image's content follows
4   the file's hash. Nothing about a guest's image is different; only the
4   data handed to the run.
1
2 ## Decided
2
2 From Carl's first round of feedback. These close open questions 1, 4 and
2 5 of the first draft.
2
2 - **user-mode-nixos is a CLI application.** It reads a guest spec, sets
2   the guests up, and executes user Python. The runner owns the process.
2   The script stops being the program.
2 - **The Python runs on the host**, in both cases. It reaches a guest over
2   the RPC. Sandboxed, the same Python runs inside the build sandbox, and
2   still not inside a guest.
2 - **The CLI takes one or more scripts.** More than one is what allows a
2   standard library: boot a machine, bring up a Kubernetes cluster, and so
2   on, written once and reused by every consumer.
2 - **Output: `$out` when sandboxed, and a path the caller names when not.**
2   CLI flag or environment variable, either is fine.
2 - **A sandboxed run is worth keeping.** Nix distributes it to a build
2   machine, so it does not have to run locally. Its one cost is that the
2   guests have no internet.
3
3 From the second round:
3
3 - **Python contributes one coroutine per script.** No phase names, no
3   decorators, no registry in Python.
3 - **Phases are declared in the NixOS module system**, and Nix orders
3   them. The module system already has the merging and ordering machinery,
3   and a consumer can reorder or drop a phase the way they override any
3   other option.
3 - **Recipes are scripts in this repository**, each with a submodule that
3   declares it. `recipes/kubernetes.py` beside the option that names it.
3 - **Knobs are module options**, resolved by a helper:
3   `envOrDefault = envvar: default:`. An environment variable that is unset
3   reads `""`, and so does `builtins.getEnv` under a pure evaluation, so
3   both cases fall through to the declared default.
3 - **nanopynix is the candidate evaluator**, so the runner can ask an
3   evaluation questions at will instead of being handed one JSON file.
4
4 From the third round:
4
4 - **Nothing that is built may depend on the spec.** The runner is a tool,
4   the way `pynix` is a tool: it reads a spec and acts on it. The recipe
4   scripts are the same. Neither is rebuilt because a knob moved.
5
5 From the fourth round:
5
5 - **The module system is the only source.** Evaluating it produces three
5   things: the image specs, a wrapper that runs the runner unsandboxed,
5   and a wrapper that runs it sandboxed.
5 - **The CLI evaluates that same module system** to get the spec it runs.
5 - **A phase contributes configuration as well as a script.** Much of what
5   a phase is will be systemd units, plus whatever coordinates several
5   nodes.
6
6 From the fifth round:
6
6 - **The MCP server is a stateful version of this CLI.** That is why the
6   CLI is a separate thing rather than something Nix generates. It is not
6   a later feature; it is the reason for the shape.
18
18 From the eighteenth round: one door, `defaults`, helpers that a
18 test brings, an evaluated signal and a quiet monitor. Area 9 has
18 each of them.
7
7 ## How to build it
7
7 The question was whether to reshape this tree or start a separate one,
7 because the tree was written fast and its structure may fight the new
7 shape. Counted instead of guessed. 4060 lines in `uml_runner`:
7
7 | | lines | share | what happens to it |
7 | --- | --- | --- | --- |
7 | `harness.py`, `cli.py` | 414 | 10% | replaced |
7 | `cluster.py` | 665 | 16% | moves; it is a recipe in the wrong place |
7 | everything else | 2981 | 73% | untouched |
7
7 That 73% is the expensive part, and none of it has an opinion about
7 sessions: fd passing, passt, virtiofsd, copy-on-write disks, rpyc over a
7 serial line in raw mode, `die_with_parent`. Starting again means earning
7 all of it a second time to fix 10%.
7
7 The coupling that looked structural is seven lines. `report.RUN` is
7 reached from five places in `machine.py` and two in `harness.py`. That is
7 a constructor parameter, not a rewrite.
7
7 **So: a new package beside the old one, in this repository.** The session
7 and the CLI are written fresh and import the mechanism. `uml_runner`
7 keeps working while they are built, and `harness.py` and `cli.py` are
7 deleted when the new door passes the tests the old one passes.
7
7 Not a separate repository, for one reason above the others: `tests/` and
7 `default.nix` are the only proof that any of this works. Somewhere else
7 means no proof, or a copy of the proof that drifts.
7
7 **And with real guests, not a fake backend.** The smallest guest here
7 boots in 6.9 seconds and its whole test runs in 7. A stub backend is a
7 second implementation to keep honest, and it cannot fail the way the real
7 one fails. Develop against `impure`; it is already the small one.
1
4 ## Area 0a — the spec is input, not an ingredient
4
4 The rule: **the runner and the recipes are built once and do not move
4 when the spec does.** The runner is a tool, the way `pynix` is a tool. A
4 tool reads its input; it is not rebuilt by it.
4
4 Today that is false, and the number says how false. Adding one plain
4 string to a test's `settings` rebuilds seven derivations, and one of them
4 is `uml-root-image` — the guest's disk. Nothing inside the guest is
4 different. The cause is one line:
4
4 ```nix
4 boot.uml.nixDatabase.extraRoots =
4   lib.optional (settings != { }) "${settingsFile}";   # lib.nix:241
4 ```
4
4 The settings file is registered in the guest's Nix database, so the image
4 follows the file's hash. The registration exists for a good reason:
4 `settings` may carry store paths, and Nix inside the guest must know
4 them, or it calls the path invalid and goes looking for a substituter.
4
4 So the fix is to separate the two things `settings` does today:
4
4 - **Paths** a guest must be able to resolve. These belong to what is
4   built, and registering their closure is right.
4 - **Values** a run is given — a knob, a selection, a tag. These are data.
4   They must reach the runner without touching an image, a database or a
4   derivation the guests depend on.
4
4 Get that right and the cost of an evaluation-time knob collapses to an
4 evaluation and a small JSON file. That is most of what makes question 6
4 a question, so it is worth doing first.
4
4 **The obvious way to split them does not work.** Asking the JSON for its
4 string context looks like it names the paths, and it does not:
4
4 ```nix
4 builtins.getContext (builtins.toJSON { a = "${pkgs.hello}"; })
4 # => { "/nix/store/...-hello-2.12.3.drv" = { outputs = [ "out" ]; }; }
4 ```
4
4 A derivation in a value gives its **`.drv`**, not its output path. The
4 guest needs the output. So context alone cannot build the list, although
4 it looks like it can — checked, because the failure would be silent: the
4 database would register a path nothing in the guest ever asks for, and
4 the real one would be invalid at run time.
4
4 Three candidates, none tried:
4
4 - Scan the JSON text for store paths. The text already holds the output
4   path; only the context names the `.drv`. Crude, and it is what Nix's
4   own scanner does.
4 - Write the sorted path list to its own file and build the database from
4   that. Two settings that differ only in a plain value then produce the
4   same file, so the image does not move.
4 - Make the author name the paths. No discovery, no trap, and the doc
4   comment at `lib.nix` says why that was rejected once already: a path
4   that is missed is not a build error, it is a guest that goes looking
4   for a substituter.
4
6 ## Area 0d — the CLI is one drive of a session
6
6 The MCP server is this CLI with the state kept. So the CLI is not the
6 program either: **a session is**, and `uml run` is one linear drive of
6 one session from start to teardown. The MCP server drives the same
6 session, more slowly, from outside.
6
6 That turns area 4 from a later feature into a constraint on this one.
6 Write the CLI as a `main` with the logic inside it, and the MCP server
6 cannot reuse any of it.
6
6 What the session has to look like, and what stands in the way today.
6
6 **Each step is addressable, not one `run()`.** Evaluate, build, boot,
6 run a phase, run the next phase, hold, tear down. A caller must be able
6 to stop between any two and come back later. `run_test` does the whole
6 sequence in one call and tears down in a `finally` (`harness.py:113`), so
6 there is no point at which anything outside can speak.
6
6 **A session has an identity and a lifetime.** An MCP caller names one.
6 Something has to reap a session nobody is using: `die_with_parent`
6 covers the parent being killed, but an MCP server that stays up *is* the
6 parent, so a forgotten session is a UML kernel spinning on a core until
6 somebody notices.
6
6 **Nothing per-run may be a module global.** One process holds several
6 sessions. Two things break that today:
6
6 - `report.RUN` is a module-level `Report()` (`report.py:188`), and its
6   docstring says why: "a test never makes a second run in one process".
6   That assumption is exactly what the MCP server ends.
6 - The signal handling belongs to the CLI, not to the library.
6   `_unwind_on_signal` cancels the current task (`harness.py:263`), which
6   is right for a program and wrong for one session among several.
6
6 **Two sessions race for host addresses.** Picking a free one means
6 binding a port and letting it go, so the choice is only safe while
6 something serialises it. Today that is a `taken` set local to one run
6 (`harness.py:116`), and the comment beside it names the race. Two
6 sessions in one process have two sets and will pick the same address.
6 The set has to move up to whatever owns the sessions.
6
8 ## Area 7 — a "main" guest
8
8 Proposal: a uml module contributes NixOS configuration to the guests, and
8 that configuration differs depending on whether the guest is "main".
8
8 **The pattern already exists and works.** `services.uml-k8s.role` is an
8 enum of `control-plane` and `worker` (`modules/k8s.nix:497`), and four
8 places branch on it: the packages, the tmpfs for etcd, `uml-k8s-join`,
8 and the agent's `KUBECONFIG`. So "a module behaves differently by role"
8 is proven in this tree, not speculative.
8
8 Three things to get right, and one rule it must not break.
8
8 **The rule it must not break.** `modules/k8s.nix` opens with a decision:
8
8 > Everything a node can know about itself lives here. Everything that
8 > needs to know about the *other* nodes — the join command, which pod
8 > subnet each one was given, the routes between them — is left to the
8 > test, which is the only thing that has the whole cluster in view.
8
8 And it names what that buys: "the same three lines describe a one-node
8 cluster or a five-node one".
8
8 A guest knowing **what it is** keeps that. A guest knowing **which other
8 guest is main** does not. The second is a peer list, and the moment a
8 module can ask for one, a one-node cluster and a five-node cluster stop
8 being the same three lines.
8
8 **1. Per recipe, not global.** One `main` for the whole run couples
8 recipes that have nothing to do with each other: a Kubernetes control
8 plane and a database primary need not be the same guest. `role` is
8 already per recipe and does not have this problem.
8
8 **2. A name, not a boolean.** `main = "cp"` — a reference to a node,
8 declared once — rather than `main = true` on each node. A boolean set
8 twice, or never, is a silent misconfiguration that surfaces as a cluster
8 that will not form. A name is checked while evaluating, and zero or two
8 is an assertion with a message.
8
8 **3. Configuration and orchestration are different needs.** "This guest
8 runs the API server" is configuration and belongs in Nix. "Run kubectl
8 here" is orchestration and only needs `vms.main` in Python. Both are
8 wanted; they are not the same mechanism, and conflating them is how the
8 peer list gets in.
8
8 **The alternative worth weighing.** A phase script runs on the host and
8 talks to every guest, so it *already* has the whole-cluster view the
8 module deliberately lacks. Coordination there needs nothing new: read the
8 token from the control plane, hand it to the workers.
8
8 The in-guest alternative would need something built. There is no shared
8 filesystem between guests today — each gets its own `/artifacts`
8 subdirectory, and `tests/artifacts.py` asserts that they do not share
8 one. A `/shared` mount across guests is cheap on the same mechanism, but
8 it is a new thing, and it is the thing that makes peer lists easy to
8 write by accident.
8
7 ## Prior art: nixpkgs' own test driver
7
7 `nixos/lib/test-driver` answers most of these questions already. Read it
7 before inventing anything. Four things to copy and one to avoid.
7
7 **Copy: the spec is a validated model.** `DriverConfiguration` is a
7 pydantic model loaded from a JSON file (`driver.py:42`). Same shape area
7 0a argues for, and it gets a schema and an error message free.
7
7 **Copy: the driver is already a session.** `Driver` has `__enter__` and
7 `__exit__` (`driver.py:170`). It is not decomposed into steps, but the
7 object exists.
7
7 **Copy: one event API, several sinks.** `CompositeLogger` holds a list of
7 loggers; `JunitXMLLogger` is one of them; and `log_serial(message,
7 machine)` is a separate call from `log` (`logger.py:63`). That is area 3
7 as a working design, and junit XML comes with it.
7
7 **Copy: breakpoint-on-failure exists and works in a sandbox.**
7 `debug.py` is 53 lines. It forks a `sleep <random>` as a findable marker,
7 prints the command to attach, and hands the frame to `RemotePdb` on a TCP
7 port. Sandboxed tests turn it on with `enableDebugHook`. So the hard part
7 of area 4 — holding a failed run open inside `nix build` — is proven,
7 not speculative.
7
7 **Avoid: the test script is `exec`'d.** `test_script()` runs
7 `exec(self.tests, symbols)` with the driver's methods injected as globals
7 (`driver.py:390`). No imports, no type checking, and every frame is named
7 `<string>` — the driver carries a traceback-filtering hack to make an
7 assertion readable. Importing a module and calling a coroutine is
7 strictly better, and it is the direction already chosen.
7
7 ## Area 0e — the session, and what it answers
7
7 ### Ordering: `after`, sorted by `lib.toposort`
7
7 Recommended over `mkBefore`/`mkAfter`/`mkOrder`.
7
7 `lib.toposort` takes a "comes before" predicate and returns either
7 `{ result }` or `{ cycle, loops }` (`lib/lists.nix:1244`). So a cycle
7 between phases is an evaluation error with the cycle printed, for free.
7
7 The argument against `mkOrder` is not style. An order number says a phase
7 is 1200 and another is 1500, and nothing anywhere says why. `after`
7 names a real dependency — and that same graph answers the next question,
7 which a number cannot.
7
7 ### After a phase fails: skip its dependents, run the rest
7
7 Recommended, and only possible because of the graph above.
7
7 The two obvious answers are both wrong here. nixpkgs stops everything: a
7 `subtest` logs and re-raises (`driver.py:306`), so one failure ends the
7 run. pytest runs everything: each test is independent. Phases are
7 neither — `check` needs `cluster`, and running it after `cluster` failed
7 produces a second failure that says nothing.
7
7 With `after` known, a failure marks its dependents skipped and leaves
7 everything else to run. One run then reports every independent failure
7 instead of the first one.
7
7 ### The session's operations
7
7 The CLI drives these in a line. An MCP tool is one of them.
7
7 | operation | what it does | MCP tool |
7 | --- | --- | --- |
7 | `evaluate` | module system → spec, knobs resolved | `list`, `describe` |
7 | `build` | realise the images and the paths | — |
7 | `boot` | guests up, agents answering | `start` |
7 | `phases` | the sorted list, with state | `phases` |
7 | `run(phase)` | one phase | `run_phase` |
7 | `exec(machine, cmd)` | one command in a guest | `exec` |
7 | `python(machine, code)` | code in the guest's agent | `python` |
7 | `hold` | stop here, keep everything | implicit on failure |
7 | `teardown` | guests down, output written | `stop` |
7
7 `uml run` is `evaluate, build, boot, [run each phase], teardown`, with
7 `hold` instead of `teardown` when a phase fails and the caller asked.
7
7 ### Considerations, not yet decided
7
7 **Two front ends on one hold.** A human at a held session wants a shell;
7 an agent wants structure and must not screen-scrape a pdb prompt. The
7 hold is one mechanism either way. `remote_pdb` is the human half and
7 already works; the agent half is a call on the session.
7
7 **`python` in a guest inherits the agent's limit.** The agent serves one
7 request at a time and each runs to completion (`agent.py:10`). Code that
7 blocks stops every other question to that guest — the same limit as area
7 5, met again.
7
7 **The report becomes a parameter.** `report.RUN` is reached from five
7 places in `machine.py` and two in `harness.py`. A `Machine` takes its
7 recorder; the session owns one. That is the whole change.
7
7 **A phase is the unit everywhere.** It is a section in the report, a span
7 in the event stream, a name a breakpoint can take, and a row in the MCP
7 tool above. Worth keeping that alignment deliberate rather than letting
7 three names for one thing appear.
7
5 ## Area 0c — one evaluation, three outputs
5
5 Evaluating the module system gives the image specs, an unsandboxed
5 wrapper and a sandboxed wrapper. The CLI evaluates the same thing when it
5 is run by hand.
5
5 The gain is real and it is the one worth naming first: today `.run` and
5 the sandboxed `attempt` are written separately, and every asymmetry
5 between them is a bug somebody meets later — no log by hand, no report by
5 hand, `$@` promised and not delivered. Two outputs of one evaluation
5 cannot drift that way, **as long as they are the same script generated
5 twice with a flag**, and not two scripts that happen to agree today.
5
5 Five things to watch.
5
5 **1. Typed options make the path problem disappear.** Question 10 asks
5 how the guests learn which store paths the spec mentions. That question
5 only exists because `settings` is a free-form attrset, so the paths have
5 to be discovered. Options have types. A `types.package` or `types.path`
5 option *is* a path; a `types.str` option is not. The module system
5 already knows the difference, so nothing has to scan anything and the
5 `.drv` trap above never arises. This is a better answer than the sorted
5 list file, and it comes free with the direction already chosen.
5
5 **2. Every run pays for an evaluation.** `.run` is a store path today, so
5 running it costs nothing but the boot. A CLI that evaluates pays the
5 NixOS module system on every run, for every guest, and it lands on the
5 case the whole design is for: change one line, run again.
5
5 Measured, warm store, `nix eval` of the test's `drvPath`:
5
5 | what | seconds |
5 | --- | --- |
5 | this repo's smallest guest (`impure`) | 2.7 |
5 | this repo's Kubernetes guest (`k8s`) | 4.8 |
5 | nixkube's `umlTest`, the largest real consumer | 9.3 |
5
5 Acceptable. The smallest guest here takes 6.9s to boot, and nixkube's
5 test runs for about twenty minutes, so the evaluation is a minority of
5 even the shortest run. An evaluation cache is an optimisation, not a
5 requirement. Worth re-measuring on a cold store, which these numbers are
5 not.
5
5 **3. The CLI now builds, and a build can fail.** Evaluating gives
5 derivations; something must realise them. So a failure that used to
5 happen before the runner started now happens inside it, and its progress
5 and its errors need somewhere to go. That is new surface, and it is the
5 part users see first when something is wrong.
5
5 **4. The CLI needs the same door as `nix`.** `uml run mytest` has to find
5 the file to evaluate, the nixpkgs to evaluate it against, and the
5 arguments to pass. `nix build --file . <attr>` answers all three. The CLI
5 should answer them the same way and with the same spelling, or people
5 learn two conventions for one thing.
5
5 **5. Selecting a phase must not rebuild a guest.** A phase contributes
5 configuration, so enabling one changes what is built — correct. But
5 *choosing which phases to run* is the thing this design exists to make
5 fast. So the rule has to be: every declared phase's configuration is
5 always built, and selection happens at run time. A guest carries the
5 units for all its phases and runs the ones it is asked to. Selecting
5 otherwise would rebuild the image for every selection, which is the cost
5 area 0a exists to remove.
5
2 ## Area 0 — the CLI, and what a script is
2
2 This is the change that cements the rest, so it comes first.
2
2 Today the concerns are mixed. `harness.py` parses the command line, boots
2 the guests, runs the test, tears the guests down and writes the report.
2 `run_test(test)` is both the library call and the entry point, so a test
2 module cannot be imported without starting a run. `cli.py` is a second
2 door into the same work with different arguments.
2
2 The shape to move to:
2
2 ```console
2 $ uml run --spec <spec.json> --out <dir> recipes/kubernetes.py mytest.py
2 ```
2
2 The CLI owns the spec, the guests, the output directory, the steer and
2 the teardown. A script contributes work and nothing else.
3
3 **The Python side is as small as it can be.** One coroutine per script:
3
3 ```python
3 async def test(vms: Machines) -> None: ...
3 ```
3
3 Nothing else is exported and nothing runs on import. The module is data
3 until the runner calls it.
3
3 **The ordering lives in Nix.** A phase is an option, not a Python name:
3
3 ```nix
3 uml.phases.cluster = {
3   script = ./recipes/kubernetes.py;
3   after = [ "boot" ];
3 };
3 uml.phases.check = {
3   script = ./mytest.py;
3   after = [ "cluster" ];
3 };
3 ```
3
3 That buys what a Python registry cannot. A consumer reorders a phase,
3 replaces one, or drops it with `lib.mkForce`, the same way they override
3 any NixOS option. A recipe can require another by name. And the order is
3 visible without running anything.
3
3 Open: whether `after` is the right spelling, or whether the list merging
3 the module system already has (`mkBefore`, `mkAfter`, `mkOrder`) is
3 enough. The second is less to invent and less to explain.
2
2 **A script as an argument is also how iteration gets fast.** Today the
2 script is baked into the `run` wrapper, so editing one line of Python
2 re-evaluates Nix. A path on the command line does not. The sandboxed
2 build still names the script as an input, because the check has to be
2 reproducible — so both doors exist, and only one of them is fast.
3
3 Phases in Nix put that in tension: if the phase list names the scripts,
3 then changing a script means re-evaluating after all. The likely answer
3 is that the CLI can override one phase's script with a path, so the fast
3 door stays open for the file being worked on.
2
2 The type check has to follow. `typeCheck` runs over the script because
2 `mkTest` names it (`lib.nix:252`). A script that arrives on a command
2 line needs its own door into the same check.
3
3 ## Area 0b — Nix as a library, not a file
3
3 The runner is handed one JSON file today. Everything it can ever know was
3 decided when that file was written.
3
3 With nanopynix the runner evaluates instead. It opens a session, asks for
3 the attribute the caller named, and reads the phases, the knobs, the
3 guests and the paths out of the evaluation as it needs them.
3
3 What that buys:
3
3 - One command. `uml run mytest` evaluates, builds what it needs and
3   boots, with no `nix build` first and no store path to paste.
3 - Questions asked late. A breakpoint that wants to know which phase comes
3   next, or an MCP tool that lists what can be run, asks the evaluation
3   rather than a file that was written before either existed.
3 - Knobs resolve where they are declared, so an environment variable can
3   change what is *built*, not only what the script does at run time.
3
3 What it costs:
3
3 - The runner gains a large dependency. Today `uml_runner` needs rpyc and
3   qemu-qmp and nothing else.
3 - **The sandboxed path must not evaluate.** Inside `nix build` everything
3   is already decided, the sandbox has no network, and a second evaluation
3   would be a different answer from the one the derivation was built from.
3   So the spec file stays, and the two doors differ: the CLI evaluates,
3   the check reads. That is a seam to keep honest.
3 - Bounds "any machine" further. See below.
1
1 ## Area 1 — steering a run
1
3 Landed as `d36934d1`. **This is now superseded** by knobs as module
3 options; the commit stays until the replacement exists, so that nothing
3 regresses in between.
1
1 `mkTest` takes `impurities`, a list of environment variable *names*. The
1 spec carries the list. It never carries a value, so no store path moves
1 with what a caller sets, and `nix build` needs no `--impure`. The runner
1 reads the names into `vms.env`. A script passes what it chooses into a
1 guest with `succeed(..., env = {...})`.
1
1 Two properties, both measured:
1
1 - The derivation path is the same with the variable set and unset.
1 - A sandboxed run prints `UML_TEST_IMPURITY=unset` and takes the test's
1   own default. A run by hand reads the value.
1
1 The command line reaches a script as `vms.argv`. It did not before: the
1 parser was strict and exited 2.
3
3 **The knob shape makes the opposite trade, on purpose.** `envOrDefault`
3 reads the environment during evaluation, so a set variable changes the
3 derivation. That is the price of the thing it buys: a knob can choose a
3 phase order, a guest's memory or a different image, which no amount of
3 run-time reading can do.
3
3 Two consequences to hold on to:
3
4 - A set knob is a cache miss, and how much that costs depends entirely on
4   area 0a. Today it rebuilds the guest's disk image, measured. Once the
4   spec stops reaching what is built, it costs an evaluation and a small
4   JSON file, which is the right price.
3 - A variable left in a shell silently builds something that is not the
3   check. The run must print every knob, its value and where the value
3   came from — the environment or the default — before it boots anything.
3
3 Pure evaluation is what makes the default reliable: `builtins.getEnv`
3 returns `""` there, which is the same as unset, so a flake consumer and a
3 CI check both get the declared default with no special case.
3
4 Open, and it turns on area 0a: a knob that only changes what the script
4 *does* — one case out of a suite — costs a guest image today. Once the
4 spec stops reaching what is built, it costs an evaluation, and one kind
4 of knob is probably enough. See question 6.
1
1 ## Area 2 — output
1
1 The sandboxed path is already right. The by-hand path keeps nothing that
1 anybody can find.
1
2 Decided: `$out` when sandboxed, and a directory the caller names
2 otherwise, through a flag or an environment variable. So the question is
2 no longer whether to default to `./uml-out/`, and `lib.nix:330` stands —
2 nothing writes to a directory nobody chose.
1
2 Open: whether the two layouts are identical, whether a named directory
2 that already holds a run is overwritten or given a second entry, and
2 whether the directory records the steer the run was given. A log that
2 does not say which case it ran is a log that cannot be read a week later.
1
1 ## Area 3 — streams
1
1 This is not a filter on top of the current output. It is what the current
1 output becomes.
1
1 There are four streams today, interleaved into one and told apart by a
1 prefix. Each guest's console. The harness's own events. What the test
1 prints. The timings.
1
1 If the runner owns the process, it can keep them apart:
1
1 - Each stream goes to its own file in the output directory. A guest's
1   console is already buffered in `Machine._history`.
1 - The terminal gets a filtered view of the same events.
1 - The MCP server reads the events, not the text.
1
1 That last point is why this belongs with item 4 and not on its own. An
1 agent that greps a log is an agent that breaks when a message changes.
3
3 With phases in Nix, a phase is also the natural unit here: each one gets
3 its own section in the report and its own span in the event stream.
2
2 ## Area 5 — copying and streaming out of a guest
2
2 Copying is nearly free already: `/artifacts` is a host directory, so a
2 guest that writes a file has already delivered it. What is missing is the
2 short way to say it, and the case where the thing to collect is not a
2 file the test wrote — a journal, a unit's log, a directory.
2
2 Streaming is the harder half, and it hits a real limit. The agent runs
2 one command at a time and each command runs to completion
2 (`agent.py:10`). So `journalctl -f` cannot be followed while the test
2 does anything else, and a slow command reports nothing until it ends.
2
2 Three ways out, and this is a question for the design:
2
2 - A second channel to each guest, so a stream does not take the one the
2   commands use.
2 - An agent that starts a command, returns a handle, and serves output as
2   it arrives.
2 - Nothing in the agent: the guest writes to `/artifacts` and the host
2   follows the file. Cheapest, and it covers most of what a test wants.
2
2 The last one is worth trying first because it needs no protocol change.
1
1 ## Area 4 — sessions, breakpoints, MCP
1
1 Issue #10 holds the idea. Two notes from the code:
1
1 - Running Python in a guest is one more `exposed_` method. The dispatch is
1   already generic.
1 - The hard part is the session. `run_test` tears the guests down whichever
1   way the run ends, so the state that a failure creates is gone before
1   anything can look at it.
1
1 A hold-on-failure mode is the first step, and it is useful with no MCP
1 server at all. Reboots (#11) want the same ownership: a machine that comes
1 back is a machine something outside it drives.
6
6 This area is no longer last. The MCP server is the CLI with its state
6 kept, so the session shape it needs is a constraint on area 0, not a
6 thing to add afterwards. See area 0d.
3
3 Phases give a breakpoint a name. "Stop before `check`" is a thing a
3 caller can say without reading any Python, and an agent can list the
3 phases from the evaluation.
2
2 ## Area 6 — an unsandboxed run with the internet off
2
2 A sandboxed run has no network, and that is the environment most of these
2 tests are written for. Iterating on such a test outside the sandbox
2 changes the environment under it: the guest suddenly resolves names and
2 reaches a cache, so a test that would fail in the check passes by hand.
2
2 So the runner needs a switch that gives an unsandboxed guest the same
2 empty network a sandboxed one has. Then a failure reproduces where it can
2 be looked at.
2
2 Open: what the switch turns off. Not starting passt at all is the
2 simplest, and it also removes the host-to-guest forwards that a test may
2 use to reach an API server. Keeping passt and cutting only the uplink
2 keeps the forwards. The second is probably what is wanted, and it is more
2 work.
16
16 ## Area 8 — a guest as a container
16
16 Many tests need no kernel of their own: a service, a CLI, a pytest
16 suite. A container runs the same NixOS system with no kernel boot,
16 native speed, and the host's page cache. UML stays for what needs a
16 kernel: modules, sysctls outside a namespace, block devices, reboots,
16 kubelet. So this is a third `boot.uml.backend`, `container`, chosen per
16 node like the other two. A test script does not know which it got.
16
16 **Measured.** A NixOS system with `boot.isContainer = true` boots under
16 rootless crun as uid 1000, with no root and no daemon: 1.39 s of
16 userspace to `multi-user.target`. A `User=nobody` service runs. The
16 only failures are the debugfs and tracefs mounts, which a container
16 module masks. Measured on 2026-09-25; the crun config is in issue
16 #17.
16
16 ### Runtime
16
16 Recommended: **crun, directly.** An OCI runtime with no daemon. Nix
16 builds it, and Nix renders its `config.json` the way it renders the UML
16 and QEMU command lines today. `--cgroup-manager=disabled` keeps the
16 container in the runner's own cgroup, and systemd inside manages it.
16
16 The others, and why not first:
16
16 - **systemd-nspawn.** nixpkgs' test driver already runs guests with it
16   (`NspawnMachine`, `nixos/lib/testing/run.nix`). It needs root, so in
16   the sandbox it needs the `uid-range` feature. It is the source to copy
16   sandbox details from: `--private-users=no`, `/proc` and `/sys` bound
16   to `/run/host`, and a notify socket for readiness. Unprivileged nspawn
16   through `systemd-nsresourced` exists in recent systemd; not measured.
16 - **CRI** (containerd, CRI-O). A daemon, root, and an image pipeline.
16   It answers a different question: a guest as a pod on a cluster. The
16   store could reach the pod through nixkube's CSI driver. Worth an area
16   of its own later; not a local backend.
16 - **podman.** A CLI over crun. Its value is `pasta`, the same passt this
16   repository already carries, so the uplink comes for free either way.
16
16 ### What the host must have
16
16 Probed by doing each thing, not by reading configuration. The same
16 probe ran as a build in the Nix sandbox (as `nixbld`, `uid-range` off)
16 and on the host (uid 1000).
16
16 | need | sandbox | host | remedy |
16 | --- | --- | --- | --- |
16 | a user namespace, and one inside it | ok | ok | — |
16 | pid namespace, `/proc`, tmpfs | ok | ok | — |
16 | a cgroup to write in | no `/sys/fs/cgroup` | `mkdir`: permission denied | host: `systemd-run --user --scope -p Delegate=yes`; sandbox: `use-cgroups` |
16 | a range of uids | one uid | `/etc/subuid` and setuid `newuidmap` | NixOS: `autoSubUidGidRange`; sandbox: `uid-range` |
16 | sysfs | "Mount too revealing" | ok | sandbox: bind `/sys`, as nixpkgs does |
16 | `/dev/net/tun`, for a LAN | absent | ok | sandbox: `/dev/net` in `extra-sandbox-paths` (nixpkgs' `devnet`) |
16
16 With one uid, systemd boots but the system is broken: `users`
16 activation fails with "Failed to change ownership of /etc/shadow",
16 devpts with "Invalid gid '3'", and every `User=` service with
16 `216/GROUP`. So a uid range is a hard requirement, not an extra.
16
16 A nested user namespace works in both places. So a guest's own Nix
16 sandbox, and a test that makes namespaces, work inside the container.
16
16 Consequences:
16
16 - **By hand, on this host, everything is present.** The one gap, the
16   cgroup, the runner closes itself: when its own cgroup is not
16   writable and a user systemd answers, it re-executes under a
16   delegated scope. One dependency becomes none.
16 - **The sandboxed check needs `uid-range`.** That feature gives the
16   build 65536 uids and its own cgroup. `.container` asks for it in
16   `requiredSystemFeatures`, the way `.qemu` asks for `kvm`, so Nix
16   refuses to build it where it cannot run. This host's daemon has
16   neither `uid-range` nor `use-cgroups` today.
16 - **An Ubuntu 24.04 GitHub runner** blocks unprivileged user
16   namespaces through AppArmor. Only the attempt shows it, which is why
16   every row above is a probe and not a read. Not measured here.
16
16 ### Detection
16
16 Each backend owns a list of probes. A probe does the thing once and
16 returns what is missing, why, and the remedy from the table. The runner
16 runs the probes of every backend the spec uses, before it starts any
16 guest. It reports all the missing things at once, and exits with its
16 own status. `uml doctor` runs the same list with no spec, for every
16 backend.
16
16 This follows `BackendError` in `backend.py`, but earlier: today a
16 missing `/dev/kvm` shows as a failed launch. The sandbox results above
16 are the negative control. A probe that passes where the table says it
16 fails is wrong.
16
16 ### Two parts of the runner that change
16
16 - **The agent channel.** A container has no serial line. The runner
16   binds `<rundir>/agent/` into the container after the `/run` tmpfs,
16   and the agent listens on a unix socket there.
16   `UML_AGENT_DEVICE` names a socket as well as a tty. `AGENT_READY`
16   still appears on the console, which is crun's stdout, so
16   `Machine._wait_for_line` does not change.
16 - **The LAN.** A tap device in the container's network namespace works
16   as uid 1000 (measured). Its frames go into the same segment code the
16   other two backends use, so one run mixes containers, UML and QEMU.
16   Not built: how the tap's file descriptor leaves the namespace. The
16   candidate is a holder process that makes the user and network
16   namespaces first, keeps the tap, and lets crun join both by path.
17
17 ### Three more facts, measured before building it
17
17 - **The console needs a pty.** With `terminal: false`, the container
17   has no `/dev/console`, and systemd's console output (every unit's
17   `journal+console`, so the agent's ready line too) goes nowhere. With
17   `terminal: true` and `--console-socket`, crun sends the pty master
17   over the socket, and systemd's status lines arrive on it.
17 - **crun will not relay the pty itself.** `terminal: true` with stdout
17   a pipe and no console socket exits 1 with "tcgetattr: Inappropriate
17   ioctl for device". So a small launcher takes the master and copies
17   it to its own stdout, which is what `Machine` reads.
17 - **SIGKILL of crun leaves the container running.** The container's
17   init survived crun's death. `setpriv --pdeathsig KILL` in front of
17   init fixes it: init then dies with crun, and the whole PID namespace
17   with it. So the chain is runner, launcher, crun, init, each killed
17   by the death of the one before, as `die_with_parent` does for the
17   other backends.
17
17 ### Built: the first step
17
17 `boot.uml.backend = "container"` runs, by hand: `uml-eval run
17 container` under a delegated scope. One guest, 1.5 s to boot, 2.9 s
17 for the whole run. No unit fails, and a `nobody` service runs as 65534. A
17 runner killed with SIGKILL while the guest is paused leaves nothing
17 behind: 14 guest processes before, none after. Without a delegated
17 cgroup the run refuses at once and names the fix.
17
17 Not built yet: memory control.
17
17 The sandboxed door is built, on a daemon with `uid-range` (dynhetz
17 has it since 2026-09-25). `nix build --file . container` passes in
17 2.6 s. CI runs it too, as `test-container`: ghanix's
17 `nix.install.uidRange` turns on `auto-allocate-uids`, `use-cgroups`
17 and `extra-system-features = uid-range`, and a stock GitHub runner
17 needs nothing else. There it booted in 1.8 s and passed in 5.9 s.
17
17 With /dev/net in the sandbox (ghanix's `devNet`, nixpkgs' `devnet`)
17 the sandboxed guests have the uplink and the LAN too: `container-lan`
17 passes in the sandbox, two containers and a UML guest on one segment.
17 One more thing differed. The guest shares the build's user namespace,
17 and joining the namespace you are in is EINVAL, so pasta and the tap
17 relay join only the network namespace there. With no external
17 interface pasta uses its local mode, and vec0 gets 169.254.2.1/16.
17
17 `container-probe` runs the runner's host checks in the sandbox and
17 fails in seconds, naming each missing piece and its fix. Every session
17 with a container guest depends on it, and CI builds it first on its
17 own. Without /dev/net, the `-tun` probe fails: "missing a tap device
17 ... fix: put /dev/net in extra-sandbox-paths" (the negative control). Four things differ from the by-hand run, each met
17 by trying it:
17
17 - The build is root with 65536 ids and no `/etc/subuid`. The guest
17   shares that user namespace, as nixpkgs' nspawn tests do.
17 - crun refuses to start unless `/sys/fs/cgroup` is cgroup2, and the
17   build mounts nothing there. The launcher makes its own mount and
17   cgroup namespace and mounts one: the cgroup Nix delegated.
17 - The store is one bind per input, which an overlay does not show,
17   so it is bound read-only. No `/dev/net/tun`, so no uplink or LAN.
17 - Nix's seccomp filter refuses setuid bits, so suid-sgid-wrappers
17   failed. The runner tries a setuid bit and, refused, leaves a file
17   the unit's condition reads. PID 1's environment did not work: stage
17   2 starts systemd without the variable (measured).
17
17 The writable store is built: an overlay over the host's `/nix/store`,
17 its upper layer in the run's root. A guest adds a path and runs a
17 sandboxed `nix-build`, and the host's store gets neither. Not all of
17 `/nix`, as the other backends have it: unprivileged, `lowerdir=/nix`
17 fails with EINVAL where the host's store is a mount of its own, and
17 `lowerdir=/nix/store` works (measured with unshare). The single mount
17 exists for a kubelet `subPath`, and a container guest runs no kubelet.
17
17 The delegated scope is built. When its own cgroup is not writable,
17 the runner starts the launcher under `systemd-run --user --scope -p
17 Delegate=yes`. That execs in place, so the parent-death chain holds
17 (measured again with SIGKILL). A plain `uml-eval run container` and a
17 run started over MCP both pass with no wrapper.
17
17 The LAN is built. A helper joins the guest's user and network
17 namespaces, makes `vec1` as a tap, and copies frames to the segment
17 fd, one frame per read each way. `uml-eval run container-lan` puts two
17 containers and a UML guest on one segment: each reaches the others,
17 and an 8000-byte ping crosses unfragmented. The containers booted in
17 1.6 s, the UML guest in 7.0 s. SIGKILL of the runner took both
17 launchers, both relays and both containers with it.
17
17 The uplink is built: pasta joins the guest's namespaces by the init's
17 pid and gives it `vec0`, with passt's addressing, DNS and forwards.
17 One trap cost a run. With `/sys` writable, udevd starts in the
17 container, gets no uevents in a user namespace, and networkd leaves
17 every link "pending" for ever. `/sys` is now read-only, as nspawn and
17 podman have it, which is udevd's own condition for not starting.
1
18 ## Area 9 — agent and developer experience
18
18 **AX** is how an agent drives a run: the MCP tools, the monitor, and
18 what each reply costs in context. **DX** is the same for a person at a
18 shell. Seven findings started this area. An agent measured them on
18 2026-09-30, while it debugged Kata under nixkube. Carl's answers to
18 them are round 18.
18
18 The seven findings, ranked by the time they cost:
18
18 1. A consumer's session cannot take one more module from outside.
18 2. A `mkTest` test cannot pause.
18 3. `uml monitor` wakes an agent at each phase, and exits only at the
18    verdict.
18 4. Output from a VM inside a guest is not visible.
18 5. A reply can be very large.
18 6. No signal says that evaluation is done.
18 7. When `exec` fails, a command's output is only in the traceback.
18
18 ### Decided
18
18 - **One door.** `mkTest` and `mkSession` merge. A test has one
18   configuration. The output attribute that a caller picks sets how it
18   runs: `.check`, `.run` or `.phases`. An argument never sets it. This
18   is suggestion 8. It also closes finding 2, because every session can
18   pause already (`--break-on-failure`).
18 - **`defaults`, as nixos-tests has it.** It is a module that every node
18   imports, beside `nodes.<name>`. `run.nix` has no such option today:
18   `nodes` is `attrsOf deferredModule`, so a module common to all nodes
18   is written once for each node.
18 - **A test brings its own helpers.** A need that only one test has
18   becomes a Python module on that test's `pythonPath`. An agent calls it
18   from a paused run. The runner gets no feature for it. So finding 4
18   becomes a helper in the Kata test that reads the Kata guest console.
18   It is not a `vm.capture` in the runner.
18 - **An "evaluated" signal (finding 6).** An agent must know when it can
18   edit the working copy again. Trap: `events.jsonl` exists only when
18   `uml run` starts, and `uml-eval` evaluates before that. So the part
18   that wraps the evaluation sends the signal: MCP `start`, and a line
18   from `uml-eval run`. The session's event stream cannot send it.
18 - **A quiet monitor (finding 3).** `uml monitor` gets a mode that
18   prints only `paused`, `failed`, `finished` and `exited`. A `progress`
18   line wakes an agent and tells it nothing it must act on.
18   `channel_event` already sorts events into these kinds, so the mode is
18   a filter. When the monitor exits is open question 14.
18
18 ### Proposed, not decided
18
18 **An override from outside (finding 1).** `defaults` does not close
18 this finding. The time went into a scratch `default.nix` that
18 evaluated `<nixpkgs>` and not nixkube's pin, so each comparison was
18 against a different system. An override must run inside the
18 consumer's own evaluation. Two shapes:
18
18 1. **`.extend { modules = [ ... ]; }` on a session's output**, as
18    nixos-tests has `extend`. `lib.evalModules` gives it through
18    `extendModules`. A scratch file imports nixkube's session and adds
18    a module to one node. The pin stays, because the evaluation is
18    nixkube's. It needs a file, but no edit to the consumer.
18    Recommended.
18 2. **A knob that `uml-eval` reads**, such as `--node-module
18    cp=./scratch.nix`. It needs no file around the session. But it is a
18    second way to configure a run, and the one-door decision is against
18    that.
18
18 **A budget for replies (finding 5).** Measured: `events(kind="case")`
18 returned 233k characters, and `run_phase` returned 114k. A failed
18 nixkube case puts its whole pod and route dump in the event text.
18 `events` limits how many events it returns (50), but not their size.
18 The proposal has two parts:
18
18 1. **In the MCP server.** Each reply gets a budget: 16k characters by
18    default, and a `max_chars` argument changes it. The server cuts each
18    event's text to its last 2k characters. A cut event carries its line
18    number in `events.jsonl`, and `events(line=N)` returns it whole. The
18    same budget applies to `exec`, `inject`, `run_pytest` and
18    `run_phase`. Their whole output goes to a file under
18    `<out>/replies/`, and the reply names the file.
18 2. **At the source.** A large dump is an artifact, not event text. The
18    consumer writes it under `/artifacts`, and the event carries the
18    path. This is guidance for nixkube. It becomes a helper in
18    `uml_runner` when a second consumer needs it.
18
18 Part 1 comes first, because it keeps the context small whatever a
18 consumer does.
18
18 **Two things named "exec" (finding 7).** Carl read `exec` as Python
18 that runs inside a guest. That is not what exists today:
18
18 - **`exec` runs Python on the host**, in the paused session's
18   namespace: `session`, `vms`, and each guest as a `Machine`. It
18   reaches a guest the same way a phase does, with shell strings through
18   the agent (`await cp.succeed("...")`). It calls runner code. It does
18   not replace it. Finding 7 is about this one: when `succeed` raises,
18   the command's output is in the traceback text and not in the reply's
18   `output` field.
18 - **Python inside a guest does not exist.** The guest agent is a
18   Python process already (`uml_runner/agent.py`), but it offers only
18   `exposed_run`, which runs shell. An `exposed_python(source)` is small.
18   This is "Python instead of bash into the guests".
18
18 The two can both exist. Open question 15 asks whether the second one
18 is wanted.
18
18 ### Order
18
18 Each step lands alone. Small, independent steps come first. The merge
18 comes last, because it moves every consumer.
18
18 | step | what | finding | state | needs |
18 | --- | --- | --- | --- | --- |
18 | 1 | quiet monitor | 3 | decided | question 14 |
18 | 2 | evaluated signal | 6 | decided | — |
18 | 3 | reply budget, MCP part | 5 | proposed | question 17 |
18 | 4 | `defaults` | 1 | decided | — |
18 | 5 | `.extend` | 1 | proposed | question 16 |
18 | 6 | Kata console helper, in the Kata test | 4 | decided | — |
18 | 7 | `exec` output; Python in a guest | 7 | open | question 15 |
18 | 8 | one door: this repository's tests and nixkube's `ciTest` move to `mkSession`, then `mkTest` goes | 2 | decided | 4 and 5 make the move easier |
19
19 ### Round 19: one entrypoint, and one run
19
19 Decided:
19
19 - **The entrypoint follows nixos-test.** Its option names and output
19   names are the model, so a NixOS developer knows them already.
19 - **Unsandboxed and offline is the same run as sandboxed.** The guests
19   are the same derivations, and they see the same store. So a failure
19   in CI reproduces by hand, and nixos-test's `enableDebugHook` (a
19   pause inside the sandbox, attached with `sudo`) is not needed.
19 - **A sandboxed run only exits on failure.** Nothing can talk to it.
19 - **Pause-on-failure is chosen at run time, never in Nix.** The runner
19   reads it from an environment variable or a flag, so no derivation
19   changes. The unsandboxed outputs include two wrappers: one pauses on
19   failure and one exits on failure. The MCP server starts the one that
19   pauses.
19 - **A normal run needs no Nix daemon,** sandboxed or not. Everything is
19   built before the runner starts.
19 - **Guests are declared one by one**, so a caller changes one guest in
19   Nix and does not rebuild it from Python at run time.
19 - **Pause on start.** The runner stops before the first phase. A caller
19   then sets breakpoints, or changes which phases run and in what order,
19   and then continues. Rebuilding a guest from that pause (with `extend`
19   and the host's daemon) is possible, but it is not a goal.
19
19 A fact that breaks "the same run": **an unsandboxed guest sees the
19 whole host `/nix`.** `MachineSpec.store` is `/nix` by default
19 (`uml_runner/machine.py`). UML serves it over hostfs, and QEMU over
19 virtiofs as the overlay's lower layer (`modules/qemu.nix`). In the
19 sandbox the same guest sees only the closure of the run's inputs. So a
19 guest that uses a path it never declared passes by hand and fails in
19 CI. The guest's Nix database is the same in both, because the image
19 carries it; only direct file access differs.
19
19 Proposed: the unsandboxed runner gives the guests a store that holds
19 only the run's closure. It does this the way the Nix sandbox does:
19 in a user and mount namespace, one read-only bind for each path in the
19 closure, and hostfs and virtiofsd started inside that namespace. The
19 container backend has a user namespace already (area 8). Still to
19 measure: the time to make the binds for a closure of a Kubernetes
19 guest, and whether hostfs follows a bind mount.
19
19 Still open: whether the closure is the guests' closure or the whole
19 input closure of the sandboxed attempt. The second one is what the
19 sandbox really holds.
20
20 ### Round 20: measured on dynhetz
20
20 The spike is `spike/default.nix`, which is gitignored. Each line below
20 is one attribute in it, for example `nix run --file spike uplink.view
20 -- --out ./spike/out/view`. `variants` wraps any `mkSession`.
20
20 | question | answer | attribute |
20 | --- | --- | --- |
20 | user + mount namespace, host | yes | `probe.host` |
20 | the same in a Nix build, plain and `uid-range` | yes, both | `probe.sandbox`, `probe.sandboxUidRange` |
20 | virtiofsd over a bind view, `--sandbox none` and `namespace` | listens, all three places | `probe.*` |
20 | a UML run, user namespaces refused (seccomp) | fails: passt exits 1 | `uplink.blocked` |
20 | the same, passt replaced by a stub | boots and passes | `uplink.stubBlocked` |
20 | a UML run in a view of its own closure | passes, passt nested | `uplink.view` |
20 | what the guest sees, whole host store | 92699 entries, `hello` visible | `uplink.counted` |
20 | what the guest sees, in the view | 510 entries, `hello` hidden | `uplink.countedView` |
20 | two UML guests on `vec1`, user namespaces refused, passt a stub | ping each other both ways | `lan.stubBlocked` |
20
20 What follows from it:
20
20 - **UML, hostfs and the agent need no user namespace. passt does.**
20   passt says "Couldn't create user namespace" and exits. It has no
20   option to skip that isolation. `--offline` still starts passt, to
20   keep the forwards. So the level with no features is a UML guest with
20   no uplink and no forwards, and the runner must be able to start a
20   guest with no passt at all.
20 - **The store view gives sandbox parity for UML.** The view of the
20   runner's closure held 510 paths. The sandboxed attempt's inputs add
20   stdenv, about 30 paths. Not yet run: QEMU with virtiofsd inside the
20   view, which the probe only started.
20 - **Binding costs about 6 ms a path from bash** (510 paths in 3.0 s,
20   two `mount` processes each). A runner that calls `mount_setattr` and
20   `open_tree` directly spawns no process. Not measured yet.
20 - **UML fails at a soft limit of 1024 open files** (EMFILE in
20   `start_userspace`). A `systemd-run --user` unit has that limit, and so
20   do many login shells. The runner can raise its soft limit to the hard
20   limit at start without privilege.
20 - **Traps found in the spike.** A remount of the view's tmpfs re-parses
20   its options, and `uid=1000` is not mapped in the namespace, so it
20   fails. A bind remount does not. That bind must be recursive, or it
20   hides every path bound under it.
20 - **Where it depends on the host.** The kernel's
20   `user.max_user_namespaces`, and on Ubuntu 24.04
20   `kernel.apparmor_restrict_unprivileged_userns`. On dynhetz, Nix did
20   not stop a build from making a nested namespace. Not measured: a
20   GitHub runner, and Ubuntu.
20
20 Levels, from nothing to everything, each detected at start:
20
20 | level | needs | gives |
20 | --- | --- | --- |
20 | 0 | nothing | UML guests, the agent and the LAN; no uplink; the whole host store |
20 | 1 | a user namespace | passt uplink and forwards; a store view equal to the sandbox |
20 | 2 | `/dev/kvm` | QEMU guests |
20 | 3 | subordinate ids, a delegated cgroup | container guests |
20
20 Open: is the store view an output of its own, beside sandboxed and
20 unsandboxed (Carl's reading), or a level-1 feature that an unsandboxed
20 run turns on when it detects a user namespace?
21
21 ### Round 21: passt makes a sandboxed run need a user namespace
21
21 Carl wants to send sandboxed runs to remote builders. A builder can be
21 a host that forbids unprivileged user namespaces. Nix still sandboxes
21 there, because its daemon is root. But passt runs as the build user
21 and must make a namespace of its own.
21
21 Measured with `uplink.sandboxed` and `uplink.sandboxedNoUserns`. The
21 second runs the same build inside `bwrap --disable-userns`, and a probe
21 in the build proves that the block took ("No space left on device"):
21
21 | sandboxed run | nested user namespace | result |
21 | --- | --- | --- |
21 | `uplink.sandboxed` | allowed, as on dynhetz | passes; no route out |
21 | `uplink.sandboxedNoUserns` | refused | fails: "passt exited (1); the guest has no uplink" |
21
21 So a sandboxed run works only on a builder that allows unprivileged
21 user namespaces. No system feature says so: `featuresFor` asks for
21 `kvm` and `uid-range`, never for this. A builder without it takes the
21 job and fails after the guests start.
21
21 In a sandbox, passt gives three things and none is essential:
21
21 1. Forwards from the host into a guest. nixkube's CI test runs
21    `kubectl` on the host through a forward of 6443.
21 2. A default route, DHCP and a DNS server on `vec0`, all leading
21    nowhere.
21 3. A guest that has the same interfaces in every run.
21
21 `vec1` does not need passt. It carries frames between UML, QEMU and
21 container guests (`mixed`, `container-lan`), and it works with user
21 namespaces refused (`lan.stubBlocked`).
21
21 Proposed: passt only for the uplink, that is, only in an online run.
21 Forwards go through a channel that the runner owns, and `vec1` or the
21 guest configuration gives the default route. A sandboxed run then
21 needs nothing from a builder beyond Nix's own sandbox. An offline run
21 needs a user namespace only for the store view.
22
22 ### Round 22: user namespaces are a requirement
22
22 Decided. The round-21 proposal is rejected.
22
22 - **Every run depends on user namespaces:** sandboxed, offline and
22   online. user-mode-nixos is a modern test framework, and a host
22   without them is not a target. This replaces the levels table of round
22   20: level 0 is not built.
22 - **passt runs everywhere,** so a guest has the same interfaces, the
22   same addresses and the same forwards in every run.
22 - **Store views run everywhere,** in the sandbox as well as by hand.
22 - **Each guest gets its own store view**, of its own closure, in both
22   kinds of run. A guest that uses a path only another guest or the
22   runner has then fails in every run, not only in CI. The round-20
22   probe shows that each piece works inside both sandbox kinds; a whole
22   run with one view per guest is not measured yet.
22
22 Consequence: a builder must allow unprivileged user namespaces. No Nix
22 system feature says so. So checking for them is the first thing a run
22 does, sandboxed and by hand, before it starts anything. Without them
22 it fails at once and names the fix, as `container-probe` does for the
22 container backend. It never fails after the guests boot.
18
1 ## What "any machine" means
1
1 UML is Linux only. QEMU without KVM is slow enough to be a different
1 promise. The doc needs one line that says where the claim stops, or
1 "any machine" means "any of Carl's machines" and nobody finds out until
1 they try.
3
3 The evaluator narrows it again: a CLI that evaluates needs nanopynix
3 built for the machine it runs on. The check still runs anywhere Nix runs,
3 because the check reads a file.
1
1 ## Open questions
1
7 1. **How is a phase ordered?** Recommended: `after = [ ... ]`, sorted by
7    `lib.toposort`, which makes a cycle an evaluation error for free. See
7    area 0e. `mkOrder` is rejected: a number carries no reason, and the
7    dependency graph is what question 3 needs.
3 2. **Does a phase pick its guests?** A recipe that brings up a cluster
3    also wants to say what the guest must be. If a phase can contribute
3    NixOS configuration as well as a script, a recipe becomes one thing
3    instead of two that must be used together.
8    Answered: yes, configuration and a script. Roles follow in area 7 —
8    per recipe, as a node name, and self-knowledge only.
8 13. **Is there a shared filesystem between guests?** No, and adding one
8    is what makes a peer list easy to write by accident. See area 7.
7 3. **What happens after a phase fails?** Recommended: skip its
7    dependents, run everything else. Possible only because `after` gives
7    the graph. nixpkgs stops the whole run; pytest runs it all; phases are
7    neither. See area 0e.
3 4. **Does the CLI let a phase's script be overridden with a path?** It is
3    what keeps iteration fast once the phase list lives in Nix.
1 5. **Does `nix build` ever take a steer?** Today: no, by design. The cost
1    is nixkube's `NIXKUBE_UML_SCENARIOS`.
3    Answered in part: with `envOrDefault`, `nix build` *does* take a
3    steer, and pays for it with a rebuild.
4 6. **One kind of knob, or two?** Mostly dissolved by area 0a. Once
4    nothing built depends on the spec, an evaluation-time knob costs an
4    evaluation, not a rebuild, and one kind is enough. Re-open only if an
4    evaluation turns out to be slow enough to notice.
2 7. **How does a stream leave a guest?** See area 5. The `/artifacts`
2    answer needs no protocol change; the other two do.
2 8. **What does "no internet" turn off?** See area 6.
3 9. **Does the sandboxed path keep the spec file?** Assumed yes — the
3    sandbox must not evaluate. Worth confirming, because it means two
3    doors into the same run for good.
5 10. **How does a guest learn about a store path in the spec?** Probably
5    answered: typed options. A `types.package` option is a path and a
5    `types.str` option is not, so nothing is discovered and nothing is
5    scanned. See area 0c. The sorted-list-file answer stays written down
5    in area 0a for the case where a free-form attrset survives somewhere.
5 11. **What does an evaluation cost per run?** Answered: 2.7s to 9.3s,
5    warm. Acceptable against a 6.9s boot and a twenty-minute test. See
5    area 0c for the table.
5 12. **Where do the CLI's build output and build failures go?** New
5    surface: realising a derivation moves inside the runner.
18 14. **When does a quiet monitor exit?** At the first pause, so that
18    an agent's Monitor ends and wakes it once? Or at the verdict, with
18    one line for each pause? Carl: not clear yet.
18 15. **Is Python inside a guest wanted?** And if so, what may it
18    import there? See area 9.
18 16. **How does an override reach a session from outside?**
18    Recommended: `.extend`. The alternative is a knob in `uml-eval`.
18 17. **Are 16k characters a reply and 2k an event the right budget?**
18    Both numbers are a first guess.
1
1 ## Issues
1
1 - #14 steering a run (the landed part)
1 - #15 output directory, and the stdout filter
1 - #10 MCP server and interactive sessions
1 - #11 reboots
16 - #17 a guest as a rootless container (Area 8)
