# The public surface of this repository, for a consumer who uses flakes.
#
# `default.nix` is how it builds. This file names what is public and calls it
# with a package set built from the flake's own nixpkgs, so a consumer who
# writes an input for this repository gets a curated set of outputs rather
# than nothing.
#
# Two inputs. nixpkgs is the one dependency. nixidae pins nothing that this
# file reads; it is in `flake.lock` because nix/sources.nix reads the nixidae
# commit from there when this repository stands alone.
{
  description = "NixOS integration tests on User-Mode Linux: no KVM, no root";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    nixidae = {
      url = "github:nixidae/nixidae";
      flake = false;
    };
  };

  outputs =
    { nixpkgs, ... }:
    let
      # x86_64 only: the guest kernel is User-Mode Linux, built from the
      # host's nixpkgs, and UML is an x86 port.
      system = "x86_64-linux";
      inherit (nixpkgs) lib;

      built = import ./. { pkgs = import nixpkgs { inherit system; }; };
    in
    {
      packages.${system} = {
        inherit (built)
          umlKernel
          lan
          forward
          fuse
          iperf
          containerd
          k8s
          check-k8s-images
          check-k8s-config
          ;
      };

      checks.${system} = built.checks;

      # mkNode and mkTest, for a consumer writing a test of its own. It takes
      # a package set, so the caller decides which nixpkgs the guests use.
      lib = import ./lib.nix;

      nixosConfigurations.demo = built.demo;

      apps.${system} = {
        speedtest = {
          type = "app";
          meta.description = "Run speedtest-cli inside a UML guest";
          program = lib.getExe built.speedtest;
        };
      };
    };
}
