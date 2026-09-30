from pathlib import Path

from vivarium.session import with_kernel

MINE = Path("/home/me/linux/linux")


class TestWithKernel:
    def test_uml_takes_it_from_the_toolchain(self):
        tools, machines = with_kernel(
            {"kernel": "/nix/store/k-linux", "passt": "/p"}, [{"name": "one"}], MINE
        )
        assert tools == {"kernel": str(MINE), "passt": "/p"}
        assert machines == [{"name": "one"}]

    def test_qemu_takes_it_from_each_machine(self):
        tools, machines = with_kernel(
            {"qemu": "/q"},
            [{"name": "a", "boot": {"kernel": "/nix/store/b", "initrd": "/i"}}, {"name": "b", "boot": {"kernel": "/x"}}],
            MINE,
        )
        assert tools == {"qemu": "/q"}, "a QEMU toolchain has no kernel to swap"
        assert [m["boot"]["kernel"] for m in machines] == [str(MINE), str(MINE)]
        assert machines[0]["boot"]["initrd"] == "/i"

    def test_the_spec_is_not_changed(self):
        machines = [{"name": "a", "boot": {"kernel": "/nix/store/b"}}]
        with_kernel({}, machines, MINE)
        assert machines[0]["boot"]["kernel"] == "/nix/store/b"
