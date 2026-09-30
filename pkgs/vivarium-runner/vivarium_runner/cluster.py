"""Bring a kubeadm cluster up on UML guests, and ask it questions.

A node knows nothing about the others -- see ``modules/k8s.nix`` -- so
everything needing the whole cluster in view lives here rather than in a
module: who joins whom, which /24 each node was given, the routes between
them, and whether anything is Ready.

This is a library and not a test.  ``tests/k8s.py`` is the three-node case,
and a caller outside this repository can build its own:

    from vivarium_runner import run_test
    from vivarium_runner.cluster import bring_up, kubectl

    async def test(vms):
        cp = await bring_up(vms)
        await kubectl(cp, "apply --filename /nix/store/...")

    run_test(test)

Everything a caller is likely to need is a coroutine over ``Machine``
objects, so nothing here assumes the guests came from this repository's
``flake.nix`` -- only that they run ``services.uml-k8s``.
"""

import asyncio
import json

from .machine import MachineError

# These are stuck-detectors, and that is the only thing they are.
#
# Nothing here is slow.  A guest is a process, its store is the host's, and
# every image was imported before kubelet started -- so there is nothing to
# compile, nothing to pull and nothing to fetch.  A control plane that is
# working is serving inside a couple of minutes.
#
# They used to be 30, 20 and 15 minutes, on the theory that a failure here is
# "nearly always slowness rather than breakage".  That theory is what makes a
# bug look like a delay: a stuck kubeadm and a slow one are indistinguishable
# for half an hour, and the half hour is spent either way.  Short deadlines
# turn the same bug into a report with the pod list, the events, kubelet,
# containerd and the pod logs attached.
#
# If one of these fires on something that was genuinely still working, the
# answer is to find out what took the time and fix that -- not to raise the
# number.
INIT_TIMEOUT = 5 * 60
JOIN_TIMEOUT = 5 * 60
READY_TIMEOUT = 5 * 60

# A systemd unit reaching `active`.  Same reasoning, and the same number:
# these units start local programs against local files.
UNIT_TIMEOUT = 5 * 60

POLL = 10

# How often `until` says what it is still waiting for.
_SAY_EVERY = 30

# The taint kubeadm puts on a control plane so that nothing schedules there.
CONTROL_PLANE_TAINT = "node-role.kubernetes.io/control-plane"

# The two addon pods, as label selectors, for `bring_up`'s `addons`.
#
# **kube-proxy is not optional in the way it looks.** It makes ClusterIP
# Services reachable, and the cluster has one whether or not a test declares
# any: `kubernetes.default`, at the first address of the service subnet, is
# how everything inside a pod reaches the API server. Take kube-proxy away
# and nothing gets DNAT'd there, so any in-cluster client -- kubectl in a
# Job, a controller using its ServiceAccount -- fails to reach the apiserver
# at all. Measured: nixkube's init Job runs `kubectl get secret` and exits
# non-zero without it.
#
# CoreDNS genuinely is optional. In-cluster clients read
# KUBERNETES_SERVICE_HOST, which is an address rather than a name, so nothing
# resolves anything unless a test asks it to. Skipping it saves two pods on a
# one-CPU guest and a great deal of log noise, because a sandboxed CoreDNS
# spends its life timing out against an upstream resolver it cannot reach.
# What a node writes when it declares PersistentVolumes -- see
# `services.uml-k8s.persistentVolumes` and `provision_storage`.
STORAGE_MANIFEST = "/etc/kubernetes/uml-storage.yaml"
# `services.uml-k8s.runtimes` and `provision_runtimes`.
RUNTIME_MANIFEST = "/etc/kubernetes/uml-runtimes.yaml"

KUBE_PROXY = "--selector k8s-app=kube-proxy"
KUBE_DNS = "--selector k8s-app=kube-dns"

DEFAULT_ADDONS = (KUBE_PROXY, KUBE_DNS)


async def kubectl(cp, args, timeout=120):
    """Run kubectl on the control plane; returns its output."""
    return await cp.succeed(f"kubectl {args}", timeout=timeout)


async def get_json(cp, args, timeout=120):
    return json.loads(await kubectl(cp, f"{args} --output json", timeout=timeout))


