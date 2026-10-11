# By hand: test.nix's checkPerlInstalledInDebian through the UML
# overlay -- perl running inside a full Debian 13 image. Needs the
# network (full distro closure), so never a check.
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
  pkgs.runCommand "check-perl"
    {
      diskImage = extended.vmTools.diskImages.debian13x86_64;
      memSize = 2048;
    }
    ''
      echo Check if perl is present
      perl -v
      echo Check if perl is installed
      dpkg -l | grep 'ii *perl'
      mkdir $out
    ''
)
