"""Run NixOS systems under User-Mode Linux and drive them from Python.

An alternative to ``nixosTest`` that needs no KVM and no privileges: the
guest kernel is an ordinary user-space process, its uplink is passt, and
the host talks to it over a serial line rather than a QEMU monitor.

Tests are written against :func:`run_test`; see ``tests/`` in this repo.
"""

from .forward import ForwardError
from .harness import Machines, load_spec, machines, run_test
from .machine import Machine, MachineError, MachineSpec, Toolchain

__all__ = [
    "ForwardError",
    "Machine",
    "MachineError",
    "MachineSpec",
    "Machines",
    "Toolchain",
    "load_spec",
    "machines",
    "run_test",
]
