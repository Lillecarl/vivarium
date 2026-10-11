# By hand: boot the minimal openSUSE image as a UML guest root and
# read its os-release. Proves a SUSE userspace runs under
# runInLinuxImage -- and that the release file is openSUSE's, not the
# installer's (see the `otherproviders` note in rpm-closure-suse.patch).
{ sources ? import ../nix/sources.nix
, pkgs ? import sources.nixpkgs { }
}:
let
  vivarium = import ../lib.nix {
    inherit pkgs;
    inherit (pkgs) lib;
  };
  suseImage = import ./mini-suse.nix { inherit sources pkgs; };
in
vivarium.umlVmTools.runInLinuxImage (
  pkgs.runCommand "suse-release-proof" {
    diskImage = suseImage;
  } ''cat /etc/os-release > $out''
)
