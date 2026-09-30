# Working in this repo

Read README.md first — it explains how the pieces fit together.

## One function

`mkTest` is the function: guests plus named phases, ordered in Nix,
with `.driver`, `.driverDebug` and `.driverInteractive` as the doors by
hand. Every test here is one. `docs/design/runner.md` is the design and
records what is decided and what is not.

`fromNixosTest` (`nixos-test.nix`) maps a nixos-test spec onto it and is
kept apart from it.

The split underneath is the point. `pkgs/vivarium-runner` is the **mechanism**
— guests, backends, the agent channel — and it has no opinion about
sequence. `pkgs/vivarium` owns the **sequence**: a `Session` something drives
a step at a time, and `vivarium run` is one linear drive of it. An MCP server
will be the same object driven slowly, which is why teardown is never
automatic and why nothing per-run may be a module global.

Logic goes in `pkgs/vivarium/vivarium/phases.py` as pure functions and is tested
without a guest. Effects go in `session.py`. A guest test is for what a
pure test cannot see — and it has already earned that: the phase-skip
rule was correct in `phases.py` while the driver ignored its answer, and
only `nix build --file . phase-rules` caught it.

## Running a session by name

```sh
nix run --file . vivarium-eval -- run pytest-phase --out ./out -- -k hostname
nix run --file . vivarium-eval -- phases recipes
```

`vivarium-eval` evaluates with nanopynix, builds the attribute's `.driver` (so
the phase type check runs) and hands the spec to `vivarium run`. A check that
wraps a session carries it as `.session`, and `vivarium-eval` steps into it,
so the check's name works. Evaluation is impure, like `nix build
--file`, so a knob reads the environment.

It is its own package on nanopynix's Python set (`pkgs/vivarium-eval`), and
nothing a consumer uses depends on it: the sandboxed check never
evaluates. Not a CI check yet, because CI would have to build nanopynix.
Its unit tests are `nix build --file . vivarium-eval.tests`.

## Reaching into a paused run

Do not iterate by editing a phase and re-running: that is an evaluation
and a store path each time, and a guest change is a new image. Pause and
send Python in:

```sh
nix run --file . mine.driver -- --out ./o --break check &   # or mine.driverDebug
vivarium ctl --out ./o state
vivarium ctl --out ./o exec 'await one.succeed("systemctl --failed")'
vivarium ctl --out ./o exec - < snippet.py         # top-level await; names persist
vivarium ctl --out ./o inject ./scratch.py         # a file's test(vms), from the working tree
vivarium ctl --out ./o pytest ./tests/chaos -- -k etcd   # pytest from the working tree
vivarium ctl --out ./o run check                   # a declared phase
vivarium ctl --out ./o continue
```

In scope for `exec`: `session`, `vms`, each guest by name, `anyio`.
`exec` and `inject` also work while phases run, beside them: their
prints go to the reply by task, and the agent channel multiplexes
calls. `pytest`, `run` and `continue` are refused unless the run is
paused.

`pytest` is the loop for a pytest phase: edit a test, send it again,
against guests a long setup already built. It takes only its own
arguments, and its cases are `case` events marked `data.by_hand`, left
out of `status` and `junit.xml`. Each pytest run drops the modules it
loaded from its tests directory when it ends; pytest's importlib mode
would otherwise hand the next run the old module. Every
operation and its output is an event. The socket is `<out>/control.sock`,
mode 0600, and exists for every run except a sandboxed check, which
passes `--no-control`.
`vivarium/control.py` is the whole of it; `nix build --file . breakpoint`
drives it against a guest.

## Iterating on a kernel

`--kernel PATH` boots a kernel from your own tree instead of the one Nix
built: `linux` from `make ARCH=um`, or a bzImage for QEMU with virtio
built in (the guest's modules match Nix's kernel, not yours). It is by
hand only, and the run records that it is not the check's kernel. `nix
build --file . kernel-override` proves both directions: a copy boots,
and a file that is not a kernel fails the boot at once.

To rebuild the kernel as Nix builds it without starting cold each
time, build `umlKernelCcache` with a cache directory mounted into the
sandbox:

```sh
pynix build --file . --attr umlKernelCcache --namespaced \
  --sandbox-path /ccache=$HOME/.cache/uml-ccache
