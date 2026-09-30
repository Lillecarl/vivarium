# vivarium

NixOS integration tests, in the spirit of `nixosTest`. You describe some
guests as NixOS modules and write Python that checks them. vivarium boots
the guests, runs your checks and tears everything down.

vivarium is part of [nixidae](https://github.com/nixidae/nixidae). A
vivarium is where the nixidae keep their guests.

## Why use it

- **No KVM needed.** The default guest is a [User-Mode Linux][uml]
  kernel, which runs as an ordinary process. It needs no `/dev/kvm`, no
  root and no tap device, so tests run in a Nix build sandbox, in CI, or
  in a container.
- **Three backends, one test.** A guest can also be a QEMU virtual
  machine or a container. One run can mix all three on one network, and
  a test script never knows which kind it got.
- **Each guest sees only its own closure.** The runner gives each guest
  a view of the Nix store with its own paths and nothing else, in the
  sandbox and outside it. A sandboxed run and a run by hand are the same
  run.
- **You can stop a run and look inside.** Pause on a failure, keep the
  guests up, and send Python into them. An agent can do the same through
  the MCP server.
- **Every run leaves evidence.** Each run writes one event per line to
  `events.jsonl`, one log file per guest and a timing report. You query
  them with `jq`, not with a regex.
- **Your nixos-tests still run.** `fromNixosTest` takes an existing
  `nixosTest` file and runs it here.

## Requirements

- Linux on x86_64, with Nix.
- User namespaces. vivarium checks for them first and stops with a clear
  message if they are missing.
- `/dev/kvm` only for QEMU guests.
- For container guests: 65536 subordinate ids and a cgroup the runner
  can delegate. A probe names each missing piece before a guest boots.

## Try it

From a checkout of this repository:

```console
$ nix build --file . lan                      # two guests on a LAN, in the sandbox
$ nix run --file . lan.driver -- --out ./out  # the same run, by hand
$ nix run --file . lan.driverInteractive      # a Python prompt, paused before the first phase
$ nix build --file . lan.qemu                 # the same test, as QEMU machines
```

A run by hand writes its evidence to `./out`. A sandboxed run writes it
to `result/`.

## Write a test

A test is a set of guests and a set of phases. A phase is a Python file
with one coroutine. Nix orders the phases.

```nix
let
  vivarium = import (sources.vivarium + "/lib.nix") { inherit pkgs; };
in
vivarium.mkTest {
  name = "hello";

  # Every guest gets this module.
  defaults = {
    environment.systemPackages = [ pkgs.hello ];
  };

  # Each guest is a NixOS module.
  nodes.server.vivarium.lan = { network = "lan"; address = "192.168.1.1/24"; };
  nodes.client.vivarium.lan = { network = "lan"; address = "192.168.1.2/24"; };

  phases.check = {
    script = ./check.py;
    after = [ "boot" ];   # `boot` waits until every guest is up
  };
}
```

```python
# check.py
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    await vms.client.succeed("hello")
    await vms.client.succeed("ping -c 1 server")   # peers know each other by name
```

Guests on the same `network` share a LAN. Every guest has the other
guests in `/etc/hosts`, and gets the `nodes` module argument, as in
`nixosTest`. Nix type-checks each phase script with pyright before the
run.

A phase that fails skips the phases that come `after` it. A phase that
does not depend on it still runs. So one run reports every independent
failure.

## Run a test

`mkTest` returns a derivation, and the derivation has attributes. Each
attribute is one way to run the same test.

| Attribute | What it does |
| --- | --- |
| the derivation | Runs in the Nix sandbox. Fails the build if the test fails. This is what CI builds. |
| `.driver` | Runs by hand, with no sandbox. Stops at the first failure. |
| `.driverDebug` | Runs by hand, and pauses at the first failure with the guests still up. |
| `.driverInteractive` | Runs by hand, and gives you a Python prompt before the first phase. |
| `.phases` | Lists the phases in order, and boots nothing. |
| `.uml`, `.qemu`, `.container` | The same test with every guest on that backend. |
| `.extend { modules = [ ... ]; }` | The same test with more modules. |
| `.nodes`, `.config` | The evaluated guests and the evaluated test. |

`.driver` takes flags after `--`. Some useful ones:

```console
$ nix run --file . lan.driver -- --out ./out --only check   # run one phase
$ nix run --file . lan.driver -- --out ./out --break check  # pause before a phase
$ nix run --file . lan.driver -- --out ./out --offline      # no internet, as in the sandbox
$ nix run --file . lan.driver -- --out ./out -v             # show every command sent to a guest
```

A run always cleans up, even when you kill it. SIGTERM powers the guests
off first. `--keep` leaves the run's temporary directory for you to look
at.

## Run a nixos-test

```nix
vivarium.fromNixosTest (pkgs.path + "/nixos/tests/simple-vm.nix")
```

The result is an ordinary `mkTest` run, with all the attributes above.
`testScript` runs as one phase, through a shim that gives it the
`nixosTest` API: `start_all`, `machine.succeed`, `wait_for_unit`,
`subtest` and the rest. OCR and screenshots are not available.
`nixos-tests` in `default.nix` runs four tests from nixpkgs this way.

## What a run leaves behind

```
status          0 or 1
log             everything, readable
events.jsonl    everything, one JSON object per line
console/        one file per guest
phases.json     what each phase did, and why a phase was skipped
junit.xml       for CI
report.json     where the time went
artifacts/      what the guests wrote to /artifacts
```

An event carries its machine, phase and duration as fields:

```console
$ jq -r 'select(.kind=="rpc") | "\(.phase)\t\(.seconds)\t\(.text)"' out/events.jsonl
```

## Backends

| | `uml` (default) | `qemu` | `container` |
| --- | --- | --- | --- |
| needs | user namespaces | `/dev/kvm` | subordinate ids, a cgroup |
| processors | one | `vivarium.cpus` | the host's |
| kernel | its own, built for `ARCH=um` | its own | the host's |

A guest chooses with `vivarium.backend`, and `mkTest`'s `backend` sets
the default for every guest.

## For agents

`vivarium-mcp` is an MCP server. `.mcp.json` registers it as `vivarium`.
It starts runs, pauses them on failure, and runs Python against the live
guests. AGENTS.md describes the tools and the pause-and-inspect loop.

## Repository layout

```
lib.nix              mkTest, mkNode, fromNixosTest: what a consumer imports
nixos-test.nix       the nixos-test mapper
default.nix          this repository's own tests
modules/             the guest modules: vivarium options, backends, k8s
pkgs/vivarium        the `vivarium` CLI: runs, phases, pauses, cleanup
pkgs/vivarium-runner guests, backends, networking, the guest agent
pkgs/vivarium-eval   evaluate-and-run by name, and the MCP server
pkgs/uml-kernel      the User-Mode Linux kernel
tests/               the phase scripts for the tests in default.nix
ci/                  the GitHub Actions workflows, written in Nix
```

## Read more

- [docs/reference.md](docs/reference.md): how a run behaves, the
  networking, the backends, memory, and the Kubernetes test, with
  measurements.
- [docs/design/runner.md](docs/design/runner.md): the design, what is
  decided and what is still open.
- [AGENTS.md](AGENTS.md): how to work in this repository.

[uml]: https://docs.kernel.org/virt/uml/user_mode_linux_howto_v2.html
