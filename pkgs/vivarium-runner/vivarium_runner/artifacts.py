"""Taking back the files a guest wrote.

A container guest's service users run as subordinate ids, so a file they
write under ``/artifacts`` lands on the host owned by an id the caller can
neither read nor remove. The run is root of a user namespace whose mapping
is the guest's (``vivarium.namespace``), so :func:`hand_back` chowns the
tree to 0 -- the caller outside -- and every file becomes the caller's.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def hand_back(root: Path) -> None:
    """Chown *root* and everything under it to uid 0.

    A no-op where the files are already the caller's, and best-effort where
    the caller is not root of the run's namespace: a chown it may not make is
    dropped, never raised, so a teardown that calls this cannot fail on it.
    """
    if not root.exists():
        return
    for directory, _, files in os.walk(root):
        for path in (Path(directory), *(Path(directory) / name for name in files)):
            with contextlib.suppress(OSError):
                os.chown(path, 0, 0, follow_symlinks=False)
