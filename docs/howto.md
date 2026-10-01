# vivarium howto

Each section is one task, with the commands for it. The examples use a
test named `mine` in `default.nix`. Replace the name with your test's.

[reference.md](reference.md) explains how a run behaves and why. This
file only tells you what to type.

## Run a test

```console
$ nix build --file . mine                          # in the sandbox, as CI runs it
$ nix run --file . mine.driver -- --out ./out      # by hand, output in ./out
$ nix run --file . mine.driverDebug -- --out ./out # by hand, pause at the first failure
```

A run by hand prints the commands that reach it, before the first
phase and again at each pause:

```
[vivarium] wait for a pause or the verdict (exits 4 at a pause): /nix/store/…/bin/vivarium monitor /home/me/out --quiet --until-pause
[vivarium] run Python against the guests: /nix/store/…/bin/vivarium ctl --out /home/me/out exec 'print(list(vms))'
[vivarium] run a file's `async def test(vms)`: /nix/store/…/bin/vivarium ctl --out /home/me/out inject ./check.py
[vivarium] each phase's state: /nix/store/…/bin/vivarium ctl --out /home/me/out state
[vivarium] resume a paused run: /nix/store/…/bin/vivarium ctl --out /home/me/out continue
```

Copy a line as it is. The paths are absolute, so the command works from
any directory and needs nothing on `PATH`. In the rest of this file,
`vivarium` means the path in those lines.

## Wait for the run without watching it

```console
$ vivarium monitor ./out --quiet --until-pause
```

The monitor prints one line for each pause, failure and verdict. Then
it exits:

| Exit | Meaning |
| --- | --- |
| 0 | The run passed. |
| 1 | The run failed. |
| 2 | The run stopped without a verdict, for example it crashed. |
| 3 | The monitor lost the run's stream before a verdict. |
| 4 | The run is paused. Only with `--until-pause`. |

Start it in the background. When it exits, look at the exit code. After
you resume a paused run, start the monitor again. It does not stop again
for a pause that already ended.

Without `--until-pause`, the monitor runs until the verdict. Without
`--quiet`, it also prints each phase that starts or passes.

## Look inside a paused run

A paused run keeps its guests up, in the state the failure left.

```console
$ vivarium ctl --out ./out state
$ vivarium ctl --out ./out exec 'await one.succeed("systemctl --failed")'
$ vivarium ctl --out ./out exec - < snippet.py
```

`exec` runs Python with top-level `await`. `vms` holds every guest, and
each guest is also a name of its own. A name you set stays for the next
`exec`. `exec` and `inject` also work while phases run.

## Fix a failing test without a new run

Do not edit a phase and start a new run. That evaluates and boots again.
Change the test file and send it to the guests that are already up:

```console
$ vivarium ctl --out ./out inject ./tests/check.py         # a file's `async def test(vms)`
$ vivarium ctl --out ./out pytest ./tests/guest -- -k etcd # pytest tests
$ vivarium ctl --out ./out run check                       # a phase from the test
```

`inject` and `pytest` read the file each time, so edit and send again.
When the test passes, resume:

```console
$ vivarium ctl --out ./out continue
```

`continue` returns when the run is no longer paused.

## Switch a guest to another configuration

Declare each configuration on the node. Each one is the node plus a
module:

```nix
nodes.one.vivarium.configurations.two = {
  services.nginx.enable = true;
};
```

A phase switches the running guest, as `nixos-rebuild` does:

```python
await vms.one.switch_to("two")            # switch: profile, then activate
await vms.one.switch_to("two", "test")    # activate only
await vms.one.switch_to()                 # back to the booted system
rc, out = await vms.one.switch_to("two", check=False)  # exit 4: a unit failed
```

Host Nix evaluates each configuration. The guest has no nixpkgs. A
reboot does not boot the new system: the guest always boots the system
in its spec.

## Find out what went wrong

```console
$ jq -r 'select(.kind=="phase_finished") | "\(.phase)\t\(.data.state)"' out/events.jsonl
$ jq -c 'select(.kind=="journal" and .data.priority <= 3)' out/events.jsonl   # every error, every guest
$ less out/console/one.log                                                     # one guest's console
```

[reference.md](reference.md#what-a-run-leaves-behind) lists every file a
run writes.

## Run a test from an agent

With MCP, `vivarium-mcp` starts runs and reaches into them. AGENTS.md
lists its tools. `start` returns three monitor commands:

- `monitor`: pauses, failures and the verdict.
- `monitor_all`: every event.
- `monitor_pause`: `monitor` that also exits 4 at a pause.

An agent that only runs shell commands does not need MCP:

1. Start the run in the background, with its output in a file.
2. Read the file. Copy the "wait for a pause" line.
3. Start that command in the background. It exits at a pause or at the
   verdict.
4. At a pause, copy the `exec` line from the file, change the Python,
   and run it.
5. Run the `continue` line, then start the monitor again.
