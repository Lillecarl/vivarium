# The container images a kubeadm cluster needs, built from nixpkgs.
#
# A guest has no route to registry.k8s.io inside a build sandbox, so the
# images cannot be pulled -- they are built here and imported into
# containerd at boot.  Each one is a binary from nixpkgs wearing the name
# and tag kubeadm looks up, which means the *tag* is whatever kubeadm
# asks for and the *binary* is whatever nixpkgs has.  Those are close but
# not equal (nixpkgs may carry a newer etcd than the one kubeadm names),
# and that is what `check` below is for.
#
# The images are almost empty.  A guest already has the whole host store
# under /nix/store over hostfs, so an image that carried its own copy of
# glibc would be asking containerd to unpack, onto a slow virtual disk,
# something the node can already see -- half a gigabyte of it, on every
# node, before kubelet is any use.  Instead each image is a handful of
# symlinks into /nix/store, and modules/k8s.nix mounts the store into
# every container so that they resolve.
{
  lib,
  runCommand,
  dockerTools,
  kubernetes,
  etcd,
  coredns,
  iptables,
  ipset,
  conntrack-tools,
  ethtool,
  pkgsStatic,
  # Overrides for the tags below.  A Kubernetes newer than the one these
  # were pinned for names different etcd and CoreDNS tags, and the images
  # are looked up by tag -- see `services.vivarium-k8s.imageTags`.
  imageTags ? { },
}:
let
  version = kubernetes.version;

  /*
    What kubeadm asks for, for this Kubernetes version.

    Written down rather than parsed out of the kubeadm source: the
    constants moved between releases, and a regex over Go source fails by
    silently producing "unknown" -- which looks like a pull failure at
    boot, three minutes into a test.  `check` runs the real kubeadm and
    fails the build if any of this has drifted.
  */
  # These follow the kubeadm in the nixpkgs `default.nix` is given, which
  # is the umbrella's pin -- Kubernetes 1.37.0 today.  `flake.nix` brings
  # a newer nixpkgs of its own and `check-k8s-images` will say so there.
  tags = {
    coredns = "v1.14.6";
    etcd = "3.7.0-0";
    pause = "3.10.2";
  } // imageTags;

  /*
    One image: *command*, and whatever it shells out to, as symlinks.

    The path matters.  kubeadm's static pods run `kube-apiserver` and
    friends by bare name, resolved against the image's PATH; the
    kube-proxy DaemonSet hard-codes `/usr/local/bin/kube-proxy`; CoreDNS
    and pause have no command at all and run the entrypoint.  Putting the
    binary at /usr/local/bin/<command> and pointing the entrypoint at it
    satisfies all three.

    `includeStorePaths` is what makes these tiny: the layer holds the
    symlinks and nothing they point at.  They are written straight into
    the layer rather than passed as `contents`, which would route each
    one through a store path of its own -- correct, but two hops to say
    what one says.
  */
  mkImage =
    {
      name,
      tag,
      command,
      package,
      extraPackages ? [ ],
      selfContained ? false,
    }:
    dockerTools.buildLayeredImage {
      inherit name tag;
      includeStorePaths = selfContained;
      extraCommands = ''
        mkdir -p usr/local/bin tmp
        ${lib.concatMapStringsSep "\n" (
          pkg: "ln -sfn ${pkg}/bin/* usr/local/bin/"
        ) extraPackages}
        ln -sfn ${package}/bin/${command} usr/local/bin/${command}
      '';
      config = {
        Entrypoint = [ "/usr/local/bin/${command}" ];
        # A container gets no environment at all otherwise, and Go's
        # exec.LookPath has no built-in default.
        Env = [ "PATH=/usr/local/bin" ];
        WorkingDir = "/";
      };
    };

  imageSpecs = [
    {
      name = "registry.k8s.io/kube-apiserver";
      tag = "v${version}";
      command = "kube-apiserver";
      package = kubernetes;
    }
    {
      name = "registry.k8s.io/kube-controller-manager";
      tag = "v${version}";
      command = "kube-controller-manager";
      package = kubernetes;
    }
    {
      name = "registry.k8s.io/kube-scheduler";
      tag = "v${version}";
      command = "kube-scheduler";
      package = kubernetes;
    }
    {
      name = "registry.k8s.io/kube-proxy";
      tag = "v${version}";
      command = "kube-proxy";
      package = kubernetes;
      # kube-proxy does not write rules itself: it execs iptables-save
      # and iptables-restore, and reads conntrack, from inside its own
      # container.  Upstream's image bundles these for the same reason.
      extraPackages = [
        iptables
        ipset
        conntrack-tools
        ethtool
      ];
    }
    {
      name = "registry.k8s.io/etcd";
      tag = tags.etcd;
      command = "etcd";
      package = etcd;
    }
    {
      name = "registry.k8s.io/coredns/coredns";
      tag = tags.coredns;
      command = "coredns";
      package = coredns;
    }
    {
      name = "registry.k8s.io/pause";
      tag = tags.pause;
      command = "pause";
      package = kubernetes.pause;
      # The one image that has to carry its own closure.  pause runs as
      # the pod sandbox, and containerd builds the sandbox's OCI spec
      # without consulting base_runtime_spec -- so it is the one
      # container on the node that does not get /nix/store mounted.  It
      # is also the smallest: a dynamically linked hello-world and the
      # glibc under it.
      selfContained = true;
    }
  ];

  images = map mkImage imageSpecs;

  # The images that resolve through the store mount rather than carrying
  # what they run.
  linked = lib.filter (spec: !(spec.selfContained or false)) imageSpecs;

  /*
    Every store path those symlinks point at.

    A layered image is a *gzipped* tar, so the store paths written into
    it are invisible to Nix: `nix-store --query --references` on the
    tarball comes back empty.  Nothing would pull etcd into a build that
    only asked for the images, and the guest would boot with a
    /usr/local/bin full of dangling symlinks -- which runc reports as
    `executable file not found in $PATH`, indistinguishable from an image
    built without the binary in it.  kube-apiserver and friends survive
    that by accident, because the node installs kubeadm and kubelet out
    of the same derivation; etcd and CoreDNS have nothing else asking for
    them.  modules/k8s.nix turns this into a real dependency.
  */
  runtimeInputs = lib.unique (
    lib.concatMap (spec: [ spec.package ] ++ spec.extraPackages or [ ]) linked
  );

  # Something to actually schedule.  Static busybox is three megabytes
  # and brings httpd, wget and nslookup, which between them are enough to
  # tell whether pods, Services and cluster DNS work.
  workload = dockerTools.buildLayeredImage {
    name = "vivarium.test/busybox";
    tag = "1";
    # A copy rather than a symlink: this one owes nothing to /nix/store,
    # so it also works as a check that a plain image still runs.
    includeStorePaths = false;
    extraCommands = ''
      mkdir -p bin tmp
      cp ${pkgsStatic.busybox}/bin/busybox bin/busybox
      for applet in $(bin/busybox --list); do
        ln -sfn busybox "bin/$applet"
      done
    '';
    config = {
      Entrypoint = [ "/bin/sh" ];
      Env = [ "PATH=/bin" ];
      WorkingDir = "/";
    };
  };

  built = [
    "registry.k8s.io/kube-apiserver:v${version}"
    "registry.k8s.io/kube-controller-manager:v${version}"
    "registry.k8s.io/kube-scheduler:v${version}"
    "registry.k8s.io/kube-proxy:v${version}"
    "registry.k8s.io/coredns/coredns:${tags.coredns}"
    "registry.k8s.io/pause:${tags.pause}"
    "registry.k8s.io/etcd:${tags.etcd}"
  ];
