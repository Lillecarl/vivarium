# Step and job constructors for ./workflows.nix.
#
# GitHub Actions YAML has no way to say "these six jobs differ only in
# which attribute they build", so the workflows are Nix and the YAML
# under .github/workflows is generated from them -- see ./default.nix.
#
# Everything here is plain data: attrsets that render straight to YAML.
{ lib }:
rec {
  # Every job runs on the same image.  Pinned rather than `ubuntu-latest`
  # so that a runner image rollout cannot change what a test ran on
  # between two commits.
  runsOn = "ubuntu-24.04";

  # `if` is a Nix keyword, so it cannot be a function argument -- callers
  # pass `cond` and this puts it under the quoted name GitHub wants.
  withCond = cond: attrs: if cond == null then attrs else attrs // { "if" = cond; };

  /*
    What every job here asks ghanix for before it does anything of its own.

    These were five step definitions in this file: a checkout, the Nix
    install, cachix, the user-namespace sysctls and the /dev/kvm udev rule.
    nixkube had its own copies of the last two and nanopynix a third copy
    of one, which is how the copies drifted and how the job that needed the
    namespace most came to be without it -- nixkube issue #35.

    `job.ghanix` is ghanix's own and never reaches the YAML; each option
    enabled here puts a step at the front of that job's `steps`.
  */
  bootstrap = {
    checkout.enable = true;
    nix.install = {
      enable = true;
      # No `nix_path`. `default.nix` asks the umbrella for nixpkgs -- see
      # ../nix/sources.nix -- so a runner needs nothing in NIX_PATH and
      # every job here builds against the revision nixidae pins, which is
      # what makes the cache able to serve them.
      settings = {
        # A runner has four cores and the guests are processes: letting
        # Nix run several builds at once only makes each one slower and
        # the timings meaningless.
        max-jobs = 1;
        cores = 4;
      };
    };
    nix.cachix = {
      enable = true;
      /*
        Cache what the tests are built out of, not whether they passed.

        A `vivarium-test-*` output is an empty file whose existence means
        "this booted some guests and they behaved".  Push that and the
        next run with the same inputs substitutes it instead of booting
        anything -- so re-running a commit, which is the one thing you
        do when you suspect a result, is guaranteed to agree with
        itself.

        The cheap checks stay cacheable on purpose.  Asking kubeadm
        whether it accepts a config is a pure function of that config,
        so a cached yes is as good as a fresh one.  A test that boots
        three guests and waits on a control plane is not pure in that
        way however much Nix would like it to be, and those are exactly
        the ones worth paying to repeat.  The kernel -- the only build
        here that costs real time -- is unaffected.
      */
      pushFilter = "(-vivarium-test-)";
    };
  };

  steps = {
    # `--keep-going` so that one failing attribute does not hide whether
    # the others build; `--print-build-logs` because the test output *is*
    # the build log.
    build =
      {
        name,
        attrs,
        timeoutMinutes,
      }:
      {
        inherit name;
        timeout-minutes = timeoutMinutes;
        # `--file .`, which is how this repository is built everywhere
        # else. flake.nix is a second door and CI does not use it.
        run = "nix build --no-link --print-build-logs --keep-going --file . ${
          lib.concatStringsSep " " attrs
        }";
      };
  };

  /*
    Run even though something upstream was skipped rather than run.

    GitHub skips anything downstream of a skipped job, so without this a
    dispatch asking for one test would skip it for want of the kernel job
    it deliberately did not ask for.  `!cancelled()` is what makes the
    condition evaluated at all in that case; the rest says a skipped
    dependency is as good as a successful one, but a failed one is not.
  */
  reached =
    needs:
    lib.concatStringsSep " && " (
      [ "!cancelled()" ]
      ++ map (id: "(needs.${id}.result == 'success' || needs.${id}.result == 'skipped')") needs
    );

  /*
    One job.

    `timeoutMinutes` is the whole job; the steps carry their own caps so
    that a hung `nix build` is distinguishable from a slow one.  The
    condition lets `workflow_dispatch` run a single job by name, which is
    the difference between iterating on the cluster test in five minutes
    and in forty.
  */
  job =
    {
      id,
      steps,
      ghanix ? bootstrap,
      needs ? [ ],
      timeoutMinutes,
      cond ? null,
    }:
    let
      parts = lib.optional (needs != [ ]) (reached needs) ++ lib.optional (cond != null) "(${cond})";
    in
    withCond (if parts == [ ] then null else lib.concatStringsSep " && " parts) {
      runs-on = runsOn;
      timeout-minutes = timeoutMinutes;
      inherit steps ghanix;
    }
    // lib.optionalAttrs (needs != [ ]) { inherit needs; };

  # Only run `id` when the dispatch input asked for it, or when the run
  # is not a dispatch at all.
  selectable =
    id:
    "github.event_name != 'workflow_dispatch' || inputs.jobs == ''"
    + " || contains(format(',{0},', inputs.jobs), ',${id},')";
}