async def until(what, check, timeout, machine):
    """Poll *check* until it says yes; returns what it last saw.

    *check* returns ``(done, evidence)``.  The evidence is reported on
    both paths, because "timed out waiting for nodes" on its own says
    nothing about which node was not ready.

    It is also reported while waiting, so that a watcher can tell a slow
    thing from a stopped one without waiting for the deadline.

    The report names how long the evidence has been *unchanged*, which is
    the number that says whether anything is happening.  A pod that goes
    Pending -> ContainerCreating -> Running is working; the same three
    words for four minutes are not, and the elapsed clock alone cannot
    tell them apart.  That figure is in the failure too: "stuck for 290s"
    and "still moving, just slow" are different bugs and want different
    fixes.

    Timed as one step, because this is the only poll loop a cluster test
    has and it is most of the run: measured on nixkube's nine scenarios,
    632 of 786 seconds were in here and invisible until this line.
    """
    with machine.waiting(what):
        return await _until(what, check, timeout, machine)


async def _until(what, check, timeout, machine):
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + timeout
    said = None
    seen = None
    changed = started

    # `asyncio.timeout`, and not a deadline tested between polls.
    #
    # A poll is two kubectl calls with timeouts of their own, so testing the
    # clock only after one completes lets a slow guest overshoot by minutes:
    # a 300s deadline was still running at 291s having last checked at 258s,
    # and could have run to 540s had both calls hit their own limits. This
    # bounds `check` itself, so the deadline means what it says.
    try:
        async with asyncio.timeout(timeout):
            while True:
                done, evidence = await check()
                if done:
                    return evidence

                now = loop.time()
                if evidence != seen:
                    seen = evidence
                    changed = now

                if said is None or now - said >= _SAY_EVERY:
                    said = now
                    first = str(evidence).strip().splitlines()[:1]
                    print(
                        f"[{machine.name}] {what}: {int(now - started)}s elapsed, "
                        f"{int(now - changed)}s unchanged, {int(deadline - now)}s left"
                        + (f" -- {first[0]}" if first else ""),
                        flush=True,
                    )
                await asyncio.sleep(POLL)
    except TimeoutError:
        # Outside the block above, so gathering the evidence is not itself
        # cancelled by the deadline that just fired.
        now = loop.time()
        raise MachineError(
            f"[{machine.name}] gave up waiting for {what} after "
            f"{int(now - started)}s, the last {int(now - changed)}s of it "
            f"with nothing changing.\n"
            f"Nothing here is slow enough for this to be patience: the images "
            f"are on the node and the store is the host's. Something is "
            f"stuck.\n{seen}\n" + await diagnose(machine)
        ) from None


async def unscheduled(vm):
    """The events, and a describe of every pod that is not Running.

    **A pod that never leaves Pending leaves no other trace.** It starts no
    container, so `crictl ps -a` does not list it; it writes no pod log, so
    the log dump below is empty for it; and the kubelet never sees it, so
    its journal says nothing either. The reason is the scheduler's, and the
    scheduler writes it to an Event and to `describe`.

    Measured on nixkube's `test-qemu-ci-cache`: two runs reported
    `pynixd-0: Pending` for 300s each, and everything this function did not
    yet collect said nothing at all about it.

    `execute` and not `succeed`: this runs on a machine that is already in
    trouble, and a diagnostic must never be the thing that fails.
    """
    # `--output name` gives `pod/<name>` and no namespace, so the namespace
    # comes from custom columns instead.
    columns = "NS:.metadata.namespace,NAME:.metadata.name"
    return (
        await vm.execute(
            "kubectl get events --all-namespaces"
            " --sort-by=.lastTimestamp 2>&1 | tail -n 60;"
            " echo;"
            " kubectl get pods --all-namespaces --no-headers"
            " --field-selector=status.phase!=Running,status.phase!=Succeeded"
            f" --output custom-columns={columns} 2>/dev/null"
            " | while read -r ns name; do"
            "     kubectl describe pod -n $ns $name 2>&1 | tail -n 30;"
            "   done"
        )
    )[1]


