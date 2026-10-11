# By hand: the SUSE round-trip. GNU hello configured, compiled and
# packaged by buildRPM inside an openSUSE Leap toolchain image, then
# installed with real dependency resolution and executed -- all on
# SUSE userspace, no KVM. Needs the network, so never a check.
#
# Deliberately NOT AutoReqProv:no (compare the check's bare-image
# hello spec): the image's own database names the providers, so the
# generated Requires must resolve at install time. No
# `diskImageFormat` either: both images are directories, which the
# port reads as qcow2 by default.
{ sources ? import ../nix/sources.nix
, pkgs ? import sources.nixpkgs { }
}:
let
  vivarium = import ../lib.nix {
    inherit pkgs;
    inherit (pkgs) lib;
  };
  toolchain = import ./mini-suse.nix {
    inherit sources pkgs;
    extraPackages = [
      "gcc"
      "make"
      "rpm-build"
      "python-rpm-macros"
      "binutils"
      "sed"
      "gawk"
      "tar"
      "gzip"
      "findutils"
    ];
  };
  version = pkgs.hello.version;
  helloSpec = pkgs.writeText "hello-suse.spec" ''
    Name: hello
    Version: ${version}
    Release: 1
    Summary: The GNU hello program
    License: GPL-3.0-or-later
    Source0: hello-${version}.tar.gz

    %description
    The GNU hello program prints a friendly greeting.

    %prep
    %setup -q

    %build
    ./configure --prefix=/usr
    make

    %install
    make install DESTDIR=%{buildroot}

    %files
    /usr/bin/hello
    /usr/share/info/hello.info*
    /usr/share/locale/*/LC_MESSAGES/hello.mo
    /usr/share/man/man1/hello.1
  '';
  helloSrc = pkgs.runCommand "hello-${version}.tar.gz" {
    buildInputs = [
      pkgs.gnutar
      pkgs.gzip
    ];
  } ''
    mkdir -p pkg
    tar -xzf ${pkgs.hello.src} -C pkg
    cp ${helloSpec} pkg/hello-${version}/hello.spec
    tar -czf $out -C pkg hello-${version}
  '';
  built = vivarium.umlVmTools.buildRPM {
    diskImage = toolchain;
    name = "uml-suse-built-hello";
    src = helloSrc;
    preBuild = vivarium.umlVmTools.bareImageRpmMacros + ''
      # SUSE's texinfo drags the whole TeX universe (600MB+); hello's
      # install only wants the dir index updated, so stub it out.
      printf '#!/bin/sh\nexit 0\n' > /usr/bin/install-info
      chmod +x /usr/bin/install-info
    '';
    postBuild = ":";
    preInstall = ":";
    postInstall = ":";
    extraRPMs = "";
  };
in
vivarium.umlVmTools.runInLinuxImage (
  pkgs.runCommand "suse-hello-roundtrip" {
    diskImage = toolchain;
    memSize = 1024;
  } ''
    /usr/bin/rpm -iv ${built}/rpms/opensuse-leap-15.6-x86_64/hello-${version}-1.x86_64.rpm
    test "$(/usr/bin/hello)" = "Hello, world!"
    echo ok > $out
  ''
)
