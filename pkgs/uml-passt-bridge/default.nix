{ lib, rustPlatform }:

rustPlatform.buildRustPackage {
  pname = "uml-passt-bridge";
  version = "0.1.0";

  src = ./.;
  cargoLock.lockFile = ./Cargo.lock;

  meta = with lib; {
    description = "Bridge UML fd vector transport to passt for unprivileged networking";
    license = licenses.asl20;
    platforms = platforms.linux;
    mainProgram = "uml-passt-bridge";
  };
}
