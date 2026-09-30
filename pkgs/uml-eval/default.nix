# `uml-eval`: the evaluating front door, built on nanopynix.
#
# On nanopynix's own Python set rather than nixpkgs' python3Packages,
# because nanopynix is built there and one interpreter has to hold it,
# `uml` and `uml_runner` together. `uml` and `uml-runner` are lifted into
# that set from their own pyproject.toml, as easykubenix does for `ekn`.
#
# Nothing else in this repository depends on this. `mkTest` and every
# check use the nixpkgs build of `uml`, so a consumer never
# builds nanopynix -- the sandboxed path must not evaluate anyway.
{
  pkgs,
  sources,
}:
let
  inherit (pkgs) lib;

  # The umbrella's sources, handed on, as easykubenix does: from a store
  # path nanopynix would otherwise find no umbrella and fetch its own.
  nanopynix = import sources.nanopynix { inherit pkgs sources; };

  projects = {
    uml-runner = ../uml-runner;
    uml = ../uml;
    uml-eval = ./.;
  };

  pythonSet = nanopynix.pythonSetWith {
    projectRoots = lib.attrValues projects;
    overlay =
      pySelf: _pyPrev:
      lib.mapAttrs (
        _: projectRoot:
        pySelf.callPackage (nanopynix.ps.mkProject {
          inherit projectRoot;
          inherit (nanopynix.pythonSet) python;
        }) { }
      ) projects;
  };

  app = nanopynix.mkApp {
    name = "uml-eval";
    inherit pythonSet;
  };

  # pyproject-nix builds a wheel and runs nothing, so the tests are run
  # here, in the same environment the app gets.
  tests =
    pkgs.runCommand "uml-eval-tests"
      {
        nativeBuildInputs = [ (pythonSet.mkVirtualEnv "uml-eval-test-env" { uml-eval = [ ]; }) ];
      }
      ''
        cp -r ${./tests} tests
        export HOME=$TMPDIR
        python -m pytest -p no:cacheprovider tests
        touch $out
      '';
in
app.overrideAttrs (old: {
  passthru = (old.passthru or { }) // {
    inherit tests pythonSet;
  };
})