async def diagnose(vm):
    """Everything worth knowing about a node that would not come up.

    A CI round trip on a cluster test is the better part of an hour, so a
    failure should answer the next question as well as the first one.
    kubelet says why it will not start; containerd says why a container
    would not; crictl says which ones exist; and the pod logs are where
    the control plane itself complains -- kubeadm's own output shows none
    of that, because from where it stands the apiserver simply never
    answered.
    """
    # A test's own pods, and only then the control plane.
    #
    # This used to tail every pod log under one 400-line budget, which the
    # control plane wins every time: etcd narrates each slow read and
    # kube-apiserver each synced cache, so on a UML guest the two of them
    # produce more than the budget by themselves.  Measured -- a failing
    # nixkube DaemonSet whose init container was the whole question, and
    # whose log did not appear in the report at all.
    #
    # kube-system is still worth having, so it gets a budget of its own.
    # Second, because `tail` keeps the end.
    logs = (
        await vm.execute(
            "{ for f in /var/log/pods/*/*/*.log; do"
            "   case $f in /var/log/pods/kube-system_*) ;;"
            "               *) tail -n 40 -v \"$f\";; esac;"
            " done; } 2>&1 | tail -n 300;"
            " echo;"
            " tail -n 15 -v /var/log/pods/kube-system_*/*/*.log 2>&1 | tail -n 150"
        )
    )[1]
    # containerd or crio: the unit `services.uml-k8s.cri` put behind the target.
    runtime = (
        await vm.execute("systemctl show --property Requires --value uml-k8s-cri.target")
    )[1].split() or ["containerd.service"]
    return (
        f"--- [{vm.name}] addresses and routes ---\n"
        f"{(await vm.execute('ip -brief addr; ip route; ip -6 route'))[1]}\n"
        f"--- [{vm.name}] events, and the pods that are not running ---\n"
        f"{await unscheduled(vm)}\n"
        f"--- [{vm.name}] crictl ps -a ---\n{(await vm.execute('crictl ps -a'))[1]}\n"
        f"--- [{vm.name}] kubelet ---\n{await vm.journal('kubelet.service', lines=80)}\n"
        f"--- [{vm.name}] {runtime[0]} ---\n{await vm.journal(runtime[0], lines=40)}\n"
        f"--- [{vm.name}] pod logs ---\n{logs}"
    )


async def wait_for_images(vms):
    """No node is any use to kubeadm until containerd has the images.

    `UNIT_TIMEOUT` and not a number of its own.  This unit reads tarballs
    off the host store and hands them to containerd, so it is bounded by
    a local disk and nothing else -- if it has not finished in five
    minutes it is not importing slowly, it is stuck.
    """
    await asyncio.gather(
        *(
            vm.wait_for_unit("k8s-load-images.service", timeout=UNIT_TIMEOUT)
            for vm in vms.values()
        )
    )


# Where kubeadm's own narration lands, so that a deadline does not throw it
# away.
#
# `execute` returns the output when the command returns, and a command that
# is killed at `timeout` returns nothing at all -- which is how a GHA run of
# this test failed with "timed out after 300 seconds" and not one word about
# which phase kubeadm was in.  A file on the node survives the kill.
INIT_LOG = "/tmp/kubeadm-init.log"


async def init_control_plane(cp, nix_images=True):
    print("[k8s] cp: kubeadm init ...", flush=True)
    # Redirected and then printed, rather than piped through `tee`: a pipe
    # would report tee's exit status, and /bin/sh here has no `pipefail`.
    command = (
        "kubeadm init --config /etc/kubernetes/kubeadm-config.yaml --v=2"
        f" > {INIT_LOG} 2>&1; rc=$?; cat {INIT_LOG}; exit $rc"
    )
    try:
        rc, out = await cp.execute(command, timeout=INIT_TIMEOUT, label="kubeadm init")
    except Exception as err:
        raise MachineError(
            f"[cp] kubeadm init did not finish in {INIT_TIMEOUT}s: {err}\n"
            + await cp.succeed(f"tail -n 60 {INIT_LOG} 2>&1 || true")
            + "\n"
            + await diagnose(cp)
        ) from err
    if rc != 0:
        raise MachineError(
            f"[cp] kubeadm init failed (exit {rc}):\n{out}\n" + await diagnose(cp)
        )
    if nix_images:
        await patch_kube_proxy(cp)
    print("[k8s] cp: control plane is up", flush=True)


# kube-proxy's store mount, as a strategic-merge patch.
#
# Every image on these nodes is symlinks into /nix/store, so every
# container built from one needs the store to resolve them.  kubeadm's
# `patches.directory` covers the four static pods and CoreDNS; kube-proxy is
# applied from a manifest baked into kubeadm and has no patch target at all,
# so it is patched here, once, right after init.
#
# Doing it this way rather than through containerd's `base_runtime_spec`,
# which would give *every* container the store in three lines: a node that
# hands the store to pods that never asked for it cannot be used to test
# anything that puts /nix into a pod.  It would pass with its subject
# switched off.
KUBE_PROXY_PATCH = json.dumps(
    {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "kube-proxy",
                            "volumeMounts": [
                                {
                                    "name": "nix-store",
                                    "mountPath": "/nix/store",
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "nix-store",
                            "hostPath": {"path": "/nix/store", "type": "Directory"},
                        }
                    ],
                }
            }
        }
    }
)


