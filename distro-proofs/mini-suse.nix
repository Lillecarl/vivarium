# By hand: a minimal openSUSE Leap 15.6 image through the UML
# overlay. nixpkgs ships no SUSE distro metadata (only the
# `commonOpenSUSEPackages` name list), so this defines the distro
# inline in `rpmDistros` shape and runs it through the overlay's
# `makeImageFromRPMDist` -- the same door every other distro uses.
# Needs the network (real repository metadata and RPMs), so never a
# check. Metadata pinned by hash; the oss snapshot rotates, so
# re-read repomd.xml if the fetch 404s.
{ sources ? import ../nix/sources.nix
, pkgs ? import sources.nixpkgs { }
, extraPackages ? [ ]
}:
let
  vivarium = import ../lib.nix {
    inherit pkgs;
    inherit (pkgs) lib;
  };
  extended = pkgs.extend vivarium.overlays.uml-vm-tools;
  # GWDG mirrors the same tree as download.opensuse.org and served
  # the metadata when the origin was mid-sync; one mirror for both
  # metadata and RPMs keeps a consistent snapshot.
  mirror = "https://ftp.gwdg.de/pub/opensuse/distribution/leap/15.6/repo/oss";
  leap = {
    name = "opensuse-leap-15.6-x86_64";
    fullName = "openSUSE Leap 15.6 (x86_64)";
    packagesLists = [
      (pkgs.fetchurl {
        url = "${mirror}/repodata/72ade5926fe32d0008e6c2b8d931d47abbf209bd488d4156b3756b23734a47b6c63917f1940ee441fb363adb08daa46632d51cedfe9c4bea87a0f3fd9452e9db-primary.xml.gz";
        hash = "sha256-rC2WEP2uUEt5XNPiDNK+YUbTzk+Fe+Z9srkuPDIYf+o=";
      })
    ];
    urlPrefixes = [ mirror ];
    archs = [
      "noarch"
      "x86_64"
    ];
    unifiedSystemDir = true;
  };
in
extended.vmTools.makeImageFromRPMDist (leap // {
  size = 2048;
  memSize = 1024;
  packages = [
    "aaa_base"
    "bash"
    "filesystem"
    "glibc"
    "rpm"
    "shadow"
    "openSUSE-release"
  ] ++ extraPackages;
})
