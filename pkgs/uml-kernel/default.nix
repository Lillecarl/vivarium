# The guest kernel, built for ARCH=um from whatever source the guest's
# own kernel package uses, with a hand-written config: allnoconfig plus
# exactly what NixOS needs to boot (systemd, cgroups, ext4, overlayfs,
# hostfs for the store, and the UML vector/ubd/serial drivers).
{
  stdenv,
  lib,
  linuxKernel,
  ccache,
  version,
  modDirVersion,
  src,
  # UML is uniprocessor unless asked otherwise; SMP costs boot time and
  # is only worth it for tests that measure parallelism.
  smp ? false,
  /*
    A directory inside the build sandbox to keep compiled objects in, or
    null. Set, the build compiles through ccache and fails at once if the
    directory is not writable: only a builder that mounts one has it, as

      pynix build --namespaced --sandbox-path /ccache=$HOME/.cache/uml-ccache ...

    A different derivation from the plain kernel, so CI never builds it.
  */
  ccacheDir ? null,
}:

let
  ccacheCC = "CC=${lib.getExe ccache} ${lib.getExe stdenv.cc}";

  baseConfig = with lib.kernel; {
    BINFMT_ELF = yes;
    BINFMT_SCRIPT = yes;

    TTY = yes;
    VT = yes;
    UNIX98_PTYS = yes;

    PROC_FS = yes;
    SYSFS = yes;
    TMPFS = yes;
    TMPFS_XATTR = yes;
    SHMEM = yes;
    DEVTMPFS = yes;

    EXT4_FS = yes;
    EXT4_FS_SECURITY = yes;
    OVERLAY_FS = yes;
    HOSTFS = yes;

    # composefs: an EROFS image under an overlay whose redirects are xattrs
    # in the image, mounted from the image file without a loop device.
    # EROFS sits under MISC_FILESYSTEMS, which allnoconfig leaves off.
    MISC_FILESYSTEMS = yes;
    EROFS_FS = yes;
    EROFS_FS_XATTR = yes;
    EROFS_FS_BACKED_BY_FILE = yes;

    NET = yes;
    INET = yes;
    UNIX = yes;
    PACKET = yes;
    UML_NET = yes;
    UML_NET_VECTOR = yes;

    BLOCK = yes;
    BLK_DEV = yes;
    BLK_DEV_LOOP = yes;
    BLK_DEV_UBD = yes;
    TUN = yes;

    # allnoconfig leaves the default channel strings empty, so UML logs
    # a setup failure for each of the 16 consoles and 64 serial lines it
    # cannot bring up.  con0 is the console we read on the host's stdout;
    # ssl0 is the control channel, wired by the runner.  Everything else
    # goes to the null channel, which needs NULL_CHAN to be more than a
    # stub that fails.
    NULL_CHAN = yes;
    CON_ZERO_CHAN = freeform "fd:0,fd:1";
    CON_CHAN = freeform "null";
    SSL = yes;
    SSL_CHAN = freeform "null";
    PRINTK = yes;
    EARLY_PRINTK = yes;

    # UML's management console, and the only way a guest gives RAM back:
    # `config mem=-256M` allocates guest pages and MADV_REMOVEs them from
    # the file UML uses as that guest's memory.  Nothing else in the
    # kernel can do it -- virtio-balloon has no backend to speak to here,
    # because `virtio_uml` is a vhost-user transport.  See issue #12.
    #
    # allnoconfig answers no to this although its Kconfig says `default
    # y`, which is why it is named here.
    MCONSOLE = yes;

    # And the same thing without being asked: the guest punches a hole in
    # its own memory file whenever it has a free block to report, so the
    # host pays for what a guest is using rather than for what it has ever
    # used. `select`s PAGE_REPORTING, which allnoconfig would otherwise
    # leave off. Carried as a patch here; see the file for why upstream's
    # virtio-balloon cannot do this job under UML.
    UML_FREE_PAGE_REPORTING = yes;

    MULTIUSER = yes;
    ADVISE_SYSCALLS = yes;
    MEMBARRIER = yes;
    SIGNALFD = yes;
    TIMERFD = yes;
    EPOLL = yes;
    EVENTFD = yes;
    INOTIFY_USER = yes;
    FHANDLE = yes;
    CGROUPS = yes;

    SECURITY = yes;
    SECURITYFS = yes;
    SECCOMP = yes;
    SECCOMP_FILTER = yes;
    SECURITY_YAMA = yes;
    SECURITY_LANDLOCK = yes;
    INTEGRITY = yes;

    BPF = yes;
    BPF_SYSCALL = yes;
    BPF_JIT = yes;
    CGROUP_BPF = yes;
    BPF_LSM = yes;

    CGROUP_SCHED = yes;
    # kubelet turns a pod's CPU limit into a CFS quota, and refuses to
    # start when the kernel cannot enforce one.
    CFS_BANDWIDTH = yes;
    CGROUP_CPUACCT = yes;
    CGROUP_PIDS = yes;
    # No CPUSETS: it depends on SMP, and a guest with one processor has
    # nothing to partition.  kubelet only asks for the cpuset controller
    # under the static CPU manager policy, which needs more than one CPU
    # to mean anything either.
    CGROUP_HUGETLB = yes;
    HUGETLBFS = yes;
    BLK_CGROUP = yes;
    CGROUP_DEVICE = yes;
    CGROUP_FREEZER = yes;

    PERF_EVENTS = yes;

    MEMCG = yes;

    PSI = yes;
    AUDIT = yes;

    AUTOFS_FS = yes;
    CONFIGFS_FS = yes;

    # /dev/fuse, so a guest can host a userspace filesystem.  FUSE is
    # architecture independent, so ARCH=um builds it like any other
    # architecture does.
    FUSE_FS = yes;

    /*
      io_uring is on, and it does not work.  Left on here and refused at
      runtime instead; read this before deciding otherwise.

      allnoconfig leaves IO_URING at its `default y`, so a guest has it.
      Without the sysctl below, a process that uses it takes the guest
      down.  Headless chromium did, and got as far as launching before
      the kernel said

          BUG: Bad page map in process chrome-headless  pte:0e6c5061
          file:[io_uring] fault:0x0 mmap:io_uring_mmap

      once per ring page, after which the guest stops answering the host
      at all and a test dies as "agent did not come up" rather than as
      anything naming io_uring.  UML's mm does not handle the rings
      io_uring_mmap installs.

      Switching it off is not one line.  `config IO_URING` is
      `bool "..." if EXPERT`, so allnoconfig cannot express "no" without
      EXPERT -- and EXPERT makes a great many core options answerable
      that were previously forced on, which allnoconfig then answers no
      to.  FUTEX, AIO and BASE_FULL go with it.  Measured: the kernel
      built that way prints nothing at all, not one line, and never
      reaches init.

      So the guest refuses it at runtime instead:
      `kernel.io_uring_disabled=2`, set for every UML guest in
      modules/guest.nix.  `io_uring_setup` then returns -EPERM, which a
      caller handles, rather than a mapping that panics.

      Chromium was the first thing to hit this, and the workaround then
      was firefox, which does not use io_uring.  Not retried since the
      sysctl.

      Fixing this properly is UML mm work upstream, and the note is here
      so the next person reaches that conclusion in a minute rather than
      in an afternoon.
    */
  };

  # What it takes to run containers, which is what a test wants a guest
  # for as often as not.  allnoconfig turns all of this off, and without
  # it containerd cannot create a sandbox and kube-proxy cannot write a
  # rule -- both fail in ways that look like the container runtime is
  # broken rather than the kernel being unable to host it.
  #
  # This is unconditional rather than an option: there is one kernel per
  # nixpkgs revision and it is expensive to build, so paying for a
  # slightly larger image once beats every consumer needing to know
  # whether their test is "the container one".
  containerConfig = with lib.kernel; {
    # runc unshares all of these before it execs anything.
    NAMESPACES = yes;
    UTS_NS = yes;
    IPC_NS = yes;
    PID_NS = yes;
    NET_NS = yes;
    USER_NS = yes;
    TIME_NS = yes;
    # No CGROUP_NS: cgroup namespaces have not been optional since 4.6,
    # and asking for the option that used to switch them on gets silently
    # dropped rather than warned about.

    # runc mounts /dev/mqueue and expects SysV IPC to exist.
    POSIX_MQUEUE = yes;
    SYSVIPC = yes;

    # Nothing here uses the keyring, but kubelet raises the root user's
    # key quota before it starts its container manager, and a sysctl it
    # cannot open is fatal rather than skipped -- kubelet exits, systemd
    # restarts it, and the control plane never comes up.  This is what
    # puts kernel/keys/root_maxkeys and root_maxbytes under /proc/sys.
    KEYS = yes;

    # A CNI plugin puts one end of a veth in the sandbox and the other
    # on a bridge; br_netfilter is what makes the host's iptables rules
    # apply to what crosses that bridge, which is how a Service works.
    VETH = yes;
    BRIDGE = yes;
    BRIDGE_NETFILTER = yes;
    DUMMY = yes;

    # kube-proxy writes its rules through iptables by default and nft
    # in its newer mode, so build both front ends.
    #
    # Both front ends really does mean both.  Since 6.16 the iptables
    # tables -- filter, nat, mangle -- hang off IP_NF_IPTABLES_LEGACY
    # rather than IP_NF_IPTABLES, and that off NETFILTER_XTABLES_LEGACY.
    # Set only the latter two and every table quietly disappears, which
    # reads as `iptables: can't initialize` from kube-proxy on a kernel
    # whose config claims iptables support.
    NETFILTER = yes;
    NETFILTER_ADVANCED = yes;
    NETFILTER_NETLINK = yes;
    NETFILTER_XTABLES = yes;
    NETFILTER_XTABLES_LEGACY = yes;
    NF_CONNTRACK = yes;
    # kube-proxy runs `conntrack -D` to drop entries for an endpoint that
    # has gone away, and that talks to conntrack over netlink rather than
    # through a rule.  Without it, a Service keeps sending traffic to the
    # pod it used to have.
    NF_CT_NETLINK = yes;
    NF_NAT = yes;
    NF_NAT_MASQUERADE = yes;
    NF_TABLES = yes;
    NF_TABLES_INET = yes;
    NFT_CT = yes;
    NFT_NAT = yes;
    NFT_MASQ = yes;
    NFT_REDIR = yes;
    NFT_COMPAT = yes;
    IP_NF_IPTABLES = yes;
    IP_NF_IPTABLES_LEGACY = yes;
    IP_NF_FILTER = yes;
    IP_NF_MANGLE = yes;
    IP_NF_NAT = yes;
    IP_NF_TARGET_MASQUERADE = yes;
    IP_NF_TARGET_REJECT = yes;
    NETFILTER_XT_MARK = yes;
    NETFILTER_XT_NAT = yes;
    NETFILTER_XT_MATCH_ADDRTYPE = yes;
    NETFILTER_XT_MATCH_COMMENT = yes;
    NETFILTER_XT_MATCH_CONNTRACK = yes;
    NETFILTER_XT_MATCH_MULTIPORT = yes;
    NETFILTER_XT_MATCH_STATISTIC = yes;
    NETFILTER_XT_TARGET_MASQUERADE = yes;
    NETFILTER_XT_TARGET_REDIRECT = yes;

    # Routing between the nodes' pod subnets, and the forwarding that
    # any of this is pointless without.
    IP_ADVANCED_ROUTER = yes;
    IP_MULTIPLE_TABLES = yes;

    # A routed fabric: ECMP across spines, a VRF per tenant, VXLAN for
    # EVPN. examples/frr-clos uses all three.
    # NET_VRF needs both of the two after it, or the config drops it.
    IP_ROUTE_MULTIPATH = yes;
    NET_VRF = yes;
    NET_L3_MASTER_DEV = yes;
    IPV6_MULTIPLE_TABLES = yes;
    VXLAN = yes;

    # Other distributions' roots: openSUSE's cloud image is XFS, its
    # others and SLE are btrfs.
    XFS_FS = yes;
    BTRFS_FS = yes;
    # Their /boot/efi, which fstab mounts: boot waits for it otherwise.
    VFAT_FS = yes;
    NLS_CODEPAGE_437 = yes;
    NLS_ISO8859_1 = yes;

    # kubelet reads cgroup pressure, and containerd's cgroup v2 driver
    # wants the io controller as well as the ones NixOS already needs.
    BLK_DEV_THROTTLING = yes;

    # Enough IPv6 to not look like a broken host to anything that asks.
    IPV6 = yes;
    NF_TABLES_IPV6 = yes;
    IP6_NF_IPTABLES = yes;
    IP6_NF_IPTABLES_LEGACY = yes;
    IP6_NF_FILTER = yes;
    IP6_NF_MANGLE = yes;
    IP6_NF_NAT = yes;
  };

  smpConfig = lib.optionalAttrs smp (
    with lib.kernel;
    {
      SMP = yes;
      NR_CPUS = freeform "64";
    }
  );
