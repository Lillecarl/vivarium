# UML-backed implementations of the `pkgs.vmTools` build-time primitives.
#
# Same call shapes as `pkgs/build-support/vm/default.nix`, so a caller that
# builds through `vmTools.runInLinuxVM` today can swap this set in with the
# `uml-vm-tools` overlay and change nothing else. No `/dev/kvm`, no `kvm`
# feature, no root: the guest is a User-Mode Linux kernel running as a
# child of the builder, which is why this works in a Nix build sandbox and
# on machines without KVM.
#
# Covered: `runInLinuxVM`, `runInLinuxImage`, `extractFs`,
# `extractMTDfs`, `defaultCreateRootFS`, `fillDiskWithRPMs`,
# `fillDiskWithDebs`, `buildRPM`, `createEmptyImage`,
# `rpmClosureGenerator`, `debClosureGenerator`,
# `makeImageFromRPMDist`, `makeImageFromDebDist`, `diskImageFuns`,
# `diskImageExtraFuns`, `diskImages`, `makeImageTestScript`,
# `vmRunCommand` and `hd`, named in `names` below
# so the overlay (`lib.nix`) cannot drift from this set. The makers
# have to be ported too, not just the primitives: nixpkgs composes
# them from its siblings inside its own `rec`, so an overlay that
# replaced only the leaves would still boot QEMU underneath (seen
# live: `makeImageFromRPMDist` through a leaves-only overlay starts
# virtiofsd). Pure host-side data (`rpmDistros`, `debDistros`, the
# package sets) needs no port and rides the overlay as is.
# Ours on top: `createEmptyRawImage`, the empty image plus the raw
# conversion the fill ports share, and the toolchain builders
# (`makeStoreImage`, `makeRPMToolchainImage`, `makeDeb`,
# `makeDebShim`): images and packages assembled from the store for
# callers with no distro, e.g. `buildRPM` over a symlinked compiler.
# Left out on purpose:
# Left out because UML has no counterpart, not from neglect: `qemu`
# and `qemuCommandLinux` are QEMU itself; `initrd`, `initrdUtils`,
# `modulesClosure`, `stage1Init` and `stage2Init` build and boot a
# QEMU initrd, while the guest here boots its host directory straight
# over hostfs with no initrd at all.
{ pkgs
, lib ? pkgs.lib
, storeDir ? builtins.storeDir
}:
let
  inherit (pkgs) stdenv bash coreutils busybox;

  # A shell with no dynamic dependencies. The kernel execs `/stage2`
  # before the store is mounted, so its interpreter must not live in
  # the store the way nixpkgs' stage 2 (via its initrd) can assume.
  # Same for the tools stage 2 mounts with: a static busybox.
  staticBash = pkgs.pkgsStatic.bash;
  staticBusybox = pkgs.pkgsStatic.busybox;

  # The guest kernel, built for ARCH=um from the host set's own kernel
  # source -- the same source the QEMU builders boot, so the guest sees
  # the same version. x86_64 only: UML is an x86 port, and the build costs
  # about half an hour the first time (see pkgs/uml-kernel).
  umlKernel = pkgs.callPackage ./pkgs/uml-kernel {
    inherit (pkgs.linux) src version modDirVersion;
  };

  /*
    Stage 2, running as guest PID 1. It mirrors nixpkgs' `stage2Init`
    (`pkgs/build-support/vm/default.nix:214`): same saved-env and structured
    attrs handling, same `NIX_STORE`/`NIX_BUILD_TOP`/`TMPDIR`, same
    `stdenv/setup` plus original builder run, same exit-code protocol over
    `xchg/in-vm-exit`.

    What differs, and why:

    - No stage 1, no initrd. The guest root is a host directory booted
      straight over hostfs (`rootfstype=hostfs`), so one script does what
      the initrd plus stage 2 did. `runInLinuxImage` is the exception:
      it mounts its image at `/fs` and enters it with `chroot`, as
      nixpkgs' stage 1 enters its root (see the branch in the script).
    - `virtiofs` mounts become `hostfs` mounts: the host store read-write
      at its own path (outputs land in the host store directly, as with
      virtiofs `shared-dir`), the build directory at `/tmp/xchg`.
    - No modprobe setup. The UML kernel carries its config built in
      (`CONFIG_BLK_DEV_LOOP` and `CONFIG_EXT4_FS` are `=y` in its built
      config), and `runInLinuxVM` payloads load no modules. `extractFs`
      mounts loop images with no setup lines at all for the same reason.
    - No debug shell. nixpkgs drops to an interactive shell when an
      image runs a second time, but every build here boots a fresh root,
      so that branch is unreachable; `-K` keeps `run-uml`, `console.log`
      and `stage2.log` for inspection by hand instead.
    - No `lo` interface. The QEMU guest brings one up, but the payload
      runs offline either way; nothing in the protocol needs it.
  */
  stage2Uml = pkgs.writeScript "uml-run-stage2" ''
    #!/bin/sh
    set -euo pipefail
    export PATH=/bin

    # One mount, creating its point first: every mount below is this,
    # so a point is never mounted without existing. Usage reads like
    # the mount itself: `mnt /proc -t proc none`.
    mnt() {
      target=$1
      shift
      mkdir -p "$target"
      mount "$@" "$target"
    }

    # After `chroot` below, the working directory lies outside the new
    # root, so anchor it before anything looks anything up. Plain boots
    # never set the sentinel and take the mounts below instead; after
    # `chroot`, every mount travelled with the move, so there is
    # nothing left to do.
    if test -e /.uml-in-image; then
      cd /
    else
      mnt /proc -t proc none
      mnt /sys -t sysfs none
      mnt /dev -t devtmpfs devtmpfs
      ln -s /proc/self/fd /dev/fd
      ln -s /proc/self/fd/0 /dev/stdin
      ln -s /proc/self/fd/1 /dev/stdout
      ln -s /proc/self/fd/2 /dev/stderr

      # `/etc` arrives pre-created from the host (the hostfs root mounts
      # read-only): belt and braces for the plain root. Not after
      # `chroot`: there `/bin` is the image's, which need not have `mkdir`.
      mkdir -p /etc

      mnt ${storeDir} -t hostfs none -o "$UML_STORE"
      mnt /tmp -t tmpfs -o "mode=1777" none
      mkdir -p /run
      mnt /tmp/xchg -t hostfs none -o "$UML_XCHG"

      # Userspace output written to the guest console never reaches the
      # host in a sandbox (measured: kernel messages do, PID 1's own do
      # not), so from here stage 2 logs to a file the builder replays.
      # Before the image branch on purpose: a failure in there must land
      # in the log too, and after `chroot` the same path resolves
      # through the bind below, so the log continues in one file.
      exec >>/tmp/xchg/stage2.log 2>&1
      echo "guest store and exchange directory mounted"

      # `runInLinuxImage`: the image on `/dev/ubda` becomes the guest
      # root, mirroring nixpkgs' stage 1 (`default.nix:157`) with hostfs
      # where it mounts virtiofs. `/fs` arrives pre-created from the
      # host, as the other mount points do.
      if [ -n "''${UML_IMAGE-}" ]; then
        mnt /fs /dev/ubda
        echo "image mounted at /fs"

        mnt /fs/dev -o bind /dev

        mnt /fs/dev/shm -t tmpfs -o "mode=1777" none
        mnt /fs/dev/pts -t devpts none

        mnt /fs${storeDir} -t hostfs none -o "$UML_STORE"

        mnt /fs/tmp -t tmpfs -o "mode=1777" none
        mnt /fs/run -t tmpfs -o "mode=755" none
        mkdir -p /fs/var
        ln -sfn /run /fs/var/run

        mnt /fs/tmp/xchg -o bind /tmp/xchg

        mnt /fs/proc -t proc none

        mnt /fs/sys -t sysfs none

        mkdir -p /fs/etc
        ln -sf /proc/mounts /fs/etc/mtab
        echo "127.0.0.1 localhost" > /fs/etc/hosts
        if [ ! -e /fs/etc/passwd ]; then
          echo "root:x:0:0:System administrator:/root:/bin/sh" > /fs/etc/passwd
        fi

        # Enter through the store bash: after the entry `/bin/sh` is
        # the image's shell, which a bare image need not have, while the
        # mounted store keeps this path valid. The environment
        # (`UML_STORE`, `UML_XCHG`, `UML_IMAGE`) crosses `chroot`, and
        # the sentinel keeps the second entry from entering again.
        #
        # `chroot`, not `switch_root`: busybox `switch_root` is the wrong
        # tool here by its own design -- before its `rm -rf /` it demands
        # `/init` exist and `/` be ramfs/tmpfs (measured: it dies with
        # "'/init' is not a regular file"), while this root is hostfs
        # with `/stage2`. There is also nothing to free with a move:
        # the hostfs root costs the guest no RAM, so leaving it pinned
        # behind the image for the build's lifetime is free.
        touch /fs/.uml-in-image
        cp /stage2 /fs/stage2
        echo "entering image root at /fs"
        exec chroot /fs ${staticBash}/bin/bash /stage2
      fi
    fi

    source /tmp/xchg/saved-env
    if [ -f /tmp/xchg/.attrs.sh ]; then
      source /tmp/xchg/.attrs.sh
      export NIX_ATTRS_JSON_FILE=/tmp/xchg/.attrs.json
      export NIX_ATTRS_SH_FILE=/tmp/xchg/.attrs.sh
    fi

    export NIX_STORE=${storeDir}
    export NIX_BUILD_TOP=/tmp
    export TMPDIR=/tmp
    export PATH=/empty
    cd "$NIX_BUILD_TOP"

    source $stdenv/setup

    # As in nixpkgs' stage 2, a failing payload aborts before writing its
    # exit code, and the host then reports a missing `in-vm-exit` rather
    # than the payload's status. Same behaviour, kept on purpose.
    exec < /dev/null
    declare -a argsArray=()
    concatTo argsArray origArgs
    "$origBuilder" "''${argsArray[@]}"
    echo $? > /tmp/xchg/in-vm-exit

    ${busybox}/bin/poweroff -f
  '';

  /*
    The host half. It mirrors nixpkgs' `vmRunCommand`
    (`pkgs/build-support/vm/default.nix:284`): same `xchg` setup, same
    structured-attrs forwarding, same `preVM` on the host before the boot,
    same exit-code check and `postVM` after. Only the boot itself differs:
    a `run-uml` script (kept for `-K` debugging, as `run-vm` is) starts the
    UML kernel instead of QEMU plus two `virtiofsd` daemons.

    `memSize` maps to the kernel's `mem=`. `UML_OPTS` appends to the kernel
    command line. `QEMU_OPTS` is accepted so callers that set it still
    evaluate, but the kernel ignores it -- acceleration flags have no
    target here. `diskImage`, when set, appears as `/dev/ubda` without
    being mounted; the `mountDisk` plus image-root semantics belong to
    the `runInLinuxImage` port.
  */
  umlRunCommand =
    pkgs.writeText "uml-run" ''
      ${coreutils}/bin/mkdir xchg
      export > xchg/saved-env

      if [ -f "''${NIX_ATTRS_SH_FILE-}" ]; then
        ${coreutils}/bin/cp $NIX_ATTRS_JSON_FILE $NIX_ATTRS_SH_FILE xchg
        source "$NIX_ATTRS_SH_FILE"
      fi
      source $stdenv/setup

      eval "$preVM"

      # The guest root: a static busybox for the mounts, a static bash
      # for `/bin/sh` (shebangs in the payload expect a real bash, as in
      # nixpkgs' stage 2, and the store is not mounted yet when the
      # kernel execs `/stage2`), and the stage-2 script itself. The store
      # arrives over hostfs, so nothing from it is copied here.
      guestRoot=$NIX_BUILD_TOP/uml-root
      ${coreutils}/bin/mkdir -p $guestRoot/bin
      ${coreutils}/bin/cp -p ${staticBusybox}/bin/busybox $guestRoot/bin/busybox
      for applet in mount mkdir ln cat echo ls poweroff cp chroot touch; do
        ${coreutils}/bin/ln -s busybox "$guestRoot/bin/$applet"
      done
      ${coreutils}/bin/cp ${staticBash}/bin/bash "$guestRoot/bin/bash"
      ${coreutils}/bin/ln -s bash "$guestRoot/bin/sh"
      ${coreutils}/bin/cp ${stage2Uml} $guestRoot/stage2
      ${coreutils}/bin/chmod +x $guestRoot/stage2

      # The hostfs root mounts read-only, so stage 2 creates nothing in
      # it: mount points and /etc arrive from here instead. `/mnt` is
      # where disk-building payloads mount their target, as under QEMU's
      # writable tmpfs root; an overlay over `/` itself is refused by the
      # kernel (measured), so anything else written outside the mounted
      # trees stays a divergence. `/fs` only the image branch mounts,
      # as the guest root (see the branch in stage 2).
      ${coreutils}/bin/mkdir -p $guestRoot/etc $guestRoot/tmp $guestRoot/run \
        $guestRoot/dev $guestRoot/proc $guestRoot/sys $guestRoot/mnt \
        $guestRoot/fs "$guestRoot${storeDir}"
      echo "127.0.0.1 localhost" > $guestRoot/etc/hosts
      echo "root:x:0:0:System administrator:/root:/bin/sh" > $guestRoot/etc/passwd
      ${coreutils}/bin/ln -sf /proc/mounts $guestRoot/etc/mtab

      # UML management-console state. `uml_dir` defaults to `~/.uml`, but
      # the sandbox's HOME has no parent for it: the mkdir fails and
      # `make_umid` segfaults early in boot (measured). A per-build
      # directory avoids HOME entirely, as the vivarium runner does.
      ${coreutils}/bin/mkdir -p uml-dir

      # The kernel passes unknown `name=value` arguments to PID 1 as
      # environment, which is how the guest learns the two host paths
      # (both free of spaces: the store and the build directory, so no
      # quoting -- the kernel keeps quote characters literally).
      # Written to a file so a failed build keeps it for debugging.
      # Everything with a `$` below expands when this script runs, so
      # `run-uml` carries baked absolute paths.
      # `runInLinuxImage` sets `mountDisk=true` on its derivation (as in
      # nixpkgs); only a true value takes the image branch in stage 2.
      # Nix stringifies boolean `true` to `1` in the builder environment,
      # so both spellings count. Anything else -- unset, false, a QEMU
      # device path -- boots the plain root. Like the two host paths,
      # the flag travels as kernel-environment.
      umlImageFlag=
      case "''${mountDisk-}" in
        1|true) umlImageFlag="UML_IMAGE=1" ;;
      esac

      ${coreutils}/bin/cat > ./run-uml <<EOF
      #! ${bash}/bin/sh
      ${umlKernel}/linux UML_STORE=${storeDir} UML_XCHG=$PWD/xchg $umlImageFlag rootfstype=hostfs rootflags=$PWD/uml-root init=/stage2 mem=''${memSize:-512}M panic=1 uml_dir=$PWD/uml-dir umid=uml ''${diskImage:+ubd0=$diskImage} $UML_OPTS
      EOF

      ${coreutils}/bin/chmod +x ./run-uml
      # The guest console goes to a file: piped straight to the build
      # log, the kernel's early messages never arrive in a sandbox
      # (measured: only blank lines), while a file captures all of it,
      # as the vivarium runner's per-guest console log does. Userspace
      # output needs its own file on top: PID 1's writes never reach the
      # host console there at all (measured), so stage 2 redirects into
      # xchg/stage2.log. A background tail streams both live, as QEMU's
      # serial line does; a failed boot replays both files, so nothing is
      # lost to the stream's race at shutdown, and `-K` keeps them.
      ${coreutils}/bin/touch console.log xchg/stage2.log
      ${coreutils}/bin/tail -q -n +1 -f console.log xchg/stage2.log &
      tailpid=$!
      kernel_rc=0
      ./run-uml >console.log 2>&1 || kernel_rc=$?
      kill "$tailpid" 2>/dev/null || true
      wait "$tailpid" 2>/dev/null || true

      if ! test -e xchg/in-vm-exit; then
        echo "UML guest didn't produce an exit code (kernel exit $kernel_rc)."
        ${coreutils}/bin/cat console.log xchg/stage2.log
        exit 1
      fi

      exitCode="$(${coreutils}/bin/cat xchg/in-vm-exit)"
      if [ "$exitCode" != "0" ]; then
        exit "$exitCode"
      fi

      eval "$postVM"
    '';

  /*
    A bash fragment that produces a disk image at `destination`, for
    `preVM`. Host-only -- `qemu-img` runs on the builder -- so this is
    nixpkgs' `createEmptyImage`
    (`pkgs/build-support/vm/default.nix:347`) with only the tool path
    retargeted, byte for byte otherwise. The image is qcow2, as there;
    convert to raw before attaching it: UML reads block devices, not
    image formats (wiring that conversion is `runInLinuxImage`'s job).
  */
  createEmptyImage =
    {
      # Disk image size in MiB (1024*1024 bytes)
      size,
      # Name that will be written to ${destination}/nix-support/full-name
      fullName,
      # Where to write the image files, defaulting to $out
      destination ? "$out",
    }:
    ''
      mkdir -p ${destination}
      diskImage=${destination}/disk-image.qcow2
      ${pkgs.buildPackages.qemu_kvm}/bin/qemu-img create -f qcow2 $diskImage "${toString size}M"

      mkdir ${destination}/nix-support
      echo "${fullName}" > ${destination}/nix-support/full-name
    '';

  /*
    After `createEmptyImage`, `$diskImage` names qcow2: make the raw
    copy UML boots from, for `preVM`. UML reads block devices, not
    image formats (see `runInLinuxImage`).
  */
  convertToRawImage = ''
    ${pkgs.buildPackages.qemu_kvm}/bin/qemu-img convert -O raw $diskImage $out/disk-image.raw
    diskImage=$out/disk-image.raw
  '';

  /*
    After the guest filled `$out/disk-image.raw`, refresh the qcow2
    beside it from the raw, for `postVM`. The raw is what UML boots,
    but consumers (and `runInLinuxImage`'s directory branch) reach for
    `disk-image.qcow2`, which `createEmptyImage` left pristine; without
    this the output pairs a live raw with a dead qcow2.
  */
  refreshQcow2Image = ''
    ${pkgs.buildPackages.qemu_kvm}/bin/qemu-img convert -O qcow2 $out/disk-image.raw $out/disk-image.qcow2
  '';

  /*
    A writable raw image at `$out/disk-image.raw`, for derivations
    whose output is the image: `createEmptyImage` plus the conversion
    above. `fillDiskWithRPMs`, `fillDiskWithDebs` and the disk check
    share it; `runInLinuxImage` keeps its own, since it converts a
    caller-provided image rather than making one.
  */
  createEmptyRawImage =
    {
      # Disk image size in MiB (1024*1024 bytes)
      size,
      # Name that will be written to $out/nix-support/full-name
      fullName,
    }:
    createEmptyImage { inherit size fullName; } + convertToRawImage;

  /*
    A raw ext4 image at `$out`, assembled host-side from store
    symlinks: a static busybox for `/bin/sh`, every `bin/*` of each
    package in `tools` linked into `/usr/bin`, and the loader, laid
    with `mkfs.ext4 -d` (no mounting). Nix binaries carry their
    RUNPATH, so the links run wherever the image boots. On colliding
    names the first package wins (the compiler wrapper and binutils
    share names); `setup` is a shell fragment populating `root/`
    further, e.g. a package database; `buildInputs` covers what it
    runs.
  */
  makeStoreImage =
    {
      name,
      # Disk image size in MiB (1024*1024 bytes)
      size ? 64,
      # Packages whose bin/* lands in the image's /usr/bin
      tools ? [ ],
      # Extra shell populating root/, e.g. mkdir plus database init
      setup ? "",
      buildInputs ? [ ],
    }:
    pkgs.runCommand name
      {
        buildInputs = [ pkgs.e2fsprogs ] ++ buildInputs;
      }
      ''
        mkdir -p root/{bin,usr/bin,lib64,etc,tmp}
        ln -s ${pkgs.pkgsStatic.busybox}/bin/busybox root/bin/busybox
        ln -s busybox root/bin/sh
        for p in ${toString tools}; do
          for b in $p/bin/*; do
            [ -e "root/usr/bin/$(basename "$b")" ] || ln -s "$b" root/usr/bin/
          done
        done
        ln -s ${pkgs.glibc}/lib/ld-linux-x86-64.so.2 root/lib64/ld-linux-x86-64.so.2
        ${setup}
        truncate -s ${toString size}M $out
        ${pkgs.e2fsprogs}/bin/mkfs.ext4 -F -d root $out
      '';

  /*
    A raw ext4 image carrying an RPM toolchain: `makeStoreImage` plus
    the RPM tools and an empty database. `buildRPM` takes it as
    `diskImage`; the caller brings the compiler through `tools`, since
    only it knows what the package builds with.
  */
  makeRPMToolchainImage =
    {
      name,
      # Disk image size in MiB (1024*1024 bytes)
      size ? 64,
      # Packages whose bin/* lands in the image's /usr/bin
      tools ? [ ],
      # Extra shell populating root/, after the database init
      setup ? "",
    }:
    makeStoreImage {
      inherit name size;
      buildInputs = [ pkgs.rpm ];
      tools = [ pkgs.rpm ] ++ tools;
      setup = ''
        mkdir -p root/var/lib/rpm
        rpm --initdb --dbpath $PWD/root/var/lib/rpm
        ${setup}
      '';
    };

  /*
    A `.deb` at `$out`, assembled host-side: `files` is a shell
    fragment populating `pkg/` (the payload directory; `DEBIAN`
    arrives pre-created), and the control file comes from the rest.
  */
  makeDeb =
    {
      name,
      version ? "1.0",
      architecture ? "amd64",
      maintainer ? "uml-vm-tools",
      description ? "${name} payload",
      extraControl ? "",
      files,
    }:
    pkgs.runCommand "${name}_${version}_${architecture}.deb" { buildInputs = [ pkgs.dpkg ]; } ''
      mkdir -p pkg/DEBIAN
      cat > pkg/DEBIAN/control <<EOF
      Package: ${name}
      Version: ${version}
      Architecture: ${architecture}
      Maintainer: ${maintainer}
      Description: ${description}${lib.optionalString (extraControl != "") "\n${extraControl}"}
      EOF
      ${files}
      dpkg-deb --build pkg $out
    '';

  /*
    The `.deb` a bare image needs before `fillDiskWithDebs` can
    install into it: store-backed `dpkg` (with the loader) for the
    chrooted `--install`, the `start-stop-daemon` the install dance
    moves aside, a `/bin/sh` (static busybox) plus a base tool set for
    maintainer scripts, and the skeleton a distro's base files would
    lay (the port's DB setup only touches these, never creates them).
    Install it first in every component: it is never a distro, so it
    stays check and bootstrap scaffolding rather than a payload.
  */
  makeDebShim =
    {
      name ? "uml-shim",
      version ? "1.0",
    }:
    makeDeb {
      inherit name version;
      description = "store shims for bare images";
      files = ''
        mkdir -p pkg/bin pkg/usr/bin pkg/sbin pkg/lib64 pkg/etc pkg/var/lib/dpkg
        ln -s ${pkgs.pkgsStatic.busybox}/bin/busybox pkg/bin/busybox
        ln -s busybox pkg/bin/sh
        for p in ${pkgs.coreutils} ${pkgs.findutils} ${pkgs.gnugrep} ${pkgs.gnused} ${pkgs.gawk}; do
          for b in $p/bin/*; do
            [ -e "pkg/bin/$(basename "$b")" ] || ln -s "$b" pkg/bin/
          done
        done
        ln -s ${pkgs.dpkg}/bin/dpkg pkg/usr/bin/dpkg
        ln -s ${pkgs.glibc}/lib/ld-linux-x86-64.so.2 pkg/lib64/ld-linux-x86-64.so.2
        echo '#!/bin/true' > pkg/sbin/start-stop-daemon
        chmod 755 pkg/sbin/start-stop-daemon
        touch pkg/etc/shells pkg/var/lib/dpkg/status pkg/var/lib/dpkg/available pkg/var/lib/dpkg/diversions
      '';
    };

  /*
    A bash fragment that formats the attached image and mounts it at
    `/mnt`, for the disk-filling ports. nixpkgs'
    `defaultCreateRootFS` (`pkgs/build-support/vm/default.nix:365`)
    with the device retargeted: `/dev/ubda`, the only disk UML
    attaches, for QEMU's `/dev/${hd}`. Two required changes: `/mnt`
    arrives pre-created from the host (the hostfs root mounts
    read-only), where QEMU's fresh tmpfs root never has it, and
    `/mnt/tmp` is laid too, for the `/tmp` bind the fill ports do
    next -- on a distro the filesystem package owns it, on a bare
    image nothing does (measured: the bind fails and the guest
    panics).
  */
  defaultCreateRootFS = ''
    mkdir -p /mnt
    ${pkgs.e2fsprogs}/bin/mkfs.ext4 /dev/ubda
    ${pkgs.util-linux}/bin/mount -t ext4 /dev/ubda /mnt

    if test -e /mnt/.debug; then
      exec ${bash}/bin/sh
    fi
    touch /mnt/.debug

    mkdir /mnt/proc /mnt/dev /mnt/sys /mnt/tmp
  '';

in
rec {
  inherit umlKernel createEmptyImage createEmptyRawImage defaultCreateRootFS;
  inherit makeStoreImage makeRPMToolchainImage makeDeb makeDebShim;

  # The nixpkgs-compatible surface: exactly the `vmTools` names this
  # set replaces. The overlay reads this list rather than naming them
  # again, so the two cannot drift apart; `umlKernel` and the helpers
  # above (`createEmptyRawImage`, the toolchain builders) stay out,
  # since nixpkgs' `vmTools` has none of them.
  names = [
    "runInLinuxVM"
    "runInLinuxImage"
    "extractFs"
    "extractMTDfs"
    "defaultCreateRootFS"
    "fillDiskWithRPMs"
    "fillDiskWithDebs"
    "buildRPM"
    "createEmptyImage"
    "rpmClosureGenerator"
    "debClosureGenerator"
    "makeImageFromRPMDist"
    "makeImageFromDebDist"
    "diskImageFuns"
    "diskImageExtraFuns"
    "diskImages"
    "makeImageTestScript"
    "vmRunCommand"
    "hd"
  ];

  /*
    Drop-in for `vmTools.vmRunCommand`
    (`pkgs/build-support/vm/default.nix:284`): the host half every
    guest boots through, documented where it is defined above. An
    alias, so callers reaching past `runInLinuxVM` to the runner
    itself stay on UML through the overlay instead of silently
    falling back to QEMU.
  */
  vmRunCommand = umlRunCommand;

  /*
    Drop-in for `vmTools.runInLinuxVM`
    (`pkgs/build-support/vm/default.nix:401`): same override protocol over
    the input derivation -- `memSize`, `QEMU_OPTS`, `UML_OPTS`, `builder`,
    `args`, `preVM`, `postVM`, `diskImage` all behave as documented there,
    except `QEMU_OPTS` is ignored (see `umlRunCommand`) and there is
    deliberately no `requiredSystemFeatures`: nothing here needs KVM.
    The guest is uniprocessor -- the UML kernel builds without SMP, which
    only costs boot time -- so `NIX_BUILD_CORES` and `-smp` in `QEMU_OPTS`
    have no effect.
  */
  runInLinuxVM =
    drv:
    lib.overrideDerivation drv (
      {
        memSize ? 512,
        QEMU_OPTS ? "",
        UML_OPTS ? "",
        args,
        builder,
        ...
      }:
      {
        builder = "${bash}/bin/sh";
        args = [
          "-e"
          umlRunCommand
        ];
        origArgs = args;
        origBuilder = builder;
        __structuredAttrs = true;
      }
    );

  /*
    Drop-in for `vmTools.runInLinuxImage`
    (`pkgs/build-support/vm/default.nix:493`): the derivation's
    `diskImage` becomes the guest root, while the payload still runs
    through the normal stage 2, with `PATH` and `SHELL` set to the
    image's native paths at the end. `postHook`, `origPostHook` and
    `fixupPhase` are nixpkgs' lines verbatim.

    What differs, and why:

    - `preVM` makes a writable raw copy (`qemu-img convert`) instead of
      a qcow2 copy-on-write overlay: UML reads block devices, not image
      formats. A directory `diskImage` still resolves to its
      `disk-image.qcow2`, as there. `diskImageFormat` defaults to `raw`
      when the caller sets none; nixpkgs requires it.
    - Only `mountDisk=true` (the default) takes the image branch: the
      builder environment carries it as `1`, which `umlRunCommand`
      turns into `UML_IMAGE=1`. QEMU device paths have no meaning here:
      the image is always `/dev/ubda`.
    - Setting `preVM` clobbers a caller-provided one, as in nixpkgs.
  */
  runInLinuxImage =
    drv:
    runInLinuxVM (
      lib.overrideDerivation drv (attrs: {
        mountDisk = attrs.mountDisk or true;

        preVM = ''
          diskImage=$(pwd)/disk-image.raw
          origImage=${attrs.diskImage}
          if test -d "$origImage"; then origImage="$origImage/disk-image.qcow2"; fi
          ${pkgs.buildPackages.qemu_kvm}/bin/qemu-img convert -f "${attrs.diskImageFormat or "raw"}" -O raw "$origImage" $diskImage
        '';

        postHook = ''
          PATH=/usr/bin:/bin:/usr/sbin:/sbin
          SHELL=/bin/sh
          eval "$origPostHook"
        '';

        origPostHook = lib.optionalString (attrs ? postHook) attrs.postHook;

        # Don't run Nix-specific build steps like patchelf.
        fixupPhase = "true";
      })
    );

  /*
    Drop-in for `vmTools.extractFs`
    (`pkgs/build-support/vm/default.nix:425`): loop-mount the file
    read-only inside the guest and copy its tree to `$out`. The
    `modprobe` and `/lib` lines are gone: `loop` and `ext4` are built
    into the guest kernel, so there is nothing to load. Images needing
    a driver the kernel leaves out stay QEMU's job.
  */
  extractFs =
    {
      file,
      fs ? null,
    }:
    runInLinuxVM (
      stdenv.mkDerivation {
        name = "extract-file";
        buildInputs = [ pkgs.util-linux ];
        buildCommand = ''
          mkdir -p $out
          mkdir -p tmp
          mount -o loop,ro,ufstype=44bsd ${lib.optionalString (fs != null) "-t ${fs} "}${file} tmp ||
            mount -o loop,ro ${lib.optionalString (fs != null) "-t ${fs} "}${file} tmp
          cp -Rv tmp/* $out/ || exit 0
        '';
      }
    );

  /*
    Drop-in for `vmTools.extractMTDfs`
    (`pkgs/build-support/vm/default.nix:454`): copy the file onto the
    RAM-emulated MTD device, mount its block interface and copy the
    tree to `$out`. The `modprobe` and `/lib` lines are gone: `mtd`,
    `mtdram`, `mtdblock` and `jffs2` are built into the guest kernel
    (`CONFIG_MTD_MTDRAM=y` and friends in its built config, sized by
    `MTDRAM_TOTAL_SIZE=131072` to match nixpkgs' `modprobe mtdram
    total_size=131072`), and the char device (`/dev/mtd0`) ships with
    the MTD core, so there is nothing to load.

    One deliberate divergence: with `fs = null` (the default) this
    mounts `-t jffs2` instead of leaving detection to `mount`.
    Detection cannot work -- `mount` without `-t` skips `nodev`
    filesystems, `jffs2` sets no `FS_REQUIRES_DEV` (verified in the
    6.18.55 tree), and `blkid` carries no jffs2 prober -- so nixpkgs'
    bare `mount` fails the same way on every backend (it has no test
    and no callers). An explicit `fs` still passes through for other
    MTD filesystems.
  */
  extractMTDfs =
    {
      file,
      fs ? null,
    }:
    runInLinuxVM (
      stdenv.mkDerivation {
        name = "extract-file-mtd";
        buildInputs = [
          pkgs.util-linux
          pkgs.mtdutils
        ];
        buildCommand = ''
          mkdir -p $out
          mkdir -p tmp

          dd if=${file} of=/dev/mtd0
          mount -t ${if fs != null then fs else "jffs2"} /dev/mtdblock0 tmp

          cp -R tmp/* $out/
        '';
      }
    );

  /*
    Drop-in for `vmTools.fillDiskWithRPMs`
    (`pkgs/build-support/vm/default.nix:533`): format the image with
    `createRootFS`, unpack the RPMs under `/mnt`, and install them
    through a chroot. The `buildCommand` is nixpkgs' line for line,
    with only the tool paths retargeted; `createRootFS` defaults to
    the `defaultCreateRootFS` above. `preVM` makes a writable raw
    image with `createEmptyRawImage`, as in `runInLinuxImage`: UML
    reads block devices, not qcow2. `postVM` refreshes the qcow2 from
    the filled raw, so both files in `$out` are the image.
  */
  fillDiskWithRPMs =
    {
      size ? 4096,
      rpms,
      name,
      fullName,
      preInstall ? "",
      postInstall ? "",
      runScripts ? true,
      createRootFS ? defaultCreateRootFS,
      QEMU_OPTS ? "",
      memSize ? 512,
      unifiedSystemDir ? false,
    }:

    runInLinuxVM (
      stdenv.mkDerivation {
        inherit
          name
          preInstall
          postInstall
          rpms
          QEMU_OPTS
          memSize
          ;
        preVM = createEmptyRawImage { inherit size fullName; };

        postVM = refreshQcow2Image;

        buildCommand = ''
          ${createRootFS}

          chroot=$(type -tP chroot)

          # Make the Nix store available in /mnt, because that's where the RPMs live.
          mkdir -p /mnt${storeDir}
          ${pkgs.util-linux}/bin/mount -o bind ${storeDir} /mnt${storeDir}
          # Some programs may require devices in /dev to be available (e.g. /dev/random)
          ${pkgs.util-linux}/bin/mount -o bind /dev /mnt/dev

          # Newer distributions like Fedora 18 require /lib etc. to be
          # symlinked to /usr.
          ${lib.optionalString unifiedSystemDir ''
            mkdir -p /mnt/usr/bin /mnt/usr/lib /mnt/usr/lib64
            ln -s /usr/bin /mnt/bin
            ln -s /usr/bin /mnt/sbin
            ln -s /usr/bin /mnt/usr/sbin
            ln -s /usr/lib /mnt/lib
            ln -s /usr/lib64 /mnt/lib64
            ${pkgs.util-linux}/bin/mount -t proc none /mnt/proc
          ''}

          echo "unpacking RPMs..."
          set +o pipefail
          for i in "''${rpms[@]}"; do
              echo "$i..."
              ${pkgs.rpm}/bin/rpm2cpio "$i" | chroot /mnt ${pkgs.cpio}/bin/cpio -i --make-directories --unconditional
          done

          eval "$preInstall"

          echo "initialising RPM DB..."
          PATH=/usr/bin:/bin:/usr/sbin:/sbin $chroot /mnt \
            ldconfig -v || true
          PATH=/usr/bin:/bin:/usr/sbin:/sbin $chroot /mnt \
            rpm --initdb

          ${pkgs.util-linux}/bin/mount -o bind /tmp /mnt/tmp

          echo "installing RPMs..."
          PATH=/usr/bin:/bin:/usr/sbin:/sbin $chroot /mnt \
            rpm -iv --nosignature ${lib.optionalString (!runScripts) "--noscripts"} "''${rpms[@]}"

          echo "running post-install script..."
          eval "$postInstall"

          rm /mnt/.debug

          ${pkgs.util-linux}/bin/umount /mnt${storeDir} /mnt/tmp /mnt/dev ${lib.optionalString unifiedSystemDir "/mnt/proc"}
          ${pkgs.util-linux}/bin/umount /mnt
        '';

        passthru = { inherit fullName; };
      }
    );

  /*
    Drop-in for `vmTools.fillDiskWithDebs`
    (`pkgs/build-support/vm/default.nix:724`): format the image with
    `createRootFS`, unpack the Debs under `/mnt`, and install them
    through a chroot, component by component. The `buildCommand` is
    nixpkgs' line for line, with only the tool paths retargeted, plus
    one bind: the store at its canonical path under `/mnt`, as
    `fillDiskWithRPMs` already binds. On a distro the image carries
    its own toolchain and the extra bind is inert; on a bare image
    the symlinked tools (the shim's dpkg above all) dangle without
    it (measured: the chrooted install dies silently behind
    `|| true`). `preVM` makes a writable raw image, as above, and
    `postVM` refreshes the qcow2 from the filled raw, as above.
    `debs` keeps its grouped shape (list of lists); `...` absorbs
    the rest into the derivation, as there -- a caller-passed
    `postVM` replaces the refresh, as a caller-passed `preVM` would
    replace the image.
  */
  fillDiskWithDebs =
    {
      size ? 4096,
      debs,
      name,
      fullName,
      postInstall ? null,
      createRootFS ? defaultCreateRootFS,
      QEMU_OPTS ? "",
      memSize ? 512,
      ...
    }@args:

    runInLinuxVM (
      stdenv.mkDerivation (
        {
          inherit
            name
            postInstall
            QEMU_OPTS
            memSize
            ;

          debsFlat = lib.flatten debs;
          debsGrouped = map toString debs;

          preVM = createEmptyRawImage { inherit size fullName; };

          postVM = refreshQcow2Image;

          buildCommand = ''
            ${createRootFS}

            PATH=$PATH:${
              lib.makeBinPath [
                pkgs.dpkg
                pkgs.glibc
                pkgs.xz
              ]
            }

            # Unpack the .debs.  We do this to prevent pre-install scripts
            # (which have lots of circular dependencies) from barfing.
            echo "unpacking Debs..."

            for deb in "''${debsFlat[@]}"; do
              echo "$deb..."
              dpkg-deb --extract "$deb" /mnt
            done

            # Make the Nix store available in /mnt, because that's where the .debs live.
            mkdir -p /mnt/inst${storeDir}
            ${pkgs.util-linux}/bin/mount -o bind ${storeDir} /mnt/inst${storeDir}
            ${pkgs.util-linux}/bin/mount -o bind /proc /mnt/proc
            ${pkgs.util-linux}/bin/mount -o bind /dev /mnt/dev
            # And at its canonical path: bare images carry the install
            # toolchain as store symlinks, which dangle in the chroot
            # without this (see above).
            mkdir -p /mnt${storeDir}
            ${pkgs.util-linux}/bin/mount -o bind ${storeDir} /mnt${storeDir}

            # Misc. files/directories assumed by various packages.
            echo "initialising Dpkg DB..."
            touch /mnt/etc/shells
            touch /mnt/var/lib/dpkg/status
            touch /mnt/var/lib/dpkg/available
            touch /mnt/var/lib/dpkg/diversions

            # Now install the .debs.  This is basically just to register
            # them with dpkg and to make their pre/post-install scripts
            # run.
            echo "installing Debs..."

            export DEBIAN_FRONTEND=noninteractive

            for component in "''${debsGrouped[@]}"; do
              echo
              echo ">>> INSTALLING COMPONENT: $component"
              debs=
              for i in $component; do
                debs="$debs /inst/$i";
              done
              chroot=$(type -tP chroot)

              # Create a fake start-stop-daemon script, as done in debootstrap.
              mv "/mnt/sbin/start-stop-daemon" "/mnt/sbin/start-stop-daemon.REAL"
              echo "#!/bin/true" > "/mnt/sbin/start-stop-daemon"
              chmod 755 "/mnt/sbin/start-stop-daemon"

              PATH=/usr/bin:/bin:/usr/sbin:/sbin $chroot /mnt \
                /usr/bin/dpkg --install --force-all $debs < /dev/null || true

              # Move the real start-stop-daemon back into its place.
              mv "/mnt/sbin/start-stop-daemon.REAL" "/mnt/sbin/start-stop-daemon"
            done

            echo "running post-install script..."
            eval "$postInstall"

            rm /mnt/.debug

            ${pkgs.util-linux}/bin/umount /mnt/inst${storeDir}
            ${pkgs.util-linux}/bin/umount /mnt${storeDir}
            ${pkgs.util-linux}/bin/umount /mnt/proc
            ${pkgs.util-linux}/bin/umount /mnt/dev
            ${pkgs.util-linux}/bin/umount /mnt
          '';

          passthru = { inherit fullName; };
        }
        // args
      )
    );

  /*
    Drop-in for `vmTools.buildRPM`
    (`pkgs/build-support/vm/default.nix:650`): build the tarball `src`
    (which must contain an RPM specfile) with `rpmbuild` inside the
    distribution installed in `diskImage`, and copy the resulting RPMs
    to `$out`. The derivation is nixpkgs' verbatim, run through
    `runInLinuxImage` here instead of there: `prepareImagePhase`,
    `sysInfoPhase`, `buildPhase` and `installPhase` all execute under
    the image's root with its native toolchain.
  */
  buildRPM =
    attrs:
    runInLinuxImage (
      stdenv.mkDerivation (
        {
          prePhases = [
            "prepareImagePhase"
            "sysInfoPhase"
          ];
          dontConfigure = true;

          outDir = "rpms/${attrs.diskImage.name}";

          prepareImagePhase = ''
            if test -n "$extraRPMs"; then
              for rpmdir in $extraRPMs ; do
                rpm -iv $(ls $rpmdir/rpms/*/*.rpm | grep -v 'src\.rpm' | sort | head -1)
              done
            fi
          '';

          sysInfoPhase = ''
            echo "System/kernel: $(uname -a)"
            if test -e /etc/fedora-release; then echo "Fedora release: $(cat /etc/fedora-release)"; fi
            if test -e /etc/SuSE-release; then echo "SUSE release: $(cat /etc/SuSE-release)"; fi
            echo "installed RPM packages"
            rpm -qa --qf "%{Name}-%{Version}-%{Release} (%{Arch}; %{Distribution}; %{Vendor})\n"
          '';

          buildPhase = ''
            eval "$preBuild"

            srcName="$(rpmspec --srpm -q --qf '%{source}' *.spec)"
            cp "$src" "$srcName" # `ln' doesn't work always work: RPM requires that the file is owned by root

            export HOME=/tmp/home
            mkdir $HOME

            rpmout=/tmp/rpmout
            mkdir $rpmout $rpmout/SPECS $rpmout/BUILD $rpmout/RPMS $rpmout/SRPMS

            echo "%_topdir $rpmout" >> $HOME/.rpmmacros

            if [ `uname -m` = i686 ]; then extra="--target i686-linux"; fi
            rpmbuild -vv $extra -ta "$srcName"

            eval "$postBuild"
          '';

          installPhase = ''
            eval "$preInstall"

            mkdir -p $out/$outDir
            find $rpmout -name "*.rpm" -exec cp {} $out/$outDir \;

            for i in $out/$outDir/*.rpm; do
              echo "Generated RPM/SRPM: $i"
              rpm -qip $i
            done

            eval "$postInstall"
          ''; # */
        }
        // attrs
      )
    );

  /*
    The disk nixpkgs' ports attach. QEMU's is `/dev/vda`; UML's only
    disk is `/dev/ubda`, so callers building device paths from
    `vmTools.hd` keep working through the overlay.
  */
  hd = "ubda";

  /*
    Drop-in for `vmTools.rpmClosureGenerator`
    (`pkgs/build-support/vm/default.nix:835`): host-side Perl over the
    repositories' metadata, producing a Nix expression of `fetchurl`
    calls for the package closure. nixpkgs' body verbatim, with only
    the tool paths retargeted and the closure script read from the
    consumer's own nixpkgs, so it cannot skew from the `vmTools`
    being replaced.
  */
  rpmClosureGenerator =
    {
      name,
      packagesLists,
      urlPrefixes,
      packages,
      archs ? [ ],
    }:
    assert (builtins.length packagesLists) == (builtins.length urlPrefixes);
    pkgs.runCommand "${name}.nix"
      {
        nativeBuildInputs = [
          pkgs.perl
          pkgs.perlPackages.URI
          pkgs.perlPackages.XMLSimple
          pkgs.zstd
        ];
        inherit archs;
      }
      ''
        ${lib.concatImapStrings (i: pl: ''
          echo "decompressing ${pl}..."
          case ${pl} in
            *.zst)
              zstd -d < ${pl} > ./packages_${toString i}.xml
              ;;
            *.xz | *.lzma)
              xz -d < ${pl} > ./packages_${toString i}.xml
              ;;
            *.bz2)
              bunzip2 < ${pl} > ./packages_${toString i}.xml
              ;;
            *.gz)
              gunzip < ${pl} > ./packages_${toString i}.xml
              ;;
            *)
              cp ${pl} ./packages_${toString i}.xml
              ;;
          esac
        '') packagesLists}
        perl -w ${pkgs.path + "/pkgs/build-support/vm/rpm/rpm-closure.pl"} \
          ${
            lib.concatImapStrings (i: pl: "./packages_${toString i}.xml ${pl.snd} ") (
              lib.zipLists packagesLists urlPrefixes
            )
          } \
          ${toString packages} > $out
      '';

  /*
    Drop-in for `vmTools.debClosureGenerator`
    (`pkgs/build-support/vm/default.nix:942`): same shape, over a
    Debian/Ubuntu `Packages` file. Same retargeting as above.
  */
  debClosureGenerator =
    {
      name,
      packagesLists,
      urlPrefix,
      packages,
    }:

    pkgs.runCommand "${name}.nix"
      {
        nativeBuildInputs = [
          pkgs.perl
          pkgs.dpkg
          pkgs.nixfmt
        ];
      }
      ''
        for i in ${toString packagesLists}; do
          echo "adding $i..."
          case $i in
            *.xz | *.lzma)
              xz -d < $i >> ./Packages
              ;;
            *.bz2)
              bunzip2 < $i >> ./Packages
              ;;
            *.gz)
              gzip -dc < $i >> ./Packages
              ;;
          esac
        done

        perl -w ${pkgs.path + "/pkgs/build-support/vm/deb/deb-closure.pl"} \
          ./Packages ${urlPrefix} ${toString packages} > $out
        nixfmt $out
      '';

  /*
    Drop-in for `vmTools.makeImageFromRPMDist`
    (`pkgs/build-support/vm/default.nix:891`): the closure generator
    above plus `fillDiskWithRPMs`. nixpkgs' body verbatim -- in this
    scope `fillDiskWithRPMs` and `rpmClosureGenerator` are already
    this set's, which is the whole point: through the overlay this is
    the function that used to boot QEMU.
  */
  makeImageFromRPMDist =
    {
      name,
      fullName,
      size ? 4096,
      urlPrefix ? "",
      urlPrefixes ? [ urlPrefix ],
      packagesList ? "",
      packagesLists ? [ packagesList ],
      packages,
      extraPackages ? [ ],
      preInstall ? "",
      postInstall ? "",
      archs ? [
        "noarch"
        "i386"
      ],
      runScripts ? true,
      createRootFS ? defaultCreateRootFS,
      QEMU_OPTS ? "",
      memSize ? 512,
      unifiedSystemDir ? false,
    }:

    fillDiskWithRPMs {
      inherit
        name
        fullName
        size
        preInstall
        postInstall
        runScripts
        createRootFS
        unifiedSystemDir
        QEMU_OPTS
        memSize
        ;
      rpms = import (rpmClosureGenerator {
        inherit
          name
          packagesLists
          urlPrefixes
          archs
          ;
        packages = packages ++ extraPackages;
      }) { inherit (pkgs) fetchurl; };
    };

  /*
    Drop-in for `vmTools.makeImageFromDebDist`
    (`pkgs/build-support/vm/default.nix:987`): same shape, over
    `fillDiskWithDebs`.
  */
  makeImageFromDebDist =
    {
      name,
      fullName,
      size ? 4096,
      urlPrefix,
      packagesList ? "",
      packagesLists ? [ packagesList ],
      packages,
      extraPackages ? [ ],
      postInstall ? "",
      extraDebs ? [ ],
      createRootFS ? defaultCreateRootFS,
      QEMU_OPTS ? "",
      memSize ? 512,
      ...
    }@args:

    let
      expr = debClosureGenerator {
        inherit name packagesLists urlPrefix;
        packages = packages ++ extraPackages;
      };
    in
    (fillDiskWithDebs (
      {
        inherit
          name
          fullName
          size
          postInstall
          createRootFS
          QEMU_OPTS
          memSize
          ;
        debs = import expr { inherit (pkgs) fetchurl; } ++ extraDebs;
      }
      // args
    ))
    // {
      inherit expr;
    };

  /*
    Drop-ins for `vmTools.diskImageFuns`, `diskImageExtraFuns` and
    `diskImages` (`pkgs/build-support/vm/default.nix:1599`): the
    makers above mapped over the distro data, which rides
    `pkgs.vmTools` unchanged (pure host-side data, no builder in it).
  */
  diskImageFuns =
    (lib.mapAttrs (
      name: as: as2:
      makeImageFromRPMDist (as // as2)
    ) pkgs.vmTools.rpmDistros)
    // (lib.mapAttrs (
      name: as: as2:
      makeImageFromDebDist (as // as2)
    ) pkgs.vmTools.debDistros);

  diskImageExtraFuns = lib.mapAttrs (
    name: f: extraPackages:
    f { inherit extraPackages; }
  ) diskImageFuns;

  diskImages = lib.mapAttrs (name: f: f { }) diskImageFuns;

  /*
    Drop-in for `vmTools.makeImageTestScript`
    (`pkgs/build-support/vm/default.nix:622`): build a script that
    boots `image` for inspection by hand. nixpkgs' script makes a
    qcow2 copy-on-write overlay and boots QEMU with the distro's own
    init; this one copies the raw image aside (preferring the raw
    beside the qcow2, converting when only the qcow2 is there -- UML
    reads block devices, not image formats) and boots the UML kernel
    with `/bin/sh` as init, since there is no systemd here to parent
    a distro boot. The copy keeps the store image pristine, as the
    overlay does. Usage: `image-test [writable-copy]`, memory via
    `UML_MEMSIZE` (MiB, default 1024).
  */
  makeImageTestScript =
    image:
    pkgs.writeScript "image-test" ''
      #! ${bash}/bin/sh
      if test -z "$1"; then
        echo "Syntax: $0 <writable-copy>"
        exit 1
      fi
      diskImage="$1"
      if ! test -e "$diskImage"; then
        if test -f ${image}/disk-image.raw; then
          cp --sparse=always ${image}/disk-image.raw "$diskImage"
        else
          ${pkgs.buildPackages.qemu_kvm}/bin/qemu-img convert -f qcow2 -O raw ${image}/disk-image.qcow2 "$diskImage"
        fi
      fi
      chmod +w "$diskImage"
      exec ${umlKernel}/linux root=/dev/ubda rw init=/bin/sh mem=''${UML_MEMSIZE:-1024}M panic=1 umid=uml-image-test-$$ ubd0="$diskImage"
    '';
}
