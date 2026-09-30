#!/usr/bin/env python3
"""A three-node kubeadm cluster, from `kubeadm init` to a working Service.

The nodes themselves know nothing about each other -- see modules/k8s.nix
-- and everything that needs the whole cluster in view is in
`vivarium_runner.cluster`, which any test can call:

    init      kubeadm init on cp, and kubeadm join on the two workers
    network   read each node's podCIDR, write its CNI config, and route
              the other nodes' pod subnets over the segment
    verify    all three Ready, CoreDNS up, and a pod on one worker
              reaching a Service backed by a pod on the other

What is left here is the last step, which is the point: it cannot pass by
accident, because it only works if the CNI bridge, the inter-node routes,
kube-proxy's iptables rules and cluster DNS all do their jobs.

Then storage, which is the other thing a chart assumes a cluster has: a
claim with no class named, bound to a node's own directory.
"""

from vivarium_runner import Machine, Machines
from vivarium_runner.cluster import (
    READY_TIMEOUT,
    bring_up,
    kubectl,
    until,
    wait_for_pods,
)


# The store, declared the way any pod on any cluster declares a volume.
#
# Every image here is symlinks into /nix/store, so a container built from
# one cannot start without it -- see modules/k8s-images.nix.  The node does
# not hand it out unasked: kubeadm patches give the control plane its copy
# and nothing else gets one, so that a test whose subject puts /nix into a
# pod can tell its subject's work from the node's.
STORE_VOLUME = """
    volumeMounts:
    - name: nix-store
      mountPath: /nix/store
      readOnly: true
  volumes:
  - name: nix-store
    hostPath:
      path: /nix/store
      type: Directory"""

MANIFEST = """
apiVersion: v1
kind: Pod
metadata:
  name: web
  labels: {{ app: web }}
spec:
  nodeName: {server}
  containers:
  - name: web
    image: {image}
    imagePullPolicy: Never
    command:
    - /bin/sh
    - -c
    - mkdir -p /www && echo {greeting} > /www/index.html && exec /bin/httpd -f -p 8080 -h /www
    ports:
    - containerPort: 8080
{store}
---
apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  selector: {{ app: web }}
  ports:
  - port: 80
    targetPort: 8080
---
apiVersion: v1
kind: Pod
metadata:
  name: probe
spec:
  nodeName: {client}
  containers:
  - name: probe
    image: {image}
    imagePullPolicy: Never
    command: ["/bin/sleep", "3600"]
{store}
"""


# A claim written the way a chart writes one: no `storageClassName`, so
# it binds only if the cluster has a default class.  That is what
# `services.vivarium-k8s.persistentVolumes` installs, and the thing most charts
# assume a cluster has.
#
# Two pods, one after the other, because a volume that does not keep what
# was written to it would pass a single-pod test: the first writes and
# exits, the second reads.  The claim is bound to one node by then, so the
# scheduler puts the second pod where the directory is.
STORAGE = """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: scratch
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: {pod}
spec:
  restartPolicy: Never
  containers:
  - name: {pod}
    image: {image}
    imagePullPolicy: Never
    command: ["/bin/sh", "-c", "{command}"]
    volumeMounts:
    - name: scratch
      mountPath: /scratch
    - name: nix-store
      mountPath: /nix/store
      readOnly: true
  volumes:
  - name: scratch
    persistentVolumeClaim:
      claimName: scratch
  - name: nix-store
    hostPath:
      path: /nix/store
      type: Directory
"""


async def check_storage(cp: Machine, image: str) -> None:
    """A PersistentVolumeClaim that binds, and keeps what a pod wrote."""
    token = "kept-across-pods"

    async def ran(pod: str) -> None:
        async def check():
            out = await kubectl(
                cp, f"get pod {pod} --output jsonpath='{{.status.phase}}'"
            )
            return out.strip() == "Succeeded", out.strip() or "(no phase yet)"

        await until(f"the pod {pod}", check, READY_TIMEOUT, cp)

    for pod, command in (
        ("writer", f"echo {token} > /scratch/kept"),
        ("reader", "cat /scratch/kept"),
    ):
        manifest = STORAGE.format(pod=pod, image=image, command=command)
        await cp.succeed(
            f"cat <<'EOF' | kubectl apply --filename -\n{manifest}\nEOF",
            timeout=180,
        )
        await ran(pod)

    bound = await kubectl(
        cp, "get pvc scratch --output jsonpath='{.status.phase}'"
    )
    assert bound.strip() == "Bound", f"the claim is {bound.strip()!r}"
    logs = await kubectl(cp, "logs reader")
    assert token in logs, f"the second pod read {logs!r}"
    print("[k8s] a claim bound, and a second pod read what the first wrote", flush=True)


async def check_cluster_networking(cp: Machine, vms: Machines, image: str) -> None:
    """A pod on one worker, reached through a Service from the other.

    Deliberately the long way round: name resolution through CoreDNS, a
    ClusterIP translated by kube-proxy, and a packet that has to leave
    one node's CNI bridge and arrive on another's.
    """
    greeting = "hello-from-the-other-node"
    workers = [name for name in vms if name != cp.name]
    manifest = MANIFEST.format(
        image=image,
        server=workers[0],
        client=workers[1],
        greeting=greeting,
        store=STORE_VOLUME,
    )
    # nodeName rather than a nodeSelector: the point is to put the two
    # pods on different nodes, and asking the scheduler to do it leaves
    # the test dependent on how it feels about a two-worker cluster.
    await cp.succeed(
        f"cat <<'EOF' | kubectl apply --filename -\n{manifest}\nEOF",
        timeout=180,
    )

    for pod in ("web", "probe"):
        await wait_for_pods(
            cp, f"--field-selector metadata.name={pod}", namespace="default"
        )

    async def check():
        rc, out = await cp.execute(
            "kubectl exec probe -- wget -T 10 -qO-"
            " http://web.default.svc.cluster.local/",
            timeout=120,
        )
        return rc == 0 and greeting in out, out

    body = await until("the Service to answer", check, READY_TIMEOUT, cp)
    print(
        f"[k8s] probe on {workers[1]} reached web on {workers[0]}: {body.strip()}",
        flush=True,
    )


async def test(vms: Machines) -> None:
    image = vms.settings["workloadImage"]
    print(
        f"[k8s] kubernetes {vms.settings['kubernetesVersion']}, "
        f"pods on {vms.settings['podSubnet']}",
        flush=True,
    )

    cp = await bring_up(vms)

    await check_cluster_networking(cp, vms, image)
    await check_storage(cp, image)

    print("[k8s] " + await kubectl(cp, "get nodes --output wide"), flush=True)

