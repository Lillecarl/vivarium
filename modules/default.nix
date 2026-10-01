# Makes a NixOS configuration bootable under User-Mode Linux.
#
# UML compiles the kernel as an ordinary Linux program, so a "VM" here is
# just a process: no KVM, no root, no tap devices.  The pieces are split
# across three files:
#
#   default.nix  the vivarium options and the packages a guest needs
#   guest.nix    what the guest system itself looks like
#   image.nix    the root image, /init, and the vivarium-run wrapper
{
  config,
  lib,
  pkgs,
  extendModules,
  ...
}:
let
  portPair = lib.types.submodule {
    options = {
      host = lib.mkOption {
        type = lib.types.port;
        description = "Port to listen on, on the host.";
      };
      guest = lib.mkOption {
        type = lib.types.port;
        description = "Port it reaches inside the guest.";
      };
    };
  };

  # A host port and the guest port behind it, written as a bare port
  # when the two are the same -- which they are unless the host will not
  # give us the number the guest wants.
  samePort = port: {
    host = port;
    guest = port;
  };
  portMap = lib.types.coercedTo lib.types.port samePort portPair;

  forwardRule = lib.types.submodule {
    options = {
      address = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "0.0.0.0";
        description = ''
          Host address to listen on.  Null means the runner picks this
          guest a free address out of `127.0.0.2` upwards and keeps it
          for the guest's lifetime, which is what makes two guests able
          to serve the same port number without arranging anything.

          Anything else is taken literally, so `0.0.0.0` reaches the
          guest from off the machine.  Note that a shared address
          collides with every other guest's `all` rule, one port at a
          time, so give it an explicit `ports` list.
        '';
      };

      ports = lib.mkOption {
        type = lib.types.either (lib.types.enum [ "all" ]) (lib.types.listOf portMap);
        default = "all";
        example = lib.literalExpression ''[ 8080 { host = 9090; guest = 80; } ]'';
        description = ''
          Which ports to forward, or `"all"` for every port passt is
          willing to bind on this address.

          `"all"` costs about 36000 sockets and 17 MB, and takes under a
          second, which is cheap enough that a guest with an address to
          itself need not know its own port list in advance.  It is
          still not free: a test that boots three guests does not want
          it, which is why the default here is the ssh port alone.
        '';
      };

      protocols = lib.mkOption {
        type = lib.types.listOf (lib.types.enum [ "tcp" "udp" ]);
        default = [ "tcp" ];
        description = ''
          Which protocols to forward these ports for.  UDP doubles the
          socket count, so it is off unless asked for.
        '';
      };

      remapPrivileged = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          What to do about host ports below
          `net.ipv4.ip_unprivileged_port_start`, which nothing here may
          bind: move them up by `privilegedOffset`, so guest port 22 is
          reachable on host port 10022.

          The runner says so on the console every time it does this --
          a port that is not the port you asked for is worth hearing
          about at boot rather than deducing from a refused connection.
          Turn this off to leave those ports unforwarded instead.
        '';
      };

      privilegedOffset = lib.mkOption {
        type = lib.types.port;
        default = 10000;
        description = ''
          How far up to move privileged ports.  The default keeps the
          original port readable in the new one: 22 becomes 10022, 80
          becomes 10080.
        '';
      };
    };
  };

  interfaceType = lib.types.submodule {
    options = {
      segment = lib.mkOption {
        type = lib.types.str;
        example = "fabric-a";
        description = ''
          The Ethernet segment this interface is plugged into.  Every
          interface in a run naming the same segment is on one link.
        '';
      };
      addresses = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [
          "10.0.0.1/31"
          "fd00:a::1/64"
        ];
        description = ''
          Static addresses with their prefix length, IPv4 or IPv6.  Empty
          leaves the interface with its IPv6 link-local address alone.
        '';
      };
    };
  };

  hex = n: (lib.optionalString (n < 16) "0") + lib.toHexString n;

  # `vec1` first, so the `lan` shorthand keeps the NIC and MAC it always
  # had; the rest by name. UML names NIC n `vecN` before the guest renames
  # it, which is why no other `vecN` may be a name.
  nicsOf =
    interfaces: index:
    let
      names = lib.attrNames interfaces;
      ordered = lib.optional (interfaces ? vec1) "vec1" ++ lib.filter (name: name != "vec1") names;
    in
    lib.imap1 (nic: name: {
      inherit name nic;
      inherit (interfaces.${name}) segment addresses;
      mac = "52:54:00:12:${hex nic}:${hex index}";
    }) ordered;
