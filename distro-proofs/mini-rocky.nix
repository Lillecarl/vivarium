# By hand: a minimal Rocky Linux 9.6 image through the UML overlay.
#
#     nix build --file distro-proofs/mini-rocky.nix --print-build-logs
#
# Not a check: it fetches the real repository metadata and RPMs over
# the network, which a sandbox cannot promise.
{ sources ? import ../nix/sources.nix
, pkgs ? import sources.nixpkgs { }
}:
let
  vivarium = import ../lib.nix {
    inherit pkgs;
    inherit (pkgs) lib;
  };
  extended = pkgs.extend vivarium.overlays.uml-vm-tools;
  rocky = extended.vmTools.rpmDistros.rocky9x86_64;
in
extended.vmTools.makeImageFromRPMDist {
  name = "mini-rocky-9.6-x86_64";
  fullName = "Minimal Rocky Linux 9.6 (x86_64)";
  inherit (rocky) packagesLists urlPrefixes archs unifiedSystemDir;
  size = 2048;
  memSize = 1024;
  packages = [
    "bash"
    "filesystem"
    "setup"
    "glibc"
    "libgcc"
    "rpm"
    "rocky-release"
  ];
}
