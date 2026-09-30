#!/usr/bin/env python3
"""A cluster built the way a real one is: images pulled, nothing patched.

`tests/k8s.py` builds every image out of nixpkgs and imports it before
kubelet starts, so that a cluster is possible inside a build sandbox. The
price is a node that is not like other nodes: `/nix/store` is mounted into
the static pods, CoreDNS and kube-proxy, and `imagePullPolicy` is `Never`
everywhere. Anything whose job is to put a store into a pod therefore
passes here with its subject switched off.

This asks the other question. `services.vivarium-k8s.images = "pull"` turns all
of that off: kubeadm fetches from `registry.k8s.io`, no patch is applied,
nothing is imported. What comes up is an ordinary kubeadm node.

**Outside the build sandbox only, and it cannot be otherwise** -- a sandbox
has no network, so there is nothing to pull from:

    nix run --file . k8s-pull.driver     # and k8s-pull.uml.driver
"""

from vivarium_runner import Machines
from vivarium_runner.cluster import KUBE_DNS, KUBE_PROXY, bring_up, kubectl, until

# Nothing built it, nothing imported it, and it is not in the guest's Nix
# store: the only way this pod runs is a pull over the network.
WORKLOAD = "registry.k8s.io/e2e-test-images/busybox:1.29-4"

POD = """
apiVersion: v1
kind: Pod
metadata:
  name: pulled
spec:
  restartPolicy: Never
  containers:
    - name: pulled
      image: %s
      command: ["sh", "-c", "echo pulled-ok"]
""" % WORKLOAD


async def test(vms: Machines) -> None:
    cp = await bring_up(vms, nix_images=False, addons=(KUBE_PROXY, KUBE_DNS))

    # The node is stock. Under `images = "nix"` every one of these would
    # carry a nix-store volume, and that is the difference this test is
    # here to keep.
    for kind, name in (
        ("pod", "kube-apiserver-cp"),
        ("pod", "etcd-cp"),
        ("daemonset", "kube-proxy"),
    ):
        out = await kubectl(
            cp,
            f"get {kind} {name} --namespace kube-system"
            " --output jsonpath='{..volumes[*].name}'",
        )
        assert "nix-store" not in out, f"{kind}/{name} still has the store mounted: {out}"
    print("[test] the control plane has no store mounted into it")

    # And the images really came from a registry rather than from a
    # tarball somebody imported.
    images = await cp.succeed("crictl images --output json")
    assert "registry.k8s.io/kube-apiserver" in images, "no upstream apiserver image"
    print("[test] images are the upstream ones")

    await kubectl(cp, f"create --filename - <<'EOF'\n{POD}\nEOF")

    async def ran():
        out = await kubectl(
            cp, "get pod pulled --output jsonpath='{.status.phase}'"
        )
        return out.strip() == "Succeeded", out.strip() or "(no phase yet)"

    await until(f"the pod to pull {WORKLOAD} and run", ran, 300, cp)
    logs = await kubectl(cp, "logs pulled")
    assert "pulled-ok" in logs, f"the pod ran but said {logs!r}"
    print(f"[test] a pod pulled {WORKLOAD} and ran it")

