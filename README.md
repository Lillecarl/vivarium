# user-mode-nixos

NixOS integration tests, as an alternative to `nixosTest`. A guest is an
ordinary NixOS configuration, a test is a Python coroutine over the guests,
and the machine underneath is a choice.

The default is [User-Mode Linux][uml], which compiles the kernel as an
ordinary Linux program. A guest is then a process: no KVM, no root, no tap
devices, no `/dev/net/tun`. Tests run inside a Nix build sandbox, in a
container, or on a builder with no virtualisation to offer.

The other is QEMU with KVM, which is multiprocessor and much faster, and
needs `/dev/kvm`. Same test script, same node configurations, same
host-side switch — see [Backends](#backends).

```console
$ nix build .#lan .#iperf     # run the tests
$ nix build .#lan.qemu        # the same test, as virtual machines
$ nix build .#k8s             # three guests, a kubeadm cluster (CI-sized)
$ nix run --file . lan.run    # the same test, outside the sandbox
$ nix run .#speedtest         # boot a guest and run speedtest-cli in it
$ ./run.sh --command hostname # boot the demo guest and poke at it
```

Every test answers to `.uml` and `.qemu`, so picking one needs no Nix
edit. Nothing is duplicated to make that work: one script, one set of node
configurations, and neither knows which machine it got.

## Writing a run

A run is guests plus phases. Each phase is a Python module exporting one
coroutine, and Nix says what order they go in.

```nix
mkTest {
  name = "mine";
  nodes.one = { };
  phases = {
    boot.script = ./boot.py;
    check = {
      script = ./check.py;
      after = [ "boot" ];
    };
  };
}
```

```python
from vivarium_runner import Machines

async def test(vms: Machines) -> None:
    await vms.one.succeed("systemctl is-system-running --wait")
```

Three things come out, and they are one program run three ways:

```console
$ nix build --file . mine              # the sandboxed check, what CI builds
$ nix run --file . mine.run -- --out ./out
$ nix run --file . mine.phases         # what would run, without booting
```

### What a run leaves behind

The same directory whichever door you came through — `$out` for the
check, `--out` by hand:

```
status          0 or 1
log             everything, readable, never filtered
events.jsonl    everything, one JSON object per line
console/        one file per guest, its own output
phases.json     what each phase did, and why it was skipped
junit.xml       for CI
report.json     where the time went
artifacts/      what the guests wrote to /artifacts
```

`events.jsonl` is the one worth knowing about. An event carries the
machine, the phase and the seconds as **fields**, so questions are `jq`
rather than a regex over a log:

```console
$ jq -r 'select(.kind=="rpc") | "\(.phase)\t\(.seconds)\t\(.text)"' out/events.jsonl
boot      0.147  systemctl is-system-running --wait
cluster   0.009  exit 1
```

It is appended as the run goes, so a run you killed still has everything
up to the moment it died.

### What reaches the terminal

**A guest's console does not, by default.** A failing run prints about 50
lines instead of 350, and the ones left are what the phases did.

Nothing is lost by that. Every console line is always in
`console/<guest>.log`, and **when a phase fails the last 20 lines from
every guest are printed automatically** — the context you would have gone
looking for, without going to look.

```console
$ nix run --file . mine.run -- --out ./out        # phases and what they print
$ nix run --file . mine.run -- --out ./out -v     # plus every command sent to a guest
$ nix run --file . mine.run -- --out ./out -vv    # plus the guests' consoles
$ nix run --file . mine.run -- --out ./out -q     # failures and the verdict only
```

The `log` file ignores all of that and keeps everything. `--quiet`
changes what you watch, never what you can go back to.

### `after` is a dependency, not a hint

A phase whose `after` failed is **skipped**, and a phase that depends on
nothing still runs. So one run tells you about every independent failure
rather than the first one, and it never reports a second failure about a
world that was never built.

A run holding a skip is not a pass: nobody knows what that phase would
have done.

An `after` naming a phase that does not exist is an evaluation error. A
typo there would not stop anything — the phase would simply have no
dependency, so an upstream failure would never skip it.

### Running one phase

```console
$ nix run --file . mine.run -- --out ./out --only check
```

The rest are **deselected**, which is a different thing from skipped:
nobody wanted their answers, so the run still exits 0. Only a caller can
deselect. The sandboxed check passes no `--only`, so CI cannot go green
by running a subset.

### Telling a run what to do

```nix
knobs.selection = {
  env = "UML_SELECTION";
  default = "every-case";
};
```

```console
$ UML_SELECTION=just-mounts nix run --file . mine.run -- --out ./out
```

A phase reads `vms.knobs["selection"]` and passes it where it wants —
`succeed(cmd, env = {...})` for a guest. A knob is not ambient: nothing
in a guest's environment carries it unless a phase hands it over.

Nix resolves a knob while evaluating, so it can change what is **built**
— a phase order, a guest's memory, an image — which nothing read at run
time can do. The price is that setting one moves the derivation. Inside a
sandbox `builtins.getEnv` answers `""`, which is also what an unset
variable answers, so the check always takes the declared default and an
exported variable cannot make CI run something other than the check.

Every knob, its value and where the value came from is printed before
anything boots. A misspelled variable is invisible otherwise.

### Recipes: work somebody else already wrote

A recipe is a module. It brings a phase, the guest configuration that
phase needs, and the knobs it reads — so enabling it is one line, and
overriding any part of it is an option like any other.

```nix
vivarium.recipes.boot.enable = false;           # off
phases.boot.script = ./my-own-boot.py;     # replaced
```

`boot` is on by default: it waits for every guest to reach a running
system and names the failed units when one does not. Forgetting that
wait is how a test becomes flaky.

No recipe collects the journal: every guest streams it to the host
while it runs (`vivarium.journal`, on by default), so it survives a
guest that is killed, and each entry is an event in `events.jsonl`
with its machine, unit, phase and pytest test.

### `always`: a phase that runs whatever failed

`after` normally means two things at once — run me later, and do not
bother if that failed. A phase that collects evidence wants only the
first:

```nix
phases.evidence = {
  script = ./collect.py;
  after = [ "check" ];
  always = true;
};
```

Without it, evidence ordered after everything is skipped by the very
failure it exists to explain. A failure is not passed on through such a
phase either, so a phase after it still runs.

### Running with no internet

```console
$ nix run --file . mine.run -- --out ./out --offline
```

A sandboxed check has no network, and most tests here are written for
that. Run one by hand on a connected host and the guest suddenly resolves
names and reaches a cache, so a test that would fail the check passes.

`--offline` binds passt's outbound sockets to loopback. The guest keeps
its address, its DHCP lease and the host's way in; only the way out goes.
Measured on a connected host, same session: plain reaches `1.1.1.1:53`
and resolves `example.com`, `--offline` does neither, and the forward on
`127.0.0.2` still works in both.

### Holding a failed run open

```console
$ nix run --file . mine.run -- --out ./out --hold
```

The guests stay up with the state the failure left. The evidence is
written first, so the directory is complete even though the run has not
ended.

## Writing a test

A test is a set of NixOS modules and a Python coroutine over the machines
they become. This is all of `tests/lan.py`'s counterpart in `flake.nix`:

```nix
lan = mkTest {
  name = "lan";
  script = ./tests/lan.py;
  nodes = {
    server.vivarium.lan = { network = "lan"; address = "192.168.99.2/24"; };
    client.vivarium.lan = { network = "lan"; address = "192.168.99.3/24"; };
  };
};
```

Machines naming the same `vivarium.lan.network` are wired together on
`vec1`; ssh ports and host addresses are handed out automatically. The
script gets them by name:

```python
from vivarium_runner import run_test

async def test(vms):
    await vms.server.succeed(f"ping -c2 {vms.client.ip}")
    await vms.client.wait_for_unit("sshd.service")

run_test(test)
```

`Machine` offers roughly what a `nixosTest` node does — `execute`,
`succeed`, `fail`, `wait_for_unit`, `wait_for_console_text`, `journal`,
`list_units`, `unit_state`, `unit_info`.

## How it fits together

```
        host                                  guest
  ┌──────────────┐   vec0  ┌───────┐
  │    passt     ├─────────┤       │   NAT out, forwards in
  └──────────────┘         │       │
  ┌──────────────┐   ssl0  │  UML  │
  │  run/harness ├─────────┤kernel │   arpyc on /dev/ttyS0: the agent
  └──────────────┘         │       │
  ┌──────────────┐   vec1  │       │
  │ socketpair / ├─────────┤       │   L2 to the other guests
  │ hub (net.py) │         └───┬───┘
  └──────────────┘             │ hostfs
                             /nix on the host
```

Each guest is one `uml-passt-bridge` process. It forks passt for the
uplink, then execs the UML kernel with three fds: passt on `vec0`, a
socket to its segment on `vec1`, and a socketpair to the host runner on
`ssl0`.

**Control channel.** The host drives guests over `ssl0`, not ssh, so
commands work before networking exists and inside a sandbox. `vivarium_runner.arpyc`
speaks rpyc's wire format (brine for values, vinegar for exceptions) over
asyncio, with one handler that calls a method by name — so `await
vm.succeed("...")` is a single round trip. The guest half is the
`uml-agent` systemd unit.

**Root image.** Almost empty: busybox, an `/init`, and a symlink to the
system's `init`. `/init` mounts the host's `/nix` over hostfs with a
writable overlay on top, then execs systemd. So a guest costs a sparse
512 MiB ext4 file rather than a copy of its closure, and the store is
shared between all of them.

All of `/nix`, and not `/nix/store` alone, so that `/nix` is one mount. A
bind mount is not recursive, and kubelet's volume `subPath` is a plain
bind — a pod that mounts the node's `/nix` that way would otherwise get
an empty store.

**Segments.** Two guests on a segment get the ends of one
`SOCK_SEQPACKET` socketpair and the host stays out of the data path.
Three or more get a hub in the host process that floods frames between
ports.

What a segment carries is decided by frame size, not by anything on the
host. AF_UNIX only lets about ten datagrams queue on a socket before the
sender blocks — `net.unix.max_dgram_qlen`, which a Nix sandbox's network
namespace gets at its default of 10 and cannot raise — so at a 1500-byte
MTU a guest has 15 KB in flight and no more. Guests therefore run a
65000-byte MTU by default (`vivarium.mtu`), which measures about
11 Gbit/s over `vec1` against about 4 at 1500.

## Reaching a guest from the host

A guest's uplink is `10.0.2.15/24`, with passt as the router on `10.0.2.2`
and answering DNS on `10.0.2.3` — the range QEMU's own user-mode network
has always used. Every guest gets the same address, and that is right:
each has a passt of its own, none of them shares a link, and two guests
that must reach each other do it on `vec1`.

This is set explicitly, because **passt's default is to hand the guest the
host's own address** — its real IPv4, netmask and router. That makes the
host's LAN on-link to the guest, and on a hosted machine that LAN has
other people's servers on it. IPv6 is left alone: there the guest gets an
address of its own out of the host's prefix, which is the normal way to do
it.

passt is the only way in, and **its forwards cannot be changed while it is
running**: `conf_ports()` binds every socket while parsing arguments, there
is no control socket, and the `auto` mode that watches `/proc/net/tcp` is
pasta-only — pasta shares the target namespace's `/proc`, and passt, talking
to a VM over a socket, does not. Adding a forward to a live passt means
restarting it, which drops every connection through it, including the ssh
session you were in when you started the service you wanted to reach.

So `vivarium.forward` decides them before boot, and `ports = "all"` exists
to make not deciding affordable:

```nix
vivarium.forward = [
  { ports = "all"; }                                   # the guest, privately
  { address = "0.0.0.0"; ports = [ 8080 ]; }           # and one port, publicly
];
```

`address = null` — the default — means the runner gives this guest an address
out of `127.0.0.2` upwards and keeps it for the guest's lifetime. All of
`127.0.0.0/8` is on `lo` without anyone configuring it, so that costs no
privileges and no setup, and it is what makes the collisions go away: two
guests can both serve 8080, because they are not on the same address.

`ports = "all"` is one passt spec made only of exclusions, which is what puts
passt in the mode where a port it cannot bind is skipped instead of fatal. It
comes to about 36000 sockets and 17 MB in under a second — cheap enough for a
guest you drive by hand, which is why the demo guest has it, and not cheap
enough for three guests in a test, which is why the default is the ssh port
alone.

Two things to know:

- **Privileged ports get moved, loudly.** Nothing here may bind below
  `net.ipv4.ip_unprivileged_port_start` (1024 on most hosts), so guest port
  22 is reachable on host port 10022 and the runner says so on the console
  every time. Set `remapPrivileged = false` to leave them unforwarded
  instead, or lower the sysctl on the host and the remapping stops happening.
- **A shared address collides with everything.** A rule on `0.0.0.0` fights
  every other guest's `all` rule, one port at a time. The runner probes each
  requested port before spawning passt so the error names the guest and the
  port, rather than passt exiting with a bare `Address already in use`.

`vivarium-run` with no `--command` polls the guest for what it is listening on and
prints where each port answers, so a service you start inside is followed by
the address to reach it at — or by `not forwarded`, which is the answer worth
having, since it needs a reboot to fix.

## Backends

`mkTest` takes `backend`, and a node may override `vivarium.backend` for
itself. Nothing above that line changes: the same `tests/lan.py` and the
same two node configurations run as `lan` and as `lan.qemu`.

|  | `uml` (default) | `qemu` |
| --- | --- | --- |
| needs | nothing | `/dev/kvm` |
| processors | one | `vivarium.cpus` |
| kernel | built for `ARCH=um`, all built in | the host's, with an initrd |
| the store | hostfs | virtiofs |
| default RAM | 256M | 512M |
| `vec1` carrier | `UNKNOWN` | `UP` |

The last row is not cosmetic if you write a test that waits on a link:
UML's vector driver reports no carrier, so `ip` says `UNKNOWN` on an
interface that carries traffic perfectly well. Check for the address, not
for the state.

**The host side is the same either way, and that is the point.** A segment
is a `SOCK_SEQPACKET` socketpair from `net.py`; UML takes the fd as
`vec1:transport=fd` and QEMU takes it as `dgram,local.type=fd`. Both are
plain `send` and `recv` on the fd with no framing of their own, so the two
kinds of guest could sit on one segment. The forwards are the specifiers
`forward.py` builds, unchanged.

`uml-passt-bridge` is not in the QEMU picture. It exists to add and strip
passt's 4-byte length prefix for UML's vector transport, and that prefix
*is* QEMU's socket protocol — so the runner starts passt directly and the
two talk.

Two things to know about the QEMU guest:

- **`accel=kvm`, never `accel=kvm:tcg`.** The fallback is silent and about
  ten times slower, so a builder that lost KVM would look like a slow day
  rather than a broken one. The test derivation asks the daemon for the
  `kvm` feature, so a builder without it refuses the build instead.
- **virtiofsd runs with `--no-announce-submounts`.** NixOS binds
  `/nix/store` onto itself, so `store` is a submount of the shared
  directory. Announced, the guest makes it an automount dentry, and
  overlayfs refuses one as a lower layer (`ovl_dentry_weird`). Every lookup
  under `/nix/store` then fails with `EREMOTE` — which reads as `Object is
  remote`, on a store the guest can list one directory above. The cost of
  turning it off is that the guest sees one inode number space across what
  were two host filesystems, which cannot collide while `/nix/store` is a
  bind of `/nix`.

`checks` holds the default of each test, which is UML. The `.qemu`
variants are deliberately **not** checks, until we know whether our CI
runners have `/dev/kvm`: a builder without it does not fail such a test,
it refuses to build it — which would stop CI rather than report anything.

### What the segment carries, per backend

`.#iperf` and `.#iperf.qemu` are the same two guests on the same segment,
at a 65000-byte MTU, inside the build sandbox. One run at a time,
alternating, on an idle host:

| | run 1 | run 2 |
| --- | --- | --- |
| `uml`, 1 cpu | 25.80 / 25.22 | 25.18 / 25.40 |
| `qemu`, 1 cpu | 31.33 / 30.54 | 30.72 / 30.70 |
| `qemu`, 2 cpus | 33.69 / 34.37 | — |

Gbit/s, server→client / client→server.

Two things worth keeping:

- **QEMU is about 20% faster on one processor**, before any parallelism.
  The segment is the same socketpair either way, so this is the guest's
  own cost, not the switch's.
- **A second processor helps here and hurts under UML.** `vivarium.cpus =
  2` is worth about 11% on QEMU. Under UML two vCPUs measured *slower*
  than one on this same test — the cross-CPU work costs more than the
  parallelism buys. Do not carry a conclusion from one backend to the
  other.

Compare runs only back to back. A guest is a process either way and a busy
host halves both numbers: run these two concurrently rather than one at a
time and they read 21.78 and 26.87 instead.

### Where a run spent its time

A test here is minutes of waiting and seconds of work, and which minutes is
not guessable. So every run records itself.

**A check always does**, into its own output — which is why a test's output
is a directory and not an empty file:

```console
$ nix build --file . lan --out-link result
$ jq '{total_seconds, boot_seconds, waiting_seconds}' result/report.json
{ "total_seconds": 9.106, "boot_seconds": 6.726, "waiting_seconds": 0.04 }
```

A run outside the sandbox records when `UML_TEST_REPORT` names a file:

```console
$ UML_TEST_REPORT=/tmp/run.json nix run --file . lan.run
```

Either way the file holds the per-machine boot time, every round trip to a
guest with its duration, the poll loops as single steps, the twenty slowest
steps and a per-command total. Two things to know reading it:

- **A `wait` step contains the `rpc` steps inside it.** A poll loop is many
  round trips and the sleeps between them, so the two kinds are reported
  apart and must not be added together.
- **A failing run writes one too.** The run whose timings you most want is
  the one that timed out.

Measured on `lan`, which is the cheapest test there is: 9.1 s, of which 6.7
is booting two guests. On anything small, boot *is* the test.

### Running outside the sandbox

A test can be run by hand, which is the point of a QEMU guest: it has the
host's network through passt, so a guest can reach a registry or a binary
cache, and nothing waits for CI.

Every test carries `.run`, which is that test with the sandbox taken off
and nothing to pass on a command line:

```console
$ nix run --file . iperf.run        # whatever `backend` said
$ nix run --file . iperf.qemu.run   # the same test, as machines
```

Nothing about a test belongs on a command line. The spec names the images,
the toolchain, the addresses and the ports, and Nix built every one of
them — so the invocation is a store path too, and a run by hand is the
same run the check makes. Arguments after `--` reach the script as
`vms.argv`, unparsed, which is where a test's own flags go:

```console
$ nix run --file . iperf.run -- -k mounts
```

### Telling a test what to do, without making it impure

A run by hand usually wants something the check does not: one case out of
a suite, a longer deadline, a different image tag. `mkTest` takes
`impurities`, a list of environment variable **names**:

```nix
impure = mkTest {
  name = "impure";
  script = ./tests/impure.py;
  impurities = [ "UML_TEST_IMPURITY" ];
  nodes.one = { };
};
```

```console
$ UML_TEST_IMPURITY=anything nix run --file . impure.run
```

The script reads them as `vms.env`, and passes what it chooses into a
guest command with `succeed(..., env = {...})`. Every declared name is
there; one that is unset reads as `""`, so a test branches on a value and
never on a missing key.

**Names, never values.** A value never enters the spec, so it never enters
a store path and no derivation hash moves with it. `nix build` needs no
`--impure`, and the same test under the sandbox reads an empty environment
and does whatever it does by default — which is what makes the check still
the check. That is the difference from `builtins.getEnv`, which needs an
impure evaluation and rebuilds the test for every value.

The run prints each declared name and its value before it boots anything.
A misspelled variable is invisible otherwise: the run quietly does the
whole suite instead of the one case asked for.

**A run leaves nothing behind, including when it is killed.** The guest's
disk is unlinked before QEMU starts and handed over as file descriptors,
and virtiofsd is given a socket that was unlinked as soon as it was
connected. So a `SIGKILL`, or closing the terminal, frees the disk with
the process rather than leaving a gigabyte in `/tmp`. Measured: `/tmp` is
unchanged across a full run on either backend.

The guest's `/nix/var` is its own, never the host's — see `guest.nix`. Nix
inside the guest knows the paths `vivarium.nixDatabase` covers, and nothing
else.

That set is the test's own closure, and `mkTest` works it out: the guest's
system, plus everything in the test's `settings`. So a store path handed to
a test the way a caller hands one over — an image, a program, a chart — is
valid Nix inside the guest without being named twice.

**The database is built with the image, not loaded at boot.** It sits on the
image under `/nix-state`, which is bound onto `/nix/var`, so it is in place
before pid 1 and a guest cannot come up without one. Measured at 612 ms and
256 KB for a minimal guest and 561 ms and 268 KB for a kubeadm control
plane, cached after the first build — small enough that it is on by default.
It is a dump of a closure Nix built, not a read of the host's database, so
nothing about it can be stale.

### The host's whole store, inside the guest

`vivarium.hostStore.enable` makes Nix in the guest see every path on the
host, not just the closure `vivarium.nixDatabase` registered. The guest's
`/nix` is already an overlay of the host's `/nix` under a writable layer,
which is exactly the shape Nix's `local-overlay` store wants, so this is
configuration and no new mount: the host's store below, read-only, and
`/.nix-upper/store` above.

`nix build` then works in there — with `cache.nixos.org`, with Nix's own
sandbox, and writing into the guest's own layer. `store` is the test:

```console
$ nix run --file . store.run        # and store.qemu.run
```

**Only outside the build sandbox, and it cannot be otherwise.** A sandbox
`/nix` holds `store` and nothing else, so there is no host database to
read. `uml-host-store.service` says that on the console rather than
letting Nix report a lock file it cannot open, and `store` is not in
`checks` because CI would only ever see that message.

Two measured limits worth knowing before they surprise you:

- **The guest sees the host's store as of its last WAL checkpoint.**
  `read-only=true` opens the database with SQLite's `immutable`
  parameter, which ignores the write-ahead log. A path added on the host
  seconds earlier reads as `is not valid` in the guest. Here that gap was
  100694 paths against 100697.
- **`nix path-info --all` lists the upper layer alone.** The lower store
  answers about a path you name; it cannot be enumerated through the
  overlay.

### What a guest's memory costs the host

A guest's memory is one sparse file on either backend — UML maps an
unlinked temporary file, QEMU a `memory-backend-memfd`. So
`vivarium.memory` is not what the guest costs: the host pays for the
blocks that file has allocated, which start near zero and grow towards
`memory` as the guest touches pages.

**They come back down on their own.** The guest reports the blocks it has
freed and the host punches holes in that file, so what the host pays
follows what the guest is using rather than what it has ever used.
Measured by `memory.py`, one guest at `memory = "1024M"` reading its own
closure and then dropping its page cache:

| | UML | QEMU |
| --- | --- | --- |
| at boot | 149M | 349M |
| after the read | 553M | 782M |
| after `drop_caches`, within seconds | 148M | 404M |

Nothing was asked of the guest for that last row. `vm.host_memory_kib()`
is the number, and it is the only way to see it: no figure inside the
guest can tell you what the host is still paying for.

What a test still has a lever for is the guest's own memory:

```python
await vm.drop_caches()          # free the page cache, which is most of it
await vm.shrink("256M")         # take memory away from the guest, now
await vm.grow("256M")           # and give it back, never past its mem=
```

`shrink` takes pages that are already free, so `drop_caches` comes first
and `vm.meminfo()` is how you see what moved. After reporting has run
there is little left for it to return to the host — what it still does is
squeeze the guest, which is how a test makes one run short of memory on
purpose. Measured of 256M asked for: UML gave up 256M and returned all of
it, QEMU gave up 220M and returned 165M.

**The cache the guest drops is worth less here than on a real machine.** A
miss on a store file is a host `read()` that hits the host's own page
cache — a memcpy, not disk I/O. Measured on one guest, host cache hot:
1.3-1.5 GB/s for a miss against 5.1-6.5 GB/s for a hit. A smaller
`memory` therefore costs memcpys, not seeks.

Different machinery under each backend, and a test never sees which.
QEMU gets `virtio-balloon-pci,free-page-reporting=on` and its monitor;
UML gets neither, because `virtio_uml` is a vhost-user transport with no
balloon backend to speak to, so it reports through `madvise(MADV_REMOVE)`
(a patch in `pkgs/uml-kernel`) and balloons through its management
console (`CONFIG_MCONSOLE`, `uml_dir=`, `umid=`).

Both guests are told to report at order 5, 128 KiB. The default is
`pageblock_order` — 4 MiB under UML, which has no huge pages, and 2 MiB
under QEMU — and a guest that has just dropped its page cache holds most
of its free memory in smaller pieces than that. Measured on QEMU at the
default: 254M of 552M came back, against 378M at order 5.

## Containers, and the Kubernetes test

`.#k8s` boots three guests and builds a cluster on them with `kubeadm`:
one control plane, two workers, `kubeadm init`, `kubeadm join`, then a pod
on one worker reaching a Service backed by a pod on the other. That last
step is the point — it only passes if the CNI bridge, the routes between
the nodes, kube-proxy's iptables rules and cluster DNS all work.

Then storage. `services.uml-k8s.persistentVolumes` gives a node that many
hostPath volumes and the cluster a default StorageClass named `standard`,
which is what a chart that names no class needs. The test writes from one
pod and reads from the next, because a volume that kept nothing would pass
a single-pod test.

Two things make it possible at all:

**The images have nothing in them.** Every guest already sees the host's
store over hostfs, so an image that carried its own glibc would be asking
containerd to unpack, onto a virtual disk, something the node can already
read. `modules/k8s-images.nix` builds each image as a handful of symlinks
into `/nix/store` (`includeStorePaths = false`), and `modules/k8s.nix`
mounts the store into every container through containerd's
`base_runtime_spec`.

kubeadm could do most of that itself, with `extraVolumes` or a patches
directory — but its patch targets stop short of kube-proxy, which is
applied from a manifest baked into kubeadm. That is the one that matters:
kube-proxy comes from `pkgs.kubernetes`, so letting it carry its own
closure costs 152 MiB against 14 MiB for every image here put together.
The tradeoff is that the mount is invisible in `kubectl get pod -o yaml`
— if a container cannot find `/nix/store`, look in `modules/k8s.nix`, not
at the manifest.

Only the pause image carries its closure, because containerd builds the
pod sandbox's OCI spec without consulting that file.

The catch, if you add an image: a layered image is a *gzipped* tar, so
the store paths its symlinks name are invisible to Nix. `nix-store
--query --references` on the merged tarball comes back empty, and
nothing would build etcd for a guest that only asked for the images —
the symlinks dangle, and runc reports it as `executable file not found
in $PATH`, which reads like the image was built without its binary.
`modules/k8s.nix` states the dependency Nix cannot infer, via
`system.extraDependencies`, from a list `k8s-images.nix` derives from
the image specs — so adding an image pulls its closure along. `.#containerd`
checks every entrypoint resolves, which is the cheap version of finding
out.

**Nodes know nothing about each other.** `modules/k8s.nix` describes a
node; who joins whom, which `/24` each ended up with, and the routes
between them are worked out in `tests/k8s.py`, which is the only thing
that can see all three at once.

The cluster wants about 5 GB of RAM across the three guests, so it is
built for CI rather than a laptop. Three cheaper things answer the same
questions much faster, and CI runs them first:

```console
$ nix build .#check-k8s-images  # are these the images kubeadm will want?
$ nix build .#check-k8s-config  # does kubeadm accept what we generate?
$ nix build .#containerd        # does a container run at all? (one guest)
```

`.#containerd` is the one worth knowing about. It boots a single guest and
drives CRI by hand to start one container out of an image containing
nothing but a symlink — so if the kernel is missing a namespace, or the
images did not import, or the store mount is wrong, it says which in about
a minute. The cluster test would take an hour to report the same thing as
a control plane that never became healthy.

## CI

The workflows under `.github/workflows` are generated. `ci/workflows.nix`
is the source, `nix run .#render-workflows` regenerates them, and
`.#check-workflows` fails when the two have drifted — so the YAML GitHub
runs is always what the Nix says.

```console
$ nix run .#render-workflows   # after editing ci/workflows.nix
$ nix build .#check-workflows  # what CI runs to keep you honest
```

What it costs, on a stock `ubuntu-24.04` runner with the kernel already in
the cache: about six minutes for everything, of which the cluster is four
— cold boot to a pod on one node answering another through a Service. The
kernel is the only expensive build, roughly half an hour the first time
after it changes, and cached by cachix after that.

## Layout

```
flake.nix               mkNode, mkTest, the tests and the demo guest
ci/                     the GitHub Actions workflows, as Nix
modules/default.nix     the vivarium options
modules/guest.nix       what a guest system looks like, either backend
modules/image.nix       UML: the root image, /init, and the vivarium-run wrapper
modules/qemu.nix        QEMU: the initrd, the virtiofs store, the MACs
modules/store.nix       the host's whole store as a store the guest builds into
modules/iperf3.nix      an example service module
modules/k8s.nix         a kubeadm node: containerd, kubelet, images
modules/k8s-images.nix  the images kubeadm expects, built from nixpkgs
pkgs/uml-kernel         the UML kernel, built from the guest's own source
pkgs/uml-passt-bridge   fd plumbing between UML, passt and the host
pkgs/vivarium-runner         the host runner, test harness, and guest agent
  backend.py            what to exec for a guest, per backend
  report.py             where a run spent its time
tests/                  one file per test
```

`tests/lan.py` and `tests/iperf.py` are two guests on a segment,
`tests/containerd.py` is one guest running a container, and
`tests/k8s.py` is the three-node cluster. `tests/store.py` is the one that
only runs outside the sandbox.

## Limits

- x86_64-linux only, and the guest kernel comes from the host's nixpkgs.
- Under UML: no nested virtualisation, no KVM inside a guest, no real
  block devices.
- A UML guest is single-CPU. The kernel takes `smp = true`, but UML only
  allows SMP with the seccomp userspace, and two vCPUs measured *slower*
  than one on the iperf test — the cross-CPU work costs more than the
  parallelism buys. A QEMU guest takes `vivarium.cpus`.
- `lan` and `iperf` have been run on both backends, and so has nixkube's
  own node test — a kubeadm control plane, a CSI driver and nine chaos
  scenarios — inside the sandbox and outside it. This repository's
  `containerd` and `k8s` are UML-only until someone runs them.
- Guests pick their host address by binding a port and letting go of it
  again, which only means anything while nothing else is racing. Two
  *runs* started at the same instant outside a sandbox can still land on
  the same address; within a run they cannot.

[uml]: https://docs.kernel.org/virt/uml/user_mode_linux_howto_v2.html
