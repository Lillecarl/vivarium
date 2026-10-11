# A guest that boots another distribution's cloud image instead of
# NixOS, under UML's kernel.
#
# The node is still a NixOS evaluation, for the options every guest has
# (memory, interfaces, forwards). Its system is never built: the spec
# names the image, a cloud-init seed, and the runner package the agent
# comes from.
#
# The seed's bootcmd mounts the host's store and the artifacts over
# hostfs, then starts the same agent and journal stream a NixOS guest
# runs, from the store. cloud-init runs it in its first stage, before
# the network.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.vivarium.image;
  inherit (lib) mkOption types;

  runner = config.system.build.vivariumRunnerPackage;

  # What the guest's store view holds: the agent and its units. With a
  # writable store also every `extraRoots` path, where `mkTest` puts
  # `settings`, as a NixOS guest has them. A read-only view leaves them
  # out, for `add_closure` to bring in.
  roots = [
    runner
    units
  ]
  ++ lib.optionals cfg.writableStore config.vivarium.nixDatabase.extraRoots;
  registration = pkgs.closureInfo { rootPaths = roots; };

  # A database over `roots`, built as a NixOS guest's is (default.nix
  # `vivariumNixDatabase` says why each step is there).
  database = pkgs.runCommand "vivarium-image-nix-database" {
    nativeBuildInputs = [
      pkgs.nix
      pkgs.sqlite
    ];
  } ''
    export USER=nobody
    export NIX_REMOTE="local?root=$PWD"
    nix-store --load-db < ${registration}/registration
    sqlite3 nix/var/nix/db/db.sqlite 'UPDATE ValidPaths SET registrationTime = 0'
    sqlite3 nix/var/nix/db/db.sqlite '.dump' > db.dump
    mkdir -p $out
    sqlite3 $out/db.sqlite '.read db.dump'
    cp nix/var/nix/db/schema $out/schema
    sqlite3 $out/db.sqlite 'pragma journal_mode = wal' > /dev/null
    test -s $out/db.sqlite
  '';

  rootList = pkgs.writeText "vivarium-image-roots" (lib.concatMapStrings (root: "${root}\n") roots);

  /*
    A writable /nix: the host's view as the lower layer, a tmpfs as the
    upper. Not the root disk: it has the image's own size, and Leap's had
    no room for Nix's closure ("No space left on device", measured in
    havsorm's fleet). Guest memory is a sparse file, so the host pays
    for what Nix writes, and it is lost at poweroff, as a NixOS guest's
    store writes are.

    The database registers every path the view holds, and each root is a
    GC root. Nix's collector deletes a store entry its database does not
    know (gc.cc, "delete all paths that are invalid"), so without both a
    collection in the guest deletes the agent.
  */
  mountStore =
    if cfg.writableStore then
      ''
        if ! mountpoint -q /nix; then
          mkdir -p /run/vivarium/host-nix /run/vivarium/nix-rw /nix
          mount -t hostfs none /run/vivarium/host-nix -o "hostfs=$(arg VIVARIUM_STORE)"
          mount -t tmpfs -o mode=0755 tmpfs /run/vivarium/nix-rw
          mkdir -p /run/vivarium/nix-rw/upper /run/vivarium/nix-rw/work
          mount -t overlay overlay -o lowerdir=/run/vivarium/host-nix,upperdir=/run/vivarium/nix-rw/upper,workdir=/run/vivarium/nix-rw/work /nix
        fi
        if [ ! -e /nix/var/nix/db/db.sqlite ]; then
          mkdir -p /nix/var/nix/db /nix/var/nix/gcroots/vivarium
          install -m 0644 ${database}/db.sqlite ${database}/schema /nix/var/nix/db/
          n=0
          while read -r root; do
            n=$((n + 1))
            ln -sfn "$root" "/nix/var/nix/gcroots/vivarium/$n"
          done < ${rootList}
        fi
      ''
    else
      ''
        mountpoint -q /nix || mount -t hostfs none /nix -o "hostfs=$(arg VIVARIUM_STORE)"
      '';

  # What the agent's commands find on PATH: the distribution's own.
  guestPath = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";

  units = pkgs.linkFarm "vivarium-image-units" {
    "vivarium-agent.service" = pkgs.writeText "vivarium-agent.service" ''
      [Unit]
      Description=Host control channel on /dev/ttyS0
      After=dev-ttyS0.device vivarium-journal.service

      [Service]
      Environment=VIVARIUM_AGENT_DEVICE=/dev/ttyS0
      Environment=VIVARIUM_GUEST_PATH=${guestPath}
      ExecStart=${lib.getExe' runner "vivarium-agent"}
      StandardOutput=journal+console
      StandardError=journal+console
    '';
    # The same fields as guest.nix's, through the distribution's own
    # journalctl.
    "vivarium-journal.service" = pkgs.writeText "vivarium-journal.service" ''
      [Unit]
      Description=Stream the journal to the host
      RequiresMountsFor=/artifacts

      [Service]
      ExecStart=journalctl --follow --boot --output=json --output-fields=MESSAGE,PRIORITY,_SYSTEMD_UNIT,SYSLOG_IDENTIFIER,_PID,_TRANSPORT
      StandardOutput=truncate:/artifacts/journal.jsonl
    '';
  };

  # The runner puts the store view and the artifacts directory on the
  # kernel command line, decided at run time; the seed is built before.
  # util-linux needs `hostfs=`: the bare directory fails under fsconfig(2).
  bootcmd = ''
    arg() { tr ' ' '\n' < /proc/cmdline | sed -n "s/^$1=//p"; }
    mkdir -p /nix /artifacts
    ${mountStore}
    artifacts=$(arg VIVARIUM_ARTIFACTS)
    if [ -n "$artifacts" ]; then mountpoint -q /artifacts || mount -t hostfs none /artifacts -o "hostfs=$artifacts"; fi
    cp ${units}/* /run/systemd/system/
    systemctl daemon-reload
    systemctl start --no-block vivarium-journal.service vivarium-agent.service
  '';

  # The disk has the image's own size: there is nothing to grow into,
  # and xfs_growfs fails under UML, which fails cloud-init's network stage.
  userData = {
    resize_rootfs = false;
    growpart.mode = "off";
  }
  // cfg.userData
  // {
    bootcmd = [ bootcmd ] ++ (cfg.userData.bootcmd or [ ]);
  };

  # Each interface by its MAC, renamed as a NixOS guest names it. vec0
  # has no fixed MAC under UML, so it goes by its name.
  networkConfig = {
    version = 2;
    ethernets = {
      vec0 = {
        match.name = "vec0";
        dhcp4 = true;
        dhcp6 = true;
      };
    }
    // lib.listToAttrs (
      map (nic: {
        inherit (nic) name;
        value = {
          match.macaddress = nic.mac;
          set-name = nic.name;
          inherit (nic) addresses;
        };
      }) config.vivarium.nics
    );
  };

  # JSON is YAML, so `builtins.toJSON` writes every file cloud-init reads.
  seed =
    pkgs.runCommand "vivarium-seed-${config.networking.hostName}"
      {
        nativeBuildInputs = [
          pkgs.dosfstools
          pkgs.mtools
        ];
        userData = "#cloud-config\n" + builtins.toJSON userData;
        metaData = builtins.toJSON {
          instance-id = config.networking.hostName;
          local-hostname = config.networking.hostName;
        };
        networkConfig = builtins.toJSON networkConfig;
        passAsFile = [
          "userData"
          "metaData"
          "networkConfig"
        ];
      }
      ''
        cp $userDataPath user-data
        cp $metaDataPath meta-data
        cp $networkConfigPath network-config
        truncate -s 2M $out
        mkfs.vfat -n CIDATA $out >/dev/null
        mcopy -i $out user-data meta-data network-config ::/
      '';
in
{
  options.vivarium.image = mkOption {
    default = null;
    description = ''
      Boot another distribution's cloud image instead of NixOS. UML
      backend only. Take one from `vivarium.images`, and add to its
      cloud-init with `userData`.

      The guest runs the vivarium agent, so `succeed`, the journal
      stream and `/artifacts` work as on NixOS. `switch_to` and
      `add_closure` do not: there is no NixOS to switch.
    '';
    type = types.nullOr (
      types.submodule {
        options = {
          disk = mkOption {
            type = types.path;
            description = "The cloud image, as qcow2.";
          };
          partition = mkOption {
            type = types.ints.positive;
            description = "The partition that holds the root filesystem.";
          };
          init = mkOption {
            type = types.str;
            default = "/usr/lib/systemd/systemd";
          };
          kernelParams = mkOption {
            type = types.listOf types.str;
            default = [ ];
          };
          writableStore = mkOption {
            type = types.bool;
            default = false;
            description = ''
              Make `/nix` writable, with a Nix database over the paths the
              guest boots with, as a NixOS guest has. For a test that
              installs Nix on the image, or that runs Nix there. Writes go
              to memory, within `vivarium.memory`, and are lost at
              poweroff.

              Without it, `/nix` is the host's store view, read-only, and
              Nix in the guest cannot open a local store.

              A path `add_closure` brings in later is not in the database,
              so Nix in the guest calls it invalid, and its collector
              deletes it.
            '';
          };
          userData = mkOption {
            type = types.attrsOf types.anything;
            default = { };
            example = {
              users = [
                {
                  name = "ansible";
                  ssh_authorized_keys = [ "ssh-ed25519 ..." ];
                }
              ];
            };
            description = ''
              cloud-config, written as JSON. A `bootcmd` here runs after
              the one that starts the agent.
            '';
          };
        };
      }
    );
  };

  config = lib.mkIf (cfg != null) {
    assertions = [
      {
        assertion = config.vivarium.backend == "uml";
        message = "vivarium.image boots under UML only; this guest is ${config.vivarium.backend}.";
      }
    ];
    system.build.vivariumImageGuest = {
      inherit (cfg) partition init;
      # No initrd, so nothing has made /dev/disk/by-uuid when
      # systemd-remount-fs looks root up in fstab, and it fails the boot.
      # The kernel mounted root `rw` already. Measured on Leap 16.0.
      kernelParams = [ "systemd.mask=systemd-remount-fs.service" ] ++ cfg.kernelParams;
      seed = "${seed}";
    };
    # The agent's closure: what the guest's store view holds.
    system.build.vivariumImageClosure = pkgs.closureInfo {
      rootPaths = roots ++ lib.optionals cfg.writableStore [
        database
        rootList
      ];
    };
  };
}