```

Measured on 16 cores: 122s cold, 31s after a one-line change (1187 of
1188 compiles hit). Needs nanopynix with the `--sandbox-path` fix: the
flag replaced nix.conf's `sandbox-paths`, `/bin/sh` included. It is a
different derivation from `umlKernel`, whose derivation is unchanged,
so CI never builds it. Traps, all measured: ccache on the config
derivation breaks `.config`; `buildFlags` carry their own `CC=`; `env`
does not export under structured attributes.

That last half found a bug: the UML bridge never watched its kernel
child, so a kernel that failed to start, panicked or powered off left
the runner waiting out its whole boot timeout. The bridge now exits with
the kernel's status.

## The MCP server

`.mcp.json` registers `vivarium-mcp` as `vivarium`. Its tools are `start`,
`state`, `exec`, `inject`, `run_pytest`, `run_phase`, `resume`, `stop`,
`events` and `runs`. A run started by `start` is a child process with
`--break-on-failure`, never the server itself: MCP's stdio is the
server's stdout, and a session prints to stdout.

It pushes `<channel source="vivarium" run=... event="failed|paused|finished|exited">`
into the session. Channels are a research preview, so they reach Claude
only when started from this directory with:

```sh
claude --dangerously-load-development-channels server:vivarium
```

Without the flag the tools still work, and `vivarium monitor` carries the
same events. `start` returns it as `monitor`, a command line with
`--quiet`: run that in Claude Code's Monitor tool, and it wakes you for a
pause, a failure and the verdict, not for each phase that passes.
`monitor_all` is the same without `--quiet`. Each run has `<out>/monitor.sock`, served
by the `vivarium-mcp` that started it; `vivarium monitor <out|run id>` replays the
run's events so far, prints each one as one line (`--json` for JSONL)
and exits with the verdict: 0 passed, 1 failed, 2 exited without one,
3 the stream ended first. `vivarium/monitor.py` is the client. `nix build
--file . mcp-check` drives the server over raw JSON-RPC against a guest,
and checks a monitor prints exactly the channel's events.

## Getting evidence out of a session

`grep '\[test\]'` over a build log still works. Prefer the files:

```sh
nix build --file . mine            # result/ links all of them
jq -r 'select(.kind=="phase_finished") | "\(.phase)\t\(.data.state)"' result/events.jsonl
cat result/console/cp.log          # one guest, no grep
```

An event carries `machine`, `phase` and `seconds` as fields, so a
question about timing or about one guest is `jq` and not a regex. The
`log` file holds the same run unfiltered, for reading.

Every guest streams its journal while it runs (`vivarium.journal`, on by
default). Each entry becomes a `journal` event with `data.unit`,
`data.identifier` and `data.priority`, attributed to the phase that was
running:

```sh
jq -r 'select(.kind=="journal" and .machine=="cp" and .data.unit=="kubelet.service") | .text' result/events.jsonl
jq -c 'select(.kind=="journal" and .data.priority <= 3)' result/events.jsonl   # every error, every guest
```

It goes through `/artifacts/journal.jsonl`, not the agent's RPC.
hostfs and virtiofs are write-through (measured, `default.nix` `incr`),
so an entry is on the host's disk when journald has it, and a guest
killed with SIGKILL keeps everything it logged — `nix build --file .
stream` proves that with `vm.crash()`.

**Do not add a `print` to the runner.** `Session.emit` is the one way in,
and everything downstream — terminal, log, JSONL, per-guest files, JUnit
— follows from it. A phase script may `print` freely; that is captured
and attributed to the phase.

A guest's console is not on the terminal by default. `-vv` puts it there,
and a failed phase replays the last 20 lines of every guest by itself.

## Where a test script belongs

This repository is a library: `mkTest`, the guest modules, and
`vivarium.runner` (the `vivarium_runner` package, `py.typed`). A test script belongs
in the project it tests.

    let vivarium = import (sources.vivarium + "/lib.nix") { inherit pkgs; };
    in vivarium.mkTest {
      name = "...";
      nodes.one = { };
      phases.check = { script = ./tests/vivarium/check.py; after = [ "boot" ]; };
    }

`tests/` here is for this repository's own facilities — segment,
forwards, store, `/artifacts`. Do not add another project's script to it.

A helper a second script wants belongs in `vivarium_runner` — waiting on a
unit, reading a journal, asking systemd what failed. A consumer copying
one out of `tests/` means it should be a method on `Machine`.

```nix
vivarium.typeCheck { name = "mine"; scripts = [ ./tests/vivarium/check.py ]; }
```

pyright against `vivarium_runner` in a derivation. **Annotate the parameter** —
`async def test(vms: Machines) -> None`. Unannotated, `vms` is Unknown and
nothing done to it is checked: measured, `await vms.node.succeed(123)` and
a call to a nonexistent method both passed. `reportMissingParameterType`
is on, so an unannotated script does not build.

A phase can be a pytest run instead of a script:

```nix
phases.cases = { pytest.tests = ./tests/guest; after = [ "boot" ]; };
```

Each guest is a fixture named after it, `vms` is all of them, and a test
or fixture may be `async def` and await `Machine` directly. pytest runs
in a worker thread; async code is sent back to the session's loop through
a portal, because a `Machine` only works on the loop that started it.
`vivarium run ... -- -k name` selects. Each test is a `case` event and a JUnit
case, and every command and journal entry carries `data.case`.
`vivarium/pytest_plugin.py` is the whole of it; `nix build --file .
pytest-phase` proves it against a guest.

An async generator fixture's setup and teardown are two separate portal
calls, so an anyio task group held open across its `yield` does not
work.

A suite that must run *inside* a guest -- one that starts daemons, or
builds into stores it makes -- writes JUnit to `/artifacts/junit/*.xml`
there. The session reads each new file at the end of the phase, and every
test becomes a `case` event with its machine and phase and a case in the
run's `junit.xml`. Any runner that writes JUnit works. The phase's verdict
is still its script's.

`vms.phase` names the phase running, so one script can serve phases
generated from a list in Nix. `vms.shared` is a dict that survives from
one phase to the next. `nix build --file . guest-suites` proves all
three.

## Phases at once

`phases.<name>.nodes = [ "a" ]` declares the guests a phase uses; empty
is every guest. Once its `after` has finished, a phase starts beside the
running ones when their guests do not overlap. So a session gets
parallelism by declaring what each phase touches, and one that declares
nothing runs one phase at a time.

- The script's `vms` holds only its guests, with its own `vms.phase`;
  `shared`, `settings` and `knobs` are the session's. Reaching an
  undeclared guest fails on the name.
- Commands, journal entries and prints are attributed by the guest
  that produced them, and prints through a context variable. Keep both
  exact when touching `session.py`: `settle`, the JUnit import and the
  console replay take the phase's guests, never all.
- A pytest phase holds every guest whatever its `nodes`: pytest is not
  reentrant, and `--capture=sys` swaps the process's stdout.
- A breakpoint or a failure stops new phases, and the pause starts when
  the running ones end. Paused means nothing runs.
- `vivarium run --serial` runs one at a time in Nix's order.

Logic in `phases.ready`/`launchable`; the loop in `cli._schedule`. `nix
build --file . parallel` proves the overlap, the negative control and
the attribution.

## VCS

This project uses jj (Jujutsu), not git. Do not use git commands.

Flake entry points only see tracked files, so run `jj st` after adding a
file and before building.

## Two backends

`vivarium.backend` is `uml` or `qemu`, and `mkTest` takes it. A test
script never knows which it got, and neither does a node configuration --
keep it that way. Anything that has to differ belongs in
`modules/qemu.nix` or in `vivarium_runner/backend.py`, not in a test.

Every test carries `.uml` and `.qemu`, so do not add a second attribute
to run a test on the other backend. `mkTest` builds both variants from
one set of arguments; the one `backend` names keeps the bare derivation
name.

When you change `machine.py`, `net.py` or `forward.py`, run both:

```sh
nix build .#lan .#lan.qemu --print-build-logs 2>&1 | tee /tmp/umlboth.log
```

`.#lan.qemu` needs `/dev/kvm` and asks the daemon for the `kvm` feature,
so it refuses to build where there is none rather than failing.

A run can hold both kinds: a node sets its own `vivarium.backend`, and
the run's `backend` is only the default. The toolchain and the `kvm`
feature follow the machines, and a segment carries raw frames both
accept. UML for what is single-threaded and should cost the host little,
QEMU for what wants the CPU. `nix build --file . mixed` proves it, by
hand (it needs /dev/kvm).

A third, `container`: the system under crun, no kernel of its own (Area
8 of the design, issue #17). By hand the host needs a user namespace,
65536 subordinate ids with working `newuidmap`/`newgidmap`, and a
cgroup it can delegate — `container.probe()` tries each and the boot
fails naming what is missing.

Sandboxed (`nix build --file . container`) it needs a daemon with
`uid-range` (`featuresFor` asks; dynhetz has it, and CI's
`test-container` gets it from ghanix's `nix.install.uidRange` — a stock
runner needs nothing more than `userNamespaces`). There the
build is root with 65536 ids: the guest shares that user namespace
(`owns_ids`), the store is an rbind (one bind per input defeats an
overlay), the uplink and LAN need /dev/net in `extra-sandbox-paths`
(ghanix `nix.install.devNet`; locally `--option extra-sandbox-paths
/dev/net` as a trusted user) and the guest shares the build's user
namespace, so pasta and the tap relay join the net namespace only; the
launcher
mounts the cgroup2 crun insists on (`_ensure_cgroup2`), and seccomp
refuses setuid, so the runner drops `no-setuid` in the agent directory
and container.nix skips suid-sgid-wrappers on it.

`container-probe` / `container-probe-tun` run `container.probe()` in the
sandbox and fail in seconds, naming each missing piece and its fix. Every
session with a container guest depends on the one it needs (`probeFor`:
`-tun` when a container has a LAN); CI builds it first on its own. Where its own cgroup is not writable, the
runner starts the launcher under `systemd-run --user --scope -p
Delegate=yes` (`container.scope()`), which execs in place and so keeps
the parent-death signal. Plain `vivarium-eval run container` and MCP `start`
both work.

- `modules/container.nix`: `boot.isContainer`, a root template directory
  the runner copies, an overlay over the host's `/nix/store` (not all of
  `/nix`: unprivileged, that lower fails with EINVAL). A guest adds to
  its store and builds in its own Nix sandbox. No memory control yet.
- LAN: `crun_launch tap` joins the guest's user and net namespaces (a
  process of its own: setns into a userns needs one thread), makes
  `vec1` and copies frames to the segment fd. `nix build`-free proof:
  `vivarium-eval run container-lan` (two containers and a UML guest, jumbo
  frames unfragmented).
- Uplink: the launcher starts pasta on the init's pid once crun has one,
  as `vec0` with passt's arguments; `_pasta_forwards` turns off what pasta
  forwards beyond passt. `/sys` is read-only so udevd stays off: in a
  user namespace it gets no uevents and networkd waited on it for ever.
- The agent listens on `unix:/run/host/agent/sock`; `Launch.agent_path`
  makes `Machine` connect after the ready line.
- `vivarium_runner.crun_launch` relays the pty crun hands over the console
  socket; crun will not write it to a pipe. EIO on the master is systemd
  re-opening the console, not the end.
- Lifetime chain: runner → (systemd-run, exec'd in place) launcher →
  crun, pasta, tap (`die_with_parent`)
  → init (`setpriv --pdeathsig KILL`). SIGKILL of the runner leaves nothing
  (measured). Keep every link when touching it.

A QEMU guest gets `-cpu host` minus `vmx` and `svm`, so it cannot run
VMs. `vivarium.nestedVirtualization = true` passes the flag through and
loads KVM in the guest; the host needs nesting on. `nix build --file .
nested` proves both halves, by hand only: a GitHub runner's KVM does
not nest again.

## Building and running

Always tee to a log file; these builds are slow and boot output is long.

```sh
nix build .#lan --print-build-logs --no-link 2>&1 | tee /tmp/umlbuild.log
./run.sh --command hostname 2>&1 | tee /tmp/umlrun.log
```

Then grep the log rather than rebuilding.

A guest's console is very noisy. To see just what a test did:

```sh
grep '\[test\]' /tmp/umlbuild.log
```

Every console line is prefixed with the machine it came from, so
`grep '\[server\]'` narrows to one guest.

## Getting evidence out of a run

A test derivation never fails. `<test>.attempt` runs the guests and writes
its exit code to `status`; `<test>` reads that file and nothing else. So a
failed run is a kept output:

```sh
nix build --file . artifacts          # fails, and prints the path
ls "$(nix eval --raw --file . artifacts.attempt)"   # status log report.json artifacts/
```

A passing build symlinks the same four into `result/`.

Each guest sees a host directory at `/artifacts` — hostfs under UML,
virtiofs under QEMU, and a test never knows which. The file is on the host
the moment it is written, so it survives a guest that wedges. Host side:
`vms.artifacts / "<node>"`, one per guest. Outside the sandbox it is a
temp directory, named on the first line of the run.

Put real output there — junit, logs, a `ps` snapshot — not through
`succeed`, whose output crosses the serial line into the console log.

`await vm.processes()` and `await vm.count_processes("nix-daemon")` read
`/proc` through the agent, so a node with no procps still answers. That is
how a test proves the thing under test left nothing running.

## Where the time went

Do not guess at what makes a test slow, and do not add timing prints.
Every run records itself.

A run writes `report.json` into its `--out`, which for a check is its
own output; that is why a test's output is a directory. It writes on
failure too.

```sh
jq '{total_seconds, boot_seconds, waiting_seconds}' result/report.json
jq '.slowest[:5] | .[] | {what, seconds}' result/report.json
jq '.by_command[:5]' result/report.json
```

A `wait` step *contains* the `rpc` steps inside it -- a poll loop is many
round trips and the sleeps between them. Do not sum the two kinds.

`vivarium_runner/report.py` is the whole of it. `Machine._ask` is the one
choke point every guest round trip passes through, so a new command type
is timed without touching it.

## Measuring the network

`nix build .#iperf` prints what a segment carries. Two things make those
numbers lie:

- **Host load.** A guest is a process and a busy builder halves the
  result. The same configuration measured 1.9 Gbit/s on a saturated
  host and 4.4 Gbit/s on an idle one. Only compare runs taken back to
  back.
- **The namespace.** Outside a sandbox the socketpair inherits the
  host's `net.unix.max_dgram_qlen`, which is usually far above the 10 a
  fresh network namespace gets. To measure what a test will actually
  see, run under `unshare -rn`.

## Port forwarding

`vivarium.forward` is read before the guest boots and cannot be changed
after, because passt cannot: it binds every socket while parsing its
arguments and has no control socket. `auto` mode is pasta-only. If you
find yourself designing something that watches the guest and adds a
forward, it ends in restarting passt and dropping every connection.

The specs are built in `pkgs/vivarium-runner/vivarium_runner/forward.py`. Two
things there are load-bearing and non-obvious:

- A spec of *only* exclusions (`127.0.0.2/~32768-60999`) is what puts
  passt in weak mode, where a port it cannot bind is skipped. Any base
  range in the spec makes every failure fatal instead.
- Two specs that overlap on a port are fatal, not a warning. That is why
  the privileged block is excluded from the wide range before being
  added back with an offset, rather than simply appended.

To see what a rule turns into without booting anything:

```sh
nix shell nixpkgs#python3 --command python3 -c '
import importlib.util, sys
s = importlib.util.spec_from_file_location("f", "pkgs/vivarium-runner/vivarium_runner/forward.py")
f = importlib.util.module_from_spec(s); sys.modules["f"] = f; s.loader.exec_module(f)
print(f.to_args([f.Rule(address="127.0.0.2")], start=1024))'
```

Then check it against real passt before believing it — `passt --foreground
-s /tmp/p.sock <the -t args>` and `ss -tlnH | grep -c 127.0.0.2` says
whether it bound what you meant. The socket path has to be short; passt
rejects anything near `UNIX_PATH_MAX`.

## Generated files

`.github/workflows/*.yml` is rendered from `ci/workflows.nix`. Edit the
Nix, then `nix run .#render-workflows`, then commit both — CI runs
`.#check-workflows` and fails on drift.

## The Kubernetes test

`.#k8s` is far heavier than the others: three guests and about 5 GB of RAM
between them, which is more than a dev machine usually has to spare even
though the run itself takes about four minutes. It is meant for CI. Do not
reach for it while iterating — reach for these:

```sh
nix build .#check-k8s-images .#check-k8s-config   # seconds
nix build .#containerd                            # one guest, ~2 minutes
```

The first two ask the real `kubeadm` whether the images are the ones it
will pull and whether it accepts the configuration the module generates.
`.#containerd` boots one guest and starts a single container through CRI,
which covers the kernel, the image import and the store mount. Almost
everything that breaks the cluster breaks one of these first, and CI runs
`test-k8s` only after `test-containerd` has passed.

To build everything the cluster test needs without running it:

```sh
nix build .#k8s.spec --print-build-logs 2>&1 | tee /tmp/umlk8s.log
```

Two pieces of it are easy to break without noticing:

- The images are symlinks into `/nix/store` and nothing else. They only
  run because containerd's `base_runtime_spec` bind-mounts the store into
  every container. The pod sandbox does *not* get that spec, which is why
  `pause` is the one image built with its closure.
- Those symlinks are not references. A layered image is a gzipped tar, so
  Nix cannot see the store paths inside it and the merged tarball has no
  references at all — `system.extraDependencies` in `modules/k8s.nix` is
  what actually puts etcd on the node. Adding an image to `imageSpecs`
  carries its closure along; writing a symlink by hand in `extraCommands`
  does not, and shows up as `executable file not found in $PATH`.
- Kernel options for containers live in `containerConfig` in
  `pkgs/uml-kernel/default.nix` and are unconditional. `ignoreConfigErrors`
  is on, so an option that does not exist or whose dependencies are unmet
  is dropped silently — check the built config, do not assume:

```sh
grep -E '^CONFIG_(NF_|IP_NF_|VETH|BRIDGE)' \
  "$(nix build --no-link --print-out-paths .#umlKernel)/config"
```

## Things that bite

- A failure inside the guest agent shows up on the host as an exception
  from `vm.execute`, but the guest-side traceback is only in the guest
  journal — `await vm.journal("vivarium-agent")`.
- `vivarium.memory` below ~192M gets the agent OOM-killed partway
  through a test, which looks like a hang.
- `vivarium.memory` is a ceiling, not a cost: guest memory is a sparse
  file on both backends, and the guest reports the blocks it frees so the
  host punches holes in it. `vm.host_memory_kib()` is what the host pays;
  no number inside the guest can see it. Measured at `memory = "1024M"`,
  boot / after reading the closure / seconds after `drop_caches`: UML
  149M, 553M, 148M; QEMU 349M, 782M, 404M.
- `vm.shrink("256M")` / `vm.grow(...)` squeeze the guest, not the host --
  reporting has already taken the host side. `drop_caches` first, and
  read `vm.meminfo()`: it takes pages that are already free, and how many
  it got is worth measuring. Both backends.
- The whole store is shared into the guest over hostfs, so anything the
  guest writes to `/nix/store` lands in a tmpfs overlay and is lost on
  poweroff. That is intentional.