in
{
  # One tarball, so a node imports everything in a single pass.
  tarball = dockerTools.mergeImages (images ++ [ workload ]);

  inherit runtimeInputs;

  # What each image will try to exec, for a test to check before a
  # cluster spends twenty minutes discovering it the hard way.
  entrypoints = map (spec: "${spec.package}/bin/${spec.command}") linked;

  # What a test should ask to be scheduled.  There is no registry, so
  # anything using this has to say imagePullPolicy: Never.
  workloadImage = "vivarium.test/busybox:1";

  # containerd carries its own default for this, which tracks its own
  # release rather than kubeadm's -- so it is worth saying out loud.
  # Getting it wrong means every pod sandbox fails to start, reported as
  # a pull error for an image nothing in the configuration mentions.
  sandboxImage = "registry.k8s.io/pause:${tags.pause}";

  /*
    Ask kubeadm what it will look for, and fail if it is not what we
    built.

    Without this, a nixpkgs bump that moves the CoreDNS or etcd tag turns
    into a cluster that comes up with three of its pods stuck in
    ImagePullBackOff, twenty minutes into CI.
  */
  check =
    runCommand "k8s-images-match-kubeadm"
      {
        nativeBuildInputs = [ kubernetes ];
        expected = lib.concatMapStrings (image: "${image}\n") (lib.sort (a: b: a < b) built);
        passAsFile = [ "expected" ];
      }
      ''
        kubeadm config images list --kubernetes-version "v${version}" | sort > actual
        if ! diff --unified "$expectedPath" actual; then
          echo
          echo "error: the images built by modules/k8s-images.nix are not the ones"
          echo "kubeadm ${version} will look for.  Update the tags in that file to"
          echo "the right-hand side above."
          exit 1
        fi
        touch $out
      '';
}