in
{
  imports = [
    ./container.nix
    ./guest.nix
    ./image.nix
    ./qemu.nix
    ./store.nix
  ];

  options.vivarium = {
    backend = lib.mkOption {
      type = lib.types.enum [ "uml" "qemu" "container" ];
      default = "uml";
      description = ''
        Which machine a guest becomes.

        `uml` is a process: no KVM, no root, no tap device, nothing asked
        of the host. `qemu` needs `/dev/kvm` to be worth running, and is
        then multiprocessor and much faster. `container` runs the system
        under rootless crun on the host's own kernel: no kernel boot, but
        no kernel of its own either, and the host needs user namespaces,
        subordinate ids and a writable cgroup. Its uplink and forwards
        are pasta's, and its LAN shares a segment with the other two.

        The guest is the same NixOS configuration either way, and so is
        the test script. What changes is the kernel and how `/nix`
        arrives: UML builds its own kernel and mounts the store over
        hostfs, QEMU uses the host's kernel with an initrd and mounts it
        over virtiofs.
      '';
    };

    index = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 0;
      description = ''
        This guest's position in its test, set by `mkTest`.

        It is what keeps two guests on one segment apart: their MAC
        addresses carry it, and under QEMU the interface names are
        matched on those MACs.
      '';
    };

    agentDevice = lib.mkOption {
      type = lib.types.str;
      default = "/dev/ttyS0";
      internal = true;
      description = ''
        The device the in-guest agent serves on. The backend decides it,
        because the console has to go somewhere else.
      '';
    };

    memory = lib.mkOption {
      type = lib.types.str;
      default = if config.vivarium.backend == "qemu" then "512M" else "256M";
      defaultText = lib.literalExpression ''if backend == "qemu" then "512M" else "256M"'';
      example = "1024M";
      description = ''
        Guest RAM, as UML's `mem=` and QEMU's `-m` both take it.

        A ceiling rather than a cost, under UML. The guest's memory is
        a sparse file and the guest punches holes in it as it frees
        pages, so the host pays for what a guest is using rather than
        for what it has ever used. `vm.host_memory_kib()` is that
        number; see the README.

        Under UML, below about 192M the kernel starts OOM-killing the
        agent while systemd and Python are both resident.

        A QEMU guest needs more for the same work, which is why the
        default is not one number. Its root is a tmpfs and its initrd is
        unpacked into another, so two filesystems are charged to the same
        RAM the guest is running in. Measured: at 256M the agent reaches
        its ready line and is OOM-killed seconds later, inside the build
        sandbox, on a guest that passes the same test outside it.
      '';
    };

    seccomp = lib.mkOption {
      type = lib.types.enum [
        "auto"
        "on"
        "off"
      ];
      default = "auto";
      example = "off";
      description = ''
        How UML catches a guest process's syscalls. Ignored by QEMU.

        `on` installs a seccomp filter and lets the guest's own signal
        handler do the memory management, which costs fewer context
        switches per minor fault -- a few percent of throughput and about
        five seconds of boot. `auto` uses it where the host allows the
        filter and falls back to ptrace where it does not. `off` is the
        ptrace userspace, which is a decade older and much better tested.

        UML's own `--help` says the filter "is not (yet) restrictive
        enough to prevent userspace from reading and writing all physical
        memory", so a guest process can corrupt the guest kernel's own
        structures.

        This is not the lever for a panic in the memory manager, whatever
        that suggests: issue #8 panics on both userspaces, and the cause
        was io_uring. See modules/guest.nix.
      '';
    };

    cpus = lib.mkOption {
      type = lib.types.ints.positive;
      default = 1;
      description = ''
        Processors the guest gets. Only the QEMU backend honours it.

        Worth about 11% on the iperf test at two processors, measured.
        Under UML two vCPUs measured *slower* than one on that same test
        -- UML allows SMP with the seccomp userspace alone, and the
        cross-CPU work costs more than the parallelism buys. The two
        backends disagree here, so do not carry a number between them.
      '';
    };

    nestedVirtualization = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Give the guest `/dev/kvm`, so it can run virtual machines of its
        own. QEMU backend only.

        Off, the runner hides `vmx` and `svm` from the guest's CPU. On, it
        passes the host's through, which needs nesting on the host
        (`/sys/module/kvm_{intel,amd}/parameters/nested`), and the guest
        loads KVM for that vendor. Without nesting on the host, the
        guest's `vivarium-kvm` unit fails with the reason.
      '';
    };

    diskSize = lib.mkOption {
      type = lib.types.ints.positive;
      default = 512;
      description = ''
        Size of the root image in MiB.  It holds almost nothing -- the
        Nix store comes from the host over hostfs -- so this only has to
        cover what the guest writes at runtime.
      '';
    };

    mtu = lib.mkOption {
      type = lib.types.ints.between 576 65534;
      default = 65000;
      example = 1500;
      description = ''
        MTU of both `vec` interfaces.

        A segment is a socketpair, and AF_UNIX only lets about ten
        frames sit in one before the sender blocks, so frame size is
        what decides how much a guest can have in flight: jumbo frames
        are worth roughly twice the throughput of 1500-byte ones.

        The default stops short of 64 KiB on purpose.  The driver keeps
        a receive buffer of `mtu` + 66 bytes per queue slot, and once
        that plus the skb's own footer passes 64 KiB each one costs a
        128 KiB allocation instead.  65520 measures the same as 65000
        and uses twice the memory to do it.

        Set this to 1500 for a test that cares about behaving like real
        Ethernet.  It cannot be changed from inside the guest: the
        driver leaves `max_mtu` at 1500, so this is the only way up.
      '';
    };

    journal = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Stream this guest's journal to `/artifacts/journal.jsonl` while
        it runs, one JSON object per entry.

        The host follows the file and turns each entry into an event
        that carries the machine and the unit. hostfs and virtiofs are
        both write-through, so an entry is on the host's disk as soon as
        journald has it -- a guest that is killed keeps everything it
        logged up to that moment.
      '';
    };

    sshPort = lib.mkOption {
      type = lib.types.port;
      default = 4325;
      description = ''
        Port sshd listens on, forwarded from the same port on the host's
        loopback by passt.  Tests drive the guest over the serial line
        instead; this is for looking around by hand.
      '';
    };

    rootPassword = lib.mkOption {
      type = lib.types.str;
      default = "uml";
      description = ''
        Root's password.  The guest is only reachable through the passt
        forward on the host's loopback, so this is a convenience rather
        than a secret -- do not put anything real in a guest.
      '';
    };

    forward = lib.mkOption {
      type = lib.types.listOf forwardRule;
      default = [ { ports = [ config.vivarium.sshPort ]; } ];
      defaultText = lib.literalExpression ''[ { ports = [ config.vivarium.sshPort ]; } ]'';
      example = lib.literalExpression ''
        [
          { ports = "all"; }                              # the whole guest, privately
          { address = "0.0.0.0"; ports = [ 8080 ]; }      # and one port, publicly
        ]
      '';
      description = ''
        How the host reaches services in this guest.

        passt is the only way in, and its forwards are fixed once it has
        started: it binds every socket while parsing its arguments, and
        has no way to be told about a new one afterwards short of a
        restart that would drop every connection through it.  So this is
        decided before the guest boots, and `ports = "all"` exists to
        make not having to decide affordable.
      '';
    };

    /*
      Tell Nix, inside the guest, about the store it can already see.

      `/nix/store` in a guest is the host's, over hostfs, with a writable
      overlay on top -- see image.nix.  Every path is there and readable,
      and Nix knows about none of them: there is no `/nix/var/nix/db` at
      all, so `nix-store --query --references <a path that is right there>`
      answers `path '...' is not valid`.

      That is fine for a guest that only runs programs.  It is not fine for
      one that runs Nix: a build or a `nix copy` into a second store finds
      its inputs invalid, tries to substitute them, and a build sandbox has
      no network.  So this builds a database for the closure below, which
      turns a directory the guest can see into a store it can use.

      On by default, because it stopped being worth deciding about.  It
      was off while it cost a oneshot at every boot; now it is built with
      the image, and measured at 612ms and 256K for a minimal guest and
      561ms and 268K for a kubeadm control plane, cached after the first
      build.  Against that, a guest without one looks identical to a guest
      with one until something runs Nix, and fails then as a network
      timeout that names nothing.
    */
    nixDatabase = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        example = false;
        description = ''
          Build a Nix database for the store the guest sees, and put it on
          the root image.

          Turning this off leaves a guest whose `/nix/store` is full of
          paths that Nix in there calls invalid.
        '';
      };

      extraRoots = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = lib.literalExpression ''[ "''${pkgs.hello}" ]'';
        description = ''
          Store paths to register beyond the guest's own system closure.

          `mkTest` already adds everything in its `settings` to this, so a
          test that hands its guests a store path that way needs nothing
          here.  This is for a path that reaches a guest by some other
          route.

          Each one becomes a dependency of the guest, which is also what
          puts it in the build sandbox in the first place.
        '';
      };
    };

    configurations = lib.mkOption {
      type = lib.types.attrsOf lib.types.deferredModule;
      default = { };
      example = lib.literalExpression ''
        { pynixd = { services.pynixd.enable = true; }; }
      '';
      description = ''
        Configurations this guest can switch to while it runs, by name.

        Each one is this guest plus the module, through `extendModules`, so
        it keeps the hostname, the segment and the backend. Its system is in
        the guest's Nix database, and a phase switches with
        `await vm.switch_to("pynixd")`, or back with `vm.switch_to()`.

        Evaluated here, on the host: the guest has no nixpkgs and no
        network to build one with.
      '';
    };

    /*
      `mkTest` reads this off the first guest, the way it reads the kernel
      and the toolchain. The script belongs to the test and not to a node,
      but a node's options are where a test's Nix-side settings live.
    */
    typeCheck = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        example = false;
        description = ''
          Run pyright over the test's script, as an input of the test
          derivation.

          A script is Python that nothing imports until the guests have
          booted, so a typo in it otherwise costs a boot to find.  The
          check takes seconds.

          Turn it off for a script whose imports the check's environment
          cannot provide.
        '';
      };

      extraPackages = lib.mkOption {
        type = lib.types.listOf lib.types.package;
        default = [ ];
        example = lib.literalExpression "[ pkgs.python3Packages.kubernetes ]";
        description = ''
          Python packages the script imports beyond `vivarium_runner`.

          Without them pyright reports the import as an error, which is
          the right answer: an import it cannot resolve is a name it
          cannot check.
        '';
      };

      strict = lib.mkOption {
        type = lib.types.bool;
        default = false;
        example = true;
        description = ''
          Use pyright's `strict` mode rather than `standard`.

          `reportMissingParameterType` is on either way, because without
          it an unannotated parameter is Unknown and nothing done to it is
          checked at all.
        '';
      };

      ignore = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = lib.literalExpression ''[ "reportMissingImports" ]'';
        description = ''
          pyright rules to turn off for the whole script, the way
          `pkgs.writers.writePython3Bin` takes `flakeIgnore`.

          Each name becomes `"<rule>": "none"` in the generated
          `pyrightconfig.json`.  For a check that is right about something
          the author cannot fix -- an import that only exists inside a
          guest, say -- rather than for one that is inconvenient.
        '';
      };
    };

    interfaces = lib.mkOption {
      type = lib.types.attrsOf interfaceType;
      default = { };
      example = lib.literalExpression ''
        {
          spine1 = { segment = "leaf1-spine1"; };
          spine2 = { segment = "leaf1-spine2"; };
          hosts = { segment = "leaf1"; addresses = [ "10.1.0.1/24" ]; };
        }
      '';
      description = ''
        The guest's links to Ethernet segments, by interface name: the
        attribute name is the interface's name inside the guest, on every
        backend.  So a test, or a router's configuration, names the link
        it means.  Each peer's address is in /etc/hosts as
        `<host>.<segment>`.

        `vec0` is the uplink and not one of these.  Names are at most 15
        characters, and no `vecN` but `vec1`, which `lan` defines.
      '';
    };

    nics = lib.mkOption {
      internal = true;
      readOnly = true;
      description = "`interfaces` in NIC order, each with its NIC number and MAC.";
    };

    lan = {
      network = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "lan";
        description = ''
          Shorthand for `interfaces.vec1.segment`.
          Name of an Ethernet segment to join on `vec1`.  Every machine
          in a test naming the same segment is wired together; the host
          runner creates the sockets.  Null leaves the guest with only
          its passt uplink.
        '';
      };

      address = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "192.168.99.2/24";
        description = ''
          Static address and prefix for `vec1`.  There is no DHCP server
          on a segment, so machines that need to talk to each other need
          one of these each.
        '';
      };
    };
  };

  config = {
    vivarium.interfaces.vec1 = lib.mkIf (config.vivarium.lan.network != null) {
      segment = config.vivarium.lan.network;
      addresses = lib.optional (config.vivarium.lan.address != null) config.vivarium.lan.address;
    };
    vivarium.nics = nicsOf config.vivarium.interfaces config.vivarium.index;

    vivarium.nixDatabase.extraRoots = map toString (
      lib.attrValues config.system.build.vivariumConfigurations
    );

    assertions = [
      {
        assertion = config.vivarium.lan.address != null -> config.vivarium.lan.network != null;
        message = "vivarium.lan.address is set but vivarium.lan.network is not, so nothing would be wired to vec1.";
      }
      {
        assertion = lib.all (
          name:
          lib.stringLength name <= 15
          && name != "lo"
          && (name == "vec1" || builtins.match "vec[0-9]+" name == null)
        ) (lib.attrNames config.vivarium.interfaces);
        message = "vivarium.interfaces: a name is at most 15 characters, not lo, and no vecN but vec1.";
      }
      {
        assertion =
          let
            segments = map (one: one.segment) (lib.attrValues config.vivarium.interfaces);
          in
          segments == lib.unique segments;
        message = "vivarium.interfaces: two interfaces of one guest on one segment.";
      }
      {
        # Two wide rules on one address overlap on every port, and passt
        # answers that with a warning per port before carrying on -- 36000
        # lines of console for a configuration that meant one rule.
        assertion =
          let
            wide = lib.filter (rule: rule.ports == "all") config.vivarium.forward;
            addresses = map (rule: toString rule.address) wide;
          in
          addresses == lib.unique addresses;
        message = ''
          vivarium.forward has more than one `ports = "all"` rule on the same
          address (rules with `address = null` all land on the same one).
          Give them different addresses, or fold them into a single rule.
        '';
      }
      {
        assertion = config.vivarium.nestedVirtualization -> config.vivarium.backend == "qemu";
        message = ''
          vivarium.nestedVirtualization needs `backend = "qemu"`: a UML guest
          has no virtual CPU to expose vmx or svm on.
        '';
      }
    ];

    # The UML kernel is built from the same source as the guest's own
    # kernel package, so the two always agree on module versions.
    system.build = {
      vivariumRunnerPackage = pkgs.callPackage ../pkgs/vivarium-runner { };

      vivariumConfigurations = lib.mapAttrs (
        _: module: (extendModules { modules = [ module ]; }).config.system.build.toplevel
      ) config.vivarium.configurations;

      /*
        What the guest tells Nix about the store it can see -- see
        `vivarium.nixDatabase`.

        Here rather than in guest.nix on purpose. A `closureInfo` over
        `toplevel` cannot be named by anything inside `toplevel`, and a
        systemd unit is inside it; `system.build` is downstream of the
        system and nothing is built from it, so this is where the cycle
        breaks.

        Naming the roots is also what puts them in the build sandbox. A
        path Nix has been told about and cannot open is worse than one it
        does not know.

        Nothing reads this at run time; `vivariumNixDatabase` below turns it
        into the database the guest boots with.
      */
      vivariumNixRegistration = pkgs.closureInfo {
        rootPaths = [ config.system.build.toplevel ] ++ config.vivarium.nixDatabase.extraRoots;
      };

      /*
        The guest's Nix database, built here rather than loaded at boot.

        It goes on the root image under `/nix-state`, which guest.nix
        binds onto `/nix/var`, so the database is in place before anything
        runs and a guest cannot come up with an empty one.

        `config.nix.package` and not `pkgs.nix`: the file carries a schema
        version, and the Nix that reads it is the guest's.

        Three steps here are not tidiness. Each one is a bug that nixpkgs'
        `dockerTools.mkDbExtraCommand` or nix2container's
        `makeNixDatabase` hit first, and they are worth keeping in this
        order:

          * `USER` must be set, because Nix asks the environment who it is
            and fails unhelpfully when nothing answers;
          * the dump does not carry `db/schema`, and without that file Nix
            takes the store for a new one and concurrent processes race to
            initialise it;
          * `sqlite3` leaves the file in `delete` journal mode while Nix
            switches to WAL on open -- and that switch takes an exclusive
            lock, so two openers at once get `database is busy`.

        Dumping and re-importing rather than shipping the file `--load-db`
        wrote is what makes the output the same for the same closure,
        whatever order the inserts happened in.
      */
      vivariumNixDatabase = pkgs.runCommand "vivarium-nix-database" {
        nativeBuildInputs = [
          config.nix.package
          pkgs.sqlite
        ];
      } ''
        export USER=nobody
        export NIX_REMOTE="local?root=$PWD"
        nix-store --load-db < ${config.system.build.vivariumNixRegistration}/registration

        # A build gets no clock, so give every path the same time rather
        # than whatever this one happened to run at.
        sqlite3 nix/var/nix/db/db.sqlite 'UPDATE ValidPaths SET registrationTime = 0'
        sqlite3 nix/var/nix/db/db.sqlite '.dump' > db.dump

        mkdir -p $out
        sqlite3 $out/db.sqlite '.read db.dump'
        cp nix/var/nix/db/schema $out/schema
        sqlite3 $out/db.sqlite 'pragma journal_mode = wal' > /dev/null
      '';
    }
    # Only under the UML backend, because the kernel is half an hour the
    # first time and a QEMU guest has no use for it. `system.build` is an
    # attribute set, not options, so this is optionalAttrs rather than
    # mkIf -- an attribute that is not defined cannot be evaluated by
    # accident, which is the point.
    // lib.optionalAttrs (config.vivarium.backend == "uml") {
      umlKernel = pkgs.callPackage ../pkgs/uml-kernel {
        inherit (config.boot.kernelPackages.kernel) src version modDirVersion;
      };
      umlPasstBridge = pkgs.callPackage ../pkgs/uml-passt-bridge { };
    };
  };
}
