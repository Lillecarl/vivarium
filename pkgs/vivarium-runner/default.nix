{ lib, python3Packages }:

python3Packages.buildPythonPackage {
  pname = "vivarium-runner";
  version = "0.2.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [ ./pyproject.toml ./vivarium_runner ];
  };

  pyproject = true;

  build-system = [ python3Packages.hatchling ];
  dependencies = [
    python3Packages.rpyc
    # QEMU's own monitor client, rather than a second implementation of
    # the greeting, the capabilities handshake and the difference between
    # an event and a reply. Only the QEMU backend imports it.
    python3Packages.qemu-qmp
  ];

  pythonImportsCheck = [ "vivarium_runner" ];

  meta = {
    description = "Run NixOS systems under User-Mode Linux and drive them from Python";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "vivarium-run";
  };
}
