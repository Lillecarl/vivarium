# Wait for the guests to finish booting before anything else runs.
{ config, lib, ... }:
let
  cfg = config.vivarium.recipes.boot;
in
{
  options.vivarium.recipes.boot = {
    enable = lib.mkEnableOption "a first phase that waits for every guest to come up" // {
      default = true;
      example = false;
    };

    name = lib.mkOption {
      type = lib.types.str;
      default = "boot";
      description = ''
        What the phase is called, and therefore what other phases name in
        their `after`.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    phases.${cfg.name} = {
      # `mkDefault`, so a consumer that writes its own `boot` phase wins
      # rather than getting a conflict. Overriding a recipe is the point
      # of recipes being options.
      script = lib.mkDefault ../../recipes/boot.py;
      description = lib.mkDefault "every guest reaches a running system";
    };
  };
}
