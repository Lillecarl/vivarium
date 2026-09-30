# Everything this repository builds.
#
# This is the way in, and `flake.nix` is a second door that calls it.
#
#     nix build --file . lan          # two guests on a segment
#     nix build --file . containerd   # one guest running a container
#     nix build --file . k8s          # three guests, a kubeadm cluster
#
# **One dependency, and it is nixpkgs.** A guest is an ordinary NixOS
# configuration and a kernel built from the host's own package set; nothing
# here needs anything else. So this takes a package set and nothing else, and
# a caller that has one -- a flake, another repository, an umbrella that holds
# this one as a checkout -- passes it in.
#
# A caller with none gets the umbrella's, the way every other project in the
# umbrella does: `nix/sources.nix` asks nixidae, inside or outside. It used to
# be `<nixpkgs>`, which meant `nix build --file .` built against whatever the
# machine's NIX_PATH happened to hold -- nothing on a CI runner, and something
# other than the umbrella's pin on a developer's. Store paths then agreed with
# nobody, so no cache could serve them.
#
# `mkNode` and `mkTest` come out of `lib.nix` and are re-exported here, so a
# caller with its own guests and its own script needs nothing else.
{
  sources ? import ./nix/sources.nix,
  pkgs ? import sources.nixpkgs { },
}:
let
  inherit (pkgs) lib;

  uml = import ./lib.nix { inherit pkgs lib; };
  inherit (uml)
    mkNode
    mkTest
    fromNixosTest
    runner
    session
    typeCheck
    ;

  # Two guests on one segment, addressed statically.
  pair = network: {
    server = {
      boot.uml.lan = {
        inherit network;
        address = "192.168.99.2/24";
      };
    };
    client = {
      boot.uml.lan = {
        inherit network;
        address = "192.168.99.3/24";
      };
    };
  };

  # The pair above, each running an iperf3 server. Shared by `iperf` and
  # `iperf-qemu`, so the two measure the same guests on the same segment
  # and only the machine underneath differs.
  iperfNodes = lib.mapAttrs (_: node: {
    imports = [
      node
      ./modules/iperf3.nix
    ];
    services.iperf3-server.enable = true;
  }) (pair "lan");

  # `containerd`'s questions with kata as the only extra handler. The node
  # holds kata's VM too, 2 GiB by kata's default; guest RAM is sparse, so
  # the ceiling costs nothing until touched.
  kataTest =
    cri:
    mkTest {
      name = "kata-${cri}";
      backend = "qemu";
      phases.test = {
        script = ./tests/containerd.py;
        after = [ "boot" ];
      };
      nodes.node = {
        imports = [ ./modules/k8s.nix ];
        services.uml-k8s = {
          enable = true;
          role = "worker";
          inherit cri;
          runtimes = [ "kata" ];
        };
        boot.uml = {
          nestedVirtualization = true;
          memory = "4096M";
          diskSize = 2048;
          lan = {
            network = "kata";
            address = "10.106.0.1/24";
          };
        };
      };
      settings = {
        inherit (k8sImages) sandboxImage entrypoints;
        kubernetesVersion = pkgs.kubernetes.version;
      };
    };

  /*
    Three guests running kubeadm: one control plane, two workers.

    The control plane carries etcd and the API server and needs the
    memory to prove it; the workers only run kubelet, kube-proxy and
    whatever the test schedules.  Everything else about the cluster
    -- who joins whom, which pod subnet each node got -- is worked
    out at run time in tests/k8s.py, because only the test can see
    all three at once.
  */
  k8sNodes =
    let
      node = index: role: {
        imports = [ ./modules/k8s.nix ];
        services.uml-k8s = {
          enable = true;
          inherit role;
          # One each, so the claim tests/k8s.py makes has somewhere to
          # land whichever worker the scheduler picks.  The control plane
          # is tainted and gets one anyway: a taint is not a guarantee.
          persistentVolumes = 1;
        };
        boot.uml = {
          memory = if role == "control-plane" then "2560M" else "1280M";
          # The images are symlinks into the host's store, so this
          # only has to hold containerd's state, the kubelet's, and
          # the logs -- not a copy of Kubernetes.
          diskSize = 2048;
          lan = {
            network = "k8s";
            address = "10.100.0.${toString index}/24";
          };
        };
      };
    in
    {
      cp = node 1 "control-plane";
      worker1 = node 2 "worker";
      worker2 = node 3 "worker";
    };

  # Where the tests read the cluster's settings from, rather than
  # repeating subnets and image names in Python.
  k8sConfig = (mkNode k8sNodes.cp).config;
  k8sImages = pkgs.callPackage ./modules/k8s-images.nix { };

  # The GitHub Actions workflows, and the check that the generated
  # YAML in .github is still what they render to.
  ci = pkgs.callPackage ./ci { inherit sources; };

  demo = mkNode {
    boot.uml.memory = "512M";
    # A guest you drive by hand rather than from a test: give it a
    # host address to itself with everything on it forwarded, so
    # whatever you start in there is reachable without having said
    # so in advance.  Tests keep the narrow default; three guests
    # holding 36000 sockets each is not what a builder is for.
    boot.uml.forward = [ { ports = "all"; } ];
    environment.systemPackages = [ pkgs.speedtest-cli ];
  };

  /*
    Can Nix inside a guest use the host's whole store?

    Deliberately not in `tests`, so it is neither a check nor built by
    CI: the lower layer of the guest's store is the host's Nix database,
    and a build sandbox has no `/nix/var` in it at all.  Run it by hand
    -- tests/store.py says how at its head.
  */
  store = mkTest {
    name = "store";
    phases.test = {
      script = ./tests/store.py;
      after = [ "boot" ];
    };
    # A path handed to the test the way a caller hands one over, and
    # nothing else names it. The session registers it with the guest, which
    # is the half of the guest's store that does not come from the host's
    # database -- and cannot, because a path this fresh is still in the
    # host's write-ahead log.
    settings.probe = "${pkgs.runCommand "uml-store-probe" { } "echo settings > $out"}";
    nodes.node =
      { config, ... }:
      {
        boot.uml = {
          hostStore.enable = true;
          memory = "1024M";
        };
        environment.systemPackages = [ config.nix.package ];
      };
  };

  /*
    A cluster the way a real one comes up: images pulled, nothing patched.

    `k8s` builds every image from nixpkgs and imports it, which is what
    makes a cluster possible inside a build sandbox -- and what makes the
    node unlike other nodes, because the store has to be mounted into
    every container that runs one of those images. Anything whose job is
    to put a store into a pod passes there with its subject switched off.

    So this one turns all of it off. Not in `tests`, for the same reason
    `store` is not: it pulls from registry.k8s.io, and a build sandbox has
    no network.

    10.104, and not the 10.100 `k8s` uses: unsandboxed, a guest routes for
    real, and this host has a WireGuard interface on 10.100.0.1/24.
  */
  k8s-pull = mkTest {
    name = "k8s-pull";
    backend = "qemu";
    phases.test = {
      script = ./tests/pull.py;
      after = [ "boot" ];
    };
    nodes.cp = {
      imports = [ ./modules/k8s.nix ];
      services.uml-k8s = {
        enable = true;
        role = "control-plane";
        images = "pull";
      };
      boot.uml = {
        memory = "4096M";
        diskSize = 8192;
        cpus = 4;
        lan = {
          network = "k8s-pull";
          address = "10.104.0.1/24";
        };
      };
    };
  };

  /*
    Does `--offline` give a guest the network a sandbox gives it?

    Deliberately not in `tests`, and it cannot be: a sandboxed run has no
    network whatever the flag says, so the check would pass without the
    flag doing anything. The only honest proof is by hand, on a connected
    host, comparing the two:

        nix run --file . uplink.driver -- --out ./out
        nix run --file . uplink.driver -- --out ./out --offline

    Measured on 2026-09-23, this host, UML backend:

        plain      tcp reachable, dns answered
        --offline  tcp no route,  dns no answer

    `--offline` binds passt's outbound sockets to loopback rather than
    leaving passt out. The guest keeps its address, its DHCP lease and
    the host's way in; only the way out goes. Leaving passt out would
    take vec0 with it, so a test reaching an API server through a forward
    would fail for a reason that is not the one being reproduced.
  */
  uplink = mkTest {
    name = "uplink";
    nodes.one = { };
    phases = {
      boot.script = ./tests/phases/boot.py;
      uplink = {
        script = ./tests/phases/uplink.py;
        after = [ "boot" ];
      };
    };
  };

  /*
    Does a guest's incremental write reach the host incrementally?

    The journal stream rests on this answer. Measured on both backends
    (2026-09-24): the host sees each line the moment the guest writes
    it, `(7,7) (14,14) ... (42,42)` guest/host bytes, and `sync`
    changes nothing. hostfs and virtiofs are both write-through here.

    By hand:

        nix run --file . incr.driver -- --out ./out
  */
  incr = mkTest {
    name = "incr";
    nodes.one = { };
    phases.incr = {
      script = ./tests/phases/incr.py;
      after = [ "boot" ];
    };
  };

  tests = {
    # Do the guests boot, see each other on vec1, and answer the host?
    lan = mkTest {
      name = "lan";
      phases.test = {
        script = ./tests/lan.py;
        after = [ "boot" ];
      };
      nodes = pair "lan";
    };

    /*
      Can the host reach a service in a guest?

      The only test that connects inwards.  One guest with every
      port forwarded, and a web server started long after passt
      stopped accepting arguments -- which is the case that cannot
      be checked any other way, since passt's forwards are fixed for
      its lifetime.
    */
    forward = mkTest {
      name = "forward";
      phases.test = {
        script = ./tests/forward.py;
        after = [ "boot" ];
      };
      nodes.node = {
        boot.uml.forward = [ { ports = "all"; } ];
        environment.systemPackages = [ pkgs.python3 ];
      };
    };

    /*
      Does what a guest writes reach the host, and who is left running?

      Two guests, because each one must get its own directory.
      `pkgs.util-linux` for `mountpoint`.
    */
    artifacts = mkTest {
      name = "artifacts";
      phases.test = {
        script = ./tests/artifacts.py;
        after = [ "boot" ];
      };
      nodes = lib.genAttrs [ "one" "two" ] (_: {
        environment.systemPackages = [ pkgs.util-linux ];
      });
    };

    /*
      Does a failure skip what depends on it, and nothing else?

      The claim the whole phase design rests on, against a booted guest.
      Four phases: `boot` passes, `cluster` fails on purpose, `check`
      needs `cluster` and must be skipped, `independent` needs only
      `boot` and must still run.

      Both neighbours get this wrong, which is why it is worth a test.
      nixpkgs' driver re-raises out of `subtest`, so `independent` would
      never run and a real bug in it would stay invisible behind an
      unrelated failure. pytest would run `check` anyway, against a
      cluster that was never built, and report a second failure that
      says nothing.

      The session derivation *fails* when a phase fails -- which is
      correct -- so this reads `phases.json` from the attempt instead.
    */
    phase-rules =
      let
        run = mkTest {
          name = "phase-rules";
          nodes.one = { };
          phases = {
            boot.script = ./tests/phases/boot.py;
            cluster = {
              script = ./tests/phases/cluster.py;
              after = [ "boot" ];
            };
            check = {
              script = ./tests/phases/check.py;
              after = [ "cluster" ];
            };
            independent = {
              script = ./tests/phases/independent.py;
              after = [ "boot" ];
            };
          };
        };
      in
      pkgs.runCommand "uml-check-phase-rules"
        {
          nativeBuildInputs = [ pkgs.jq ];
          passthru.session = run;
        }
        ''
          report=${run.attempt}/phases.json
          echo "--- $report ---"
          cat "$report"

          want() {
            got=$(jq -r --arg n "$1" '.phases[] | select(.name == $n) | .state' "$report")
            if [ "$got" != "$2" ]; then
              echo "phase $1 is '$got', expected '$2'" >&2
              exit 1
            fi
            echo "ok: $1 is $2"
          }

          want boot passed
          want cluster failed
          # The rule. Skipped, not failed: nothing ran it.
          want check skipped
          # The other half of the rule, and the one nixpkgs cannot do.
          want independent passed

          if [ "$(jq -r '.passed' "$report")" != "false" ]; then
            echo "a run holding a failure and a skip reported itself passed" >&2
            exit 1
          fi
          echo "ok: the run failed, as a run with unanswered phases must"

          touch $out
        '';

    /*
      What is a run told from outside, and what is a check told instead?

      A knob is resolved while evaluating, so it can change what is built
      -- a phase order, a guest's memory, an image -- which nothing read
      at run time can do. The price is that setting one moves the
      derivation, and that is the trade this records rather than hides.

      The property under test is the half that keeps CI honest: inside a
      sandbox a knob always carries its declared default, because
      `builtins.getEnv` answers "" under a pure evaluation and an unset
      variable answers "" too. So an exported variable cannot make the
      check run something other than the check.

      Also asserts a knob is not ambient. Nothing in the guest's
      environment carries it; a phase hands it over or the guest never
      sees it, which keeps a guest's behaviour a function of its own
      configuration.
    */
    knobs = mkTest {
      name = "knobs";
      nodes.one = { };
      knobs.selection = {
        env = "UML_SELECTION";
        default = "every-case";
        description = "Which cases to run; the default is all of them.";
      };
      phases = {
        boot.script = ./tests/phases/boot.py;
        knob = {
          script = ./tests/phases/knob.py;
          after = [ "boot" ];
        };
      };
    };

    /*
      Can a caller run one phase and leave the rest alone?

      `--only` is the fast door: boot once, do the one thing being worked
      on, and exit 0 when it passed. A developer who asked for one phase
      knows the rest did not run.

      The safety property is that only a *caller* can do this. The check
      passes no `--only`, so CI cannot go green by running a subset --
      and the session below proves it, because one of its phases fails on
      purpose and building it whole fails.

      `deselected` is therefore a different state from `skipped`. Skipped
      means nobody knows the answer and the run failed; deselected means
      nobody wanted it.
    */
    only-rules =
      let
        run = mkTest {
          name = "only";
          nodes.one = { };
          phases = {
            boot.script = ./tests/phases/boot.py;
            only = {
              script = ./tests/phases/only.py;
              after = [ "boot" ];
            };
            # Fails if it ever runs, which is the point: `--only` must
            # not reach it, and building this session whole must fail.
            never = {
              script = ./tests/phases/check.py;
              after = [ "boot" ];
            };
          };
        };
      in
      pkgs.runCommand "uml-check-only"
        {
          nativeBuildInputs = [ pkgs.jq ];
          passthru.session = run;
        }
        ''
          export HOME="$TMPDIR"
          out_dir="$TMPDIR/run"
          ${lib.getExe run.driver} --out "$out_dir" --only only
          echo "--- phases.json ---"
          cat "$out_dir/phases.json"

          want() {
            got=$(jq -r --arg n "$1" '.phases[] | select(.name == $n) | .state' \
              "$out_dir/phases.json")
            if [ "$got" != "$2" ]; then
              echo "phase $1 is '$got', expected '$2'" >&2
              exit 1
            fi
            echo "ok: $1 is $2"
          }

          want only passed
          want boot deselected
          want never deselected

          if [ "$(jq -r '.passed' "$out_dir/phases.json")" != "true" ]; then
            echo "asking for one phase by name reported failure" >&2
            exit 1
          fi
          echo "ok: a deselected phase does not fail the run"

          # And the guest really did the work, rather than the phase
          # being counted without running.
          test -f "$out_dir/artifacts/only-ran" \
            || { echo "the phase was counted but never ran" >&2; exit 1; }
          echo "ok: and it left its evidence in the artifacts"

          touch $out
        '';

    /*
      Does evidence get collected on the run that went wrong?

      `boot` is the standard library's recipe, which nothing here
      declares. `evidence` is ordered after a phase that fails on
      purpose, and `after` alone would skip it for exactly that reason.
      `always` is what separates "run me later" from "do not bother if
      that failed".

      Also checks the failure does not pass *through* `evidence`: `later`
      needs only `evidence`, so it still runs.
    */
    recipes =
      let
        run = mkTest {
          name = "recipes";
          nodes.one = { };
          phases = {
            cluster = {
              script = ./tests/phases/cluster.py;
              after = [ "boot" ];
            };
            evidence = {
              script = ./tests/phases/evidence.py;
              after = [ "cluster" ];
              always = true;
            };
            later = {
              script = ./tests/phases/independent.py;
              after = [ "evidence" ];
            };
          };
        };
      in
      pkgs.runCommand "uml-check-recipes"
        {
          nativeBuildInputs = [ pkgs.jq ];
          passthru.session = run;
        }
        ''
          report=${run.attempt}/phases.json
          echo "--- $report ---"
          cat "$report"

          want() {
            got=$(jq -r --arg n "$1" '.phases[] | select(.name == $n) | .state' "$report")
            if [ "$got" != "$2" ]; then
              echo "phase $1 is '$got', expected '$2'" >&2
              exit 1
            fi
            echo "ok: $1 is $2"
          }

          # The recipe's own phase, which nothing in this session declared.
          want boot passed
          want cluster failed
          # The rule `always` exists for.
          want evidence passed
          echo "ok: evidence ran although the phase before it failed"
          test -f ${run.attempt}/artifacts/one/failed-units \
            || { echo "evidence passed and left nothing" >&2; exit 1; }
          # And the failure did not travel through it.
          want later passed

          # It must still be a failed run: evidence is not an answer to
          # the question the failed phase was asked.
          if [ "$(jq -r '.passed' "$report")" != "false" ]; then
            echo "collecting evidence turned a failure into a pass" >&2
            exit 1
          fi
          echo "ok: and the run still failed"

          touch $out
        '';

    /*
      Does a guest's journal survive the guest?

      A unit logs a line, the phase waits until the line is in the
      host-side file while the guest still runs, and then kills the guest
      with SIGKILL -- no shutdown, nothing flushed. The line must be in
      `events.jsonl` afterwards as an event that carries the machine, the
      unit and the phase. That is the question an agent asks with `jq`,
      and the failure it has to answer for is the spectacular kind.
    */
    stream =
      let
        run = mkTest {
          name = "stream";
          nodes.one = { };
          phases.crash = {
            script = ./tests/phases/crash.py;
            after = [ "boot" ];
          };
        };
      in
      pkgs.runCommand "uml-check-stream"
        {
          nativeBuildInputs = [ pkgs.jq ];
          passthru.session = run;
        }
        ''
          events=${run.attempt}/events.jsonl
          jq -r '.phases[] | "\(.name)\t\(.state)"' ${run.attempt}/phases.json

          found=$(jq -c 'select(.kind == "journal"
                                and .machine == "one"
                                and .data.unit == "probe.service"
                                and .text == "streamed-before-the-crash")' "$events")
          if [ -z "$found" ]; then
            echo "the line is not in events.jsonl as a journal event from probe.service" >&2
            jq -c 'select(.kind == "journal")' "$events" | tail -20 >&2
            exit 1
          fi
          echo "ok: $found"

          if [ "$(echo "$found" | jq -r .phase)" != "crash" ]; then
            echo "the entry is not attributed to the phase that caused it" >&2
            exit 1
          fi
          echo "ok: attributed to the phase that logged it"

          grep -q streamed-before-the-crash ${run.attempt}/artifacts/one/journal.jsonl \
            || { echo "the raw journal on the host lost the line" >&2; exit 1; }
          echo "ok: and the raw stream is in the artifacts"

          # A guest that shut down cleanly would have had time to flush,
          # and then this check proves nothing about a crash.
          if grep -qE 'Reached target.*Power-Off|reboot: ' ${run.attempt}/console/one.log; then
            echo "the guest shut down cleanly; crash() did not kill it" >&2
            exit 1
          fi
          if jq -e 'select(.kind == "rpc" and .text == "systemctl poweroff")' "$events" > /dev/null; then
            echo "teardown asked a dead guest to power off" >&2
            exit 1
          fi
          echo "ok: and the guest died without a shutdown"

          n=$(jq -s 'map(select(.kind == "journal")) | length' "$events")
          echo "ok: $n journal entries streamed in all"

          touch $out
        '';

    /*
      Is a pytest phase pytest, against real guests?

      `tests/cases` uses what a test author reaches for: a guest as a
      fixture, an async fixture with a teardown, parametrize, a skip, and
      one assertion that fails on purpose. The phase fails, so this reads
      the attempt.

      The claims: each test is a JUnit case of its own, the failure
      message is pytest's rewritten assertion, the fixture's teardown ran
      on the guest, and a journal entry and a command both name the test
      that caused them.
    */
    pytest-phase =
      let
        run = mkTest {
          name = "pytest";
          nodes.one = { };
          phases.cases = {
            pytest.tests = ./tests/cases;
            after = [ "boot" ];
          };
        };
      in
      pkgs.runCommand "uml-check-pytest-phase"
        {
          nativeBuildInputs = [
            pkgs.jq
            pkgs.libxml2
          ];
          passthru.session = run;
        }
        ''
          a=${run.attempt}
          jq -r '.phases[] | "\(.name)\t\(.state)"' $a/phases.json
          fail() { echo "$*" >&2; exit 1; }

          [ "$(jq -r '.phases[] | select(.name == "cases") | .state' $a/phases.json)" = failed ] \
            || fail "a phase with a failing test did not fail"
          echo "ok: the failing test failed the phase"

          n=$(xmllint --xpath 'count(//testcase[@classname="pytest.cases"])' $a/junit.xml)
          [ "$n" = 8 ] || fail "junit has $n cases under pytest.cases, expected 8"
          echo "ok: 8 JUnit cases, one per test"

          xmllint --xpath 'string(//testcase[contains(@name,"test_fails_on_purpose")]/failure/@message)' \
            $a/junit.xml | tee message
          grep -q "assert '2' == '3'" message || fail "the failure is not pytest's rewritten assertion"
          echo "ok: the failure message is the rewritten assertion"

          test -f $a/artifacts/one/fixture-teardown || fail "the async fixture's teardown never ran"
          echo "ok: the fixture's teardown ran on the guest"

          case=$(jq -r 'select(.kind == "journal" and .text == "from-a-test") | .data.case' $a/events.jsonl)
          case "$case" in
            *::test_the_journal_names_the_test) echo "ok: the journal entry names $case" ;;
            *) fail "the journal entry names '$case'" ;;
          esac

          jq -e 'select(.kind == "rpc" and .text == "hostname" and (.data.case | endswith("::test_hostname")))' \
            $a/events.jsonl > /dev/null || fail "the command does not name its test"
          echo "ok: and so does the command"

          # Logged by a test that returned at once. Without the phase's
          # settle it was lost to the teardown -- measured, before settle.
          jq -e 'select(.kind == "journal" and .text == "logged-and-left" and .phase == "cases")' \
            $a/events.jsonl > /dev/null || fail "a line logged as a test returned was lost"
          echo "ok: a line logged on the way out still reached the phase"

          if jq -e 'select(.kind == "journal" and .data.identifier == "uml-settle")' $a/events.jsonl > /dev/null; then
            fail "the settle marker leaked into the events"
          fi
          echo "ok: and the runner's own marker stayed out of them"

          touch $out
        '';

    /*
      One guest as a rootless container under crun (Area 8 of the design).

      `nix build --file . container` needs a daemon with the `uid-range`
      feature; CI's `test-container` gets it from ghanix's
      `nix.install.uidRange`. There the guest has no uplink (no
      /dev/net/tun) and a read-only store, and the phase says so. By hand, `uml-eval run container`, it has both;
      the host needs subordinate ids and a cgroup it can delegate, and
      the runner says which is missing.
    */
    container = mkTest {
      name = "container";
      nodes.one.boot.uml.backend = "container";
      phases.check = {
        script = ./tests/phases/container.py;
        after = [ "boot" ];
      };
    };

    # Can this sandbox run a container guest? Seconds, and every missing
    # piece named with its fix. `-tun` also asks for a tap device, which a
    # LAN needs: /dev/net in `extra-sandbox-paths`.
    container-probe = uml.containerProbe { };
    container-probe-tun = uml.containerProbe { tun = true; };

    # Two containers and a UML guest on one segment. By hand, as above.
    container-lan = mkTest {
      name = "container-lan";
      nodes = lib.mapAttrs (name: value: {
        boot.uml = {
          backend = if name == "u" then "uml" else "container";
          lan = {
            network = "clan";
            address = "${value}/24";
          };
        };
      }) {
        a = "10.56.0.1";
        b = "10.56.0.2";
        u = "10.56.0.3";
      };
      phases.reach = {
        script = ./tests/phases/container-lan.py;
        after = [ "boot" ];
      };
    };

    /*
      One run, both kinds of guest: a UML guest and a QEMU guest on one
      segment. UML for what is single-threaded and wants to cost the host
      little, QEMU for what wants the CPU. A node sets its own
      `boot.uml.backend`; the run's `backend` is only the default.

      By hand: the QEMU guest needs /dev/kvm, which the session job in CI
      does not have.
    */
    mixed = mkTest {
      name = "mixed";
      nodes.small.boot.uml = {
        backend = "uml";
        lan = {
          network = "mixed";
          address = "10.55.0.1/24";
        };
      };
      nodes.fast.boot.uml = {
        backend = "qemu";
        lan = {
          network = "mixed";
          address = "10.55.0.2/24";
        };
      };
      phases.reach = {
        script = ./tests/phases/mixed.py;
        after = [ "boot" ];
      };
    };

    /*
      Can a QEMU guest run virtual machines of its own?

      By hand, not in CI: a GitHub runner is itself a VM, and its KVM
      does not nest a second time. Needs nesting on this host
      (`/sys/module/kvm_{intel,amd}/parameters/nested`).
    */
    nested = mkTest {
      name = "nested";
      backend = "qemu";
      nodes.nested = {
        boot.uml.nestedVirtualization = true;
        environment.systemPackages = [ pkgs.python3 ];
      };
      nodes.plain = { };
      phases.kvm = {
        script = ./tests/phases/nested.py;
        after = [ "boot" ];
      };
    };

    /*
      Do phases on disjoint guests run at once, and is everything they
      say still filed under the right phase?

      `left` holds `a`, `right` holds `b`, and `both` holds every guest
      and comes after them. Three claims, each read from events.jsonl:

      - `left` and `right` overlap in time.
      - `both` overlaps nothing: a phase without `nodes` runs alone. This
        is the negative control, and what a scheduler that ignored
        `nodes` would fail.
      - Every journal marker and every print carries the phase that held
        its guest, although two phases were writing at once.
    */
    parallel =
      let
        run = mkTest {
          name = "parallel";
          nodes.a = { };
          nodes.b = { };
          phases = {
            left = {
              script = ./tests/phases/parallel.py;
              nodes = [ "a" ];
              after = [ "boot" ];
            };
            right = {
              script = ./tests/phases/parallel.py;
              nodes = [ "b" ];
              after = [ "boot" ];
            };
            both = {
              script = ./tests/phases/parallel.py;
              after = [
                "left"
                "right"
              ];
            };
          };
        };
      in
      pkgs.runCommand "uml-check-parallel"
        {
          nativeBuildInputs = [ pkgs.jq ];
          passthru.session = run;
        }
        ''
          a=${run.attempt}
          fail() { echo "$*" >&2; exit 1; }
          jq -r '.phases[] | "\(.name)\t\(.state)"' $a/phases.json
          [ "$(cat $a/status)" = 0 ] || fail "the run failed"

          jq -s '[.[] | select(.kind == "phase_started" or .kind == "phase_finished")]
                 | group_by(.phase)
                 | map({(.[0].phase): {
                     start: (map(select(.kind == "phase_started"))[0].at),
                     end: (map(select(.kind == "phase_finished"))[0].at)}})
                 | add' $a/events.jsonl | tee spans.json

          jq -e '.left.start < .right.end and .right.start < .left.end' spans.json > /dev/null \
            || fail "left and right did not overlap"
          echo "ok: left and right ran at once"

          jq -e '.both.start >= ([.left.end, .right.end] | max)' spans.json > /dev/null \
            || fail "both started while left or right still ran"
          echo "ok: and both, which holds every guest, ran alone"

          jq -c 'select(.kind == "journal" and .data.identifier == "parallel")
                 | {machine, phase, text}' $a/events.jsonl | tee markers
          [ "$(wc -l < markers)" = 12 ] || fail "expected 12 markers: 3 on a, 3 on b, 6 from both"
          jq -e -s 'all(.[]; (.text | split("-")[0]) == .phase)' markers > /dev/null \
            || fail "a journal marker was filed under a phase that did not write it"
          echo "ok: every journal marker names the phase that held its guest"

          jq -c 'select(.kind == "output" and (.text | test(" step "))) | {phase, text}' \
            $a/events.jsonl | tee said
          [ "$(wc -l < said)" = 9 ] || fail "expected 9 printed lines"
          jq -e -s 'all(.[]; (.text | split(" ")[0]) == .phase)' said > /dev/null \
            || fail "a print was filed under the other phase"
          echo "ok: and so does every print"

          touch $out
        '';

    /*
      Does every guest import `defaults`, and know each peer by name?

      No guest names another in its own configuration: /etc/hosts comes
      from the `nodes` every guest receives, as in nixos-test.
    */
    peers = mkTest {
      name = "peers";
      defaults.environment.etc."uml-defaults".text = "from-defaults\n";
      nodes.server.boot.uml.lan = {
        network = "peers";
        address = "192.168.99.2/24";
      };
      nodes.client.boot.uml.lan = {
        network = "peers";
        address = "192.168.99.3/24";
      };
      phases.peers = {
        script = ./tests/phases/peers.py;
        after = [ "boot" ];
      };
    };

    /*
      Does each guest see only its own closure, on every backend, and can
      it still add paths?
    */
    store-view = mkTest {
      name = "store-view";
      nodes.u.boot.uml.backend = "uml";
      nodes.q.boot.uml.backend = "qemu";
      nodes.c.boot.uml.backend = "container";
      phases.view = {
        script = ./tests/phases/store-view.py;
        after = [ "boot" ];
      };
    };

    /*
      nixpkgs' own nixos tests, through the mapper, unchanged. Cheap ones
      with no screen: `nixos-tests.simple-vm`, and so on.
    */
    nixos-tests = lib.genAttrs [
      "simple-vm"
      "systemd-no-tainted"
      "oh-my-zsh"
      "simple-container"
    ] (name: fromNixosTest (pkgs.path + "/nixos/tests/${name}.nix"));

    # Does `.driverInteractive` behave like nixos-test's? tests/interactive.py.
    interactive =
      let
        run = mkTest {
          name = "interactive";
          nodes.one = { };
          interactive.nodes.one.environment.etc."uml-interactive".text = "yes\n";
          phases.hello = {
            script = ./tests/phases/hello.py;
            after = [ "boot" ];
          };
        };
      in
      pkgs.runCommand "uml-check-interactive" { passthru.session = run; } ''
        export HOME=$TMPDIR
        ${pkgs.python3.interpreter} ${./tests/interactive.py} ${lib.getExe run.driverInteractive} "$TMPDIR"
        touch $out
      '';

    # Does a run leave nothing behind, however it ends? tests/cleanup.py.
    cleanup =
      let
        run = mkTest {
          name = "cleanup";
          nodes.one = { };
          phases.hold = {
            script = ./tests/phases/hold.py;
            after = [ "boot" ];
          };
        };
      in
      pkgs.runCommand "uml-check-cleanup" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/cleanup.py} ${lib.getExe session} ${run.spec} "$TMPDIR"
        touch $out
      '';

    /*
      One guest of each backend on one segment, reaching each other over
      IP by name. `mixed` and `container-lan` each prove one pair; this
      holds all three at once.
    */
    backends = mkTest {
      name = "backends";
      nodes = lib.mapAttrs (name: backend: {
        boot.uml = {
          inherit backend;
          lan = {
            network = "backends";
            address =
              {
                u = "10.57.0.1/24";
                q = "10.57.0.2/24";
                c = "10.57.0.3/24";
              }
              .${name};
          };
        };
      }) {
        u = "uml";
        q = "qemu";
        c = "container";
      };
      phases.reach = {
        script = ./tests/phases/backends.py;
        after = [ "boot" ];
      };
    };

    /*
      Can a suite that has to run inside a guest report like one that
      runs on the host?

      pynixd's suites start daemons and build into stores they make, so
      they run in the guest and write JUnit there. The session reads
      `/artifacts/junit/*.xml` back at the end of each phase and each
      test becomes a case: an event with its machine and phase, and a
      case in the run's own junit.xml.

      Also the two things generating phases from one list needs: one
      script serving two phases through `vms.phase`, and a value crossing
      from one phase to the next through `vms.shared`.
    */
    guest-suites =
      let
        run = mkTest {
          name = "guest-suites";
          nodes.one = { };
          phases = {
            census = {
              script = ./tests/phases/suite.py;
              after = [ "boot" ];
            };
            suite = {
              script = ./tests/phases/suite.py;
              after = [ "census" ];
            };
          };
        };
      in
      pkgs.runCommand "uml-check-guest-suites"
        {
          nativeBuildInputs = [
            pkgs.jq
            pkgs.libxml2
          ];
          passthru.session = run;
        }
        ''
          a=${run.attempt}
          fail() { echo "$*" >&2; exit 1; }
          jq -r '.phases[] | "\(.name)\t\(.state)"' $a/phases.json
          [ "$(cat $a/status)" = 0 ] || fail "the run failed; the script's verdict is the phase's, not the cases'"
          echo "ok: both phases passed, one script serving both"

          jq -c 'select(.kind == "case")' $a/events.jsonl | tee cases
          [ "$(wc -l < cases)" = 2 ] || fail "expected the two cases the guest wrote"
          jq -e 'select(.text == "inner.test_a::test_bad" and .data.outcome == "failed"
                        and .machine == "one" and .phase == "suite")' cases > /dev/null \
            || fail "the failed case lost its outcome, machine or phase"
          echo "ok: the guest's JUnit became cases, with machine and phase"

          n=$(xmllint --xpath 'count(//testcase[@classname="guest-suites.suite"])' $a/junit.xml)
          [ "$n" = 2 ] || fail "junit.xml holds $n of the guest's cases, expected 2"
          xmllint --xpath 'string(//testcase[@name="inner.test_a::test_bad"]/failure/@message)' $a/junit.xml \
            | grep -q "assert 1 == 2" || fail "the failure message did not survive"
          echo "ok: and they are cases in the run's own junit.xml"

          touch $out
        '';

    /*
      Does `--kernel` boot the kernel it names?

      The by-hand loop for kernel work: `make` in a tree, then boot what it
      made, without a Nix build per change. A copy of the Nix-built kernel
      outside the store stands in for a working tree's `linux`. Both
      directions: the copy boots and the run passes, and a path that is not
      a kernel fails the boot -- so what boots is the override, not the
      spec's kernel behind it.
    */
    kernel-override =
      let
        run = mkTest {
          name = "kernel";
          nodes.one = { };
        };
      in
      pkgs.runCommand "uml-check-kernel-override"
        {
          nativeBuildInputs = [ pkgs.jq ];
          passthru.session = run;
        }
        ''
          export HOME="$TMPDIR"
          fail() { echo "$*" >&2; exit 1; }
          cp "$(jq -r .kernel ${run.spec})" "$TMPDIR/linux"
          chmod +x "$TMPDIR/linux"

          ${lib.getExe run.driver} --out "$TMPDIR/good" --kernel "$TMPDIR/linux" \
            || fail "the run with a copied kernel failed"
          jq -e --arg k "$TMPDIR/linux" 'select(.kind == "note" and .data.kernel == $k)' \
            "$TMPDIR/good/events.jsonl" > /dev/null || fail "the run did not record the override"
          echo "ok: a kernel from outside the store booted, and the run says so"

          printf 'not a kernel\n' > "$TMPDIR/bogus"
          chmod +x "$TMPDIR/bogus"
          if ${lib.getExe run.driver} --out "$TMPDIR/bad" --kernel "$TMPDIR/bogus"; then
            fail "a run whose kernel is not a kernel passed -- the override is ignored"
          fi
          echo "ok: and a path that is not a kernel fails the boot"

          touch $out
        '';

    /*
      Can a person reach into a paused run?

      The loop this is for: changing a phase costs an evaluation and a
      store path, and changing a guest costs an image, so the fast way is
      neither -- pause with the guests up and send Python in. Driven with
      the real binaries, the way a person or an agent drives them: `uml
      run --break` in the background, `uml ctl` against its socket.

      The file injected is written here, in the build directory, and was
      never in the store: that is the point of `inject`.
    */
    breakpoint =
      let
        run = mkTest {
          name = "breakpoint";
          nodes.one = { };
          phases.later = {
            script = ./tests/phases/independent.py;
            after = [ "boot" ];
          };
        };
      in
      pkgs.runCommand "uml-check-breakpoint"
        {
          nativeBuildInputs = [ pkgs.jq ];
          passthru.session = run;
        }
        ''
          export HOME="$TMPDIR"
          o="$TMPDIR/run"
          ctl() { ${lib.getExe session} ctl --out "$o" "$@"; }
          fail() { echo "$*" >&2; kill "$pid" 2>/dev/null; exit 1; }

          ${lib.getExe run.driver} --out "$o" --break later > run.log 2>&1 &
          pid=$!

          for _ in $(seq 1 600); do
            [ "$(ctl state 2>/dev/null | head -1)" = paused ] && break
            kill -0 "$pid" 2>/dev/null || { cat run.log; fail "the run ended without pausing"; }
            sleep 0.1
          done
          ctl state | tee state
          grep -qx "later	pending" state || fail "the phase ran through its breakpoint"
          echo "ok: paused before later"

          ctl exec 'await one.succeed("hostname")' | tee hostname
          grep -q one hostname || fail "exec did not reach the guest"
          echo "ok: exec reached the guest"

          ctl exec 'n = (await one.succeed("echo 7")).strip()'
          [ "$(ctl exec 'n')" = "'7'" ] || fail "a name did not survive to the next exec"
          echo "ok: the namespace survives between calls"

          if ctl exec '1/0' 2> err; then fail "an exception was reported as success"; fi
          grep -q ZeroDivisionError err || fail "the traceback did not come back"
          echo "ok: an exception comes back as the reply, and the run stays up"

          cat > scratch.py <<'EOF'
          from uml_runner import Machines

          async def test(vms: Machines) -> None:
              await vms.one.succeed("echo injected > /artifacts/injected")
              print("[scratch] wrote it")
          EOF
          ctl inject scratch.py | tee injected
          grep -q "wrote it" injected || fail "inject did not run the file"
          echo "ok: a file from outside the store ran against the guest"

          mkdir tree
          echo 'async def test_host(one): assert (await one.succeed("hostname")).strip() == "one"' \
            > tree/test_by_hand.py
          ctl pytest tree | tee by-hand
          grep -qx "1 passed" by-hand || fail "pytest by hand did not pass against the guest"
          echo 'async def test_host(one): assert (await one.succeed("hostname")).strip() == "edited"' \
            > tree/test_by_hand.py
          if ctl pytest tree -- -k host; then fail "pytest by hand ran the old test after an edit"; fi
          echo "ok: pytest ran a working tree's test, then its edit, against the paused guest"

          ctl continue
          wait "$pid" || { cat run.log; fail "the run failed after continue"; }
          echo "ok: continue finished the run"

          jq -r '.phases[] | "\(.name)\t\(.state)"' "$o/phases.json"
          [ "$(jq -r '.phases[] | select(.name == "later") | .state' "$o/phases.json")" = passed ] \
            || fail "later did not run after continue"
          test -f "$o/artifacts/one/injected" || fail "the injected write is not on the host"
          jq -e 'select(.kind == "output" and .phase == "inject:scratch.py")' "$o/events.jsonl" > /dev/null \
            || fail "what the injected file printed is not in the events"
          jq -e 'select(.kind == "note" and .data.op == "exec")' "$o/events.jsonl" > /dev/null \
            || fail "the exec is not recorded in the events"
          echo "ok: and events.jsonl records what was done by hand"
          [ "$(cat "$o/status")" = 0 ] || fail "a failing test sent by hand failed the run"
          if grep -q test_by_hand "$o/junit.xml"; then fail "a test sent by hand is in junit.xml"; fi
          jq -e 'select(.kind == "case" and .data.by_hand and .data.outcome == "failed")' \
            "$o/events.jsonl" > /dev/null || fail "the by-hand case is not in the events"
          echo "ok: and its cases are events, outside the verdict and junit.xml"
          test ! -e "$o/control.sock" || fail "the socket outlived the run"

          touch $out
        '';

    /*
      Can a guest host a userspace filesystem?

      The question a build sandbox cannot answer for itself: its /dev has
      null, zero, random and little else, so a FUSE mount is out of reach
      there however the test is written.  A guest brings its own kernel
      and so its own /dev/fuse.

      Unprivileged mounting takes programs.fuse, which is opt in on NixOS
      and is what puts a setuid fusermount3 under /run/wrappers.  The
      wrappers themselves a guest already has.
    */
    fuse = mkTest {
      name = "fuse";
      phases.test = {
        script = ./tests/fuse.py;
        after = [ "boot" ];
      };
      nodes.node = {
        programs.fuse.enable = true;
        programs.fuse.userAllowOther = true;
        environment.systemPackages = [
          pkgs.bindfs
          pkgs.util-linux
        ];
        users.users.alice = {
          isNormalUser = true;
          uid = 1000;
        };
      };
    };

    /*
      Does a guest give its memory back?

      Both backends, and the same script: UML reports free pages through
      `madvise(MADV_REMOVE)` and QEMU through virtio-balloon, and a test
      sees one number either way. Issues #12 and #4.
    */
    memory = mkTest {
      name = "memory";
      phases.test = {
        script = ./tests/memory.py;
        after = [ "boot" ];
      };
      nodes.node = {
        # Large enough that reading the guest's own closure is page cache
        # and not pressure, which is what lets the test attribute what it
        # frees afterwards.
        boot.uml.memory = "1024M";
      };
    };

    # How much does a segment between two guests actually carry?
    iperf = mkTest {
      name = "iperf";
      phases.test = {
        script = ./tests/iperf.py;
        after = [ "boot" ];
      };
      nodes = iperfNodes;
    };

    /*
      Does a container run at all?

      One guest, and the narrow question the cluster test answers
      only after an hour: whether the kernel has what runc wants,
      whether the images imported, and whether a container of
      symlinks can reach the store they point into.
    */
    containerd = mkTest {
      name = "containerd";
      phases.test = {
        script = ./tests/containerd.py;
        after = [ "boot" ];
      };
      nodes.node =
        { config, ... }:
        {
          imports = [ ./modules/k8s.nix ];
          services.uml-k8s = {
            enable = true;
            role = "worker";
            runtimes = [ "crun" ] ++ lib.optional (config.boot.uml.backend != "uml") "runsc";
          };
          boot.uml = {
            memory = "1024M";
            diskSize = 2048;
            lan = {
              network = "containerd";
              address = "10.101.0.1/24";
            };
          };
        };
      settings = {
        inherit (k8sImages) sandboxImage entrypoints;
        kubernetesVersion = pkgs.kubernetes.version;
      };
    };

    /*
      The same questions under Kata Containers, a QEMU VM per pod, on
      either CRI. `kata` is CRI-O, as OpenShift runs it.

      By hand, not in CI, like `nested`: a GitHub runner's KVM does not
      nest again.
    */
    kata = kataTest "crio";
    kata-containerd = kataTest "containerd";

    # The same questions under CRI-O. nixkube#74.
    crio = mkTest {
      name = "crio";
      phases.test = {
        script = ./tests/containerd.py;
        after = [ "boot" ];
      };
      nodes.node =
        { config, ... }:
        {
          imports = [ ./modules/k8s.nix ];
          services.uml-k8s = {
            enable = true;
            role = "worker";
            cri = "crio";
            # Not runsc, which runs no container under CRI-O; see the
            # module's assertion.
            runtimes = [ "crun" ];
          };
          boot.uml = {
            memory = "1024M";
            diskSize = 2048;
            lan = {
              network = "crio";
              address = "10.105.0.1/24";
            };
          };
        };
      settings = {
        inherit (k8sImages) sandboxImage entrypoints;
        kubernetesVersion = pkgs.kubernetes.version;
      };
    };

    # Does a real workload come up across three nodes?  Far heavier
    # than the others: three guests, a control plane and a container
    # runtime, so this one wants a builder rather than a laptop.
    k8s = mkTest {
      name = "k8s";
      phases.test = {
        script = ./tests/k8s.py;
        after = [ "boot" ];
      };
      nodes = k8sNodes;
      settings = {
        inherit (k8sConfig.services.uml-k8s) podSubnet workloadImage;
        kubernetesVersion = pkgs.kubernetes.version;
      };
    };

    # Are the images we build the ones kubeadm will go looking for?
    # Cheap, and the alternative is finding out from an
    # ImagePullBackOff twenty minutes into the cluster test.
    check-k8s-images = k8sImages.check;

    # And does kubeadm accept the configuration we generate for it?
    check-k8s-config =
      pkgs.runCommand "kubeadm-config-valid" { nativeBuildInputs = [ pkgs.kubernetes ]; }
        (
          lib.concatMapStrings (file: ''
            echo "validating ${file}"
            kubeadm config validate --config ${file}
          '') (lib.attrValues k8sConfig.system.build.kubeadmConfigs)
          + "touch $out"
        );

    check-workflows = ci.check ./.github/workflows;

    # The scripts in tests/, against the library they drive. Also the
    # check that `typeCheck` itself works.
    check-scripts = uml.typeCheck {
      name = "own-scripts";
      # `.py` only: a run outside the sandbox leaves `__pycache__` beside
      # them, and pyright has nothing to say about a `.pyc`.
      scripts = lib.filter (p: lib.hasSuffix ".py" (toString p)) (
        # `recipes` as well as `tests`: a recipe is shipped for other
        # projects to enable, so one that does not type check breaks a
        # consumer rather than this repository.
        lib.filesystem.listFilesRecursive ./recipes
        ++ lib.filesystem.listFilesRecursive ./tests
      );
    };
  };
