# UML-backed implementations of the `pkgs.vmTools` build-time primitives.
#
# Same call shapes as `pkgs/build-support/vm/default.nix`, so a caller that
# builds through `vmTools.runInLinuxVM` today can swap this set in with the
# `uml-vm-tools` overlay and change nothing else. No `/dev/kvm`, no `kvm`
# feature, no root: the guest is a User-Mode Linux kernel running as a
# child of the builder, which is why this works in a Nix build sandbox and
# on machines without KVM.
#
# v1 covers `runInLinuxVM`. Its siblings (`runInLinuxImage`, `extractFs`,
# `extractMTDfs`, `fillDiskWithRPMs`, `fillDiskWithDebs`, `buildRPM`) follow
# the same host/guest split and come next; each names what it needs below.
{ pkgs
, lib ? pkgs.lib
, storeDir ? builtins.storeDir
}:
let
  inherit (pkgs) stdenv bash coreutils busybox;

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

    - No stage 1, no initrd, no `switch_root`. The guest root is a host
      directory booted straight over hostfs (`rootfstype=hostfs`), so one
      script does what the initrd plus stage 2 did. Nothing is copied.
    - `virtiofs` mounts become `hostfs` mounts: the host store read-write
      at its own path (outputs land in the host store directly, as with
      virtiofs `shared-dir`), the build directory at `/tmp/xchg`.
    - No modprobe setup. The UML kernel carries its config built in, and
      `runInLinuxVM` payloads load no modules. (`extractFs` needs `loop`,
      so its port carries that instead.)
    - No debug shell. nixpkgs drops to an interactive shell when the image
      runs twice; here every run executes the payload, and `-K` keeps
      `run-uml` for inspection by hand.
    - No `lo` interface. The QEMU guest brings one up, but the payload
      runs offline either way; nothing in the protocol needs it.
  */
  stage2Uml = pkgs.writeScript "uml-run-stage2" ''
    #!/bin/sh
    set -euo pipefail
    export PATH=/bin

    mount -t proc none /proc
    mount -t sysfs none /sys
    mount -t devtmpfs devtmpfs /dev
    ln -s /proc/self/fd /dev/fd
    ln -s /proc/self/fd/0 /dev/stdin
    ln -s /proc/self/fd/1 /dev/stdout
    ln -s /proc/self/fd/2 /dev/stderr

    echo "mounting host store and exchange directory..."
    mkdir -p ${storeDir}
    mount -t hostfs none ${storeDir} -o "$UML_STORE"
    mkdir -p /tmp /run
    mount -t tmpfs -o "mode=1777" none /tmp
    mkdir -p /tmp/xchg
    mount -t hostfs none /tmp/xchg -o "$UML_XCHG"

    mkdir -p /etc
    ln -sf /proc/mounts /etc/mtab
    echo "127.0.0.1 localhost" > /etc/hosts
    echo "root:x:0:0:System administrator:/root:/bin/sh" > /etc/passwd

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
    being mounted; the `mountDisk` plus `switch_root` semantics belong to
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

      # The guest root: busybox for the mounts, bash for `/bin/sh`
      # (shebangs in the payload expect it, as in nixpkgs' stage 2), and
      # the stage-2 script as `/stage2`. The store itself arrives over
      # hostfs, so nothing from it is copied here.
      guestRoot=$NIX_BUILD_TOP/uml-root
      ${coreutils}/bin/mkdir -p $guestRoot/bin
      ${coreutils}/bin/cp -p ${busybox}/bin/busybox $guestRoot/bin/busybox
      for applet in mount mkdir ln cat echo ls poweroff; do
        ${coreutils}/bin/ln -s busybox "$guestRoot/bin/$applet"
      done
      ${coreutils}/bin/ln -s ${bash}/bin/sh "$guestRoot/bin/sh"
      ${coreutils}/bin/cp ${stage2Uml} $guestRoot/stage2
      ${coreutils}/bin/chmod +x $guestRoot/stage2

      # The kernel passes unknown `name=value` arguments to PID 1 as
      # environment, which is how the guest learns the two host paths
      # (both free of spaces: the store and the build directory, so no
      # quoting -- the kernel keeps quote characters literally).
      # Written to a file so a failed build keeps it for debugging.
      # Everything with a `$` below expands when this script runs, so
      # `run-uml` carries baked absolute paths.
      ${coreutils}/bin/cat > ./run-uml <<EOF
      #! ${bash}/bin/sh
      ${umlKernel}/linux UML_STORE=${storeDir} UML_XCHG=$PWD/xchg rootfstype=hostfs rootflags=$PWD/uml-root init=/stage2 mem=''${memSize:-512}M panic=1 ''${diskImage:+ubd0=$diskImage} $UML_OPTS
      EOF

      ${coreutils}/bin/chmod +x ./run-uml
      ./run-uml

      if ! test -e xchg/in-vm-exit; then
        echo "UML guest didn't produce an exit code."
        exit 1
      fi

      exitCode="$(${coreutils}/bin/cat xchg/in-vm-exit)"
      if [ "$exitCode" != "0" ]; then
        exit "$exitCode"
      fi

      eval "$postVM"
    '';

in
{
  inherit umlKernel;

  /*
    Drop-in for `vmTools.runInLinuxVM`
    (`pkgs/build-support/vm/default.nix:401`): same override protocol over
    the input derivation -- `memSize`, `QEMU_OPTS`, `UML_OPTS`, `builder`,
    `args`, `preVM`, `postVM`, `diskImage` all behave as documented there,
    except `QEMU_OPTS` is ignored (see `umlRunCommand`) and there is
    deliberately no `requiredSystemFeatures`: nothing here needs KVM.
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
}
