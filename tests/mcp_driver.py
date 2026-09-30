#!/usr/bin/env python3
"""Drive `vivarium-mcp` the way Claude Code does: JSON-RPC over its stdio.

Raw, not the SDK's client: the SDK validates each incoming notification
against the methods it knows, and `notifications/claude/channel` is a
Claude Code extension. What this reads is what Claude Code reads.

    mcp_driver.py <vivarium-mcp> <spec of a run that fails a phase>
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

CHANNEL = "notifications/claude/channel"


class Client:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.next_id = 0
        self.channel: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()

    async def receive(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        line = await self.process.stdout.readline()
        if not line:
            raise RuntimeError("vivarium-mcp closed its stdout")
        message = json.loads(line)
        if message.get("method") == CHANNEL:
            self.channel.append(message["params"])
            print(f"[mcp] channel: {json.dumps(message['params'])[:300]}", flush=True)
        return message

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        await self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            message = await self.receive()
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message["result"]

    async def tool(self, name: str, **arguments: Any) -> Any:
        result = await self.call("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(f"{name}: {result['content']}")
        return result["structuredContent"]

    async def until(self, event: str) -> dict[str, Any]:
        while True:
            for params in self.channel:
                if params["meta"].get("event") == event:
                    return params
            await self.receive()


def fail(text: str) -> None:
    print(f"FAIL: {text}", file=sys.stderr, flush=True)
    raise SystemExit(1)


async def main(server: str, spec: str) -> None:
    process = await asyncio.create_subprocess_exec(
        server, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    client = Client(process)
    init = await client.call(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcp-driver", "version": "0"},
        },
    )
    if "claude/channel" not in init["capabilities"].get("experimental", {}):
        fail(f"no claude/channel capability: {init['capabilities']}")
    print("ok: the server declares claude/channel", flush=True)
    await client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    started = await client.tool("start", spec=spec)
    run = started["run"]
    print(f"ok: started {run}", flush=True)
    # The channel's events without channels: the command `start` gave,
    # in a shell, as Claude Code's Monitor runs it.
    watcher = await asyncio.create_subprocess_shell(
        started["monitor_all"] + " --json", stdout=asyncio.subprocess.PIPE
    )
    armed = await asyncio.create_subprocess_shell(started["monitor_pause"], stdout=asyncio.subprocess.PIPE)

    paused = await asyncio.wait_for(client.until("paused"), 600)
    if paused["meta"].get("run") != run:
        fail(f"the pause names another run: {paused}")
    print(f"ok: a channel event said it paused: {paused['meta']}", flush=True)
    if not any(p["meta"].get("event") == "failed" for p in client.channel):
        fail("no channel event for the failed phase")
    print("ok: and one said which phase failed", flush=True)

    # For a harness that wakes an agent only when a command exits.
    output, _ = await asyncio.wait_for(armed.communicate(), 60)
    if armed.returncode != 4 or "paused" not in output.decode().splitlines()[-1]:
        fail(f"monitor_pause exited {armed.returncode} at the pause with {output.decode()!r}")
    print(f"ok: monitor_pause exited 4 at the pause: {output.decode().splitlines()[-1]}", flush=True)
    late = await asyncio.create_subprocess_shell(started["monitor_pause"], stdout=asyncio.subprocess.PIPE)
    await asyncio.wait_for(late.communicate(), 60)
    if late.returncode != 4:
        fail(f"monitor_pause armed while paused exited {late.returncode}, not 4")
    print("ok: monitor_pause armed while paused exited 4 at once", flush=True)

    reply = await client.tool("exec", run=run, code='await one.succeed("hostname")')
    if "one" not in str(reply.get("result")):
        fail(f"exec did not reach the guest: {reply}")
    print(f"ok: exec reached the paused guest: {reply['result']}", flush=True)

    cases = (await client.tool("events", run=run, kind="case"))["events"]
    failed = [c for c in cases if c["data"]["outcome"] == "failed"]
    if not failed:
        fail(f"events found no failed case: {cases}")
    print(f"ok: events found the failed case: {failed[0]['text']}", flush=True)

    # A test from a working tree, against the paused guest; then an edit
    # to it, sent again. The second answer must be the edit's.
    tree = Path(tempfile.mkdtemp()) / "by_hand"
    tree.mkdir()
    test = tree / "test_by_hand.py"
    test.write_text("async def test_host(one):\n    assert (await one.succeed('hostname')).strip() == 'one'\n")
    reply = await client.tool("run_pytest", run=run, path=str(tree))
    if not reply["ok"] or reply["result"] != "1 passed":
        fail(f"run_pytest did not pass against the guest: {reply}")
    test.write_text("async def test_host(one):\n    assert (await one.succeed('hostname')).strip() == 'edited'\n")
    reply = await client.tool("run_pytest", run=run, path=str(tree), args=["-k", "host"])
    if reply["ok"]:
        fail(f"run_pytest ran the old test after an edit: {reply}")
    print("ok: run_pytest ran a local test, then its edit, against the paused guest", flush=True)

    await client.tool("resume", run=run)
    # Armed on the reply, as an agent does: the pause it replays is closed,
    # so it runs on to the verdict.
    rearmed = await asyncio.create_subprocess_shell(started["monitor_pause"], stdout=asyncio.subprocess.PIPE)
    finished = await asyncio.wait_for(client.until("finished"), 300)
    if finished["meta"].get("passed") != "false":
        fail(f"the verdict is wrong: {finished}")
    print(f"ok: a channel event gave the verdict: {finished['content']}", flush=True)

    output, _ = await asyncio.wait_for(watcher.communicate(), 60)
    watched = [json.loads(line) for line in output.decode().splitlines()]
    heard = [{**p["meta"], "text": p["content"]} for p in client.channel if p["meta"].get("run") == run]
    if watched != heard:
        fail(f"the monitor and the channel disagree:\n{watched}\n{heard}")
    if watcher.returncode != 1:
        fail(f"the monitor exited {watcher.returncode} for a failed run, not 1")
    print(f"ok: the monitor printed the channel's {len(watched)} events and exited 1", flush=True)

    late = await asyncio.create_subprocess_shell(started["monitor_all"], stdout=asyncio.subprocess.PIPE)
    output, _ = await asyncio.wait_for(late.communicate(), 60)
    lines = output.decode().splitlines()
    if late.returncode != 1 or len(lines) != len(heard):
        fail(f"a monitor after the verdict exited {late.returncode} with {lines}")
    print(f"ok: a monitor after the verdict replayed it: {lines[-1]}", flush=True)

    await asyncio.wait_for(rearmed.communicate(), 60)
    if rearmed.returncode != 1:
        fail(f"monitor_pause armed after resume exited {rearmed.returncode}, not 1 for the verdict")
    print("ok: monitor_pause armed after resume ran on to the verdict", flush=True)

    # The one an agent runs: no progress, so it wakes only when it must.
    quiet = await asyncio.create_subprocess_shell(started["monitor"] + " --json", stdout=asyncio.subprocess.PIPE)
    output, _ = await asyncio.wait_for(quiet.communicate(), 60)
    kept = [json.loads(line) for line in output.decode().splitlines()]
    wanted = [event for event in heard if event.get("event") not in {"progress", "resumed"}]
    if quiet.returncode != 1 or kept != wanted or len(wanted) == len(heard):
        fail(f"the quiet monitor exited {quiet.returncode} with {kept}, not {wanted}")
    print(f"ok: the quiet monitor printed {len(kept)} of {len(heard)} events: no progress", flush=True)

    assert process.stdin is not None
    process.stdin.close()
    await asyncio.wait_for(process.wait(), 120)
    print("ok: the server exited when its client went away", flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
