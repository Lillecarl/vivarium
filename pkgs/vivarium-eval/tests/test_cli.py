from pathlib import Path

import pytest

from vivarium_eval.cli import entry, parse, split_attr


class TestParse:
    def test_the_rest_goes_to_uml_untouched(self):
        request = parse(["run", "lan.qemu", "--out", "o", "-v", "--", "-k", "x"])
        assert request.command == "run"
        assert request.attr == ["lan", "qemu"]
        assert request.file == Path(".")
        # `--` survives: `uml` needs it to find pytest's arguments.
        assert request.rest == ["--out", "o", "-v", "--", "-k", "x"]

    def test_file_is_ours(self):
        request = parse(["phases", "recipes", "--file", "/src", "--out", "o"])
        assert request.file == Path("/src")
        assert request.rest == ["--out", "o"]

    def test_an_option_after_dashdash_is_pytests_not_ours(self):
        request = parse(["run", "x", "--", "--file", "t.py"])
        assert request.file == Path(".")
        assert request.rest == ["--", "--file", "t.py"]


class TestAttr:
    def test_a_dotted_path(self):
        assert split_attr("lan.qemu") == ["lan", "qemu"]

    @pytest.mark.parametrize("text", ["", ".lan", "lan.", "lan..qemu"])
    def test_an_empty_part_is_refused(self, text: str):
        with pytest.raises(ValueError):
            split_attr(text)


class TestEntry:
    def test_a_directory_means_its_default_nix(self, tmp_path: Path):
        assert entry(tmp_path) == tmp_path / "default.nix"

    def test_a_file_is_itself(self, tmp_path: Path):
        path = tmp_path / "x.nix"
        path.write_text("{}")
        assert entry(path) == path
