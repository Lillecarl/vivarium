import json
from pathlib import Path

import pytest

from vivarium.spec import Spec, SpecError


def write(tmp_path: Path, **fields: object) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"machines": [], **fields}))
    return path


class TestAFieldThisRunnerDoesNotKnow:
    def test_is_refused_by_name(self, tmp_path: Path):
        """Measured on nixkube: an older runner dropped `pythonPath`
        silently, and a phase failed on the import it existed for."""
        path = write(tmp_path, vivarium="/nix/store/x-vivarium", fromTheFuture=[1])
        with pytest.raises(SpecError, match="fromTheFuture.*/nix/store/x-vivarium"):
            Spec.read(path)

    def test_inside_a_phase_too(self, tmp_path: Path):
        path = write(tmp_path, phases=[{"name": "a", "script": "/x.py", "retries": 3}])
        with pytest.raises(SpecError, match=r"phases\.0\.retries"):
            Spec.read(path)

    def test_a_known_spec_reads(self, tmp_path: Path):
        spec = Spec.read(write(tmp_path, vivarium="/nix/store/x-vivarium", pythonPath=["/lib"]))
        assert spec.vivarium == Path("/nix/store/x-vivarium")
        assert spec.pythonPath == [Path("/lib")]
