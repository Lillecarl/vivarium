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
- **Each guest sees only its own closure** in `/nix/store`: a store view
  of read-only binds, in a user and mount namespace, in every run.
  Without it a guest sees the host's whole store (`uplink.counted`:
  92699 entries; `uplink.countedView`: 510, and a path outside the
  closure is hidden).
- **Guests talk to each other on `vec1`**: socketpairs, and a hub in the
  runner for three or more. It needs no namespace and no passt
  (`lan.stubBlocked`).
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

1. **One store view per guest under UML.** QEMU already takes a store
   directory per guest (virtiofsd's `--shared-dir`). A UML guest's init
   mounts the host's `/nix` by that name (`modules/image.nix`), so each
   UML process needs its own mount namespace, or the image learns another
   path. Next spike.
2. **The output schema:** the names of the outputs above, after
   nixos-test's.
3. **When does the quiet monitor exit:** at the first pause, or at the
   verdict?
4. **Python inside a guest:** wanted? The agent is a Python process
   already; `exec` today runs on the host.
5. **A size budget for MCP replies.** A failed nixkube case returned
   233k characters. Proposed: 16k a reply, 2k an event, the rest in a
   file the reply names.
6. **Reusable modules go in `defaults`** (Carl's idea). Every guest
   imports a reusable module through `defaults`, and the module brings
   its own scripts. An option that must differ per guest has no default,
   so each guest sets it or evaluation fails. Open: how a guest-level
   NixOS module adds a script to the run. The run would collect it from
   each guest's evaluated configuration.
7. **Is a phase a systemd unit?** (Carl's idea.) Most work runs in the
   guests under systemd. A control plane runs `kubeadm init` as soon as
   it boots; a worker's unit waits until the control plane's unit has
   passed. The host then waits and checks, and does not drive. Guests
   must then ask each other about state: over a socket every guest can
   reach, or through the runner, which already talks to each agent.
   Conflict to settle: `modules/k8s.nix` opens with the rule that a node
   knows only about itself, and the test handles what needs the other
   nodes, so that "the same three lines describe a one-node cluster or a
   five-node one". A worker that waits on a named control plane is a
   reference to a peer, which that rule forbids.

## Not measured

- A whole run with one store view per guest.
- QEMU with virtiofsd serving a view. Only its start is measured.
- Binding a view with direct syscalls. From bash it costs about 6 ms a
  path (510 paths in 3.0 s).
- A GitHub runner and a stock Ubuntu builder.

## Order of work

1. Spike one store view per guest (open 1).
2. Write the entrypoint and output schema here, for review (open 2).
3. Build it in the library; move this repository's tests, then
   nixkube's.
4. The agent-experience items.