in
tests
// {
  # The library, for a caller that writes its own test.
  inherit mkNode mkTest fromNixosTest runner session typeCheck;
  lib = { inherit mkNode mkTest fromNixosTest runner session typeCheck; };

  inherit demo store k8s-pull uplink incr;
  inherit (demo.config.system.build) umlRunner umlRootImage toplevel;

  # The slowest thing in the repository and the same for every guest, so
  # CI builds it once on its own and lets the cache hand it to the test
  # jobs.
  umlKernel = k8sConfig.system.build.umlKernel;

  /*
    The same kernel, compiled through ccache into `/ccache`, for a patch
    series rebuilt as CI builds it but without starting cold each time.
    Only a builder that mounts the directory can build it:

      pynix build --file . --attr umlKernelCcache --namespaced \
        --sandbox-path /ccache=$HOME/.cache/uml-ccache

    Measured: 122s cold, 31s after a one-line change. `--kernel` with
    `make ARCH=um` in a tree is still the inner loop.
  */
  umlKernelCcache = k8sConfig.system.build.umlKernel.override { ccacheDir = "/ccache"; };

  # What CI builds. `flake.nix` re-exports this as both packages and checks.
  checks = tests;

  /*
    Every test above also answers to `.uml` and `.qemu`.

        nix build --file . lan          # as `checks` runs it
        nix build --file . lan.qemu     # the same test, as machines
        nix build --file . iperf.qemu   # what the segment carries there

    Nothing is duplicated to make that work: one script, one set of node
    configurations, and neither knows which machine it got.

    `checks` holds the default of each, which is UML. A `.qemu` variant
    asks the daemon for the `kvm` feature, and a builder without
    `/dev/kvm` does not fail it -- it refuses to build it at all, which
    would stop CI rather than report anything. That is the only reason
    they are not checks, and it goes away once we know what our runners
    have.
  */

  # A guest to poke at by hand, running one program.
  speedtest = pkgs.writeShellScriptBin "uml-speedtest" ''
    exec ${demo.config.system.build.umlRunner}/bin/run-uml --command speedtest-cli "$@"
  '';

  # Regenerate .github/workflows from ci/workflows.nix.
  render-workflows = ci.renderApp;

  /*
    Name a run and it is evaluated, built and run, with no `nix build`
    first:

        nix run --file . uml-eval -- run pytest-phase --out ./out -- -k hostname

    Built on nanopynix, so not a check and not a dependency of anything
    a consumer uses. `uml-eval.tests` holds its unit tests.
  */
  uml-eval = import ./pkgs/uml-eval { inherit pkgs sources; };

  /*
    `uml-mcp` driven the way Claude Code drives it: JSON-RPC over stdio,
    against a run that fails a phase on purpose. The channel events --
    paused, failed, finished -- are the claim; `exec` and `events` are
    checked against the paused guests.

    By spec, so nothing here evaluates. Not a check, for the same reason
    `uml-eval` is not: CI would have to build nanopynix.
  */
  mcp-check =
    let
      uml-eval = import ./pkgs/uml-eval { inherit pkgs sources; };
    in
    pkgs.runCommand "uml-check-mcp" { nativeBuildInputs = [ pkgs.python3 ]; } ''
      export HOME="$TMPDIR"
      python3 ${./tests/mcp_driver.py} ${uml-eval}/bin/uml-mcp ${tests.pytest-phase.session.spec}
      touch $out
    '';
}
