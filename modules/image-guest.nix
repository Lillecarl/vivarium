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
    mountpoint -q /nix || mount -t hostfs none /nix -o "hostfs=$(arg VIVARIUM_STORE)"
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
    system.build.vivariumImageClosure = pkgs.closureInfo { rootPaths = [ runner units ]; };
  };
}
