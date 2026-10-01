# mkNode and mkTest, as a library.
#
# These used to live in `flake.nix`'s `let`, where nothing outside this
# repository could reach them. A caller with its own guests and its own script
# is the point of the exercise -- easykubenix drives `ekn kubeapply` against a
# cluster this builds -- so they are a file that takes a package set.
#
#     let vivarium = import (sources.vivarium + "/lib.nix") { inherit pkgs; };
#     in vivarium.mkTest { name = "..."; script = ./mine.py; nodes = { ... }; }
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

        python3.withPackages (_: [ vivarium.runner ])
  */
  runner = pkgs.callPackage ./pkgs/vivarium-runner { };

  /**
    The session, and the `vivarium` CLI that drives one.

    The redesign lives here; `runner` above is the mechanism it uses and
    is not going away. See `docs/design/history/running-anywhere.md`.
  */
  session = pkgs.callPackage ./pkgs/vivarium { vivarium-runner = runner; };

  /*
    pyright over a caller's test scripts, against this library.

        typeCheck { scripts = [ ./tests/vivarium/run.py ]; }

    `mkTest` calls this itself -- see `vivarium.typeCheck`.  Call it
    directly for scripts that are not a test's.
  */
  typeCheck =
    {
      name ? "vivarium-test-scripts",
      scripts,
      extraPackages ? [ ],
      strict ? false,
      ignore ? [ ],
      # Directories of helper modules the scripts import; `mkTest`'s
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
          echo "the scripts above do not type check against vivarium_runner" >&2
          exit 1
        }
        cp report.json $out
      '';

  /**
    How one guest becomes a line in a spec.

    One place, so a field added here reaches every spec.
  */
  machineSpec =
    machine:
    {
      name = machine.networking.hostName;
      backend = machine.vivarium.backend;
      index = machine.vivarium.index;
      memory = machine.vivarium.memory;
      seccomp = machine.vivarium.seccomp;
      cpus = machine.vivarium.cpus;
      sshPort = machine.vivarium.sshPort;
      mtu = machine.vivarium.mtu;
      network = machine.vivarium.lan.network;
      address = machine.vivarium.lan.address;
      interfaces = machine.vivarium.nics;
      forward = machine.vivarium.forward;
      # Both backends get a read-only root image of `vivarium.diskSize`
      # and a per-run copy-on-write layer over it. Only what is inside
      # differs: UML boots `/init` from it, QEMU mounts it as `/`.
      image = "${machine.system.build.vivariumRootImage}";
      toplevel = "${machine.system.build.toplevel}";
      configurations = lib.mapAttrs (_: system: "${system}") machine.system.build.vivariumConfigurations;
    }
    // lib.optionalAttrs (machine.vivarium.backend == "qemu") {
      boot = machine.system.build.qemuBoot;
    }
    // lib.optionalAttrs (machine.vivarium.backend == "container") {
      boot = machine.system.build.containerBoot;
    };

  /**
    The host-side binaries a run needs, and none of the other backend's.

    Naming a store path is what builds it. A QEMU run that mentioned
    `umlKernel` would spend half an hour on a kernel it never boots, and a
    UML run that mentioned `qemu_kvm` would pull QEMU into a sandbox that
    has no use for it.

    Taken from the machines and not from the run's `backend`: a node may
    set its own `vivarium.backend`, and a run then holds both kinds. The
    runner picks a backend per machine, and a segment carries raw frames
    that both accept, so nothing else has to know.
  */
  toolchainFor =
    machines:
    let
      on = backend: lib.filter (machine: machine.vivarium.backend == backend) machines;
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
    sandbox build does not have (measured, see Area 8 of the design).

    QEMU needs `uid-range` too. virtiofsd serves a file as the guest user
    that made it, and in a sandbox that maps one uid it cannot become
    uid 1000 (EINVAL, measured: `artifacts.qemu`). UML asks for nothing,
    which is the whole point of UML.
  */
  featuresFor =
    machines:
    let
      any = backend: lib.any (machine: machine.vivarium.backend == backend) machines;
    in
    lib.optional (any "qemu") "kvm"
    ++ lib.optional (any "qemu" || any "container") "uid-range";

  /**
    Whether this sandbox can run a container guest, answered in seconds.

    The runner's own host checks (`vivarium_runner.container.probe`), in a
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
    pkgs.runCommand "vivarium-container-probe${lib.optionalString tun "-tun"}"
      {
        nativeBuildInputs = [ (runner.pythonModule.withPackages (_: [ runner ])) ];
        requiredSystemFeatures = [ "uid-range" ];
      }
      ''
        set -o pipefail
        python -m vivarium_runner.crun_launch probe ${lib.optionalString tun "--tun"} | tee $out
      '';

  # The probe a set of machines needs, or null when none is a container.
  probeFor =
    machines:
    let
      containers = lib.filter (machine: machine.vivarium.backend == "container") machines;
    in
    if containers == [ ] then
      null
    else
      containerProbe {
        tun = lib.any (machine: machine.vivarium.nics != [ ]) containers;
      };

  /*
    Every peer's segment addresses in each guest's /etc/hosts: each as
    `<host>.<segment>`, and the `vec1` address as the bare hostname too,
    as nixos-test writes it from its `nodes`.
  */
  peersModule =
    { lib, nodes, ... }:
    let
      bare = cidr: lib.head (lib.splitString "/" cidr);
    in
    {
      networking.hosts = lib.mkMerge (
        lib.concatLists (
          lib.mapAttrsToList (
            _: peer:
            let
              host = peer.networking.hostName;
              address = peer.vivarium.lan.address;
            in
            lib.optional (address != null) { ${bare address} = [ host ]; }
            ++ lib.concatMap (
              nic: map (cidr: { ${bare cidr} = [ "${host}.${nic.segment}" ]; }) nic.addresses
            ) peer.vivarium.nics
          ) nodes
        )
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

  # nixos-test's shape, mapped onto mkTest; see nixos-test.nix.
  fromNixosTest = import ./nixos-test.nix { inherit pkgs lib mkTest; };

  /**
    A run: guests, and the phases that drive them.

        mkTest {
          name = "mine";
          nodes.one = { };
          phases.check.script = ./check.py;
        }

    It takes a module, so a recipe can contribute a phase, the guest
    configuration that phase needs and the knobs it reads in a single
    import -- and a consumer overrides any of it the way they override a
    NixOS option.

    One program, run several ways. The derivation itself is the
    sandboxed run, which CI builds, and these come with it:

        .driver             the same run by hand, `--out` where you want it
        .driverDebug        the same, paused on the first failure
        .driverInteractive  a REPL, paused before the first phase, with
                            `interactive` merged in
        .phases             what would run, in order, without booting
        .nodes, .config     the evaluated guests and run
        .extend { modules; }  the run with more modules
        .uml, .qemu, .container  every guest on that backend

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
  mkTest =
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

      settingsFile = pkgs.writeText "vivarium-${name}-settings.json" (builtins.toJSON checkedConfig.settings);

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
            vivarium.sshPort = lib.mkDefault (4325 + index);
            vivarium.backend = lib.mkDefault backend;
            vivarium.index = index;
            vivarium.nixDatabase.extraRoots = lib.optional (checkedConfig.settings != { }) "${settingsFile}";
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
          typing = first.vivarium.typeCheck;
        in
        lib.optionalString typing.enable "${typeCheck {
          name = "${name}-phases";
          scripts = map (
            phase: if phase.pytest != null then phase.pytest.tests else phase.script
          ) checkedConfig.ordered;
          extraPaths = checkedConfig.pythonPath;
          inherit (typing) extraPackages strict ignore;
        }}";

      spec = pkgs.writeText "vivarium-${name}-spec.json" (
        builtins.toJSON (
          toolchainFor machines
          // {
            inherit (checkedConfig) name settings;
            unshare = "${lib.getBin pkgs.util-linux}/bin/unshare";
            # The runner that knows every field below. No cycle: the
            # package does not depend on any spec.
            vivarium = "${session}";
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
            # Each guest's closure, which the runner turns into its store
            # view. The same closureInfo its Nix database is loaded from.
            # Not for `vivarium.hostStore`, whose point is the host's whole
            # store and its database under the guest's own.
            machines = map (
              machine:
              machineSpec machine
              // lib.optionalAttrs (!machine.vivarium.hostStore.enable) {
                storePaths = "${machine.system.build.vivariumNixRegistration}/store-paths";
              }
            ) machines;
          }
        )
      );

      vivarium = lib.getExe session;

      /*
        The run outside the sandbox.

        `--out` is required and not defaulted. `lib.nix` has held since
        the beginning that a run by hand records only where it is told
        to, so that nothing writes to a directory nobody chose -- and
        being told is now cheap, because it is one flag rather than an
        environment variable nobody remembers.
      */
      /*
        `vivarium` with this run's spec and some flags baked in: a binary
        wrapper, no shell. The type check is named in its environment,
        which makes it a dependency: the by-hand door is where a type
        error gets written, so it must not skip the check.
      */
      wrap =
        program: command: flags:
        pkgs.runCommand program
          {
            nativeBuildInputs = [ pkgs.makeBinaryWrapper ];
            meta.mainProgram = program;
          }
          ''
            makeWrapper ${vivarium} $out/bin/${program} \
              --add-flags ${lib.escapeShellArg "${command} --spec ${spec} ${flags}"} \
              --set VIVARIUM_TYPECHECKED ${lib.escapeShellArg checked}
          '';

      # A run by hand that exits on the first failure, as the check does.
      driver = wrap "vivarium-driver-${name}" "run" "";
      # The same, paused on the first failure with the guests up. The MCP
      # server starts this one.
      driverDebug = wrap "vivarium-driver-debug-${name}" "run" "--break-on-failure";
      # nixos-test's REPL: paused before the first phase; see vivarium/repl.py.
      # `.driverInteractive` is this, from the run with `interactive`
      # merged in.
      driverInteractiveHere = wrap "vivarium-driver-interactive-${name}" "run" "--interactive";
      lister = wrap "vivarium-phases-${name}" "phases" "";

      extend =
        { modules }:
        mkTest {
          imports = [ module ] ++ modules;
        };
      # Every guest on one backend, whatever its own configuration says.
      onBackend = backend: extend { modules = [ { defaults.vivarium.backend = lib.mkForce backend; } ]; };

      /*
        The run inside one, which never fails.

        Nix deletes the output of a derivation that fails, so a run that
        reports failure by failing throws away the evidence of the one run
        anybody wanted to read. This always succeeds and writes what
        happened to `status`; the check below fails, and reads nothing but
        that file.

        The same `vivarium run` the developer gets, with `--out` pointed at the
        derivation's own output. Nothing branches on being in a sandbox,
        which is what stops the two drifting. `--no-control` is the one
        flag it adds: nobody can reach in here, and a socket left in
        `$out` by a killed run would fail the output.
      */
      probe = probeFor machines;
      attempt =
        pkgs.runCommand "vivarium-session-${name}-attempt"
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
            ${vivarium} run --spec ${spec} --out "$out" --no-control || true
            test -f "$out/status" || echo 1 > "$out/status"
          '';
    in
    pkgs.runCommand "vivarium-session-${name}"
      {
        passthru = {
          inherit
            attempt
            spec
            nodes
            driver
            driverDebug
            extend
            driverInteractiveHere
            ;
          driverInteractive = (extend { modules = [ checkedConfig.interactive ]; }).driverInteractiveHere;
          uml = onBackend "uml";
          qemu = onBackend "qemu";
          container = onBackend "container";
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
