# The guest, when the backend is a container.
#
# No kernel and no disk: the runner starts this system's init under crun,
# as the user who started the run, with an overlay over the host's
# /nix/store. See vivarium_runner/container.py and Area 8 of
# docs/design/history/running-anywhere.md.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.vivarium;
in
lib.mkIf (cfg.backend == "container") {
  boot.isContainer = true;
  # isContainer shares the host's resolv.conf, which guest.nix's resolved
  # refuses. The guest resolves the way the other backends do.
  networking.useHostResolvConf = false;

  # No serial line. The runner binds a directory at /run/host/agent, and
  # the agent listens on a socket in it.
  vivarium.agentDevice = "unix:/run/host/agent/sock";

  # A Nix build's seccomp filter refuses setuid bits, and the runner says
  # so with a file (vivarium_runner.container.NO_SETUID). Skipped rather than
  # failed: without it every sandboxed boot is "degraded". By hand the
  # file is absent and the wrappers are made as usual.
  systemd.services.suid-sgid-wrappers.unitConfig.ConditionPathExists = "!/run/host/agent/no-setuid";

  # isContainer's login prompt, on the console the runner reads.
  systemd.services.console-getty.enable = false;

  # A container cannot mount these, and systemd reports the failure as a
  # degraded system (measured).
  systemd.suppressedSystemUnits = [
    "sys-kernel-debug.mount"
    "sys-kernel-tracing.mount"
  ];

  /*
    The root filesystem's starting point, which the runner copies per run.

    A directory and not a disk image: a container's root is a directory on
    the host. It holds only what must exist before the init runs; the
    store is the host's, under an overlay whose upper and work
    directories are the two here, so a guest writes to its own store.
  */
  system.build.umlRootImage = pkgs.runCommand "container-root" { } ''
    mkdir -p $out/{etc,var,root,home,artifacts,nix/store,nix/var,.nix-upper,.nix-work}
    # Without it nix-daemon.socket is skipped silently -- see image.nix.
    mkdir -p $out/nix-state/nix/daemon-socket
    ${lib.optionalString cfg.nixDatabase.enable ''
      mkdir -p $out/nix-state/nix/db
      install -m 0644 ${config.system.build.umlNixDatabase}/db.sqlite $out/nix-state/nix/db/
      install -m 0644 ${config.system.build.umlNixDatabase}/schema $out/nix-state/nix/db/
      test -s $out/nix-state/nix/db/db.sqlite''}
  '';

  # What the runner starts, named here so a path it reads is a path Nix
  # built. The same field a QEMU guest carries.
  system.build.containerBoot.toplevel = "${config.system.build.toplevel}";
}
