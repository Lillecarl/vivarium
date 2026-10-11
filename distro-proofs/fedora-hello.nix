# By hand: test.nix's buildHelloInFedora through the UML overlay --
# GNU hello's default phases running inside a full Fedora 43 image
# (43, because 42's repository metadata has rotted off the mirrors).
# Needs the network (full distro closure), so never a check.
{ sources ? import ../nix/sources.nix
, pkgs ? import sources.nixpkgs { }
}:
let
  vivarium = import ../lib.nix {
    inherit pkgs;
    inherit (pkgs) lib;
  };
  extended = pkgs.extend vivarium.overlays.uml-vm-tools;
in
extended.vmTools.runInLinuxImage (
  pkgs.stdenv.mkDerivation {
    pname = "hello";
    version = pkgs.hello.version;
    src = pkgs.hello.src;
    diskImage = extended.vmTools.diskImages.fedora43x86_64;
    memSize = 2048;
  }
)
