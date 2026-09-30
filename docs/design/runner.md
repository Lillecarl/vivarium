# The runner: design

Carl and Claude write this together. It holds what is decided and what
is open, in the present tense. When a decision changes, the text changes;
the commit log holds the history. Evidence is a spike attribute
(`nix run --file spike <attr>`, gitignored) or a check in `default.nix`.
The long record up to 2026-09-30 is `history/running-anywhere.md`.

## What a test is

- **One Nix function, `mkTest`.** The old `mkTest` and `mkSession`
  merged into it, under the name a caller writes: a test. "Session"
  names the running thing only (the `Session` class, MCP). Its option
  and output names follow nixos-test, so a NixOS developer knows them
  already.
- **A test is a module:** guests under `nodes.<name>`, a `defaults`
  module every guest imports, and `phases`. Guests are declared one by
  one, so a caller changes one guest in Nix, not from Python at run time.
- **A phase** is a Python module exporting `async def test(vms)`, or a
  pytest run. Nix orders phases by `after`. A failed phase skips its
  dependents and nothing else. `always` runs a phase anyway. `nodes`
  names the guests a phase holds; phases with no common guest run at
  once.
- **Every guest knows static facts about its peers**, as in nixos-test:
  each guest's module gets `nodes`, and every guest's address is in
  every `/etc/hosts`. A guest may name another guest, its address and
  a secret shared in Nix (a fixed `kubeadm` token, for example). So
  setups that retry by themselves form inside the guests, and the host
  only waits and checks. This drops the rule in `modules/k8s.nix` that a
  node knows only about itself.
- **Recipes are modules** that add a phase, the guest configuration it
  needs and its knobs.
- **Knobs are module options** that read the environment
  (`envOrDefault`). Nothing that is built depends on the spec.
- **A test brings its own helpers**, as Python modules on `pythonPath`.
  The runner gets no feature that only one test needs.

## How a test runs

- **Sandboxed** (`nix build`): the run exits on the first failure.
  Nothing can talk to it.
- **Unsandboxed**: two wrappers among the outputs, one that pauses on
  failure and one that exits. The choice is made at run time, never in
  Nix. The MCP server starts the one that pauses.
- **Offline unsandboxed is the same run as sandboxed:** the same guests,
  the same store, the same network. A CI failure reproduces by hand, so
  no debug hook into the sandbox is needed.
- **A normal run needs no Nix daemon.** Everything is built before the
  runner starts.
- **Pause on start.** The runner can stop before the first phase, so a
  caller sets breakpoints or changes the phases, then continues.
