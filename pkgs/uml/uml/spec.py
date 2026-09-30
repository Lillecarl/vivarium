"""What Nix hands the runner, as a validated model.

A free-form dict is what the old spec was, and every field it carried had
to be read defensively at the point of use.  A model says the shape once,
rejects a malformed spec with the field named, and gives pyright
something to check a caller against.

Nothing here decides anything.  The spec is **input**: the runner and the
recipes are built once and do not move when it does, so a value in here
never reaches an image, a database or a derivation the guests depend on.
See `docs/design/history/running-anywhere.md`, area 0a.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class SpecError(ValueError):
    """The spec cannot be run by this runner."""


class Strict(BaseModel):
    """A field this runner does not know is an error, not something to drop.

    Measured on nixkube: a spec from a newer `lib.nix` carried
    `pythonPath`, an older runner ignored the field, and a phase then
    failed on an import that the field existed to make work. Nothing
    said the two were out of step.
    """

    model_config = ConfigDict(extra="forbid")


class PytestSpec(Strict):
    """A pytest run as a phase. See `uml/pytest_plugin.py`."""

    tests: Path
    """A test file or a directory of them, `conftest.py` included."""

    args: list[str] = Field(default_factory=list)
    """Given to pytest after the runner's own. `-k`, `-x`, `-m`."""


class PhaseSpec(Strict):
    """One unit of work, declared in Nix and ordered there.

    The list arrives sorted: `lib.toposort` runs during evaluation, so a
    cycle is an evaluation error and never reaches this.  `after` is kept
    anyway, because it is what says which phases a failure takes with it.

    Exactly one of `script` and `pytest`. Nix asserts it too; this is
    for a spec that did not come from Nix.
    """

    name: str
    script: Path | None = None
    pytest: PytestSpec | None = None
    after: list[str] = Field(default_factory=list)

    nodes: list[str] = Field(default_factory=list)
    """The guests this phase uses; empty is every guest. Two phases whose
    guests do not overlap run at the same time. See `phases.launchable`."""

    @model_validator(mode="after")
    def _one_kind(self) -> PhaseSpec:
        if (self.script is None) == (self.pytest is None):
            raise ValueError(f"phase {self.name} needs exactly one of script and pytest")
        return self

    always: bool = False
    """Run even when something in `after` failed.

    `after` normally means two things at once: run me later, and do not
    bother if that failed. A phase that collects evidence wants only the
    first -- a journal is most wanted on the run where something broke,
    and a journal phase ordered after everything would otherwise be
    skipped by the very failure it exists to explain.

    Nothing is expanded *through* one of these either: a phase after the
    journal is not skipped because a phase before the journal failed.
    """


class Knob(Strict):
    """One declared steer, already resolved.

    Nix resolves it, because Nix is where it can change what is *built* —
    a phase order, a guest's memory, a different image — which no amount
    of reading the environment at run time can do.

    `source` is carried rather than inferred. A knob set to the same text
    as its default would otherwise read as "default", and the whole
    reason this is printed is to make a misspelled variable visible.
    """

    value: str
    source: str
    env: str


class Spec(Strict):
    """A whole run, as evaluating the module system produced it."""

    uml: Path | None = None
    """The `uml` package that wrote this spec's library, whose runner is
    the one that knows every field in it. A front door that did not build
    the spec -- `uml-mcp` given a spec path -- runs this one rather than
    its own."""

    name: str = "uml"
    """What the run is called. Nix knows it, so nothing has to guess it
    from a store path -- a JUnit suite named after a hash is a suite
    whose name changes every time anything changes."""

    machines: list[dict]
    """Passed to `uml_runner.MachineSpec.from_json` untouched.  Modelling
    it twice would be two places to change when a backend gains a flag."""

    phases: list[PhaseSpec] = Field(default_factory=list)
    settings: dict = Field(default_factory=dict)

    pythonPath: list[Path] = Field(default_factory=list)  # noqa: N815 -- Nix writes it, so Nix spells it
    """Directories every phase script can import from."""
    knobs: dict[str, Knob] = Field(default_factory=dict)

    kernel: Path | None = None
    bridge: Path | None = None
    passt: Path | None = None
    qemu: Path | None = None
    qemuImg: Path | None = None  # noqa: N815 -- Nix writes it, so Nix spells it
    virtiofsd: Path | None = None
    crun: Path | None = None
    setpriv: Path | None = None
    unshare: Path | None = None
    """util-linux's, which puts `uml run` in its own namespace. See
    `uml/namespace.py`."""

    @classmethod
    def read(cls, path: Path) -> Spec:
        raw = json.loads(path.read_text())
        try:
            return cls.model_validate(raw)
        except ValidationError as error:
            unknown = [
                ".".join(str(part) for part in item["loc"])
                for item in error.errors()
                if item["type"] == "extra_forbidden"
            ]
            if not unknown:
                raise
            raise SpecError(
                f"the spec has fields this runner does not know: {', '.join(unknown)}."
                " A newer user-mode-nixos wrote it; run it with the `uml` it"
                f" names, {raw.get('uml') or '(none named)'}"
            ) from None

    def toolchain(self) -> dict:
        """The toolchain fields, as `uml_runner.Toolchain` wants them.

        Only what this run's backend needs is set; naming a store path is
        what makes Nix build it, so a UML run carries no QEMU and a QEMU
        run carries no kernel.
        """
        named = (
            "kernel",
            "bridge",
            "passt",
            "qemu",
            "qemuImg",
            "virtiofsd",
            "crun",
            "setpriv",
        )
        return {
            key: str(value)
            for key in named
            if (value := getattr(self, key)) is not None
        }
