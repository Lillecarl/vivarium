# The run itself, as a module.
#
# A guest is a NixOS configuration and always was. This is the other half:
# the run that drives them -- which phases there are, what order they go
# in, and what may be told to them from outside.
#
# It is a module and not a function argument so that a **recipe** can be
# one thing. A recipe contributes a phase, the guest configuration that
# phase needs, and the knobs it reads, in one import -- and a consumer
# reorders it, replaces its script or drops it with `lib.mkForce`, the way
# they override any other option.
#
# See docs/design/history/running-anywhere.md.
{
  config,
  lib,
  ...
}:
let
  inherit (lib) mkOption types;

  enabled = lib.filterAttrs (_: phase: phase.enable) config.phases;

  named = lib.mapAttrsToList (name: phase: phase // { inherit name; }) enabled;

  /*
    `a` must run before `b` when `b` names `a`.

    `lib.toposort` wants exactly this predicate and returns either
    `{ result }` or `{ cycle, loops }`, so a cycle between phases is an
    evaluation error naming the cycle rather than a run that deadlocks or
    quietly picks an order.
  */
  sorted = lib.toposort (a: b: lib.elem a.name b.after) named;

  /*
    An `after` that names nothing is the silent failure this guards.

    A typo there does not stop anything. The phase simply has no
    dependency, so a failure upstream does not skip it, and it runs
    against a world that was never built -- reporting a second failure
    that has nothing to do with the first. Checked here, where the answer
    is a message and not a mystery.
  */
  unknown = lib.unique (
    lib.concatMap (phase: lib.subtractLists (lib.attrNames enabled) phase.after) named
  );

  # A misspelled guest would otherwise be a phase holding nothing, which
  # runs beside everything.
  strangers = lib.unique (
    lib.concatMap (phase: lib.subtractLists (lib.attrNames config.nodes) phase.nodes) named
  );

  # Neither is a phase that does nothing and passes; both is a guess.
  ambiguous = map (phase: phase.name) (
    lib.filter (phase: (phase.script == null) == (phase.pytest == null)) named
  );
in
{
  options = {
    name = mkOption {
      type = types.str;
      description = "What this run is called, in derivation names and logs.";
    };

    nodes = mkOption {
      type = types.attrsOf types.deferredModule;
      default = { };
      description = ''
        The guests, by hostname. Each is an ordinary NixOS module.

        A recipe adds to these rather than replacing them, so importing
        one brings the configuration its phase needs with it.
      '';
    };

    defaults = mkOption {
      type = types.deferredModule;
      default = { };
      description = ''
        A NixOS module every guest imports, beside its own entry in
        `nodes`. A reusable module goes here. An option that must differ
        between guests takes no default in it, so each guest sets it or
        evaluation fails.
      '';
    };

    interactive = mkOption {
      type = types.deferredModule;
      default = { };
      description = ''
        A module merged into this run for `.driverInteractive` only, as
        nixos-test's `interactive`: a debug package on a guest, a longer
        timeout. It may set anything the run itself sets.
      '';
    };

    backend = mkOption {
      type = types.enum [
        "uml"
        "qemu"
        "container"
      ];
      default = "uml";
      description = ''
        What the guests become. `uml` needs nothing of the host; `qemu`
        needs /dev/kvm and is much faster. A phase script never knows
        which it got, and neither does a node configuration.
      '';
    };

    phases = mkOption {
      default = { };
      description = ''
        The work, by name. Each phase is a Python module exporting one
        `test` coroutine or a pytest run, and the runner runs them in the
        order worked out from `after`.
      '';
      type = types.attrsOf (
        types.submodule (
          { name, ... }:
          {
            options = {
              enable = mkOption {
                type = types.bool;
                default = true;
                description = "Whether to run this phase at all.";
              };

              script = mkOption {
                type = types.nullOr types.path;
                default = null;
                description = ''
                  A Python module exporting `async def test(vms: Machines)`.

                  Imported, not executed as a script, so it may import
                  whatever it likes and pyright can check it. Set this or
                  `pytest`, not both.
                '';
              };

              pytest = mkOption {
                default = null;
                description = ''
                  Run pytest against the guests instead of a script.

                  Each guest is a fixture named after it and `vms` is all
                  of them. A test or fixture may be `async def` and
                  awaits `Machine` directly:

                      async def test_hostname(one: Machine) -> None:
                          assert await one.succeed("hostname") == "one"

                  Every test is an event and a JUnit case of its own, and
                  every command and journal entry names the test that
                  caused it.
                '';
                example = lib.literalExpression ''
                  { tests = ./tests/guest; args = [ "-x" ]; }
                '';
                type = types.nullOr (
                  types.submodule {
                    options = {
                      tests = mkOption {
                        type = types.path;
                        description = "A test file, or a directory of them with its conftest.py.";
                      };
                      args = mkOption {
                        type = types.listOf types.str;
                        default = [ ];
                        example = [
                          "-k"
                          "not slow"
                        ];
                        description = ''
                          Given to pytest. `vivarium run ... -- <args>` adds to
                          these by hand; a knob adds to them in a check.
                        '';
                      };
                    };
                  }
                );
              };

              after = mkOption {
                type = types.listOf types.str;
                default = [ ];
                example = [ "cluster" ];
                description = ''
                  Phases that must run first.

                  This is a real dependency and not a hint about order.
                  When one of these fails, this phase is **skipped** --
                  running it against a world that was never built gives a
                  second failure that says nothing. So name what this
                  actually needs, not what happens to come first.
                '';
              };

              nodes = mkOption {
                type = types.listOf types.str;
                default = [ ];
                example = [ "server" ];
                description = ''
                  The guests this phase uses. Empty is every guest.

                  Phases whose guests do not overlap run at the same
                  time once their `after` has finished; a phase holding
                  every guest runs alone. So a session gets parallel
                  phases by saying what each phase touches, and a
                  session that says nothing runs one phase at a time.

                  The script's `vms` holds only these guests, so a
                  phase that reaches past its declaration fails on the
                  name. A pytest phase always runs alone: pytest is not
                  reentrant in one process.
                '';
              };

              always = mkOption {
                type = types.bool;
                default = false;
                description = ''
                  Run even when something in `after` failed.

                  `after` normally means two things at once: run me later,
                  and do not bother if that failed. A phase that collects
                  evidence wants only the first. A journal is most wanted
                  on the run where something broke, and one ordered after
                  everything would otherwise be skipped by the very
                  failure it exists to explain.

                  A failure is not passed on through such a phase either,
                  so nothing after it is skipped on account of something
                  before it.
                '';
              };

              description = mkOption {
                type = types.str;
                default = name;
                description = "One line, for `vivarium phases` and the report.";
              };
            };
          }
        )
      );
    };

    knobs = mkOption {
      default = { };
      description = ''
        What this run may be told from outside, by name.

        Nix resolves each one while evaluating, so a knob can change what
        is *built* -- a phase order, a guest's memory, an image -- which
        nothing read at run time can do. Under a pure evaluation
        `builtins.getEnv` answers `""`, which is the same as unset, so a
        flake consumer and a sandboxed check both get the declared
        default with no special case.
      '';
      example = lib.literalExpression ''
        knobs.scenarios = { env = "SCENARIOS"; default = "all"; };
      '';
      type = types.attrsOf (
        types.submodule (
          { name, ... }:
          {
            options = {
              env = mkOption {
                type = types.str;
                default = "UML_${lib.toUpper (builtins.replaceStrings [ "-" ] [ "_" ] name)}";
                defaultText = lib.literalMD "`UML_` and the knob's name in upper case";
                description = "The environment variable that sets this knob.";
              };

              default = mkOption {
                type = types.str;
                default = "";
                description = ''
                  What the knob is worth when the variable is unset --
                  which is always the case inside a build sandbox, so this
                  is what the check runs.
                '';
              };

              description = mkOption {
                type = types.str;
                default = name;
                description = "One line, printed with the value at run time.";
              };
            };
          }
        )
      );
    };

    pythonPath = mkOption {
      type = types.listOf types.path;
      default = [ ];
      example = lib.literalExpression "[ ./tests/lib ]";
      description = ''
        Directories every phase script can import from, and that pyright
        reads when it checks them.

        A phase script is copied into the store on its own, so a helper
        module beside it is not beside it any more. Name the directory
        that holds the helpers here instead. Explicit on purpose: copying
        whatever directory a script sits in would copy a whole repository
        for a script at its root.
      '';
    };

    settings = mkOption {
      type = types.attrs;
      default = { };
      description = ''
        Values only Nix knows that a phase needs -- a version, an image
        tag, a store path.

        A store path in here is a dependency like any other: the JSON
        carries its context, so the derivation builds it and the guest
        reads it from the host's store.
      '';
    };

    resolved = mkOption {
      internal = true;
      readOnly = true;
      type = types.attrs;
      description = "Each knob's value and where the value came from.";
    };

    ordered = mkOption {
      internal = true;
      readOnly = true;
      type = types.listOf types.attrs;
      description = "The enabled phases, sorted.";
    };

    # Declared here rather than imported: this is `evalModules`, not a
    # NixOS system, so nothing else brings the option in and nothing else
    # evaluates it. `lib.nix` is what turns a false one into an error.
    assertions = mkOption {
      internal = true;
      default = [ ];
      type = types.listOf (
        types.submodule {
          options = {
            assertion = mkOption { type = types.bool; };
            message = mkOption { type = types.str; };
          };
        }
      );
    };
  };

  config = {
    assertions = [
      {
        assertion = unknown == [ ];
        message =
          "uml: these phases are named in an `after` and do not exist: "
          + lib.concatStringsSep ", " unknown
          + ". A phase that depends on nothing is not skipped when its"
          + " dependency fails, so this would be silent.";
      }
      {
        assertion = strangers == [ ];
        message =
          "uml: these guests are named in a phase's `nodes` and are not in `nodes`: "
          + lib.concatStringsSep ", " strangers;
      }
      {
        assertion = ambiguous == [ ];
        message =
          "uml: each phase sets exactly one of `script` and `pytest`; these do not: "
          + lib.concatStringsSep ", " ambiguous;
      }
    ];

    resolved = lib.mapAttrs (
      _: knob:
      let
        fromEnv = builtins.getEnv knob.env;
      in
      {
        value = if fromEnv == "" then knob.default else fromEnv;
        source = if fromEnv == "" then "default" else "environment";
        inherit (knob) env;
      }
    ) config.knobs;

    ordered =
      if sorted ? cycle then
        throw (
          "uml: the phases in run '${config.name}' depend on each other in a cycle: "
          + lib.concatMapStringsSep " -> " (phase: phase.name) sorted.cycle
        )
      else
        sorted.result;
  };
}
