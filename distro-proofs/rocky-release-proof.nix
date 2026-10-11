# By hand: boot the minimal Rocky image as a UML guest root and
# read its release file. Proves a real distro userspace runs under
# runInLinuxImage: the image provides /bin/sh and the release file,
# the store provides the build tools. No `diskImageFormat`: the image
# is a directory, which the port reads as qcow2 by default.
{ sources ? import ../nix/sources.nix
, pkgs ? import sources.nixpkgs { }
}:
let
  vivarium = import ../lib.nix {
    inherit pkgs;
    inherit (pkgs) lib;
  };
  rockyImage = import ./mini-rocky.nix { inherit sources pkgs; };
in
vivarium.umlVmTools.runInLinuxImage (
  pkgs.runCommand "rocky-release-proof" {
    diskImage = rockyImage;
  } ''cat /etc/rocky-release > $out''
)
