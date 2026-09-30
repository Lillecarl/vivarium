# The workflows, as data.  `nix run .#render-workflows` turns each
# attribute here into .github/workflows/<name>.yml, and `nix build
# .#check-workflows` fails when the two have drifted.
{ lib, ghalib }:
let
  ci = import ./lib.nix { inherit lib; };
  inherit (ci) steps job selectable bootstrap;

  # A job that boots a guest needs the user namespace, whether the guest is
  # a UML process inside the Nix sandbox or a QEMU machine outside it: the
  # sandbox unshares one, and so does passt. A QEMU guest needs /dev/kvm on
  # top, and `uid-range`: virtiofsd serves a guest user's file as that
  # user, which a sandbox mapping one uid cannot (see `featuresFor`).
  #
  # `mkMerge` and not `//`, which is shallow: an addition under `nix` would
  # otherwise replace the whole install block and take the cache with it.
  sandboxBootstrap = lib.mkMerge [
    bootstrap
    { userNamespaces.enable = true; }
  ];

  guestBootstrap = lib.mkMerge [
    sandboxBootstrap
    {
      openKvm.enable = true;
      nix.install.uidRange = true;
    }
  ];

  /*
    The integration tests, and what each one costs.

    A guest is a process, so these are bounded by the runner's four
    cores rather than by anything virtual: `k8s` boots three guests and
    waits on a control plane, which is why it gets its own job and a
    much larger cap instead of being folded in with the others.
  */
  tests = {
    lan = {
      description = "two guests on a segment see each other";
      timeoutMinutes = 45;
      # The one job left on UML, and the reason the kernel is still built.
      # Everything else moved to QEMU for the speed, and a UML regression
      # has to fail here rather than somewhere else.
      backend = "uml";
    };
    memory = {
      # One guest, one closure read, and four numbers. Its own job
      # because it is the only check that measures the host rather than
      # the guest, so a regression in it says something no other job can.
      description = "a guest gives its memory back";
      timeoutMinutes = 20;
      # UML, although the test passes on both: this is the job that would
      # catch a regression in the kernel patch, and only a UML guest has
      # that patch in it.
      backend = "uml";
    };
    iperf = {
      description = "what a segment between two guests carries";
      timeoutMinutes = 45;
    };
    forward = {
      description = "the host reaches a service inside a guest";
      timeoutMinutes = 45;
    };
    fuse = {
      # One guest, no network, and a closure of bindfs and util-linux on
      # top of the base system.  The kernel comes from the cache, so what
      # is left is a root image and a six-second boot: about a minute in
      # all, so the cap is ten times what the job costs rather than fifty.
      description = "a userspace filesystem, as root and as a user";
      timeoutMinutes = 15;
    };
    artifacts = {
      description = "what a guest writes reaches the host, per guest";
      timeoutMinutes = 15;
    };
    containerd = {
      description = "a container runs, out of the host's store";
      timeoutMinutes = 45;
    };
    k8s = {
      description = "a three-node kubeadm cluster";
      timeoutMinutes = 120;
      # Everything the cluster is built on, asked in a couple of minutes
      # rather than found out from a control plane that never becomes
      # healthy.  Nothing here is worth two hours if a container cannot
      # start at all.
      after = [ "test-containerd" ];
    };
  };

  # Building the kernel once and letting the cache hand it to the test
  # jobs saves repeating a ten-minute build three times over.
  kernelJob = job {
    id = "kernel";
    timeoutMinutes = 60;
    cond = selectable "kernel";
    steps = [
      (steps.build {
        name = "Build the UML kernel";
        attrs = [ "umlKernel" ];
        timeoutMinutes = 45;
      })
    ];
  };

  /*
    One test, on one backend.

    QEMU by default, because a guest runs on the processor instead of
    trapping every syscall into the host kernel, and the job then waits on
    no `umlKernel` -- the ten-minute build the `kernel` job does once.

    It costs /dev/kvm, which `guestBootstrap` provides: the runner has the
    device, and the udev rule lets the sandbox's build user open it.  A
    `.qemu` derivation asks the daemon for the `kvm` feature, so a runner
    without it refuses the build -- a job that does not start, not a red
    one.
  */
  testJob =
    name:
    {
      description,
      timeoutMinutes,
      after ? [ ],
      backend ? "qemu",
    }:
    job {
      id = "test-${name}";
      needs = (lib.optional (backend == "uml") "kernel") ++ after;
      inherit timeoutMinutes;
      cond = selectable "test-${name}";
      ghanix = if backend == "qemu" then guestBootstrap else sandboxBootstrap;
      steps = [
        (steps.build {
          name = "Run ${name} on ${backend}: ${description}";
          attrs = [ (if backend == "qemu" then "${name}.qemu" else name) ];
          timeoutMinutes = timeoutMinutes - 10;
        })
      ];
    };

  testJobs = lib.mapAttrs' (name: spec: lib.nameValuePair "test-${name}" (testJob name spec)) tests;

  /*
    A guest as a container, in the Nix sandbox.

    The derivation asks for `uid-range`: a build as root with 65536 ids
    and a cgroup of its own, which systemd as PID 1 in the container
    needs. The runner's daemon offers it only when told to, which is
    ghanix's `uidRange`; `devNet` adds /dev/net, for the uplink and the
    LAN. The probe goes first on its own, so a runner that lacks any of
    it fails in seconds, by name. `container-lan` holds a UML guest,
    hence the kernel.
  */
  containerJob = job {
    id = "test-container";
    needs = [ "kernel" ];
    timeoutMinutes = 30;
    cond = selectable "test-container";
    ghanix = lib.mkMerge [
      sandboxBootstrap
      {
        nix.install.uidRange = true;
        nix.install.devNet = true;
      }
    ];
    steps = [
      (steps.build {
        name = "Probe: can this sandbox run a container guest with a LAN";
        attrs = [ "container-probe-tun" ];
        timeoutMinutes = 5;
      })
      (steps.build {
        name = "Run container and container-lan: NixOS guests under crun, in the sandbox";
        attrs = [
          "container"
          "container-lan"
        ];
        timeoutMinutes = 20;
      })
    ];
  };

  /*
    The one test that boots a virtual machine and reaches a registry.

    Everything above runs in the Nix sandbox, which has no network and no
    /dev/kvm -- so nothing here exercised a QEMU guest or a node that
    pulls its own images, and a bug in either was invisible to this
    repository.

    That is not hypothetical. passt needs an unprivileged user namespace
    and Ubuntu denies one, so on a runner it exited at startup and every
    guest booted with no uplink at all. On a developer machine it starts,
    so the tests passed here for months, and the failure was found in
    nixkube's CI three repositories away -- reported as a name that would
    not resolve, which is what sent two rounds of work at the resolver.
    `ghanix.userNamespaces` is the fix and the reason this job exists.

    `nix run`, not `nix build`: the point is that it is outside the
    sandbox.
  */
  pullJob = job {
    id = "test-k8s-pull";
    needs = [ "kernel" ];
    timeoutMinutes = 30;
    cond = selectable "test-k8s-pull";
    ghanix = guestBootstrap;
    steps = [
      {
        name = "Run k8s-pull: a node that pulls its images, on a virtual machine";
        timeout-minutes = 20;
        run = "nix run --print-build-logs --file . k8s-pull.driver -- --out ./out";
      }
    ];
  };

  /*
    The checks that need no guest.

    First, and on their own: each is a small derivation, so a stale
    render or an image tag kubeadm no longer asks for is reported in a
    minute rather than after the cluster test has spent an hour finding
    out the hard way.
  */
  checksJob = job {
    id = "checks";
    timeoutMinutes = 30;
    cond = selectable "checks";
    steps = [
      (steps.build {
        name = "Check the generated files, the scripts, the images and the kubeadm config";
        attrs = [
          "check-workflows"
          "check-scripts"
          "check-k8s-images"
          "check-k8s-config"
        ];
        timeoutMinutes = 20;
      })
    ];
  };
  /*
    The session's own rules, on the smallest guests there are.

    Five checks in one job rather than five jobs: each boots a single
    guest with nothing on it, so the whole set costs less than any one of
    the tests above. What they answer is about the runner and not about
    the backend -- what a failure skips, what a knob is worth, what
    `--only` leaves alone -- so one backend is enough.

    UML, because `mkTest` defaults to it. That is why this waits on
    `kernel` although nothing in it is a kernel test.
  */
  sessionJob = job {
    id = "sessions";
    needs = [ "kernel" ];
    timeoutMinutes = 30;
    cond = selectable "sessions";
    ghanix = sandboxBootstrap;
    steps = [
      (steps.build {
        name = "The session: phase rules, knobs, --only, recipes, stream, pytest, breakpoints, parallel, peers, cleanup, interactive";
        attrs = [
          "phase-rules"
          "knobs"
          "only-rules"
          "recipes"
          "stream"
          "pytest-phase"
          "breakpoint"
          "guest-suites"
          "kernel-override"
          "parallel"
          "peers"
          "cleanup"
          "interactive"
        ];
        timeoutMinutes = 20;
      })
    ];
  };
in
{
  # Through ghanix's schema rather than straight to YAML. That is what
  # turns each job's `ghanix` attribute into steps, drops the options a job
  # left unset, and refuses a job with no steps at all.
  ci = ghalib.evalWorkflow {
    name = "CI";
    # UMBRELLA_GIT makes the umbrella fetch each source over the git
    # protocol, and not through api.github.com. Anonymous api.github.com
    # allows 60 calls an hour per IP, GitHub's runners share a NAT pool, and
    # every source a job resolves is one call. nanopynix issue #301.
    env.UMBRELLA_GIT = "1";
    on = {
      push = { };
      pull_request = { };
      workflow_dispatch.inputs.jobs = {
        description = "Comma-separated job IDs to run; empty runs all of them";
        required = false;
        type = "string";
        default = "";
      };
    };
    jobs = {
      checks = checksJob;
      sessions = sessionJob;
      kernel = kernelJob;
      test-k8s-pull = pullJob;
      test-container = containerJob;
    }
    // testJobs;
  };
}
