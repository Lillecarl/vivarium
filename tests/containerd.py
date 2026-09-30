#!/usr/bin/env python3
"""Does a container run at all on a UML guest?

The narrow question the Kubernetes test cannot answer quickly.  A kubeadm
cluster failing to come up looks the same whether the kernel is missing a
namespace, the images did not import, or the store is not reachable from
inside a container -- and finding out takes an hour.  This asks the three
directly, on one guest, in about a minute:

    machine   the node looks enough like hardware for kubelet to accept
    import    containerd has the images kubeadm will ask for
    sandbox   a pod sandbox starts, so runc has the namespaces it wants
    store     a container whose only content is a symlink into
              /nix/store can exec it

The last one is the whole design of modules/k8s-images.nix: images that
carry nothing and rely on containerd's base_runtime_spec to bind the
host's store into every container.  If that mount is wrong, this fails
with a dangling symlink rather than with a control plane that never
becomes healthy.
"""

import asyncio
import json
import re

from vivarium_runner import Machine, MachineError, Machines

# Host network, so a sandbox needs no CNI: NamespaceMode.NODE is 2 in the
# CRI API.  This test is not about networking.
NODE_NETWORK = 2
# A VM has no host network to share. CRI-O refuses one for kata with "Host
# networking requested, not supported by runtime"; containerd accepts it.
POD_NETWORK = 0
VM_HANDLERS = {"kata"}
POD_CIDR = "10.244.0.0/24"

LOG_DIR = "/tmp/probe-logs"

# setupKernelTunables() in pkg/kubelet/cm/container_manager_linux.go.
TUNABLES = [
    "vm/overcommit_memory",
    "vm/panic_on_oom",
    "kernel/panic",
    "kernel/panic_on_oops",
    "kernel/keys/root_maxkeys",
    "kernel/keys/root_maxbytes",
]


# A pod in a user namespace of its own, as `hostUsers: false` asks kubelet
# for: container 0 is this host uid, for 65536 ids.
USERNS = {
    "userns_options": {
        "mode": 0,
        "uids": [{"host_id": 2_000_000_000, "container_id": 0, "length": 65536}],
        "gids": [{"host_id": 2_000_000_000, "container_id": 0, "length": 65536}],
    }
}


def pod(name: str, network: int = NODE_NETWORK, userns: bool = False) -> dict:
    return {
        "metadata": {"name": name, "namespace": "default", "uid": f"{name}-uid"},
        # A container's log_path is relative to this, and without it the
        # runtime keeps no log at all -- `crictl logs` then says the
        # container "has not set log path", which is true but unhelpful.
        "log_directory": LOG_DIR,
        "linux": {
            # kubelet always names one, and containerd's systemd cgroup driver
            # can only translate a path it was given: without this it builds
            # "/k8s.io/<id>", which runc rejects for not being
            # "slice:prefix:name".  Nothing here creates it -- system.slice
            # already exists.
            "cgroup_parent": "system.slice",
            "security_context": {
                "namespace_options": {"network": network, **(USERNS if userns else {})}
            },
        },
    }


def container(image: str) -> dict:
    return {
        "metadata": {"name": "probe"},
        "image": {"image": image},
        # By bare name, the way kubeadm's static pods invoke it -- so this
        # covers the image's PATH as well as the symlink itself.
        "command": ["kube-apiserver", "--version"],
        "log_path": "probe.log",
        # Asked for, not assumed. The node does not put the store in every
        # container -- kubeadm patches give the control plane its copy and
        # nothing else gets one -- so a container of symlinks has to say it
        # needs the thing they point at. See modules/k8s.nix.
        "mounts": [
            {
                "container_path": "/nix/store",
                "host_path": "/nix/store",
                "readonly": True,
            }
        ],
        "linux": {},
    }


async def write_json(vm: Machine, path: str, data: dict) -> None:
    await vm.succeed(f"cat <<'EOF' > {path}\n{json.dumps(data, indent=2)}\nEOF")


