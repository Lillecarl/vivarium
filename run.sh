#!/usr/bin/env bash
# Build and boot the demo guest, keeping both logs around to grep.
#
#   ./run.sh                     boot it and stream the console
#   ./run.sh --command hostname  run one command in it and exit
#
# Set VIVARIUM_BUILDERS to build somewhere else, e.g.
#   VIVARIUM_BUILDERS='ssh-ng://eu.nixbuild.net x86_64-linux - 100 1 kvm,big-parallel'
set -euo pipefail

installable=(--file . vivariumRunner)
flags=()
[[ -n ${VIVARIUM_BUILDERS:-} ]] && flags+=(--builders "$VIVARIUM_BUILDERS")

echo "=== building (log: /tmp/umlbuild.log) ==="
nix "${flags[@]}" build "${installable[@]}" --print-build-logs --no-link \
  2>&1 | tee /tmp/umlbuild.log | tail -n 30

echo "=== running (log: /tmp/umlrun.log) ==="
nix "${flags[@]}" run "${installable[@]}" -- "$@" 2>&1 | tee /tmp/umlrun.log
