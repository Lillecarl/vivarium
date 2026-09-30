# The runner: design

Carl and Claude write this together. It holds what is decided and what
is open, in the present tense. When a decision changes, the text changes;
the commit log holds the history. Evidence is a spike attribute
(`nix run --file spike <attr>`, gitignored) or a check in `default.nix`.
The long record up to 2026-09-30 is `history/running-anywhere.md`.

## What a test is

- **One Nix function.** `mkTest` and `mkSession` merge into one
  function. Its option and output names follow nixos-test, so a NixOS
  developer knows them already.
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

1. **The output schema:** the names of the outputs above, after
   nixos-test's.
2. **When does the quiet monitor exit:** at the first pause, or at the
   verdict?
3. **Python inside a guest:** wanted? The agent is a Python process
   already; `exec` today runs on the host.
4. **A size budget for MCP replies.** A failed nixkube case returned
   233k characters. Proposed: 16k a reply, 2k an event, the rest in a
   file the reply names.
5. **Reusable modules go in `defaults`** (Carl's idea). Every guest
   imports a reusable module through `defaults`, and the module brings
   its own scripts. An option that must differ per guest has no default,
   so each guest sets it or evaluation fails. Open: how a guest-level
   NixOS module adds a script to the run. The run would collect it from
   each guest's evaluated configuration.
6. **Is a phase a systemd unit?** (Carl's idea.) Most work runs in the
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

1. Write the entrypoint and output schema here, for review (open 1).
2. Build it in the library; move this repository's tests, then
   nixkube's.
3. The agent-experience items.
