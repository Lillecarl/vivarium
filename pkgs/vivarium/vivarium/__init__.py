"""Drive a run of NixOS guests a step at a time.

A session boots the guests, runs phases against them, and tears them
down when it is told to -- never on its own. `vivarium run` is one drive of
one session; an MCP server is the same object driven slowly.

The mechanism underneath is `vivarium_runner`. This package owns the
sequence.
"""

from .phases import PhaseState, dependents, passed, runnable, skipped_by, summarise
from .session import Session, SessionError, load_phase
from .spec import Knob, PhaseSpec, Spec

__all__ = [
    "Knob",
    "PhaseSpec",
    "PhaseState",
    "Session",
    "SessionError",
    "Spec",
    "dependents",
    "load_phase",
    "passed",
    "runnable",
    "skipped_by",
    "summarise",
]