async def patch_kube_proxy(cp):
    """Give kube-proxy the store, since kubeadm will not."""
    rc, out = await cp.execute(
        "kubectl --namespace kube-system patch daemonset kube-proxy"
        f" --type strategic --patch '{KUBE_PROXY_PATCH}'",
        timeout=UNIT_TIMEOUT,
    )
    if rc != 0:
        raise MachineError(
            f"[cp] could not give kube-proxy the store (exit {rc}):\n{out}"
        )
    print("[k8s] cp: kube-proxy has the store", flush=True)


async def join(cp, workers):
    """Bring the workers in with a token minted for this run.

    The token and the CA hash come from kubeadm rather than being read
    out of /etc/kubernetes, but the join itself does not use the command
    line kubeadm prints -- see `uml-k8s-join`, which wraps them in a
    configuration carrying this cluster's timeouts.

    --config is not optional here, even though the cluster already
    exists and this command only mints a token.  Without it kubeadm
    defaults an InitConfiguration first, and defaulting always calls
    ChooseAPIServerBindAddress -- which asks the kernel which interface
    owns the default route and then wants a global address on it.  That
    tolerates finding no default route at all (it warns and uses
    0.0.0.0), but treats a default route whose interface has no global
    address as fatal: "unable to select an IP from default routes", on a
    control plane that is up and serving.

    Passing the config we ran init with avoids the question rather than
    answering it: ResolveBindAddress returns an advertise address that
    is already concrete without looking at an interface at all.
    """
    if not workers:
        return
    printed = await cp.succeed(
        "kubeadm token create --print-join-command"
        " --config /etc/kubernetes/kubeadm-config.yaml"
    )
    fields = printed.split()
    try:
        token = fields[fields.index("--token") + 1]
        # Pins the cluster CA, so a worker cannot be talked into
        # bootstrapping against something else answering on that address.
        digest = fields[fields.index("--discovery-token-ca-cert-hash") + 1]
    except (ValueError, IndexError):
        raise MachineError(
            f"[cp] could not read a join command out of:\n{printed}"
        ) from None
    endpoint = f"{cp.ip}:6443"

    async def one(worker):
        print(f"[k8s] {worker.name}: kubeadm join ...", flush=True)
        # Kept on the node for the same reason as `INIT_LOG`.
        log = "/tmp/kubeadm-join.log"
        try:
            rc, out = await worker.execute(
                f"uml-k8s-join {endpoint} {token} {digest}"
                f" > {log} 2>&1; rc=$?; cat {log}; exit $rc",
                timeout=JOIN_TIMEOUT,
                label="kubeadm join",
            )
        except Exception as err:
            raise MachineError(
                f"[{worker.name}] kubeadm join did not finish in"
                f" {JOIN_TIMEOUT}s: {err}\n"
                + await worker.succeed(f"tail -n 60 {log} 2>&1 || true")
                + "\n"
                + await diagnose(worker)
            ) from err
        if rc != 0:
            raise MachineError(
                f"[{worker.name}] kubeadm join failed (exit {rc}):\n{out}\n"
                + await diagnose(worker)
            )
        print(f"[k8s] {worker.name}: joined", flush=True)

    await asyncio.gather(*(one(w) for w in workers))


