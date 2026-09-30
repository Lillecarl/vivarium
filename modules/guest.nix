# The guest system: what a NixOS configuration has to look like to come
# up as a UML process and be drivable from the host.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.vivarium;

  # "192.168.99.2/24" -> { address = "192.168.99.2"; prefixLength = 24; }
  parseCidr =
    cidr:
    let
      parts = lib.splitString "/" cidr;
    in
    {
      address = lib.elemAt parts 0;
      prefixLength = lib.toIntBase10 (lib.elemAt parts 1);
    };
in
{
  boot.kernelPackages = lib.mkDefault pkgs.linuxPackages_latest;

  # Under UML there is no firmware, no bootloader and no initrd: the
  # kernel jumps straight into /init on the root image (see image.nix),
  # nothing is modular, and there is no /lib/modules. A QEMU guest boots
  # the host's own kernel, where every virtio driver is a module, so it
  # keeps the initrd and the modprobe script. See qemu.nix.
  boot.initrd.enable = lib.mkIf (cfg.backend == "uml") false;
  boot.loader.grub.enable = false;
  boot.loader.systemd-boot.enable = false;
  system.build.installBootLoader = lib.getExe' pkgs.coreutils "true";
  # A container has no modules either, and NixOS defines no modprobe
  # script under `boot.isContainer`, so the entry must still get a text.
  system.activationScripts.modprobe.text = lib.mkIf (cfg.backend != "qemu") (
    lib.mkForce ""
  );

  /*
    No bind of /nix/store on top of /nix.

    NixOS binds /nix/store onto itself in stage 2, to give it `ro,nodev,
    nosuid`.  Here /nix is one overlay on purpose -- see image.nix -- and
    that bind puts /nix/store back inside it as a mount of its own, which
    is the shape image.nix exists to avoid.  A pod that binds the node's
    /nix through a kubelet `subPath` then sees an empty store.

    Asking for no options is what stops the bind: stage 2 makes it only
    when an option it wants is missing.

    A guest is a test fixture with one user, and its store is the host's
    over hostfs -- read-only there, whatever this says.  `ro` would be
    wrong in any case, because activation writes to the overlay.
  */
  boot.nixStoreMountOpts = lib.mkForce [ ];

  /*
    The guest's Nix state is its own, never the host's.

    `/nix` is an overlay over the host's `/nix`, so without this the lower
    layer contributes the host's `/nix/var` -- including
    `db/big-lock`, which is `root:root 0600`. The guest is root, but the
    process serving the store to it is not: virtiofsd under QEMU, the UML
    process under UML, both running as whoever started the test. So the
    copy-up fails and Nix reports `opening lock file ...: Permission
    denied`, or the registration load fails one step earlier.

    Inside a Nix build sandbox the question never comes up, because `/nix`
    there holds nothing but `store`. That is exactly why it was worth
    fixing: without this the sandboxed and unsandboxed runs differ, and
    the one that breaks is the one you reach for while iterating.

    A bind from the root disk rather than a tmpfs, because the database
    for a large closure is megabytes and a tmpfs charges them to the RAM
    the guest is running in.

    This does not split the store. `/nix/store` stays inside the `/nix`
    overlay, so a pod binding the node's `/nix` through a kubelet
    `subPath` still sees it -- see modules/image.nix. Only `/nix/var`
    would be missing from such a bind, and nothing asks for it.
  */
  fileSystems."/nix/var" = {
    device = "/nix-state";
    fsType = "none";
    options = [ "bind" ];
    depends = [ "/nix" ];
  };

  # vec0 is the passt uplink (NAT plus the forwarded ssh port); vec1, if
  # this guest is on a segment, is an L2 link to its peers.
  networking = {
    useNetworkd = true;
    useDHCP = false;
    dhcpcd.enable = false;
    # The guest is only reachable through passt's forwards, and a test
    # wants to see what a service does, not what a firewall did to it.
    firewall.enable = lib.mkDefault false;
    /*
      A resolver named here, and not whatever the uplink advertises.

      passt tells the guest to use the host's first nameserver, which on a
      systemd-resolved host is the 127.0.0.53 stub -- an address that inside
      the guest means the guest.  So the guest resolves nothing, and says so
      as `lookup registry.k8s.io: no such host` several minutes into a test.
      Every guest CI job failed that way on a GitHub runner while the same
      code passed on a laptop, because a laptop's resolved has public
      fallbacks and a runner's reaches none of them.

      Deriving the right address from the host was two rounds of CI and still
      wrong.  A constant has no host in it to get wrong.  Two of them, so one
      resolver refusing the runner's traffic is not the end of the run.

      `mkDefault`, so a test on a network that resolves neither can say so.
    */
    nameservers = lib.mkDefault [
      "1.1.1.1"
      "8.8.8.8"
    ];
    interfaces.vec0.useDHCP = true;
    interfaces.vec1 = lib.mkIf (cfg.lan.network != null) (
      {
        useDHCP = false;
      }
      // lib.optionalAttrs (cfg.lan.address != null) {
        ipv4.addresses = [ (parseCidr cfg.lan.address) ];
      }
    );
  };
  # Take the address and the route from DHCP, not the resolver.  passt
  # advertises one, resolved adds it to the link, and a link server is
  # queried beside the global ones above -- so whichever answers first
  # decides, which is the non-determinism `nameservers` is here to remove.
  # All three, because passt advertises a resolver over DHCPv4, over
  # DHCPv6 and in a router advertisement, and one of them left open puts
  # the address back on the link.
  systemd.network.networks."40-vec0" = {
    dhcpV4Config.UseDNS = false;
    dhcpV6Config.UseDNS = false;
    ipv6AcceptRAConfig.UseDNS = false;
  };

  # vec0 gets its address from passt within a second; blocking boot on a
  # 90s timeout only ever makes tests slower.
  systemd.network.wait-online.enable = false;

  # ttyS0 belongs to the agent below -- a getty on it would eat the RPC
  # frames.  Nothing is attached to tty1 either, so that getty only ever
  # fails.  The rest is weight a throwaway guest has no use for.
  systemd.services."serial-getty@".enable = false;
  systemd.services."serial-getty@ttyS0".enable = false;
  # Disabling the template also disables the autovt@ alias logind spawns.
  systemd.services."getty@".enable = false;
  systemd.services.resolvconf.enable = false;
  systemd.services.systemd-random-seed.enable = false;
  systemd.services.nsncd.enable = false;
  services.logrotate.enable = false;
  documentation.enable = false;
  documentation.nixos.enable = false;
  # No security.enableWrappers = false here.  It was weight worth dropping
  # until it turned out what it drops.  NixOS runs pam_unix's shadow lookup
  # through a setuid unix_chkpwd -- see security.wrappers in
  # nixos/modules/security/pam.nix -- so without the wrapper directory
  # pam_unix cannot read /etc/shadow and account management returns
  # PAM_AUTHINFO_UNAVAIL for every user.  su and runuser shrug that off as
  # root; sudo treats it as fatal and says "authentication service cannot
  # retrieve authentication info", which names neither PAM nor the wrapper
  # and sends you looking at the user database instead.
  #
  # Measured rather than assumed, because a setuid bit on a guest whose
  # store is hostfs under an overlay is a fair thing to doubt:
  # /run/wrappers is a tmpfs of its own, mounted rw,nodev,relatime and not
  # nosuid, so the bit is set and honoured.  It costs that tmpfs and about
  # 720 KB of wrappers.

  /*
    No io_uring in a UML guest.  UML's memory manager cannot host the
    rings `io_uring_mmap` installs: a process that uses one prints
    `BUG: Bad page map ... file:[io_uring]` once per ring page and then
    takes the guest down, which the host sees as a command that never
    returns.  Measured on pynixd's suite, which forces uvloop.

    The sysctl, and not the `UV_USE_IO_URING=0` that stood here until it
    was measured to do nothing.  libuv reads that variable only on the
    SQPOLL path, and there as an opt-in.  The ring that panics is the one
    `uv__platform_loop_init` maps with flags 0 on every event loop, and
    libuv maps that one unconditionally since 1.50.0 -- "always use
    io_uring for epoll batching".  The old note was true when it was
    written and went stale with no error, because nothing checks that an
    environment variable changed anything.

    2 is "off for everyone": `io_uring_setup` returns -EPERM, and libuv
    gives up before it maps anything.  2 and not 1, because 1 still
    allows `CAP_SYS_ADMIN`.  Guest-wide rather than libuv-only, which is
    what pkgs/uml-kernel wanted and could not get -- `config IO_URING` is
    `bool if EXPERT`, and an EXPERT allnoconfig kernel never reaches
    init.

    The knob is 0644 and takes 0 to 2, so root in a guest can set it back
    to 0 and panic the guest again.  `CONFIG_IO_URING=n` would be the
    absolute version, if the config could say it.
  */
  boot.kernel.sysctl."kernel.io_uring_disabled" = lib.mkIf (cfg.backend == "uml") 2;

  users.mutableUsers = false;
  users.users.root.initialPassword = cfg.rootPassword;

  services.openssh = {
    enable = lib.mkDefault true;
    ports = [ cfg.sshPort ];
    startWhenNeeded = false;
    settings = {
      PermitRootLogin = "yes";
      PasswordAuthentication = true;
    };
  };

  # The host's end of this is a socketpair, not a terminal, so the agent
  # is reachable before networking exists and inside a build sandbox.
  # Its "ready" line on the console is what the runner waits for.
  #
  # The device differs by backend -- ttyS0 under UML, hvc0 under QEMU,
  # where ttyS0 carries the console instead -- so the unit is ordered
  # against whichever one this guest got.
  #
  # A container's agent listens on a socket and has no device unit to wait
  # for; one named anyway never appears, and the agent never starts.
  systemd.services.uml-agent = let
    device = lib.removePrefix "/dev/" cfg.agentDevice;
    units = lib.optional (lib.hasPrefix "/dev/" cfg.agentDevice) "dev-${device}.device";
  in {
    description = "Host control channel on ${cfg.agentDevice}";
    wantedBy = [ "multi-user.target" ];
    after = units;
    bindsTo = units;
    environment.UML_AGENT_DEVICE = cfg.agentDevice;
    serviceConfig = {
      ExecStart = lib.getExe' config.system.build.umlRunnerPackage "uml-agent";
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
  };

  /*
    Before the agent, so the stream is running by the time the host sees
    "ready" and sends its first command. `--boot` carries everything
    logged before this unit started.

    `--output-fields` keeps an entry to what a reader filters on; the
    cursor and timestamps journalctl always adds come along anyway.
  */
  systemd.services.uml-journal = lib.mkIf cfg.journal {
    description = "Stream the journal to the host";
    wantedBy = [ "multi-user.target" ];
    before = [ "uml-agent.service" ];
    after = [ "systemd-journald.service" ];
    unitConfig.RequiresMountsFor = "/artifacts";
    serviceConfig = {
      ExecStart = lib.escapeShellArgs [
        "${config.systemd.package}/bin/journalctl"
        "--follow"
        "--boot"
        "--output=json"
        "--output-fields=MESSAGE,PRIORITY,_SYSTEMD_UNIT,SYSLOG_IDENTIFIER,_PID,_TRANSPORT"
      ];
      StandardOutput = "truncate:/artifacts/journal.jsonl";
    };
  };

  /*
    No unit for the Nix database.

    There was one, and it loaded a registration at every boot.  The
    database is built with the image now -- see `system.build.
    umlNixDatabase` -- and sits on it under `/nix-state`, which the bind
    above puts at `/nix/var`.  So it is there before pid 1, and the check
    that it is there belongs to the image derivation, where a missing file
    stops a build rather than a boot.

    Nothing should order against "the store is usable" any more.  It is
    usable as soon as `/nix/var` is mounted, which is `local-fs.target`,
    which every ordinary service is already after.
  */

  system.stateVersion = lib.mkDefault "25.05";
}
