"""The container backend's config and id parsing, without a container.

The boot itself is `nix build --file . container`'s job, by hand; this is
what a mistake in the pure half looks like before anything runs.
"""

from pathlib import Path

from vivarium_runner.backend import Container
from vivarium_runner.container import (
    AGENT_DIR,
    CAPABILITIES,
    SUBORDINATE_IDS,
    Range,
    oci_config,
    subordinate,
)


def config(**changes):
    args = dict(
        hostname="one",
        init="/nix/store/t-nixos-system-one/init",
        setpriv="/nix/store/u-util-linux/bin/setpriv",
        rootfs=Path("/run/r/root"),
        store="/nix",
        agent_dir=Path("/run/r/agent"),
        artifacts=Path("/out/artifacts/one"),
        uid=1000,
        gid=100,
        subuid=Range(100000, 65536),
        subgid=Range(200000, 65536),
    )
    return oci_config(**(args | changes))


def destinations(cfg) -> list[str]:
    return [mount["destination"] for mount in cfg["mounts"]]


class TestOciConfig:
    def test_init_dies_with_crun(self):
        """Without the pdeathsig, a SIGKILLed crun left the guest running."""
        args = config()["process"]["args"]
        assert args == [
            "/nix/store/u-util-linux/bin/setpriv",
            "--pdeathsig",
            "KILL",
            "--",
            "/nix/store/t-nixos-system-one/init",
        ]

    def test_a_console_for_systemd(self):
        assert config()["process"]["terminal"] is True

    def test_root_and_the_range_are_mapped(self):
        linux = config()["linux"]
        assert linux["uidMappings"] == [
            {"containerID": 0, "hostID": 1000, "size": 1},
            {"containerID": 1, "hostID": 100000, "size": SUBORDINATE_IDS},
        ]
        assert linux["gidMappings"][0]["hostID"] == 100
        assert linux["gidMappings"][1]["hostID"] == 200000

    def test_every_capability_in_its_own_namespace(self):
        """An empty set fails every `User=` service at the GROUP step."""
        caps = config()["process"]["capabilities"]
        assert caps["bounding"] == caps["effective"] == caps["permitted"] == CAPABILITIES
        assert "CAP_SETGID" in CAPABILITIES and len(CAPABILITIES) == 41

    def test_its_own_namespaces(self):
        kinds = {ns["type"] for ns in config()["linux"]["namespaces"]}
        assert kinds == {"pid", "ipc", "uts", "mount", "cgroup", "network", "user"}

    def test_the_agent_lands_on_the_run_tmpfs(self):
        """crun mounts in order: a bind before the tmpfs would be hidden."""
        order = destinations(config())
        assert order.index(AGENT_DIR) > order.index("/run")

    def test_the_store_is_an_overlay_over_the_hosts(self):
        """Writes stay in the run. Not all of /nix: unprivileged, that
        lower fails where the host's store is a mount of its own."""
        nix = [m for m in config()["mounts"] if m["destination"].startswith("/nix")]
        assert [m["destination"] for m in nix] == ["/nix/store"]
        assert nix[0]["type"] == "overlay"
        assert "lowerdir=/nix/store" in nix[0]["options"]
        assert "upperdir=/run/r/root/.nix-upper" in nix[0]["options"]
        assert "userxattr" in nix[0]["options"]

    def test_sysfs_is_read_only(self):
        """Writable, it starts udevd, which gets no uevents in a user
        namespace, and networkd then never configures vec0."""
        sysfs = next(m for m in config()["mounts"] if m["destination"] == "/sys")
        assert "ro" in sysfs["options"]

    def test_the_cgroup_is_writable(self):
        cgroup = next(m for m in config()["mounts"] if m["destination"] == "/sys/fs/cgroup")
        assert "rw" in cgroup["options"]

    def test_in_the_sandbox_the_guest_shares_the_builds_ids(self):
        """A uid-range build is root with 65536 ids and no /etc/subuid:
        no user namespace of the guest's own, and no mappings to write."""
        linux = config(subuid=None, subgid=None)["linux"]
        assert "user" not in {ns["type"] for ns in linux["namespaces"]}
        assert "uidMappings" not in linux and "gidMappings" not in linux

    def test_a_store_of_binds_is_bound_read_only(self):
        """An overlay lower does not show submounts, and the sandbox's store
        is one bind per input."""
        store = next(
            m for m in config(writable_store=False)["mounts"] if m["destination"] == "/nix/store"
        )
        assert store["type"] == "bind"
        assert store["options"] == ["rbind", "ro"]

    def test_the_cgroup_is_a_plain_cgroup2_mount(self):
        """OCI's `cgroup` makes crun read the runner's /sys/fs/cgroup."""
        cgroup = next(m for m in config()["mounts"] if m["destination"] == "/sys/fs/cgroup")
        assert cgroup["type"] == "cgroup2"

    def test_artifacts_only_when_given(self):
        assert "/artifacts" in destinations(config())
        assert "/artifacts" not in destinations(config(artifacts=None))


class TestPastaForwards:
    """pasta forwards more than passt by default; a guest must not."""

    def test_no_loopback_splice_and_no_scanned_udp(self):
        args = Container._pasta_forwards(["--tcp-ports", "127.0.0.2/4325"])
        assert args[:2] == ["--tcp-ports", "127.0.0.2/4325"]
        assert args[2:] == ["--tcp-ns", "none", "--udp-ns", "none", "--udp-ports", "none"]

    def test_a_udp_rule_is_kept(self):
        args = Container._pasta_forwards(
            ["--tcp-ports", "127.0.0.2/4325", "--udp-ports", "127.0.0.2/53"]
        )
        assert args.count("--udp-ports") == 1
        assert "none" not in args[args.index("--udp-ports") + 1]


class TestSubordinate:
    def test_by_name_or_by_number(self, tmp_path):
        path = tmp_path / "subuid"
        path.write_text("alice:100000:65536\n1001:300000:65536\n")
        assert subordinate(path, "alice", 1000) == Range(100000, 65536)
        assert subordinate(path, "bob", 1001) == Range(300000, 65536)

    def test_none_for_a_user_with_no_range(self, tmp_path):
        path = tmp_path / "subuid"
        path.write_text("alice:100000:65536\n")
        assert subordinate(path, "bob", 1001) is None

    def test_none_for_no_file(self, tmp_path):
        assert subordinate(tmp_path / "absent", "alice", 1000) is None
