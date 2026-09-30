"""A user-namespaced container from an image with zero layers, under containerd.

containerd's overlay snapshotter chowns a snapshot with no parents to the
remapped root, then returns it as a bind with uidmap options, and the bind
is idmapped as well. The owner is mapped twice and falls out of the range,
so the container's root cannot create /proc: "mkdirat rootfs/proc:
permission denied", under runc and crun alike. A one-layer image, even one
without /proc, runs. Lillecarl/containerd#1 has the detail.

Every case runs on each guest. The guests in `settings.fixed` carry the
fix and must run all of them; the others must fail exactly the
zero-layer user-namespace cases. crictl only: no kubelet, no NRI.
"""

import json

import anyio
from vivarium_runner import Machine, MachineError, Machines

HOST_ID = 3_000_000_000
IDS = [{"host_id": HOST_ID, "container_id": 0, "length": 65536}]
USERNS = {"userns_options": {"mode": 0, "uids": IDS, "gids": IDS}}
# The pod network, since kubelet refuses a user namespace on the host's.
POD_NETWORK = 0
POD_CIDR = "10.244.0.0/24"


def pod(name: str, userns: bool) -> dict:
    options: dict = {"network": POD_NETWORK, **(USERNS if userns else {})}
    return {
        "metadata": {"name": name, "namespace": "zero-layers", "uid": name},
        "log_directory": f"/tmp/zero-layers/{name}",
        "linux": {"cgroup_parent": "system.slice", "security_context": {"namespace_options": options}},
    }


def container(image: str, busybox: str, userns: bool) -> dict:
    return {
        "metadata": {"name": "c"},
        "image": {"image": image},
        # busybox picks its applet from argv[0], so the file keeps its name.
        "command": ["/busybox", "cat", "/proc/self/uid_map"],
        "log_path": "c.log",
        "mounts": [{"host_path": busybox, "container_path": "/busybox", "readonly": True}],
        "linux": {"security_context": {"namespace_options": USERNS}} if userns else {},
    }


async def case(vm: Machine, busybox: str, runtime: str, image: str, userns: bool) -> tuple[bool, str]:
    """Whether the container ran with the uid map it asked for, and a line saying so."""
    name = f"{runtime}-{image}-{'userns' if userns else 'host'}"
    await vm.succeed(
        f"mkdir -p /tmp/zero-layers/{name};"
        f" echo '{json.dumps(pod(name, userns))}' > /tmp/{name}-pod.json;"
        f" echo '{json.dumps(container(f'zero-layers.test/{image}:1', busybox, userns))}' > /tmp/{name}-ctr.json"
    )
    rc, out = await vm.execute(
        f"crictl --timeout 5m run --no-pull --runtime {runtime} /tmp/{name}-ctr.json /tmp/{name}-pod.json 2>&1"
    )
    if rc != 0:
        err = next((line for line in out.splitlines() if "failed" in line), out.strip())
        return False, f"{name}: does not start: {err[-220:]}"
    cid = out.strip().splitlines()[-1]
    with anyio.fail_after(30):
        while json.loads(await vm.succeed(f"crictl inspect {cid}"))["status"]["state"] != "CONTAINER_EXITED":
            await anyio.sleep(0.2)
    logs = (await vm.succeed(f"crictl logs {cid}")).strip()
    ok = (logs.split() == ["0", str(HOST_ID), "65536"]) == userns
    return ok, f"{name}: {'runs' if ok else 'wrong uid map'}, uid_map {' '.join(logs.split())}"


async def mechanism(vm: Machine) -> None:
    """What the zero-layer rootfs is, measured: how containerd mounts it,
    who owns it, and what an idmapped bind shows for either owner. util-linux's
    X-mount.idmap makes the same mount_setattr(MOUNT_ATTR_IDMAP) containerd does."""
    # An active snapshot with no parent is a zero-layer container's rootfs.
    # The user-namespaced ones are chowned to the remapped root.
    active = "ctr -n k8s.io snapshots ls | awk 'NR>1 && NF==2 && $2==\"Active\" {print $1}'"
    for key in (await vm.succeed(active)).split():
        mount = (await vm.succeed(f"ctr -n k8s.io snapshots mounts /x {key}")).strip()
        owner = (await vm.succeed(f"stat -c %u:%g {mount.split()[3]}")).strip()
        if owner == f"{HOST_ID}:{HOST_ID}":
            print(f"[{vm.name}] a zero-layer userns rootfs: {mount} (owned by {owner})")
            break
    for owner in ("0", str(HOST_ID)):
        shown = await vm.succeed(
            f"d=$(mktemp -d); chown {owner}:{owner} $d; t=$(mktemp -d);"
            f" mount --bind -o 'X-mount.idmap=u:0:{HOST_ID}:65536 g:0:{HOST_ID}:65536' $d $t;"
            " stat -c %u:%g $t; umount $t"
        )
        print(f"[{vm.name}] owned by {owner} on disk, through an idmapped bind: {shown.strip()}")


async def test(vms: Machines) -> None:
    (name, vm), = vms.items()
    settings = vms.settings
    fixed = name in settings["fixed"]
    version = (await vm.succeed("containerd --version")).strip()
    print(f"[{name}] {version}")
    if fixed != ("zero-layers-fix" in version):
        raise MachineError(f"[{name}] runs the wrong containerd: {version}")
    await vm.wait_for_unit("vivarium-k8s-cri.target", timeout=300)
    await vm.succeed(f"vivarium-k8s-cni {POD_CIDR}")
    for archive in settings["images"].values():
        await vm.succeed(f"ctr -n k8s.io images import {archive}")
    wrong = []
    for runtime in ("runc", "crun"):
        for image in ("zero", "one-no-proc", "one-proc"):
            for userns in (False, True):
                ok, line = await case(vm, settings["busybox"], runtime, image, userns)
                print(f"[{name}] {line}")
                if ok != (fixed or not (image == "zero" and userns)):
                    wrong.append(line)
    await mechanism(vm)
    if wrong:
        raise MachineError(f"[{name}] {'with' if fixed else 'without'} the fix, not as expected:\n" + "\n".join(wrong))
    print(f"[{name}] as expected {'with' if fixed else 'without'} the fix")