async def wire_pod_network(cp, vms):
    """Give each node its CNI config, and a route to everyone else's pods.

    kube-controller-manager hands out a /24 per node, but nothing sets up
    a data path: this is the whole CNI, and it is about as small as one
    gets away with.  A real cluster would run an overlay here; guests on
    one Ethernet segment can just route.  A single node needs no routes
    at all and still needs the CNI configuration, or kubelet stays
    NotReady with "cni plugin not initialized".
    """
    # `kubeadm join` returns once the node has registered, which is not
    # the same instant kube-controller-manager's IPAM has given it a
    # /24: reading the node list straight afterwards is a race that a
    # fast join loses.
    cidrs = {}

    async def assigned():
        nodes = await get_json(cp, "get nodes")
        cidrs.clear()
        cidrs.update(
            {
                item["metadata"]["name"]: item["spec"].get("podCIDR")
                for item in nodes["items"]
            }
        )
        missing = sorted(name for name, cidr in cidrs.items() if not cidr)
        return not missing and len(cidrs) == len(vms), (
            f"without a podCIDR: {', '.join(missing) or 'none'}; "
            f"nodes: {', '.join(sorted(cidrs)) or 'none'}"
        )

    await until("every node to be given a podCIDR", assigned, JOIN_TIMEOUT, cp)
    print(f"[k8s] pod subnets: {cidrs}", flush=True)

    if set(cidrs) != set(vms):
        raise MachineError(
            f"[cp] the cluster has {sorted(cidrs)}, the test booted {sorted(vms)}"
        )

    async def one(vm):
        await vm.succeed(f"uml-k8s-cni {cidrs[vm.name]}")
        for peer in vms.values():
            if peer.name != vm.name:
                await vm.succeed(
                    f"ip route replace {cidrs[peer.name]} via {peer.ip} dev vec1"
                )

    await asyncio.gather(*(one(vm) for vm in vms.values()))


async def wait_for_ready_nodes(cp, expected):
    async def check():
        out = await kubectl(cp, "get nodes --no-headers")
        ready = [line for line in out.splitlines() if " Ready " in f" {line} "]
        return len(ready) == expected, out

    await until(f"{expected} Ready nodes", check, READY_TIMEOUT, cp)
    print(f"[k8s] all {expected} nodes Ready", flush=True)


async def wait_for_pods(cp, selector, namespace="kube-system"):
    """Wait until every pod matching *selector* is Ready."""

    async def check():
        pods = await get_json(cp, f"get pods --namespace {namespace} {selector}")
        items = pods["items"]
        ready = [
            pod
            for pod in items
            if any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in pod["status"].get("conditions", [])
            )
        ]
        summary = "\n".join(
            f"    {pod['metadata']['name']}: {pod['status'].get('phase')}"
            for pod in items
        )
        return bool(items) and len(ready) == len(items), summary

    await until(f"pods {selector}", check, READY_TIMEOUT, cp)


async def wait_for_dns(vm, host="registry.k8s.io", timeout=120):
    """A node that pulls its images has to resolve a name first.

    kubeadm asks containerd for an image within seconds of the guest
    reaching multi-user, and the guest's resolver is configured from DHCP
    by systemd-resolved at about the same moment.  Lose that race and the
    first thing that happens is `failed to pull and unpack image
    "registry.k8s.io/etcd:3.6.8-0": ... lookup registry.k8s.io: no such
    host`, which reads as a broken registry rather than as a resolver
    that was not ready.  Measured on a GitHub runner, five seconds after
    boot; this host is slower to start the test and never lost it.

    **The evidence is vec0's address, not the resolver.**  The resolver
    is a constant now -- see `networking.nameservers` in
    modules/guest.nix -- so it is the same on a guest that resolves and
    on one that does not, and says nothing.  What differs is whether
    passt's DHCP ever answered: a guest with only a link-local address
    on vec0 has no route to any resolver, however good the address.
    That was the whole of it on a GitHub runner, under a report that
    named the resolver -- run 34943651620.

    Not `/etc/resolv.conf` either: on a systemd-resolved guest that file
    is the 127.0.0.53 stub whatever happens.
    """

    async def check():
        rc, out = await vm.execute(f"getent hosts {host}")
        if rc == 0:
            return True, out.strip()
        uplink = await vm.succeed(
            "ip -4 -brief addr show vec0 2>/dev/null | tr -s ' ' || true"
        )
        return False, f"not yet, and vec0 has {uplink.strip() or '(no IPv4 address)'}"

    await until(f"{vm.name} to resolve {host}", check, timeout, vm)


async def untaint(cp):
    """Let workloads run on the control plane.

    kubeadm taints it so that nothing schedules there, which is right for a
    cluster with workers and leaves a single-node cluster unable to run
    anything at all.  The trailing ``-`` is kubectl's syntax for removing a
    taint, and it is not an error when the taint is already gone.
    """
    await kubectl(cp, f"taint nodes --all {CONTROL_PLANE_TAINT}-")
    print("[k8s] control plane will schedule workloads", flush=True)


