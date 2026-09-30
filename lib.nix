# mkNode and mkTest, as a library.
#
# These used to live in `flake.nix`'s `let`, where nothing outside this
# repository could reach them. A caller with its own guests and its own script
# is the point of the exercise -- easykubenix drives `ekn kubeapply` against a
# cluster this builds -- so they are a file that takes a package set.
#
#     let uml = import (sources.user-mode-nixos + "/lib.nix") { inherit pkgs; };
#     in uml.mkTest { name = "..."; script = ./mine.py; nodes = { ... }; }
#
# Nothing here is specific to the tests in this repository. `flake.nix` and
# `default.nix` both call it, so the two doors cannot drift apart.
{
  pkgs,
  lib ? pkgs.lib,
}:
rec {
  /*
    The library a test script imports, reachable without evaluating a
    guest: a caller puts it in the Python it type checks with.  It carries
    `py.typed`, so pyright reads `vms.node` as a `Machine`.

        python3.withPackages (_: [ uml.runner ])
  */
  runner = pkgs.callPackage ./pkgs/uml-runner { };

  /**
    The session, and the `uml` CLI that drives one.

    The redesign lives here; `runner` above is the mechanism it uses and
    is not going away. See `docs/design/history/running-anywhere.md`.
  */
  session = pkgs.callPackage ./pkgs/uml { uml-runner = runner; };

  /*
    pyright over a caller's test scripts, against this library.

        typeCheck { scripts = [ ./tests/uml/run.py ]; }

    `mkTest` calls this itself -- see `boot.uml.typeCheck`.  Call it
    directly for scripts that are not a test's.
  */
  typeCheck =
    {
      name ? "uml-test-scripts",
      scripts,
      extraPackages ? [ ],
      strict ? false,
      ignore ? [ ],
      # Directories of helper modules the scripts import; `mkSession`'s
      # `pythonPath`.
      extraPaths ? [ ],
    }:
    let
      # `session` brings pytest with it, so a pytest phase's tests and
      # conftest check against the same pytest that runs them.
      python = pkgs.python3.withPackages (
        _:
        [
          runner
          session
        ]
        ++ extraPackages
      );

      # pyright's rule names, the way `writePython3Bin`'s `flakeIgnore`
      # is flake8's codes.
      rules = lib.listToAttrs (map (rule: lib.nameValuePair rule "none") ignore);

      settings = {
        typeCheckingMode = if strict then "strict" else "standard";
        pythonVersion = lib.versions.majorMinor python.python.version;
        reportMissingImports = "error";
        # Neither standard nor strict turns this on, and it is what makes
        # the rest worth running: an unannotated parameter is Unknown, and
        # nothing done to an Unknown is checked. Measured -- without it,
        # `await vms.node.succeed(123)` and a call to a method that does
        # not exist both passed.
        reportMissingParameterType = "error";
        extraPaths = map (path: "${path}") extraPaths;
      }
      // rules;
    in
    pkgs.runCommand "typecheck-${name}"
      {
        nativeBuildInputs = [
          pkgs.pyright
          python
        ];
      }
      ''
        # Copied in rather than checked in place: pyright follows a path,
        # and a store path is read-only.
        #
        # One directory each, numbered. Two scripts may share a basename
        # -- `recipes/boot.py` and `tests/phases/boot.py` do -- and
        # copying both to `scripts/boot.py` failed with "Permission
        # denied", because the first arrived read-only from the store and
        # the second tried to overwrite it. A collision must not depend
        # on which two files a caller happens to pass.
        ${lib.concatStringsSep "\n" (
          # `-r`: a pytest phase is a directory of tests.
          lib.imap0 (index: script: ''
            mkdir -p scripts/${toString index}
            cp -r ${script} scripts/${toString index}/${baseNameOf script}
            chmod -R +w scripts/${toString index}/${baseNameOf script}
          '') scripts
        )}
        cp ${pkgs.writeText "pyrightconfig.json" (builtins.toJSON settings)} \
          pyrightconfig.json
        # Offline: pyright downloads a node runtime unless it is told
        # which one to use, and a build sandbox has no network.
        export HOME=$TMPDIR
        pyright --pythonpath ${python}/bin/python --outputjson scripts > report.json || {
          cat report.json
          echo "the scripts above do not type check against uml_runner" >&2
          exit 1
        }
        cp report.json $out
      '';

  /**
    How one guest becomes a line in a spec.

    Shared by `mkTest` and `mkSession` so that the two cannot describe the
    same machine differently. A field added here reaches both doors.
  */
  machineSpec =
    machine:
    {
      name = machine.networking.hostName;
      backend = machine.boot.uml.backend;
      index = machine.boot.uml.index;
      memory = machine.boot.uml.memory;
      seccomp = machine.boot.uml.seccomp;
      cpus = machine.boot.uml.cpus;
      sshPort = machine.boot.uml.sshPort;
      mtu = machine.boot.uml.mtu;
      network = machine.boot.uml.lan.network;
      address = machine.boot.uml.lan.address;
      forward = machine.boot.uml.forward;
      # Both backends get a read-only root image of `boot.uml.diskSize`
      # and a per-run copy-on-write layer over it. Only what is inside
      # differs: UML boots `/init` from it, QEMU mounts it as `/`.
      image = "${machine.system.build.umlRootImage}";
    }
    // lib.optionalAttrs (machine.boot.uml.backend == "qemu") {
      boot = machine.system.build.qemuBoot;
    }
    // lib.optionalAttrs (machine.boot.uml.backend == "container") {
      boot = machine.system.build.containerBoot;
    };

  /**
    The host-side binaries a run needs, and none of the other backend's.

    Naming a store path is what builds it. A QEMU run that mentioned
    `umlKernel` would spend half an hour on a kernel it never boots, and a
    UML run that mentioned `qemu_kvm` would pull QEMU into a sandbox that
    has no use for it.

    Taken from the machines and not from the run's `backend`: a node may
    set its own `boot.uml.backend`, and a run then holds both kinds. The
    runner picks a backend per machine, and a segment carries raw frames
    that both accept, so nothing else has to know.
  */
  toolchainFor =
    machines:
    let
      on = backend: lib.filter (machine: machine.boot.uml.backend == backend) machines;
      uml = on "uml";
    in
    {
      passt = "${pkgs.passt}/bin/passt";
    }
    // lib.optionalAttrs (uml != [ ]) {
      kernel = "${(lib.head uml).system.build.umlKernel}/linux";
      bridge = lib.getExe (lib.head uml).system.build.umlPasstBridge;
    }
    // lib.optionalAttrs (on "qemu" != [ ]) {
      qemu = "${pkgs.qemu_kvm}/bin/qemu-system-x86_64";
      qemuImg = "${pkgs.qemu_kvm}/bin/qemu-img";
      virtiofsd = "${pkgs.virtiofsd}/bin/virtiofsd";
    }
    // lib.optionalAttrs (on "container" != [ ]) {
      crun = lib.getExe pkgs.crun;
      setpriv = "${lib.getBin pkgs.util-linux}/bin/setpriv";
    };

  /*
    What the daemon must give a run's derivation, by the backends in it.

    A QEMU guest is only worth booting with KVM, and the daemon only hands
    /dev/kvm to a derivation that asks for it. A container needs the
    `uid-range` feature: 65536 ids and a cgroup of its own, which a plain
    sandbox build does not have (measured, see Area 8 of the design). UML
    asks for nothing, which is the whole point of UML.
  */
  featuresFor =
    machines:
    let
      any = backend: lib.any (machine: machine.boot.uml.backend == backend) machines;
    in
    lib.optional (any "qemu") "kvm" ++ lib.optional (any "container") "uid-range";

  /**
    Whether this sandbox can run a container guest, answered in seconds.

    The runner's own host checks (`uml_runner.container.probe`), in a
    derivation that asks for what a container session asks for. A missing
    piece fails here, named with its fix, rather than as a guest that does
    not boot minutes into a run. Every session with a container guest
    depends on it; a CI job can build it first on its own.

    `tun`: a LAN is wanted, so a tap device must be possible, which in a
    sandbox needs /dev/net in `extra-sandbox-paths`.
  */
  containerProbe =
    {
      tun ? false,
    }:
    pkgs.runCommand "uml-container-probe${lib.optionalString tun "-tun"}"
      {
        nativeBuildInputs = [ (runner.pythonModule.withPackages (_: [ runner ])) ];
        requiredSystemFeatures = [ "uid-range" ];
      }
      ''
        set -o pipefail
        python -m uml_runner.crun_launch probe ${lib.optionalString tun "--tun"} | tee $out
      '';

  # The probe a set of machines needs, or null when none is a container.
  probeFor =
    machines:
    let
      containers = lib.filter (machine: machine.boot.uml.backend == "container") machines;
    in
    if containers == [ ] then
      null
    else
      containerProbe {
        tun = lib.any (machine: machine.boot.uml.lan.network != null) containers;
      };

  /*
    Every peer that has a `vec1` address, by hostname, in each guest's
    /etc/hosts. nixos-test writes the same from its `nodes`.
  */
  peersModule =
    { lib, nodes, ... }:
    {
      networking.hosts = lib.mkMerge (
        lib.mapAttrsToList (
          _: peer:
          let
            address = peer.boot.uml.lan.address;
          in
          lib.optionalAttrs (address != null) {
            ${lib.head (lib.splitString "/" address)} = [ peer.networking.hostName ];
          }
        ) nodes
      );
    };

  # A guest: an ordinary NixOS configuration plus ./modules.
  #
  # `eval-config.nix` and not `lib.nixosSystem`. That name only exists on the
  # lib a flake gets from nixpkgs' own `flake.nix`; `pkgs.lib` is the library
  # itself and has never carried it. This is what the flake wrapper calls, and
  # `system = null` is how it says that `nixpkgs.pkgs` below decides the
  # platform.
  mkNode =
    module:
    import (pkgs.path + "/nixos/lib/eval-config.nix") {
      inherit lib;
      system = null;
      modules = [
        ./modules
        { nixpkgs.pkgs = pkgs; }
        module
      ];
    };

  /*
    A test derivation: `script` run against the guests in `nodes`.

    `nodes` maps a hostname to a NixOS module.  Each becomes a guest,
    and machines whose `boot.uml.lan.network` matches get an Ethernet
    segment between them.  ssh ports are handed out from 4325 so that
    nodes do not have to keep track of them.

    The script gets a JSON spec naming the guests' images and their
    addresses, and uses uml_runner.run_test to boot them; see
    tests/ for what one looks like.

    `settings` is anything else the script needs that only Nix knows
    -- a version, an image tag -- and reaches it as `vms.settings`.

    A store path in `settings` is a dependency like any other: the JSON
    carries its context, so the derivation builds it and the guest reads
    it from the host's store.  That is how a caller gets its own program
    into a guest without an image, a copy or a network.

    `impurities` is the opposite channel: a list of environment variable
    *names* the run may read from the host, reaching the script as
    `vms.env`.

        impurities = [ "PYTEST_ARGS" ];

        PYTEST_ARGS='-k mounts' nix run --file . mytest.run

    Names, never values.  A value never enters the spec, so it never
    enters a store path and no derivation hash moves with it -- which is
    what lets the same test stay pure under `nix build`.  A sandbox has no
    environment to read, so every value is empty there and the test does
    whatever it does by default.  Prefer this to `builtins.getEnv`, which
    needs an impure evaluation and rebuilds the test for each value.

    The run prints each name and its value before it boots anything.  A
    misspelled variable is otherwise invisible: the run does the whole
    suite instead of the one case, and says nothing.

    Anything after `--spec` on the command line reaches the script as
    `vms.argv`, unparsed:

        nix run --file . mytest.run -- -k mounts

    `backend` picks what the guests become.  The script does not change
    with it, and neither does a node's configuration: `uml` needs nothing
    of the host, `qemu` needs `/dev/kvm` and is much faster.  A node may
    still override `boot.uml.backend` for itself.

    Every test carries `.uml` and `.qemu`, which are the same test forced
    to that backend.  So the choice needs no Nix edit:

        nix build --file . iperf        # whatever `backend` said
        nix build --file . iperf.qemu   # the same test, as machines

    And `.run` on each of those is the same test outside the sandbox,
    with nothing to pass on the command line:

        nix run --file . iperf.run
        nix run --file . iperf.qemu.run

    **A test derivation never fails.** `.attempt` is the run and always
    succeeds; the test reads the exit code it wrote and fails on that.  So
    a failed run keeps its log, its timings and what the guests wrote to
    `/artifacts`, and the build log says where.

    pyright runs over `script` as an input of the test.
    `boot.uml.typeCheck` on the first guest is the switch.

    The one named by `backend` keeps the bare derivation name, and is the
    same derivation as the attribute of that name -- `lan` and `lan.uml`
    are one store path, not two.
  */
  mkTest =
    args@{
      backend ? "uml",
      ...
    }:
    let
      variants = lib.genAttrs [ "uml" "qemu" ] (
        chosen: mkTestOn (builtins.removeAttrs args [ "backend" ] // { inherit chosen backend; })
      );
    in
    variants.${backend} // { inherit (variants) uml qemu; };

  # One test on one backend. `mkTest` is the door; this is what it calls
  # twice, so that `.uml` and `.qemu` cannot drift from each other.
  mkTestOn =
    {
      name,
      script,
      nodes,
      settings ? { },
      impurities ? [ ],
      # `.uml`, `.qemu`, `.attempt` and `.run` are added after this, so a
      # name here cannot take one of theirs.
      passthru ? { },
      chosen,
      backend,
    }:
    let
      # The default backend keeps the bare name, so a second one appearing
      # does not move store paths or rename anything in a CI log.
      suffix = lib.optionalString (chosen != backend) "-${chosen}";

      /*
        `settings` on its own, so a guest can be told about the store paths
        in it.

        A test hands its guests store paths through `settings` -- an image,
        a program, a chart -- and nothing in the module system sees them, so
        `boot.uml.nixDatabase` used to need each one named again by hand in
        `extraRoots`. One that was missed is not a build error: Nix in the
        guest calls the path invalid and goes looking for a substituter.

        A file is what breaks that. Registering its closure registers every
        path it mentions, and this file cannot mention the machines, so
        naming it from a machine is not a cycle -- which naming the spec
        would be, since the spec names each machine's root image.
      */
      settingsFile = pkgs.writeText "uml-${name}${suffix}-settings.json" (builtins.toJSON settings);

      machines = lib.imap0 (
        index: hostName:
        (mkNode {
          imports = [ nodes.${hostName} ];
          networking.hostName = lib.mkDefault hostName;
          boot.uml.sshPort = lib.mkDefault (4325 + index);
          boot.uml.backend = lib.mkDefault chosen;
          boot.uml.index = index;
          boot.uml.nixDatabase.extraRoots = lib.optional (settings != { }) "${settingsFile}";
        }).config
      ) (lib.attrNames nodes);

      # Every guest builds these from the same pkgs, so any of them
      # will do.
      first = lib.head machines;

      # Named in the builder below rather than added to it: naming a store
      # path is what makes Nix build it, so the check runs before the
      # guests do.
      checked =
        let
          cfg = first.boot.uml.typeCheck;
        in
        lib.optionalString cfg.enable "${typeCheck {
          name = "${name}${suffix}-script";
          scripts = [ script ];
          inherit (cfg) extraPackages strict ignore;
        }}";

      toolchain = toolchainFor machines;

      spec = pkgs.writeText "uml-${name}${suffix}-spec.json" (
        builtins.toJSON (
          toolchain
          // {
            inherit settings impurities;
            machines = map machineSpec machines;
          }
        )
      );

      python = pkgs.python3.withPackages (_: [ first.system.build.umlRunnerPackage ]);

      /*
        The same run, outside the sandbox: `nix run --file . iperf.run`.

        Nothing about a test belongs on a command line. The spec names the
        images, the toolchain, the addresses and the ports, and Nix is what
        built every one of them -- so the invocation is a store path too,
        and running one by hand is the same run the check makes with the
        sandbox taken off.

        `$@` reaches the script, which is where a test's own flags go.
      */
      run = pkgs.writeShellApplication {
        name = "run-uml-test-${name}${suffix}";
        runtimeInputs = [ python ];
        # `UML_TEST_REPORT` is not set here. A run by hand records only
        # when it is asked to, so nothing writes to a directory nobody
        # chose; the sandboxed build below always records, because there
        # is an output to put it in.
        text = ''
          exec python3 ${script} --spec ${spec} "$@"
        '';
      };

      /*
        The run itself, which never fails.

        Nix deletes the output of a derivation that fails, so a test that
        reports failure by failing throws away the evidence of the one run
        anybody wanted to read.  This one always succeeds and writes what
        happened to `status`; the derivation below fails, and reads nothing
        but that file.

            status       the run's exit code, as text
            log          everything the run printed
            report.json  where the time went, see report.py
            artifacts/   what the guests wrote to /artifacts
      */
      attempt =
        pkgs.runCommand "uml-test-${name}${suffix}-attempt"
          {
            nativeBuildInputs = [ python ];
            requiredSystemFeatures = featuresFor machines;
            passthru = { inherit spec python run; };
          }
          ''
            export HOME="$TMPDIR"
            mkdir -p "$out/artifacts"
            # Empty when `boot.uml.typeCheck.enable` is off.
            echo "script checked: ${checked}" > "$out/typecheck"
            export UML_TEST_REPORT=$out/report.json
            export UML_TEST_ARTIFACTS=$out/artifacts

            # The shell writes the marker, not the runner: the runner can die
            # before any Python of ours runs, and a missing marker would then
            # be read as a pass. `tee` keeps `--print-build-logs` streaming;
            # PIPESTATUS is the runner's exit code rather than tee's.
            set +e
            python3 ${script} --spec ${spec} 2>&1 | tee "$out/log"
            status=''${PIPESTATUS[0]}
            set -e
            echo "$status" > "$out/status"
          '';
    in
    /*
      The check reads the marker and nothing else, and names the run's
      output in the build log -- the one thing a failed build leaves.

      The trap: a failed run is a *successful* build of `attempt`, so Nix
      caches it.  Building the test again re-reads the marker and fails in
      a second, booting nothing, until an input changes.
    */
    pkgs.runCommand "uml-test-${name}${suffix}"
      {
        passthru = passthru // {
          inherit
            attempt
            spec
            python
            run
            ;
        };
      }
      ''
        echo "the run is at ${attempt}"
        echo "  log:       ${attempt}/log"
        echo "  timings:   ${attempt}/report.json"
        echo "  artifacts: ${attempt}/artifacts"

        status=$(cat ${attempt}/status)
        if [ "$status" != 0 ]; then
          echo
          echo "--- the last 50 lines of ${attempt}/log ---"
          tail -n 50 ${attempt}/log
          echo "--- end ---"
          echo
          echo "the test failed (exit $status); the paths above hold what it left" >&2
          exit 1
        fi

        mkdir -p $out
        ln -s ${attempt} $out/attempt
        ln -s ${attempt}/log $out/log
        ln -s ${attempt}/report.json $out/report.json
        ln -s ${attempt}/artifacts $out/artifacts
      '';

  /**
    A run: guests, and the phases that drive them.

        mkSession {
          name = "mine";
          nodes.one = { };
          phases.check.script = ./check.py;
        }

    `mkTest` is the older door and takes one script. This one takes a
    module, so a recipe can contribute a phase, the guest configuration
    that phase needs and the knobs it reads in a single import -- and a
    consumer overrides any of it the way they override a NixOS option.

    Three attributes come out, and they are one program run three ways:

        .check   the sandboxed derivation, which CI builds
        .run     the same run by hand, `--out` where you want it
        .phases  what would run, in order, without booting anything

    A phase script exports one coroutine:

        async def test(vms: Machines) -> None: ...

    It is imported rather than executed, so it may import whatever it
    likes and pyright checks it.

    **`after` is a dependency, not a hint.** A phase whose `after` failed
    is skipped, because running it against a world that was never built
    gives a second failure that says nothing. An `after` naming a phase
    that does not exist is an evaluation error, since the alternative is
    a phase that quietly never gets skipped.
  */
  mkSession =
    module:
    let
      run = lib.evalModules {
        modules = [
          ./modules/run.nix
          # The standard library is always imported and nothing in it is
          # on by default except `boot`, which every run wants and any
          # run can turn off. A recipe a consumer has to import by path
          # is a recipe nobody finds.
          ./modules/recipes
          module
        ];
        specialArgs = { inherit pkgs; };
      };

      cfg = run.config;

      failed = lib.filter (each: !each.assertion) cfg.assertions;
      checkedConfig =
        if failed == [ ] then cfg else throw (lib.concatMapStringsSep "\n" (each: each.message) failed);

      inherit (checkedConfig) name backend;

      settingsFile = pkgs.writeText "uml-${name}-settings.json" (builtins.toJSON checkedConfig.settings);

      /*
        Every guest, evaluated, by name. Each one receives all of them as
        the module argument `nodes`, as in nixos-test, so a guest may read
        static facts about its peers: a name, an address, a secret written
        in Nix. Lazy, so a guest reading a peer's address is no cycle.
      */
      evaluated = lib.listToAttrs (
        lib.imap0 (
          index: hostName:
          lib.nameValuePair hostName (mkNode {
            imports = [
              checkedConfig.defaults
              checkedConfig.nodes.${hostName}
              peersModule
            ];
            _module.args.nodes = nodes;
            networking.hostName = lib.mkDefault hostName;
            boot.uml.sshPort = lib.mkDefault (4325 + index);
            boot.uml.backend = lib.mkDefault backend;
            boot.uml.index = index;
            boot.uml.nixDatabase.extraRoots = lib.optional (checkedConfig.settings != { }) "${settingsFile}";
          })
        ) (lib.attrNames checkedConfig.nodes)
      );

      nodes = lib.mapAttrs (_: node: node.config) evaluated;

      machines = map (hostName: nodes.${hostName}) (lib.attrNames checkedConfig.nodes);

      first = lib.head machines;

      # Named in the builder rather than added to it: naming a store path
      # is what makes Nix build it, so the check runs before the guests
      # do. Every phase's script, not one -- a recipe that does not type
      # check is a recipe that breaks its consumers.
      checked =
        let
          typing = first.boot.uml.typeCheck;
        in
        lib.optionalString typing.enable "${typeCheck {
          name = "${name}-phases";
          scripts = map (
            phase: if phase.pytest != null then phase.pytest.tests else phase.script
          ) checkedConfig.ordered;
          extraPaths = checkedConfig.pythonPath;
          inherit (typing) extraPackages strict ignore;
        }}";

      spec = pkgs.writeText "uml-${name}-spec.json" (
        builtins.toJSON (
          toolchainFor machines
          // {
            inherit (checkedConfig) name settings;
            unshare = "${lib.getBin pkgs.util-linux}/bin/unshare";
            # The runner that knows every field below. No cycle: the
            # package does not depend on any spec.
            uml = "${session}";
            pythonPath = map (path: "${path}") checkedConfig.pythonPath;
            knobs = checkedConfig.resolved;
            phases = map (
              phase:
              {
                inherit (phase)
                  name
                  after
                  always
                  nodes
                  ;
              }
              // (
                if phase.pytest != null then
                  {
                    pytest = {
                      tests = "${phase.pytest.tests}";
                      inherit (phase.pytest) args;
                    };
                  }
                else
                  { script = "${phase.script}"; }
              )
            ) checkedConfig.ordered;
            machines = map machineSpec machines;
          }
        )
      );

      uml = lib.getExe session;

      /*
        The run outside the sandbox.

        `--out` is required and not defaulted. `lib.nix` has held since
        the beginning that a run by hand records only where it is told
        to, so that nothing writes to a directory nobody chose -- and
        being told is now cheap, because it is one flag rather than an
        environment variable nobody remembers.
      */
      runner = pkgs.writeShellApplication {
        name = "uml-run-${name}";
        text = ''
          # checked: ${checked}
          #
          # Named in a comment, which is enough: Nix scans the text for
          # store paths, so the type check is a dependency of this script
          # and runs before it can. The by-hand door is where a type
          # error gets written, so it is the door that must not skip the
          # check.
          exec ${uml} run --spec ${spec} "$@"
        '';
      };

      lister = pkgs.writeShellApplication {
        name = "uml-phases-${name}";
        text = ''
          exec ${uml} phases --spec ${spec} "$@"
        '';
      };

      /*
        The run inside one, which never fails.

        Nix deletes the output of a derivation that fails, so a run that
        reports failure by failing throws away the evidence of the one run
        anybody wanted to read. This always succeeds and writes what
        happened to `status`; the check below fails, and reads nothing but
        that file.

        The same `uml run` the developer gets, with `--out` pointed at the
        derivation's own output. Nothing branches on being in a sandbox,
        which is what stops the two drifting.
      */
      probe = probeFor machines;
      attempt =
        pkgs.runCommand "uml-session-${name}-attempt"
          {
            requiredSystemFeatures = featuresFor machines;
            passthru = { inherit spec probe; };
          }
          ''
            export HOME="$TMPDIR"
            mkdir -p "$out"
            echo "phases checked: ${checked}" > "$out/typecheck"
            # A dependency, so a sandbox that cannot run a container guest
            # fails there, in seconds and by name, before this boots one.
            ${lib.optionalString (probe != null) ''cp ${probe} "$out/probe"''}
            ${uml} run --spec ${spec} --out "$out" || true
            test -f "$out/status" || echo 1 > "$out/status"
          '';
    in
    pkgs.runCommand "uml-session-${name}"
      {
        passthru = {
          inherit attempt spec nodes;
          run = runner;
          phases = lister;
          config = checkedConfig;
        };
      }
      ''
        echo "the run is at ${attempt}"
        echo "  log:       ${attempt}/log            everything, readable"
        echo "  events:    ${attempt}/events.jsonl   everything, for a machine"
        echo "  console:   ${attempt}/console/       one file per guest"
        echo "  phases:    ${attempt}/phases.json    what each phase did"
        echo "  junit:     ${attempt}/junit.xml"
        echo "  timings:   ${attempt}/report.json"
        echo "  artifacts: ${attempt}/artifacts"

        status=$(cat ${attempt}/status)
        if [ "$status" != 0 ]; then
          echo
          echo "--- the last 50 lines of ${attempt}/log ---"
          tail -n 50 ${attempt}/log
          echo "--- end ---"
          echo
          echo "the run failed (exit $status); the paths above hold what it left" >&2
          exit 1
        fi

        mkdir -p $out
        ln -s ${attempt} $out/attempt
        # Named in a loop rather than one line each: a sink added to the
        # runner should not need an edit here to be reachable from
        # `result/`, and one that was forgotten is invisible.
        for each in log events.jsonl console phases.json junit.xml report.json artifacts; do
          ln -s ${attempt}/"$each" "$out/$each"
        done
      '';
}
