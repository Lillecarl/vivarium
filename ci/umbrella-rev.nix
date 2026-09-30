# One umbrella revision for a whole workflow run.
#
# `nix/sources.nix` resolves the umbrella from UMBRELLA_REV when it is set.
# Without it that reference is unlocked, so every job resolves the head of the
# default branch again. `umbrella land` pushes the working copies, which starts
# the run, and the umbrella lock commit follows seconds later. A run that
# straddles the push reads two revisions.
#
# Measured in nixkube run 35026926963: seven seconds between the first job
# starting and the lock commit, two different `cacheEnv` store paths, and a
# later job asking for one that nothing had built. nanopynix issue #301.
#
# So one job resolves it and every other job takes the answer through `needs`.
#
# **It resolves the umbrella that locks this commit, not the head of the
# umbrella default branch.** The head is a moving answer: it is whatever
# landed most recently, which for a branch nobody landed is not related to
# this commit at all. `ci/walkback.sh` reads
# `refs/umbrella/<name>/<revision>` from the umbrella remote instead, walking
# HEAD backwards to the nearest revision the umbrella has locked. One
# `git ls-remote` and one `git rev-list`.
#
# It wraps `evalWorkflow` rather than being written into each workflow, so a
# workflow or a job added later cannot be the one that forgets it.
evalWorkflow: workflow:
let
  jobName = "umbrella-rev";

  resolver = {
    runs-on = "ubuntu-24.04";
    timeout-minutes = 5;
    # One `git ls-remote` and one `git rev-list`, so there is nothing to make
    # room for, and the step is not free: it deletes tens of gigabytes and
    # prunes every Docker image, which is minutes. The cap above is five, and
    # it was set for a job that takes seconds.
    ghanix.freeDiskSpace.enable = false;
    outputs.rev = "\${{ steps.resolve.outputs.rev }}";
    steps = [
      {
        uses = "actions/checkout@v4";
        # walkback walks HEAD backwards until it reaches a commit the
        # umbrella has locked. A branch nobody landed needs its branch
        # point, so the checkout has to reach that far back.
        "with".fetch-depth = 100;
      }
      {
        id = "resolve";
        name = "Resolve the umbrella revision";
        run = "ci/walkback.sh https://github.com/nixidae/nixidae vivarium | sed 's/^/rev=/' >> \"$GITHUB_OUTPUT\"";
      }
    ];
  };

  # `needs` is a string, a list or absent, and it has to stay whichever it was
  # with this one added.
  asList =
    value:
    if value == null then
      [ ]
    else if builtins.isList value then
      value
    else
      [ value ];

  pin =
    _: job:
    job
    // {
      needs = asList (job.needs or null) ++ [ jobName ];
      # A job that sets UMBRELLA_REV itself wins, because its own `env` is
      # written after this one.
      env = {
        UMBRELLA_REV = "\${{ needs.${jobName}.outputs.rev }}";
      }
      // (if job.env or null == null then { } else job.env);
    };
in
evalWorkflow (
  workflow
  // {
    jobs = builtins.mapAttrs pin workflow.jobs // {
      ${jobName} = resolver;
    };
  }
)