async def provision_storage(cp, vms):
    """Apply the PersistentVolumes every node declares, if any does.

    A node writes ``STORAGE_MANIFEST`` when
    ``services.uml-k8s.persistentVolumes`` is above zero, and nothing when
    it is not -- so this asks the guests rather than taking an argument,
    and a test that wants storage says so in one place.

    Each node's file names volumes pinned to that node, so the file is
    read where it was written and piped to the one machine that has a
    kubeconfig.  Applying the same StorageClass once per node is
    deliberate and costs nothing: `kubectl apply` is idempotent.
    """
    for name in await _apply_node_manifests(cp, vms, STORAGE_MANIFEST):
        print(f"[k8s] {name} offers storage", flush=True)


async def provision_runtimes(cp, vms):
    """Apply the RuntimeClasses every node declares, if any does.

    ``services.uml-k8s.runtimes`` writes ``RUNTIME_MANIFEST``, on the same
    terms as ``provision_storage``.
    """
    for name in await _apply_node_manifests(cp, vms, RUNTIME_MANIFEST):
        print(f"[k8s] {name} offers runtime classes", flush=True)


async def _apply_node_manifests(cp, vms, path):
    """Apply *path* from every node that has it; return those nodes' names."""
    applied = []
    for name, vm in vms.items():
        rc, _ = await vm.execute(f"test -e {path}")
        if rc != 0:
            continue
        manifest = await vm.succeed(f"cat {path}")
        await cp.succeed(f"kubectl apply --filename - <<'EOF'\n{manifest}\nEOF")
        applied.append(name)
    return applied


async def bring_up(
    vms, cp_name="cp", schedulable=None, addons=DEFAULT_ADDONS, nix_images=True
):
    """The whole sequence, from booted guests to a cluster that works.

    Returns the control plane machine.

    *nix_images* says which `services.uml-k8s.images` the guests were
    built with, and the two must agree.  Under `"nix"` the node imports
    every image at boot and each one is symlinks into `/nix/store`, so
    this waits for that import and gives kube-proxy the store.  Under
    `"pull"` there is no import to wait for -- waiting would hang until
    the deadline on a unit nobody created -- and nothing needs patching.
    It waits for a name to resolve instead, which is the thing a pulling
    node cannot start without.  See `wait_for_dns`.

    *schedulable* removes the control plane's taint.  The default decides by
    size: a single-node cluster has nowhere else to put a pod, and one with
    workers should keep the control plane for the control plane.

    *addons* are the addon pods to wait for, as label selectors.  Pass `()`
    for a cluster that runs neither -- see `DEFAULT_ADDONS`.  It has to
    agree with `services.uml-k8s.skipAddons`, which decides what kubeadm
    installs in the first place: waiting for a pod nobody created hangs
    until the deadline, and not waiting for one that exists lets a test
    start before cluster DNS answers.
    """
    cp = vms[cp_name]
    workers = [vm for name, vm in vms.items() if name != cp_name]
    if schedulable is None:
        schedulable = not workers

    for vm in vms.values():
        await vm.wait_for_unit("uml-k8s-cri.target", timeout=UNIT_TIMEOUT)
    if nix_images:
        await wait_for_images(vms)
    else:
        for vm in vms.values():
            await wait_for_dns(vm)

    await init_control_plane(cp, nix_images=nix_images)
    await join(cp, workers)
    await wire_pod_network(cp, vms)

    await wait_for_ready_nodes(cp, len(vms))
    await provision_storage(cp, vms)
    await provision_runtimes(cp, vms)
    for selector in addons:
        await wait_for_pods(cp, selector)

    if schedulable:
        await untaint(cp)

    return cp


__all__ = [
    "CONTROL_PLANE_TAINT",
    "DEFAULT_ADDONS",
    "INIT_TIMEOUT",
    "KUBE_DNS",
    "KUBE_PROXY",
    "JOIN_TIMEOUT",
    "POLL",
    "READY_TIMEOUT",
    "RUNTIME_MANIFEST",
    "STORAGE_MANIFEST",
    "UNIT_TIMEOUT",
    "bring_up",
    "diagnose",
    "get_json",
    "init_control_plane",
    "join",
    "kubectl",
    "provision_runtimes",
    "provision_storage",
    "untaint",
    "until",
    "wait_for_dns",
    "wait_for_images",
    "wait_for_pods",
    "wait_for_ready_nodes",
    "wire_pod_network",
]