in

(linuxKernel.buildLinux {
  inherit version src modDirVersion;
  pname = "linux-uml";
  kernelPatches = [
    {
      name = "um-return-free-pages-to-the-host";
      patch = ./0001-um-return-free-pages-to-the-host.patch;
    }
    {
      name = "um-report-cpu-mhz";
      patch = ./0002-um-report-cpu-mhz.patch;
    }
  ];
  kernelArch = "um";
  target = "linux";
  defconfig = "allnoconfig";
  enableCommonConfig = false;
  autoModules = false;
  ignoreConfigErrors = true;
  extraMakeFlags = [
    "ARCH=um"
    "SUBARCH=x86_64"
    "CC=${lib.getExe stdenv.cc}"
  ];
  structuredExtraConfig = baseConfig // containerConfig // smpConfig;
  extraMeta.platforms = lib.platforms.linux;
}).overrideAttrs
  (
    old:
    {
      installTargets = [ ];
      preInstall = "";
      installPhase = ''
        mkdir -p $out $dev $modules
        cp -v linux $out/
        cp -v System.map $out/
        cp -v .config $out/config
      '';
    }
    # Only when asked, so the plain kernel's derivation is the one CI builds.
    // lib.optionalAttrs (ccacheDir != null) {
      # Here and not in `extraMakeFlags`, which the config derivation
      # shares: there ccache had no directory, every compiler probe
      # failed, and `.config` was never written. The last `CC=` on make's
      # command line wins, and `buildFlags` follow `makeFlags` there and
      # end in `extraMakeFlags` -- measured, only the configure phase's
      # 117 calls reached ccache until both carried it.
      makeFlags = old.makeFlags ++ [ ccacheCC ];
      buildFlags = old.buildFlags ++ [ ccacheCC ];
      # Exported here and not through `env`: this derivation has
      # structured attributes, and measured, `env` reached the build as
      # unexported shell variables, so the compiler's ccache saw none.
      # preConfigure, because `make oldconfig` and `make prepare` in the
      # configure phase already compile through it.
      preConfigure = (old.preConfigure or "") + ''
        export CCACHE_DIR=${ccacheDir}
        # Every build unpacks to the same /build path, so hashing it costs
        # nothing; relative paths keep hits across a changed source hash.
        export CCACHE_BASEDIR=/build CCACHE_NOHASHDIR=true
        # Patching and unpacking give every file a fresh mtime, and ccache's
        # direct mode refuses a header newer than the compile otherwise.
        export CCACHE_SLOPPINESS=include_file_mtime,include_file_ctime,time_macros
        export CCACHE_MAXSIZE=5G
        if ! touch "$CCACHE_DIR/.writable" 2>/dev/null; then
          echo "ccacheDir ${ccacheDir} is not writable in the sandbox; build with" >&2
          echo "  pynix build --namespaced --sandbox-path ${ccacheDir}=<a host directory>" >&2
          exit 1
        fi
        ${lib.getExe ccache} --zero-stats
      '';
      postBuild = (old.postBuild or "") + ''
        ${lib.getExe ccache} --show-stats
      '';
    }
  )
