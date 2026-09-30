{
  lib,
  python3Packages,
  vivarium-runner,
}:

python3Packages.buildPythonPackage {
  pname = "vivarium";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./vivarium
      ./tests
    ];
  };

  pyproject = true;

  build-system = [ python3Packages.hatchling ];
  dependencies = [
    # The mechanism: guests, backends, the agent channel. This package
    # owns the sequence and none of that.
    vivarium-runner
    python3Packages.anyio
    # The spec is input, so it is validated rather than read
    # defensively at each point of use. See vivarium/spec.py.
    python3Packages.pydantic
    # A phase may be a pytest run. See vivarium/pytest_plugin.py.
    python3Packages.pytest
  ];

  pythonImportsCheck = [ "vivarium" ];

  # The phase logic is pure, so it is checked here rather than by booting
  # a guest to find out what a failure skips. `vivarium/tests/` holds only
  # what needs no machine; anything that needs one is a test in `tests/`
  # at the root, where a guest is available.
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  meta = {
    description = "Drive a run of NixOS guests a step at a time";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "vivarium";
  };
}
