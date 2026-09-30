/*
  Give the guest the host's whole store as a Nix store it can build into.

  `vivarium.nixDatabase` registers one closure, which is enough for a guest
  that runs the programs a test put in it. It is not enough for a guest that
  runs Nix: everything else on the host is right there under `/nix/store`,
  readable, and invalid as far as Nix is concerned.

  Nix's own answer to this shape is the local-overlay store, and the shape is
  already here: `/nix` is an overlay of the host's `/nix` under a writable
  layer -- see modules/image.nix. So the lower store is the host's, read-only;
  the upper layer is the overlay's upper directory; and the merged store
  directory is the guest's ordinary `/nix/store`.

  ONLY OUTSIDE THE BUILD SANDBOX. A sandbox `/nix` holds `store` and nothing
  else, so there is no host database to read. The unit below says so on the
  console rather than letting Nix report a permission error on a lock file.

  Measured, and the reason this is off by default: `read-only=true` opens the
  database with SQLite's `immutable` parameter, which ignores the write-ahead
  log. So the guest sees the host's store as of its last WAL checkpoint. A
  path added to the host seconds earlier reads as `is not valid` in here.
*/
{
  config,
  lib,
  ...
}:
let
  cfg = config.vivarium;

  # Nested store URLs go in a parameter, so their own separators have to
  # stop being separators.
  lowerStore = "local%3Froot%3D%2Fhost%26read-only%3Dtrue";

  /*
    `real` and `state` keep their defaults -- `/nix/store`, and the
    `/nix/var/nix` that guest.nix binds from the guest's own disk.

    `check-mount=false` because Nix looks for an overlay mounted on the
    store directory, and this one is mounted on `/nix` instead. That is
    deliberate and modules/image.nix says why: splitting it would give a
    kubelet `subPath` bind an empty store.
  */
  storeUrl = lib.concatStringsSep "&" [
    "local-overlay://?upper-layer=/.nix-upper/store"
    "check-mount=false"
    "lower-store=${lowerStore}"
  ];
in
{
  options.vivarium.hostStore.enable = lib.mkEnableOption ''
    a Nix store in the guest whose lower layer is the host's whole store.

    Needs the guest to run outside a Nix build sandbox, because a sandbox
    does not have the host's Nix database in it
  '';

  config = lib.mkIf cfg.hostStore.enable {
    nix.settings = {
      # Two features, not one: the overlay store itself, and the
      # read-only opening of its lower store.
      experimental-features = [
        "nix-command"
        "local-overlay-store"
        "read-only-local-store"
      ];
      store = storeUrl;
    };

    # `nix.checkConfig` runs the guest's `nix` against this configuration at
    # build time, where `/host` does not exist.
    nix.checkConfig = false;

    systemd.services.vivarium-host-store = {
      description = "Check the host's Nix database is readable";
      wantedBy = [ "multi-user.target" ];
      before = [ "multi-user.target" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        if [ ! -e /host/nix/var/nix/db/db.sqlite ]; then
          echo "no /host/nix/var/nix/db/db.sqlite, so the host's store cannot" >&2
          echo "be the lower layer -- vivarium.hostStore needs a guest that" >&2
          echo "runs outside a Nix build sandbox" >&2
          exit 1
        fi
      '';
    };
  };
}
