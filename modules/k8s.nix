# A kubeadm node: a container runtime, kubelet, and the images to feed them.
#
# Everything a node can know about itself lives here.  Everything that
# needs to know about the *other* nodes -- the join command, which pod
# subnet each one was given, the routes between them -- is left to the
# test, which is the only thing that has the whole cluster in view.  That
# keeps this module free of peer lists and lets the same three lines
# describe a one-node cluster or a five-node one.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.uml-k8s;
  images = pkgs.callPackage ./k8s-images.nix { };

  kubernetes = pkgs.kubernetes;
  # The unit that serves the CRI, and where. `uml-k8s-cri.target` names the
  # unit, so `bring_up` waits for either without knowing which.
  cri =
    {
      containerd = {
        unit = "containerd.service";
        socket = "unix:///run/containerd/containerd.sock";
      };
      crio = {
        unit = "crio.service";
        socket = "unix:///run/crio/crio.sock";
      };
    }
    .${cfg.cri};
  criSocket = cri.socket;

  # The node's address on the segment.  vec0 is passt's NAT, and every
  # guest sits behind it on the same 10.0.2.x address -- so a node that
  # let kubelet pick its own IP would register the same InternalIP as
  # every other node, and the API server would talk to whichever one it
  # happened to reach.
  address = config.boot.uml.lan.address;
  nodeIp = if address == null then "0.0.0.0" else lib.head (lib.splitString "/" address);

  # kubeadm takes extraArgs as a list of name/value pairs from v1beta4 on.
  args = lib.mapAttrsToList (name: value: { inherit name value; });

  /*
    YAML 1.2, and not `pkgs.formats.yaml`, which is 1.1.

    The 1.1 writer puts `%YAML 1.1` and a document start over every mapping,
    and kubeadm's reader refuses the directive outright: a file holding one
    document and nothing else fails as "did not find expected <document
    start>", measured on 1.36.  Concatenating four of them is worse still --
    the separator in front of the second opens an empty document, which fails
    as "kind and apiVersion is mandatory".

    1.2 writes the mapping and nothing above it, which is what a reader that
    splits a stream on `---` wants.  Kubernetes reads 1.2 happily; the one
    thing to keep in mind is that 1.1's octal-looking scalars are gone, so a
    file mode belongs in a configuration here as the decimal number the API
    takes (420, not 0644).  Nix has no octal literal, so nothing here can
    write one by accident.
  */
  yaml = pkgs.formats.yaml_1_2 { };

  /*
    The store, mounted where it is actually needed.

    Every image here is a handful of symlinks into /nix/store
    (`includeStorePaths = false` in ./k8s-images.nix), so each container
    built from one needs the store to resolve them.  There are two ways to
    arrange that, and only one of them is honest.

    The tempting one is containerd's `base_runtime_spec`, which adds a
    mount to *every* container on the node in three lines.  It also makes
    the node lie: a pod that never asked for the store gets it anyway, so
    anything whose job is to put /nix into a pod -- nixkube, for one --
    can no longer be told apart from the node doing it.  A test of such a
    thing passes with its subject switched off.

    So the mount goes where it belongs.  kubeadm patches the four static
    pods and CoreDNS; `bring_up` patches kube-proxy, which kubeadm applies
    from a manifest baked into itself and offers no patch target for.  A
    test that schedules a pod of its own declares the volume itself, the
    way any pod on any cluster would.
  */
  storeVolume = {
    name = "nix-store";
    hostPath = {
      path = "/nix/store";
      type = "Directory";
    };
  };

  storeMount = {
    name = "nix-store";
    mountPath = "/nix/store";
    readOnly = true;
  };

  # A strategic-merge patch, which merges `containers` by name -- so naming
  # the one container is enough and nothing else in the pod is touched.
  podPatch =
    container:
    yaml.generate "patch-${container}.yaml" {
      spec = {
        containers = [
          {
            name = container;
            volumeMounts = [ storeMount ];
          }
        ];
        volumes = [ storeVolume ];
      };
    };

  deploymentPatch =
    container:
    yaml.generate "patch-deployment-${container}.yaml" {
      spec.template.spec = {
        containers = [
          {
            name = container;
            volumeMounts = [ storeMount ];
          }
        ];
        volumes = [ storeVolume ];
      };
    };

  /*
    What kubeadm will apply, named the way it looks them up.

    `<target>+<patchtype>.<extension>`, and the targets are fixed:
    etcd, kube-apiserver, kube-controller-manager, kube-scheduler,
    kubeletconfiguration and corednsdeployment.  kube-proxy is not among
    them -- see `bring_up`.
  */
  kubeadmPatches = pkgs.runCommand "kubeadm-patches" { } ''
    mkdir -p $out
    cp ${podPatch "etcd"} $out/etcd+strategic.yaml
    cp ${podPatch "kube-apiserver"} $out/kube-apiserver+strategic.yaml
    cp ${podPatch "kube-controller-manager"} $out/kube-controller-manager+strategic.yaml
    cp ${podPatch "kube-scheduler"} $out/kube-scheduler+strategic.yaml
    cp ${deploymentPatch "coredns"} $out/corednsdeployment+strategic.yaml
  '';

  nodeRegistration = {
    criSocket = criSocket;
  }
  // lib.optionalAttrs (cfg.images == "nix") {
    # There is no registry to reach: everything came from
    # k8s-load-images.service before kubelet was allowed to start.
    imagePullPolicy = "Never";
  }
  // {
    # A guest has no swap, one CPU and not much memory, and /proc/config.gz
    # is not compiled in -- all of which kubeadm would rather refuse than
    # warn about.
    ignorePreflightErrors = [
      "Swap"
      "SystemVerification"
      "NumCPU"
      "Mem"
    ];
    kubeletExtraArgs = args { node-ip = nodeIp; };
  };

  /*
    Deadlines, scaled for a guest that is a process on a shared builder.

    kubeadm's defaults assume a machine where the API server answers in
    milliseconds.  Under UML the control plane takes minutes to become
    healthy, and every one of these firing looks like a different bug --
    a join that "cannot reach the API server", a kubelet that "is not
    healthy", an etcd that "timed out".
  */
  timeouts = {
    controlPlaneComponentHealthCheck = "15m";
    kubeletHealthCheck = "10m";
    kubernetesAPICall = "5m";
    etcdAPICall = "5m";
    tlsBootstrap = "15m";
    discovery = "10m";
  };

  initConfig = yaml.generate "kubeadm-init.yaml" (
    {
      apiVersion = "kubeadm.k8s.io/v1beta4";
      kind = "InitConfiguration";
      localAPIEndpoint = {
        advertiseAddress = nodeIp;
        bindPort = 6443;
      };
      inherit nodeRegistration timeouts;
    }
    // lib.optionalAttrs (cfg.images == "nix") {
      # Where the store mount comes from -- see `kubeadmPatches`. An
      # upstream image needs no such thing.
      patches.directory = "${kubeadmPatches}";
    }
    // lib.optionalAttrs (cfg.skipAddons != [ ]) {
      skipPhases = map (addon: "addon/${addon}") cfg.skipAddons;
    }
  );

  clusterConfig = yaml.generate "kubeadm-cluster.yaml" {
    apiVersion = "kubeadm.k8s.io/v1beta4";
    kind = "ClusterConfiguration";
    # Pinned, or kubeadm asks dl.k8s.io what "stable" means and a
    # sandboxed guest waits out the DNS timeout before failing.
    kubernetesVersion = "v${kubernetes.version}";
    networking = {
      inherit (cfg) podSubnet serviceSubnet;
    };
    etcd.local = {
      dataDir = "/var/lib/etcd";
      # etcd measures the cluster in disk latency, and a UML block device
      # is slow enough that the defaults cost it an election every few
      # minutes.
      extraArgs = args {
        heartbeat-interval = "500";
        election-timeout = "5000";
      };
    };
  };

  kubeletConfig = yaml.generate "kubeadm-kubelet.yaml" (
    {
      apiVersion = "kubelet.config.k8s.io/v1beta1";
      kind = "KubeletConfiguration";
      cgroupDriver = "systemd";
      failSwapOn = false;
      /*
        Not /etc/resolv.conf.  With networkd that is a symlink to
        resolved's stub, which names 127.0.0.53 -- an address that inside a
        pod's own network namespace is the pod, so CoreDNS forwards to
        itself and its loop detector shoots it.

        Under `images = "nix"` this is a file that resolves nothing at all;
        see it below.  Under `"pull"` it is resolved's *other* file, the
        one listing the real upstream servers DHCP gave the guest, because
        a pod on such a node has names to resolve: the node pulls from
        registry.k8s.io, and whatever it deploys may substitute from a
        binary cache.  Measured -- with the unroutable file, nixkube's init
        container spends its life on "Resolving timed out after 15000
        milliseconds" against nixkube.cachix.org.
      */
      resolvConf =
        if cfg.images == "nix" then
          "/etc/kubernetes/resolv.conf"
        else
          "/run/systemd/resolve/resolv.conf";
      # Nothing here is fast, and a CRI call that takes a minute on a
      # loaded builder is normal rather than a hung runtime.
      runtimeRequestTimeout = "15m";
      # The defaults evict everything the moment a 1 GB guest gets busy,
      # which reads as pods mysteriously disappearing mid-test.
      evictionHard = {
        "memory.available" = "50Mi";
        "nodefs.available" = "5%";
        "imagefs.available" = "5%";
      };
    }
    /*
      With no CoreDNS, do not point pods at a resolver that is not there.

      kubeadm creates the `kube-dns` Service as part of the CoreDNS addon,
      so skipping the addon means the address kubelet hands to every pod --
      10.96.0.10 by default -- has no Service behind it at all.  Not an
      empty one that would answer with a refusal: nothing, so kube-proxy
      writes no rule and the packets are dropped.  A single lookup then
      costs the resolver's whole timeout chain, several times over for the
      search domains, and a container that resolves one name on startup
      looks hung for minutes.

      An empty `clusterDNS` makes kubelet give pods the node's own
      resolvConf instead, which resolves nothing either but says so at
      once.
    */
    // lib.optionalAttrs (lib.elem "coredns" cfg.skipAddons) { clusterDNS = [ ]; }
  );

  proxyConfig = yaml.generate "kubeadm-proxy.yaml" {
    apiVersion = "kubeproxy.config.k8s.io/v1alpha1";
    kind = "KubeProxyConfiguration";
    mode = "iptables";
    # Zero means "leave nf_conntrack_max alone".  kube-proxy's default
    # sizes the table from the core count and the node's memory, and on a
    # guest this small it picks a number the kernel refuses.
    conntrack = {
      maxPerCore = 0;
      min = 0;
    };
  };

  # kubeadm reads one file and splits it on ---, so the documents have to
  # arrive concatenated rather than as four --config arguments.  The
  # separator goes in front, not behind: a trailing one leaves an empty
  # final document.  Each part is a bare mapping; see the yaml note above.
  kubeadmConfig = pkgs.runCommand "kubeadm-config.yaml" { } ''
    for part in ${initConfig} ${clusterConfig} ${kubeletConfig} ${proxyConfig}; do
      echo ---
      cat "$part"
    done > $out
  '';

  /*
    Write the CNI configuration for this node.

    Not baked into the image because the subnet is not known until the
    cluster exists: kube-controller-manager carves a /24 out of the pod
    subnet per node and publishes it as the Node's spec.podCIDR, so the
    test reads it back and calls this.  Until then the node stays
    NotReady with "cni plugin not initialized", which is correct -- it
    is not.
  */
  /*
    The node's CNI, written once its podCIDR is known.

    isDefaultGateway implies isGateway, and makes the bridge plugin add
    the container's default route itself via the gateway it derives from
    the range.  Naming 0.0.0.0/0 in ipam.routes as well -- which most
    published conflists do, alongside the plain isGateway -- adds it
    twice here: the plugin treats an existing default route as its own
    only if that route names a gateway, and an ipam route does not, so it
    appends a second one and the netlink add comes back EEXIST.  kubelet
    reports that as `failed to add route ...: file exists`, and nothing
    that needs CNI ever gets a sandbox -- which in a cluster this size
    means CoreDNS and nothing else, long after the nodes all went Ready.
  */
  /*
    `ipMasq` follows `services.uml-k8s.images`.

    Under `"nix"` a pod has nowhere to go: the node holds every image it
    will ever run and the guest is in a build sandbox, so masquerading pod
    traffic out of `vec0` would add a rule nothing ever matches.

    Under `"pull"` it is the difference between a working cluster and a
    silent one. Pods live on `cni0` in the node's podCIDR, and without
    SNAT nothing they send past the node is ever answered. Measured: with
    it off, nixkube's init container spends its life on "Resolving timed
    out after 15000 milliseconds" and the DaemonSet never rolls out --
    with no error anywhere naming a route.
  */
  cniSetup = pkgs.writeShellApplication {
    name = "uml-k8s-cni";
    text = ''
      if [ $# -ne 1 ]; then
        echo "usage: uml-k8s-cni <pod-cidr>" >&2
        exit 1
      fi
      mkdir -p /etc/cni/net.d
      cat > /etc/cni/net.d/10-uml.conflist <<EOF
      {
        "cniVersion": "1.0.0",
        "name": "uml",
        "plugins": [
          {
            "type": "bridge",
            "bridge": "cni0",
            "isDefaultGateway": true,
            "hairpinMode": true,
            "ipMasq": ${if cfg.images == "nix" then "false" else "true"},
            "ipam": {
              "type": "host-local",
              "ranges": [ [ { "subnet": "$1" } ] ]
            }
          },
          {
            "type": "portmap",
            "capabilities": { "portMappings": true }
          }
        ]
      }
      EOF
      echo "uml-k8s-cni: $1 on cni0"
    '';
  };

  /*
    Join this node to an existing cluster.

    Takes what `kubeadm token create --print-join-command` prints, but as
    three arguments rather than a command line, so that the timeouts and
    the node's own registration above apply here too -- a join run from
    the printed command line gets kubeadm's defaults and gives up on the
    TLS bootstrap after five minutes.
  */
  joinConfig =
    {
      endpoint,
      token,
      hash,
    }:
    yaml.generate "kubeadm-join.yaml" (
      {
      apiVersion = "kubeadm.k8s.io/v1beta4";
      kind = "JoinConfiguration";
      inherit nodeRegistration timeouts;
      discovery.bootstrapToken = {
        apiServerEndpoint = endpoint;
        inherit token;
        caCertHashes = [ hash ];
      };
      }
      // lib.optionalAttrs (cfg.images == "nix") {
        patches.directory = "${kubeadmPatches}";
      }
    );

  joinNode = pkgs.writeShellApplication {
    name = "uml-k8s-join";
    runtimeInputs = [ kubernetes ];
    text = ''
      if [ $# -ne 3 ]; then
        echo "usage: uml-k8s-join <endpoint> <token> <ca-cert-hash>" >&2
        exit 1
      fi
      config=$(mktemp)
      trap 'rm -f "$config"' EXIT
      sed -e "s|@ENDPOINT@|$1|" -e "s|@TOKEN@|$2|" -e "s|@HASH@|$3|" \
        ${
          joinConfig {
            endpoint = "@ENDPOINT@";
            token = "@TOKEN@";
            hash = "@HASH@";
          }
        } > "$config"
      exec kubeadm join --config "$config" --v=2
    '';
  };

  storageRoot = "/var/lib/uml-storage";

  /*
    A default StorageClass, and the volumes behind it.  See
    `services.uml-k8s.persistentVolumes`.

    One document, as a `v1 List`, because `yaml.generate` writes a single
    mapping and `kubectl apply` reads a List as the resources in it.

    The capacity is a label and not a limit: nothing enforces a hostPath
    volume's size.  It is `boot.uml.diskSize` because that *is* the bound --
    the volumes are directories on the guest's root image, which also holds
    everything else the guest writes.  A fixed number here would be a claim
    the disk cannot honour, and the only thing that goes wrong is silence:
    a claim binds, the writes fail later with ENOSPC, and the PV still says
    it had room.

    The number is the whole disk, so it is still generous rather than
    honest -- it simply cannot be exceeded.

    `WaitForFirstConsumer` with `nodeAffinity`, the way a `local` volume is
    written.  The directory is on one node, so the scheduler has to place the
    pod before the claim can bind, or a multi-node cluster binds a claim to a
    directory on a machine the pod is not on.

    `Retain`, because there is no deleter to call: nothing reclaims a
    released volume here, and a test that wants a clean one starts a new
    guest.
  */
  storageManifest = yaml.generate "uml-storage.yaml" {
    apiVersion = "v1";
    kind = "List";
    items = [
      {
        apiVersion = "storage.k8s.io/v1";
        kind = "StorageClass";
        metadata = {
          name = "standard";
          annotations."storageclass.kubernetes.io/is-default-class" = "true";
        };
        provisioner = "kubernetes.io/no-provisioner";
        volumeBindingMode = "WaitForFirstConsumer";
        reclaimPolicy = "Retain";
      }
    ]
    ++ map (index: {
      apiVersion = "v1";
      kind = "PersistentVolume";
      metadata.name = "${config.networking.hostName}-${toString index}";
      spec = {
        capacity.storage = "${toString config.boot.uml.diskSize}Mi";
        accessModes = [ "ReadWriteOnce" ];
        persistentVolumeReclaimPolicy = "Retain";
        storageClassName = "standard";
        hostPath = {
          path = "${storageRoot}/${toString index}";
          type = "DirectoryOrCreate";
        };
        nodeAffinity.required.nodeSelectorTerms = [
          {
            matchExpressions = [
              {
                key = "kubernetes.io/hostname";
                operator = "In";
                values = [ config.networking.hostName ];
              }
            ];
          }
        ];
      };
    }) (lib.range 1 cfg.persistentVolumes);
  };

  /*
    The OCI runtimes a pod can ask for by `runtimeClassName`, beside runc.

    crun is a second runc: same shim, same spec, another binary. runsc is
    gVisor, and brings its own shim. Its `systemd-cgroup` matches the
    `SystemdCgroup` runc gets -- kubelet hands every runtime a cgroup parent
    in the systemd form, and runsc refuses one it was not told to expect.
    kata runs each pod in a QEMU VM of its own, through its own shim.
  */
  runtimeHandlers = {
    crun = {
      runtime_type = "io.containerd.runc.v2";
      options = {
        BinaryName = lib.getExe pkgs.crun;
        SystemdCgroup = true;
      };
    };
    runsc = {
      runtime_type = "io.containerd.runsc.v1";
      options = {
        TypeUrl = "io.containerd.runsc.v1.options";
        ConfigPath = toString (
          (pkgs.formats.toml { }).generate "runsc.toml" {
            runsc_config.systemd-cgroup = "true";
          }
        );
      };
    };
    kata = {
      runtime_type = "io.containerd.kata.v2";
      options.ConfigPath = kataConfig;
    };
  };

  # Not nixpkgs' 3.32.0, whose guest kernel oopses in virtio-fs; see the
  # package. A kata-runtime >= 4.0.0 in nixpkgs replaces it.
  kata = pkgs.callPackage ../pkgs/kata-runtime/package.nix { };
  kataConfig = "${kata}/share/defaults/kata-containers/configuration-qemu.toml";

  # What `bring_up` applies, like `storageManifest`: a RuntimeClass per
  # handler, named after it.
  runtimeManifest = yaml.generate "uml-runtimes.yaml" {
    apiVersion = "v1";
    kind = "List";
    items = map (name: {
      apiVersion = "node.k8s.io/v1";
      kind = "RuntimeClass";
      metadata.name = name;
      handler = name;
    }) cfg.runtimes;
  };
in
{
  options.services.uml-k8s = {
    enable = lib.mkEnableOption "a kubeadm Kubernetes node";

    role = lib.mkOption {
      type = lib.types.enum [
        "control-plane"
        "worker"
      ];
      description = ''
        Whether this node runs the control plane.  The difference is
        small: a control plane gets the kubeadm init configuration and a
        tmpfs for etcd, and a worker gets `uml-k8s-join`.  Both run the
        same kubelet and containerd.
      '';
    };

    podSubnet = lib.mkOption {
      type = lib.types.str;
      default = "10.244.0.0/16";
      description = ''
        Addresses pods are given, from which kube-controller-manager
        hands each node a /24.  Nothing routes between those /24s by
        itself -- see `uml-k8s-cni`.
      '';
    };

    serviceSubnet = lib.mkOption {
      type = lib.types.str;
      default = "10.96.0.0/12";
      description = "Addresses ClusterIP Services are given.";
    };

    skipAddons = lib.mkOption {
      type = lib.types.listOf (
        lib.types.enum [
          "coredns"
          "kube-proxy"
        ]
      );
      default = [ ];
      example = [
        "coredns"
        "kube-proxy"
      ];
      description = ''
        Addons kubeadm should not install, as `skipPhases` entries.

        Both exist to serve Services: kube-proxy makes a ClusterIP reachable
        and CoreDNS resolves its name. A test that deploys no Service needs
        neither, and installing them is not free -- two more pods on a guest
        that has one CPU, and a CoreDNS that spends the whole run timing out
        against an upstream resolver a build sandbox cannot reach, several
        lines per second.

        Keep this in step with `bring_up`'s `addons` argument in
        `vivarium_runner.cluster`. Waiting for a pod kubeadm was told not to
        create hangs until the deadline; not waiting for one that does exist
        lets a test run before cluster DNS answers.
      '';
    };

    nri = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Enable containerd's NRI plugin.

        NRI lets a plugin change a container's OCI spec between the moment
        kubelet asks for the container and the moment runc starts it --
        adding a mount, for instance. containerd ships it disabled.

        A plugin that finds no socket at /var/run/nri waits for one and says
        nothing, so a test whose subject uses NRI has to turn this on, and
        the failure without it looks like a plugin that never ran.
      '';
    };

    cri = lib.mkOption {
      type = lib.types.enum [
        "containerd"
        "crio"
      ];
      default = "containerd";
      description = ''
        The container runtime kubelet talks to. Both run runc by default,
        load the same images and speak NRI when `nri` is on.
      '';
    };

    runtimes = lib.mkOption {
      type = lib.types.listOf (
        lib.types.enum [
          "crun"
          "runsc"
          "kata"
        ]
      );
      default = [ ];
      example = [
        "crun"
        "runsc"
      ];
      description = ''
        OCI runtimes to offer beside runc, each as a containerd handler and
        a RuntimeClass of the same name. A pod picks one with
        `runtimeClassName`; a pod without one still gets runc.

        `runsc` is gVisor, and needs the QEMU backend and containerd: under
        UML its shim panics at start with "None of the address space sizes
        could be successfully mmaped", and under CRI-O no container runs.

        `kata` is Kata Containers, and needs `boot.uml.nestedVirtualization`:
        it starts a VM per pod.
      '';
    };

    images = lib.mkOption {
      type = lib.types.enum [
        "nix"
        "pull"
      ];
      default = "nix";
      description = ''
        Where the node's container images come from.

        `nix` builds every image kubeadm needs out of nixpkgs, imports
        them into containerd before kubelet starts, and never contacts a
        registry.  That is what makes a cluster possible inside a build
        sandbox, which has no network at all.  It costs the rest of this
        module: the images are symlinks into `/nix/store`, so the store
        has to be mounted into the static pods, CoreDNS and kube-proxy,
        and `imagePullPolicy` has to be `Never` everywhere.

        `pull` does none of that.  kubeadm fetches the upstream images
        from `registry.k8s.io` the way it would on any machine, no patches
        are applied, and nothing is imported.  **It needs a network, so it
        only works outside a Nix build sandbox.**  Use it for a test whose
        subject is what a real cluster does with an unmodified node --
        anything that would otherwise pass because this module had already
        put `/nix` in every container.
      '';
    };

    extraImages = lib.mkOption {
      type = lib.types.listOf lib.types.path;
      default = [ ];
      example = lib.literalExpression "[ ./my-operator.tar.gz ]";
      description = ''
        More image tarballs to import into containerd at boot, beside the
        ones kubeadm needs.

        A test that deploys something of its own needs its images on the
        node before kubelet asks for them: there is no registry here, so a
        pod naming an image nobody imported sits in `ErrImagePull` until the
        test times out. Each entry is a docker-archive tarball, gzipped or
        not.

        The tag inside the tarball is what a pod has to ask for, and a pod
        using one has to say `imagePullPolicy: Never`.

        A tarball's layers are opaque to Nix's reference scanner, so it
        carries no references at all. Whatever the images point into the
        store has to be named separately, or a guest whose /nix/store is the
        build sandbox's will not have it -- see `system.extraDependencies`
        below, which is how the images this module builds do it.
      '';
    };

    persistentVolumes = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 0;
      example = 1;
      description = ''
        How many PersistentVolumes this node offers, and whether the
        cluster gets a default StorageClass at all.

        Above zero, the node writes a StorageClass named `standard` and
        that many hostPath volumes to `/etc/kubernetes/uml-storage.yaml`,
        and `bring_up` applies every node's copy once the cluster is up.
        A chart that leaves `storageClassName` unset then binds, which is
        what a chart written for a cloud or for kind expects.

        Static volumes and no provisioner, on purpose. kind's `standard`
        is rancher's local-path-provisioner, which is an image on
        docker.io and a controller to keep working; these are a few lines
        of YAML, they need no network, and a pod cannot tell the
        difference -- both end up a directory on the node.

        The count is the pool. A claim past the last free volume stays
        Pending, because nothing here creates more.
      '';
    };

    workloadImage = lib.mkOption {
      type = lib.types.str;
      readOnly = true;
      default = images.workloadImage;
      description = ''
        A busybox image preloaded on every node, for a test that needs
        something to schedule.  There is no registry behind it, so a pod
        using it has to set `imagePullPolicy: Never`.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !(lib.elem "runsc" cfg.runtimes && config.boot.uml.backend == "uml");
        message = "services.uml-k8s.runtimes: runsc (gVisor) does not start under UML; use the qemu backend.";
      }
      {
        # Measured with CRI-O 1.36.5 and runsc 20260406, on a plain busybox
        # pod: with drop_infra_ctr the sandbox's runsc state never exists
        # ("cannot load sandbox"); without it, conmon never finds the exit
        # file and the container ends with exit code -1 and no output.
        assertion = !(lib.elem "runsc" cfg.runtimes && cfg.cri == "crio");
        message = "services.uml-k8s.runtimes: runsc (gVisor) does not run a container under CRI-O here; use cri = \"containerd\".";
      }
      {
        assertion = lib.elem "kata" cfg.runtimes -> config.boot.uml.nestedVirtualization;
        message = "services.uml-k8s.runtimes: kata starts a VM per pod and needs boot.uml.nestedVirtualization.";
      }
      {
        assertion = config.boot.uml.lan.address != null;
        message = ''
          services.uml-k8s needs boot.uml.lan.address: every guest shares
          one address behind passt, so a node with no segment of its own
          has no address to register with.
        '';
      }
    ];

    # ── the container runtime ──────────────────────────────────────

    virtualisation.containerd = lib.mkIf (cfg.cri == "containerd") {
      enable = true;
      settings = {
        # containerd 2.x still reads a version 2 file, by migrating it and
        # warning; saying 3 outright means the sections below land where
        # they are read rather than where they used to be.
        version = lib.mkForce 3;
        plugins."io.containerd.cri.v1.runtime" = {
          containerd.runtimes = {
            runc.options.SystemdCgroup = true;
          }
          // lib.getAttrs cfg.runtimes runtimeHandlers;
          # No copy into /opt/cni/bin: nothing writes to these and the
          # store path is already on the node.
          cni.bin_dirs = [ "${pkgs.cni-plugins}/bin" ];
          cni.conf_dir = "/etc/cni/net.d";
        };
        # Said explicitly because containerd's default tracks containerd
        # releases and ours has to track kubeadm's: the two agree today,
        # and the day they do not, every pod sandbox fails to start over
        # an image nothing in this file mentions.
        plugins."io.containerd.cri.v1.images".pinned_images.sandbox =
          images.sandboxImage;
        # Off in containerd, and off here unless a test says otherwise --
        # see `services.uml-k8s.nri`.
        plugins."io.containerd.nri.v1.nri".disable = !cfg.nri;
      };
    };

    # containerd finds a shim, and the shim its runtime, on PATH.
    systemd.services.containerd.path = lib.mkIf (cfg.cri == "containerd") (
      lib.optional (lib.elem "runsc" cfg.runtimes) pkgs.gvisor
      ++ lib.optional (lib.elem "kata" cfg.runtimes) kata
    );

    /*
      containers/storage bind-mounts its overlay home onto itself as
      private. A kata shim copies the mount table when it starts, so every
      rootfs CRI-O mounts after that never reaches the shim: it sees an
      empty `merged`, creates mount points in it, and shares an empty rootfs
      into the VM. Measured with kata 3.32: "the file /bin/sh was not
      found", then CRI-O retrying "replacing mount point ... merged:
      directory not empty" for ever, and the agent dead after a container
      exits. containerd has no such bind; its shim mounts the rootfs.
    */
    virtualisation.containers.storage.settings.storage.options.overlay =
      lib.mkIf (cfg.cri == "crio" && lib.elem "kata" cfg.runtimes)
        { skip_mount_home = "true"; };

    virtualisation.cri-o = lib.mkIf (cfg.cri == "crio") {
      enable = true;
      pauseImage = images.sandboxImage;
      # CRI-O runs this in place of the image's entrypoint, and its default
      # is upstream's `/pause`. Ours is where ./k8s-images.nix puts every
      # command.
      pauseCommand = "/usr/local/bin/pause";
      extraPackages = lib.optional (lib.elem "runsc" cfg.runtimes) pkgs.gvisor;
      settings.crio = {
        # runc, as under containerd. CRI-O's own default is crun, and a
        # test that compares the two CRIs should not also compare runtimes.
        runtime.default_runtime = "runc";
        runtime.runtimes = {
          runc = { };
        }
        // lib.genAttrs cfg.runtimes (_: { })
        // lib.optionalAttrs (lib.elem "runsc" cfg.runtimes) {
          runsc.runtime_root = "/run/runsc";
        }
        // lib.optionalAttrs (lib.elem "kata" cfg.runtimes) {
          kata = {
            runtime_path = "${kata}/bin/containerd-shim-kata-v2";
            runtime_type = "vm";
            runtime_root = "/run/vc";
            runtime_config_path = kataConfig;
            privileged_without_host_devices = true;
          };
        };
        nri.enable_nri = cfg.nri;
        # CRI-O saves and restores the IRQ affinity mask at start, and the
        # UML kernel has no /proc/irq: CRI-O exits with "open
        # /proc/irq/default_smp_affinity: no such file or directory".
        runtime.irqbalance_config_restore_file = "disable";
      };
    };

    systemd.targets.uml-k8s-cri = {
      description = "The container runtime kubelet talks to";
      wantedBy = [ "multi-user.target" ];
      requires = [ cri.unit ];
      after = [ cri.unit ];
    };

    # ── the images, before anything wants them ─────────────────────

    /*
      What the images point at, made a dependency of the node.

      The tarball itself does not count as one.  Its layers are gzipped,
      so the store paths inside are opaque to Nix's reference scanner and
      the tarball comes back with no references at all -- meaning nothing
      in this configuration otherwise asks for etcd, and a guest whose
      /nix/store is the build sandbox's would not have it.  The symlinks
      would dangle and runc would report an image that does not contain
      its own binary.
    */
    system.extraDependencies = lib.mkIf (cfg.images == "nix") images.runtimeInputs;

    systemd.services.k8s-load-images = lib.mkIf (cfg.images == "nix") {
      description = "Import the kubeadm images into the container runtime";
      wantedBy = [ "multi-user.target" ];
      # Not `requires`, which would import again on every runtime restart:
      # the images stay on disk, and under CRI-O `podman load` stages about
      # 770M in /var/tmp. Measured on nixkube's node: free space fell to
      # 99M, and kubelet held a DiskPressure taint for five minutes.
      wants = [ "uml-k8s-cri.target" ];
      after = [ "uml-k8s-cri.target" ];
      before = [ "kubelet.service" ];
      path = [
        pkgs.gzip
        (if cfg.cri == "containerd" then pkgs.containerd else pkgs.podman)
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        TimeoutStartSec = "10min";
      };
      /*
        Fast, because the images are symlink farms: the only real bytes
        here are the pause image's own closure.

        Piped rather than named, for two reasons.  `mergeImages` emits a
        gzipped tar and `ctr images import` does not sniff for that, so
        it reports `invalid tar header` on a file that is perfectly
        good.  And `--discard-unpacked-layers`, which drops the
        compressed copy once the snapshotter has it, is only offered on
        the `--local` path -- importing here rather than handing the tar
        to containerd's transfer service.
      */
      #
      # CRI-O reads containers-storage, which is podman's; `podman load`
      # takes a multi-image archive where `skopeo copy` wants one per tag.
      script =
        let
          import =
            {
              containerd = "ctr --namespace k8s.io images import --local --discard-unpacked-layers -";
              crio = "podman load";
            }
            .${cfg.cri};
          list =
            {
              containerd = "ctr --namespace k8s.io images list -q";
              crio = "podman images --format '{{.Repository}}:{{.Tag}}'";
            }
            .${cfg.cri};
        in
        ''
          for tarball in ${images.tarball} ${lib.escapeShellArgs cfg.extraImages}; do
            echo "importing $tarball"
            # `-f` so an uncompressed tarball passes straight through: a
            # caller's images need not be gzipped to be listed here.
            zcat -f "$tarball" | ${import}
          done
          ${list}
        '';
    };

    # ── kubelet ────────────────────────────────────────────────────

    /*
      Tell the machine how fast it is, because kubelet will not start
      otherwise.

      cadvisor, which kubelet embeds, looks for a clock speed in exactly
      two places: the cpufreq sysfs, which UML has no driver for, and a
      "cpu MHz" line in /proc/cpuinfo, which UML does not print.  Finding
      neither is fatal, and it fails before anything interesting has
      happened:

        failed to run Kubelet: could not detect clock speed from output:
        "processor\t: 0\nvendor_id\t: User Mode Linux\n..."

      which crash-loops every five seconds and shows up much later as a
      control plane that never became healthy.

      bogomips is the only number UML offers and it is not a clock speed.
      That is fine: nothing schedules on this value, it is reported as
      node metadata and never compared against anything.
    */
    systemd.services.uml-k8s-cpuinfo = {
      description = "Give /proc/cpuinfo a clock speed for cadvisor";
      wantedBy = [ "multi-user.target" ];
      before = [ "kubelet.service" ];
      path = with pkgs; [
        gawk
        util-linux
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      # No sandboxing options, deliberately: the bind has to land in the
      # machine's own mount namespace, where kubelet will read it.
      script = ''
        if grep -q '^cpu MHz' /proc/cpuinfo; then
          echo "uml-k8s-cpuinfo: already has one, leaving it alone"
          exit 0
        fi
        mhz=$(awk -F'[:[:space:]]+' '/^bogomips/ { print $2; exit }' /proc/cpuinfo)
        # cadvisor's regex wants a decimal point and will not match without one.
        case "$mhz" in
          "")  mhz=1000.000 ;;
          *.*) ;;
          *)   mhz="$mhz.000" ;;
        esac
        awk -v mhz="$mhz" '{ print } /^processor/ { print "cpu MHz\t\t: " mhz }' \
          /proc/cpuinfo > /run/cpuinfo
        mount --bind /run/cpuinfo /proc/cpuinfo
        echo "uml-k8s-cpuinfo: reporting $mhz MHz"
      '';
    };

    # kubeadm writes /var/lib/kubelet/config.yaml and kubeadm-flags.env,
    # so the unit does nothing until it has been run.  NixOS's own
    # services.kubernetes.kubelet is not this: it configures a node
    # itself, from Nix, which is the opposite of what a kubeadm test is
    # trying to exercise.
    systemd.services.kubelet = {
      description = "kubelet, the Kubernetes node agent";
      wantedBy = [ "multi-user.target" ];
      after = [
        "uml-k8s-cri.target"
        "k8s-load-images.service"
        "uml-k8s-cpuinfo.service"
      ];
      wants = [ "uml-k8s-cri.target" ];
      requires = [ "uml-k8s-cpuinfo.service" ];
      unitConfig.ConditionPathExists = "/var/lib/kubelet/config.yaml";
      path = with pkgs; [
        util-linux
        iproute2
        iptables
        ethtool
        socat
        conntrack-tools
      ];
      environment = {
        KUBELET_KUBECONFIG_ARGS =
          "--bootstrap-kubeconfig=/etc/kubernetes/bootstrap-kubelet.conf"
          + " --kubeconfig=/etc/kubernetes/kubelet.conf";
        KUBELET_CONFIG_ARGS = "--config=/var/lib/kubelet/config.yaml";
        KUBELET_EXTRA_ARGS = "--node-ip=${nodeIp}";
      };
      serviceConfig = {
        EnvironmentFile = [ "-/var/lib/kubelet/kubeadm-flags.env" ];
        ExecStart =
          "${lib.getExe' kubernetes "kubelet"} $KUBELET_KUBECONFIG_ARGS"
          + " $KUBELET_CONFIG_ARGS $KUBELET_KUBEADM_ARGS $KUBELET_EXTRA_ARGS";
        Restart = "always";
        RestartSec = 5;
        LimitNOFILE = 1048576;
        TasksMax = "infinity";
      };
    };

    # ── the node itself ────────────────────────────────────────────

    /*
      The module the sysctls below are settings of.

      The UML kernel is built with br_netfilter in it, so under UML this
      is already there and asking for it costs nothing. The stock NixOS
      kernel a QEMU guest boots has it as a module, and nothing else on
      the node loads it -- `boot.kernel.sysctl` then writes
      `net.bridge.bridge-nf-call-iptables` into a `/proc` that has no such
      key, and the setting is quietly not applied.

      What that looks like is the failure the sysctls exist to prevent,
      only harder to find: on a single-node cluster every pod is on one
      bridge, so *every* ClusterIP is unreachable from every pod, and
      CoreDNS answers nothing. Measured -- a pod could reach 1.1.1.1 over
      HTTP and could not reach 10.96.0.10 at all, which reads as broken
      DNS rather than as an unloaded module.
    */
    # kata's agent talks to its VM over vsock.
    boot.kernelModules = [
      "br_netfilter"
    ]
    ++ lib.optional (lib.elem "kata" cfg.runtimes) "vhost_vsock";

    /*
      Leave a veth's MAC where CNI put it.

      udev's default `.link` gives every new interface a persistent MAC,
      and the bridge plugin records the one it created. CRI-O runs CNI
      CHECK on the pod's network, which compares the two, and every pod
      sandbox failed with "Interface vethXXXX Mac doesn't match". containerd
      never runs CHECK, so it never saw this. Measured: addr_assign_type 3
      on each veth, and the stuck pod started once this file was in place.
    */
    systemd.network.links."05-cni-veth" = {
      matchConfig.Driver = "veth";
      linkConfig.MACAddressPolicy = "none";
    };

    boot.kernel.sysctl = {
      # Traffic between pods on one node crosses the CNI bridge, and
      # without these kube-proxy's rules never see it -- so a Service
      # works from one node and not from the pod next to it.
      "net.bridge.bridge-nf-call-iptables" = 1;
      "net.bridge.bridge-nf-call-ip6tables" = 1;
      "net.ipv4.ip_forward" = 1;
      # Go runtimes reserve far more address space than they touch, which
      # on a 1 GB guest the default heuristic refuses.
      "vm.overcommit_memory" = 1;
    };

    environment.etc = lib.optionalAttrs (cfg.persistentVolumes > 0) {
      # Where `provision_storage` looks.  A node with no volumes writes no
      # file and the runner applies nothing, so storage is one knob and not
      # two -- unlike `skipAddons`, which a test has to repeat in `addons`.
      "kubernetes/uml-storage.yaml".source = storageManifest;
    }
    // lib.optionalAttrs (cfg.cri == "crio") {
      # CRI-O's own bridge sorts before `10-uml.conflist` and would win. The
      # pod network is `uml-k8s-cni`'s, whichever CRI runs.
      "cni/net.d/10-crio-bridge.conflist".enable = false;
    }
    // lib.optionalAttrs (cfg.runtimes != [ ]) {
      # Where `provision_runtimes` looks, on the same terms.
      "kubernetes/uml-runtimes.yaml".source = runtimeManifest;
    }
    // {
      # What every pod gets as its /etc/resolv.conf, and what CoreDNS
      # forwards to.  A sandboxed guest can reach no resolver at all, but
      # CoreDNS refuses to start against a file that names none -- so
      # this names one that is guaranteed both to parse and to go
      # nowhere.  Nothing in a test needs a name from outside the
      # cluster; cluster.local is served locally.
      "kubernetes/resolv.conf".text = ''
        # RFC 5737 TEST-NET-1: unroutable on purpose.
        nameserver 192.0.2.1
      '';

      # Over CRI-O's own, which names its socket and none of the timeout.
      "crictl.yaml".text = lib.mkForce ''
        runtime-endpoint: ${criSocket}
        image-endpoint: ${criSocket}
        timeout: 60
      '';
    }
    // lib.optionalAttrs (cfg.role == "control-plane") {
      "kubernetes/kubeadm-config.yaml".source = kubeadmConfig;
    };

    environment.systemPackages = [
      kubernetes
      pkgs.cri-tools
      pkgs.cni-plugins
      pkgs.iproute2
      pkgs.conntrack-tools
      pkgs.ethtool
      pkgs.socat
      pkgs.ipset
      cniSetup
    ]
    ++ lib.optional (cfg.role == "worker") joinNode;

    # ── control plane ──────────────────────────────────────────────

    # etcd on a UML block device spends its life apologising for fsync
    # latency.  A test cluster has nothing to lose across a reboot, and
    # the whole keyspace fits in a few tens of megabytes.
    fileSystems = lib.mkIf (cfg.role == "control-plane") {
      "/var/lib/etcd" = {
        device = "tmpfs";
        fsType = "tmpfs";
        options = [
          "size=512m"
          "mode=0700"
        ];
      };
    };

    /*
      For `check-k8s-config`, which runs kubeadm's own validator over
      these.

      Worth its own derivation: kubeadm rejects unknown fields, and the
      config API is versioned, so a nixpkgs bump that retires v1beta4
      shows up here in seconds rather than as an `unknown API version`
      thirty minutes into the cluster test.  The join config carries
      placeholders, so the check substitutes something well-formed --
      only the shape is under test.
    */
    system.build.kubeadmConfigs = {
      init = kubeadmConfig;
      join = joinConfig {
        endpoint = "${nodeIp}:6443";
        token = "abcdef.0123456789abcdef";
        hash = "sha256:${lib.concatStrings (lib.genList (_: "00") 32)}";
      };
    };

    # So a test can say `kubectl get nodes` rather than carrying the
    # kubeconfig through every command.  Commands from the host run as
    # children of the agent, and systemd units do not read /etc/profile.
    systemd.services.uml-agent.environment.KUBECONFIG =
      lib.mkIf (cfg.role == "control-plane") "/etc/kubernetes/admin.conf";
  };
}
