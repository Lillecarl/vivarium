# The workflows of ./workflows.nix, as values.
#
# nixidae renders each one to `.github/workflows/vivarium-<name>.yml`, with
# vivarium as the working directory of every step, and checks the committed
# file against the value. Tests run only in nixidae. nixidae/nixidae#4.
{
  lib,
  sources,
}:
let
  # ghanix is the schema: it takes `lib` and nothing else, so it costs this
  # repository no dependency it did not already have. Before it, ci/lib.nix
  # carried its own copy of the checkout, the Nix install, cachix, the
  # user-namespace sysctls and the /dev/kvm rule -- the same five steps
  # nixkube and nanopynix each had a copy of.
  ghanix = import sources.ghanix { inherit lib; };

in
{
  workflows = import ./workflows.nix {
    inherit lib;
    ghalib = ghanix;
  };
}