async def test(vms: Machines) -> None:
    node = vms.node
    version = vms.settings["kubernetesVersion"]

    await node.wait_for_unit("vivarium-k8s-cri.target", timeout=300)
    await node.wait_for_unit("k8s-load-images.service", timeout=600)

    # cadvisor refuses to start kubelet on a machine with no clock speed.
    # The UML kernel prints one (pkgs/uml-kernel/0002-um-report-cpu-mhz.patch).
    # Checked here rather than left to the cluster test, where it costs
    # five minutes of kubeadm init to find out that kubelet has been
    # crash-looping the whole time.
    cpuinfo = await node.succeed("cat /proc/cpuinfo")
    speed = re.search(r"(?:cpu MHz|CPU MHz|clock)\s*:\s*([0-9]+\.[0-9]+)", cpuinfo)
    if not speed:
        raise MachineError(
            f"[{node.name}] /proc/cpuinfo still has no clock speed cadvisor "
            f"will accept, so kubelet would not start:\n{cpuinfo}"
        )
    print(f"[test] node reports {speed.group(1)} MHz", flush=True)

    # kubelet's container manager raises these six before it starts, and
    # one it cannot open is an exit rather than a warning.  Four are in
    # files every kernel compiles; the keyring pair needs CONFIG_KEYS,
    # which an allnoconfig kernel does not give you.
    missing = [
        tunable
        for tunable in TUNABLES
        if (await node.execute(f"test -e /proc/sys/{tunable}"))[0] != 0
    ]
    if missing:
        raise MachineError(
            f"[{node.name}] the kernel is missing sysctls kubelet exits without:\n"
            + "\n".join(f"    /proc/sys/{tunable}" for tunable in missing)
        )
    print(f"[test] all {len(TUNABLES)} of kubelet's kernel tunables are settable", flush=True)

    listed = json.loads(await node.succeed("crictl images --output json"))
    images = [tag for image in listed["images"] for tag in image.get("repoTags") or []]
    print(f"[test] the runtime has {len(images)} images", flush=True)
    for expected in (f"registry.k8s.io/kube-apiserver:v{version}", vms.settings["sandboxImage"]):
        if expected not in images:
            raise MachineError(
                f"[{node.name}] {expected} was not imported; got:\n"
                + "\n".join(f"    {image}" for image in images)
            )

    # Every image is a symlink to one of these.  Checking them all here
    # is the difference between "etcd's image has no etcd in it" and a
    # control plane that crash-loops for twenty minutes because the one
    # image this test happens to run is the one that works.
    entrypoints = vms.settings["entrypoints"]
    dangling = [
        path
        for path in entrypoints
        if (await node.execute(f"test -x {path}"))[0] != 0
    ]
    if dangling:
        raise MachineError(
            f"[{node.name}] an image points at a binary the node does not have, "
            f"so runc will say it is not in $PATH:\n"
            + "\n".join(f"    {path}" for path in dangling)
        )
    print(f"[test] all {len(entrypoints)} image entrypoints resolve", flush=True)

    await node.succeed(f"mkdir -p {LOG_DIR}")

    # Asked of the runtime, so the script is the same on either backend
    # and either CRI. The CRI status lists the handlers since 1.30. The
    # default one comes back once more with no name: JSON drops an empty
    # proto3 string.
    info = json.loads(await node.succeed("crictl info"))
    handlers = sorted({h["name"] for h in info["runtimeHandlers"] if h.get("name")})
    print(f"[test] the runtime offers {', '.join(handlers)}", flush=True)
    for handler in handlers:
        await probe(node, handler, version)
    await probe(node, "runc", version, userns=True)


async def probe(node: Machine, handler: str, version: str, userns: bool = False) -> None:
    """Run the store probe under one containerd runtime handler.

    *userns* runs it in a user namespace of its own. The runtime then
    mounts a new procfs there, which the kernel refuses while anything
    covers part of the host's /proc (`mount_too_revealing`)."""
    network = NODE_NETWORK
    if handler in VM_HANDLERS or userns:
        await node.succeed(f"vivarium-k8s-cni {POD_CIDR}")
        network = POD_NETWORK
    name = f"{handler}-userns" if userns else handler
    await write_json(node, f"/tmp/pod-{name}.json", pod(f"probe-{name}", network, userns))
    spec = container(f"registry.k8s.io/kube-apiserver:v{version}")
    if userns:
        spec["linux"] = {"security_context": {"namespace_options": USERNS}}
    await write_json(node, f"/tmp/container-{name}.json", spec)

    # --no-pull, because there is nothing to pull from: if the image is
    # not already here the test should say so rather than time out on a
    # registry it cannot reach.
    out = await node.succeed(
        f"crictl --timeout 5m run --no-pull --runtime {handler}"
        f" /tmp/container-{name}.json /tmp/pod-{name}.json",
        timeout=400,
    )
    container_id = out.split()[-1]

    # `crictl run` returns once the container has started, not once it has
    # said anything, and starting a Go binary out of a hostfs-backed store
    # under UML is not instant.
    logs = ""
    for _ in range(30):
        _, logs = await node.execute(f"crictl logs {container_id}", timeout=120)
        if f"v{version}" in logs:
            break
        await asyncio.sleep(2)

    if f"v{version}" not in logs:
        status = json.loads(await node.succeed(f"crictl inspect {container_id}"))
        raise MachineError(
            f"[{node.name}] the {handler} container did not report Kubernetes v{version}.\n"
            f"--- logs ---\n{logs}\n"
            f"--- status ---\n{json.dumps(status.get('status', {}), indent=2)}"
        )
    if userns:
        spec = json.loads(await node.succeed(f"crictl inspect {container_id}"))
        maps = spec["info"]["runtimeSpec"]["linux"].get("uidMappings")
        if not maps:
            raise MachineError(f"[{node.name}] the userns probe ran with no uid mapping")
    print(f"[test] {name}: a container of symlinks exec'd out of /nix/store: {logs.strip()}")