- **A paused run** takes `exec` (Python on the host, in the session's
  namespace), `inject` (a file's `test(vms)`), `pytest` and a declared
  phase, all from the working tree.

## The function

Reviewed by Carl on 2026-09-30. Names are nixos-test's, from
`nixos/lib/testing` in the pinned nixpkgs; where this differs, the
reason is on the line. The outputs are a superset of nixos-test's: we
add what is useful and do not limit ourselves to its set.

**A compat function maps nixos-test specs onto this one**, literally,
and lives apart from it. `testScript`, `containers`, `nodeDefaults` and
the rest of nixos-test's shape are its inputs, never the main
function's. The main function stays as clean as if nixos-test did not
exist. Built: `fromNixosTest` (`nixos-test.nix`) with nixos-test's
script API in `uml_runner/nixos_test.py`. `nixos-tests` runs
nixpkgs' simple-vm, systemd-no-tainted and oh-my-zsh sandboxed, and
simple-container by hand (it needs `/dev/net/tun`).

Built: the outputs below, `interactive`, and this repository's tests
as `mkTest`s; the old `mkTest` is gone. nixkube still calls the old
shapes and moves next.

Inputs, as module options:

| option | nixos-test | here |
| --- | --- | --- |
| `name` | same | same |
| `nodes.<name>` | a NixOS module | same; the guest's backend is an option in it, `boot.uml.backend` |
| `containers.<name>`, `nodeDefaults`, `containerDefaults` | nspawn containers apart from VMs | none: a container is a node with `backend = "container"`, so one set holds every guest and backends mix freely. The compat function maps them |
| `defaults` | a module every node imports | same (built) |
| `testScript` | one Python script | none; the compat function maps it to one phase after `boot` |
| `phases.<name>` | none | the ordered steps |
| `extraPythonPackages` | Python the script imports | same, beside `pythonPath` for local modules |
| `interactive` | a module merged in `driverInteractive` | same |
| `globalTimeout`, `meta` | same | same |
| `backend` | none | the default for every node |
| `knobs`, `settings` | none | kept |

Outputs:

| output | nixos-test | here |
| --- | --- | --- |
| the derivation | the sandboxed run | same; exits on the first failure |
| `.driver` | the run by hand | same; exits on the first failure. Replaces `.run` |
| `.driverInteractive` | by hand, into a ptpython REPL with the test's symbols; nothing runs until asked | the same: the run pauses before the first phase and a REPL attaches to it, with `vms`, each guest and a way to run a phase or the rest |
| `.driverDebug` | none | by hand, and pauses on the first failure with the guests up; the MCP server starts this one |
| `.nodes`, `.config` | the evaluated guests and test | same (`.nodes` built) |
| `.extend { modules; }` | the test with more modules | same |
| `.uml`, `.qemu`, `.container` | none | helpers over `.extend`: every node on that backend |
| `.phases` | none | kept: the phases in order, without booting |

Both drivers take the same flags at run time: `--break PHASE`,
`--break-on-start`, `--only`, `--offline`. Nothing about a run's mode is
set in Nix.

**Cleanup always happens**, unless `--keep` turns it off. However a run
ends, success, failure, ^C, SIGTERM or SIGKILL of the runner, it
leaves no guest, helper, mount or run directory behind (`cleanup`,
sandboxed, with a negative control). Built:

- One run root per run holds everything outside `--out`; `TMPDIR`
  and the short-path socket fallbacks point into it.
- A cleaner, forked before the namespace, waits on the runner's pidfd
  in a session of its own and removes the root however the runner
  died. A reaper at start removes roots whose runner and cleaner are
  both gone.
- SIGTERM and SIGHUP take the ^C path. Each guest that was asked to
  power off gets 20 s, then its process group gets SIGTERM, 30 s,
  then SIGKILL.
- Store views go with the run's mount namespace.

The MCP server drives this runner only. It knows nothing of
nixos-test's driver; a nixos-test spec reaches it through the compat
function.

Decided: the output that pauses on failure is `.driverDebug`, and
`interactive` is built now: it is small, and it is how a person learns
a test.

## Isolation

- **User namespaces are required**, in every run, sandboxed or not.
  Checking for them is the first thing a run does; without them it fails
  at once and names the fix. It never fails after the guests boot.
  Ubuntu 24.04 needs `kernel.apparmor_restrict_unprivileged_userns=0`.
- **passt runs in every run**, so a guest has the same interfaces,
  addresses and forwards everywhere. passt refuses to start without a
  user namespace; this holds inside the sandbox too
  (`uplink.sandboxedNoUserns` fails, `uplink.sandboxed` passes).
- **Each guest sees only its own closure** in `/nix/store`, in every
  run. The runner builds a view per guest: a tmpfs with one read-only
  bind per path, read-only as a whole through `mount_setattr`
  (`uml_runner/storeview.py`). The closure is the closureInfo the
  guest's Nix database is loaded from (`umlNixRegistration`), so the
  database lists exactly what the view holds. virtiofsd serves a QEMU
  guest's view, and a UML guest's `/init` mounts it from `UML_STORE` on
  the kernel command line (`store-view`: all three backends, by hand
  and sandboxed; it fails with views off).
- **A VM guest's store is writable through its own overlay**, inside its
  own kernel, on its own disk. The host serves only the read-only view,
  so the guest's build users need no ids in the run's namespace, and a
  plain sandbox maps only one.
- **A container guest's store is nixkube's layout:** a writable
  directory on the host holding one read-only bind per closure path. New
  paths sit beside the binds; a supplied path cannot be deleted or
  overwritten. No overlay, because a host-side overlay cannot see
  binds. Built; writable in the sandbox too (`store-view`).
- **Guests talk to each other on `vec1`**: socketpairs, and a hub in the
  runner for three or more. It needs no namespace and no passt
  (`lan.stubBlocked`). UML, QEMU and container guests mix in one run
  and reach each other over IP by name (`backends`).
- **The run's own user namespace maps root and, when the host has
  them, the caller's subordinate ids.** Container guests need those ids
  (`newuidmap`), and nothing else does. A run with a container guest on
  a host without subordinate ids, a delegated cgroup or a writable
  `/dev/net/tun` fails at start and names what is missing.
- **The runner raises its open-file soft limit** to the hard limit at
  start. UML fails at 1024 (EMFILE), which many shells and every
  `systemd-run --user` unit have.

## Agent experience

- `uml monitor` gets a quiet mode that prints only `paused`, `failed`,
  `finished` and `exited`.
- An "evaluated" signal tells an agent when it can edit the working copy
  again. The part that runs the evaluation sends it (MCP `start`,
  `uml-eval run`), because `events.jsonl` does not exist yet then.

## Open

1. **When does the quiet monitor exit:** at the first pause, or at the
   verdict?
2. **Python inside a guest:** wanted? The agent is a Python process
   already; `exec` today runs on the host.
3. **A size budget for MCP replies.** A failed nixkube case returned
   233k characters. Proposed: 16k a reply, 2k an event, the rest in a
   file the reply names.
4. **Reusable modules go in `defaults`** (Carl's idea). Every guest
   imports a reusable module through `defaults`, and the module brings
   its own scripts. An option that must differ per guest has no default,
   so each guest sets it or evaluation fails. Open: how a guest-level
   NixOS module adds a script to the run. The run would collect it from
   each guest's evaluated configuration.
5. **Is a phase a systemd unit?** (Carl's idea.) Most work runs in the
   guests under systemd, and the host waits and checks. A unit that
   needs another guest to be ready waits for a file that the runner
   writes, not for a retry to succeed: a worker's `kubeadm join` unit
   starts only when a join token file exists, and the runner writes that
   file through the agent once the control plane is ready.

## Decided, not built yet

- **Distributed events between guests**, after Salt's event bus. A
  guest emits an event and any guest can wait for one. The runner relays
  them over the agent channel it already has, so every event is in
  `events.jsonl` and no guest needs a network or a new library. Built
  when the first test needs it.

- **Several isolated segments per guest**, for network topology tests:
  a list of networks, one interface each, as nixos-test's
  `virtualisation.vlans`. Plus one special segment where passt has an
  address and routes to the internet. Open: passt serves one guest per
  instance (from memory, not checked), so a segment that many guests
  share reaches the internet through a router guest, or each guest keeps
  a private `vec0` of its own, as now.

## Not measured

- The time to build a view with direct syscalls. From bash it cost
  about 6 ms a path (510 paths in 3.0 s).
- A GitHub runner and a stock Ubuntu builder.

## Order of work

1. Rename the project to **vivarium** (issue #18; Carl, 2026-09-30),
   everywhere: repository, CLI, MCP server, Python packages, `UML_*`
   variables and the `boot.uml.*` options. The umbrella is nixidae, and
   a vivarium is where it keeps its guests. Rewrite the README so the
   repository is easy to approach. Then move nixkube to `mkTest`.

   The names that belong to the project change. The names that belong
   to User-Mode Linux, the kernel, do not.

   | old | new |
   | --- | --- |
   | repository `user-mode-nixos`, source key | `vivarium` |
   | CLI `uml`, package `pkgs/uml`, module `uml` | `vivarium` |
   | `pkgs/uml-runner`, `uml_runner` | `pkgs/vivarium-runner`, `vivarium_runner` |
   | `pkgs/uml-eval`, `uml_eval`, `uml-eval` | `pkgs/vivarium-eval`, `vivarium_eval`, `vivarium-eval` |
   | MCP command `uml-mcp`, server `uml` | `vivarium-mcp`, `vivarium` |
   | options `boot.uml.*` | `vivarium.*` |
   | `UML_*` runner variables | `VIVARIUM_*` |
   | `uml-agent`, `uml-journal` units | `vivarium-agent`, `vivarium-journal` |
   | `system.build.umlRootImage`, `umlNixDatabase`, `umlNixRegistration`, `umlRunner`, `umlRunnerPackage` | `vivarium…` |
   | derivations `uml-session-*`, `uml-driver-*`, `uml-check-*`, `uml-test-*` | `vivarium-…` |

   Unchanged: the backend value `"uml"` and the `.uml` output,
   `pkgs/uml-kernel` and `umlKernel`, `pkgs/uml-passt-bridge` and
   `umlPasstBridge`, and the kernel's `CONFIG_UML_*` symbols.
2. The agent-experience items.
