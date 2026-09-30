/**
  A nixos-test test, run by this runner: a literal mapping onto
  `mkTest`, kept apart from it so the function itself never learns
  nixos-test's shape.

      fromNixosTest (pkgs.path + "/nixos/tests/simple-vm.nix")

  The test module is evaluated against nixos-test's own option names,
  then mapped:

  - `nodes` and `containers` become one set of guests; a container is
    a guest on the `container` backend. `defaults` goes to every guest,
    `nodeDefaults` and `containerDefaults` to their own kind.
  - Every guest is on vlan 1 at 192.168.1.<n>, as nixos-test places it,
    and `networking.primaryIPAddress` says so.
  - `virtualisation.memorySize` and `.cores` become `vivarium.memory`
    and `.cpus`; `.diskSize`, when set, becomes `vivarium.diskSize`.
  - `virtualisation.additionalPaths` becomes
    `vivarium.nixDatabase.extraRoots`: the guest's store is the host's
    already, so what a path needs is to be registered, not copied.
  - `testScript` becomes one phase after `boot`, run through
    `vivarium_runner.nixos_test`, which gives it nixos-test's API.

  Anything else nixos-test takes (`meta`, `sshBackdoor`, ...) is
  accepted and not used. `enableOCR` is refused: nothing here reads a
  screen.
*/
{
  pkgs,
  lib,
  mkTest,
}:
test:
let
  inherit (lib) mkOption types;

  schema = {
    freeformType = types.attrsOf types.anything;
    options = {
      name = mkOption { type = types.str; };
      nodes = mkOption {
        type = types.attrsOf types.deferredModule;
        default = { };
      };
      containers = mkOption {
        type = types.attrsOf types.deferredModule;
        default = { };
      };
      defaults = mkOption {
        type = types.deferredModule;
        default = { };
      };
      nodeDefaults = mkOption {
        type = types.deferredModule;
        default = { };
      };
      containerDefaults = mkOption {
        type = types.deferredModule;
        default = { };
      };
      testScript = mkOption { type = types.either types.str (types.functionTo types.str); };
      extraPythonPackages = mkOption {
        type = types.functionTo (types.listOf types.package);
        default = _: [ ];
      };
      enableOCR = mkOption {
        type = types.bool;
        default = false;
      };
    };
  };

  t =
    (lib.evalModules {
      class = "nixosTest";
      modules = [
        schema
        test
      ];
      specialArgs = {
        inherit pkgs;
        hostPkgs = pkgs;
      };
    }).config;

  names = lib.attrNames t.nodes ++ lib.attrNames t.containers;
  address = name: "192.168.1.${toString (lib.lists.findFirstIndex (n: n == name) 0 names + 1)}";

  # What a nixos-test node may say that a plain NixOS system does not.
  compat =
    name:
    { config, ... }:
    {
      options = {
        networking.primaryIPAddress = mkOption { type = types.str; };
        virtualisation.memorySize = mkOption {
          type = types.ints.positive;
          default = 1024;
        };
        virtualisation.cores = mkOption {
          type = types.ints.positive;
          default = 1;
        };
        virtualisation.additionalPaths = mkOption {
          type = types.listOf types.package;
          default = [ ];
        };
      };
      config = {
        networking.primaryIPAddress = address name;
        vivarium.memory = "${toString config.virtualisation.memorySize}M";
        vivarium.cpus = config.virtualisation.cores;
        # NixOS declares `virtualisation.diskSize` for every system, with
        # "auto" as its default; only a number is a size.
        vivarium.diskSize = lib.mkIf (lib.isInt config.virtualisation.diskSize) config.virtualisation.diskSize;
        vivarium.nixDatabase.extraRoots = map toString config.virtualisation.additionalPaths;
        vivarium.lan = {
          network = "vlan1";
          address = "${address name}/24";
        };
      };
    };

  run = {
    inherit (t) name defaults;
    nodes =
      lib.mapAttrs (name: node: {
        imports = [
          t.nodeDefaults
          node
          (compat name)
        ];
      }) t.nodes
      // lib.mapAttrs (name: node: {
        imports = [
          t.containerDefaults
          node
          (compat name)
        ];
        vivarium.backend = "container";
      }) t.containers;
    pythonPath = map (package: "${package}/${pkgs.python3.sitePackages}") (
      t.extraPythonPackages pkgs.python3Packages
    );
  };

  # The guests alone, for a `testScript` that is a function of them. Not
  # the final run: its phase would depend on its own guests.
  script =
    if lib.isFunction t.testScript then t.testScript { inherit ((mkTest run)) nodes; } else t.testScript;

  phase = pkgs.writeText "${t.name}-testScript.py" ''
    """nixos-test's testScript for ${t.name}; see vivarium_runner/nixos_test.py."""

    from vivarium_runner import Machines
    from vivarium_runner.nixos_test import run

    SCRIPT = ${builtins.toJSON script}


    async def test(vms: Machines) -> None:
        await run(vms, SCRIPT)
  '';
in
if t.enableOCR then
  throw "${t.name}: enableOCR asks for a screen, and nothing here reads one"
else
  mkTest {
    imports = [ run ];
    phases.test = {
      script = phase;
      after = [ "boot" ];
    };
  }
