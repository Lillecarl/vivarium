"""A guest's store view: `/nix/store` holding only its closure.

A tmpfs with one read-only bind per path, made read-only as a whole in
one `mount_setattr` call. hostfs and virtiofsd walk paths, so they cross
the binds and a guest sees one filesystem; the guest puts its writable
overlay on top inside its own kernel.

Needs root of a mount namespace: `uml run` enters one first
(`uml/namespace.py`). Direct syscalls, because a `mount` process per path
costs about 6 ms (510 paths in 3.0 s, measured with util-linux).

Not `mount -o remount,ro` on the tmpfs: a remount re-parses its options,
and the `uid=` in them is not mapped in the namespace ("tmpfs: Invalid
uid"). `mount_setattr` changes the flag only.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Iterable
from pathlib import Path

_libc = ctypes.CDLL(None, use_errno=True)

_MS_BIND = 4096
_MNT_DETACH = 2
_AT_FDCWD = -100
_AT_RECURSIVE = 0x8000
_MOUNT_ATTR_RDONLY = 0x1
_SYS_MOUNT_SETATTR = 442
"""The same number on every architecture: it is newer than the split."""


class _MountAttr(ctypes.Structure):
    _fields_ = [
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    ]


def _check(result: int, what: str) -> None:
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"{what}: {os.strerror(error)}")


def _mount(source: str, target: Path, fstype: str | None, flags: int) -> None:
    _check(
        _libc.mount(
            source.encode(),
            str(target).encode(),
            fstype.encode() if fstype else None,
            flags,
            None,
        ),
        f"mount {source} on {target}",
    )


def _read_only(path: Path) -> None:
    attr = _MountAttr(attr_set=_MOUNT_ATTR_RDONLY)
    _check(
        _libc.syscall(
            _SYS_MOUNT_SETATTR,
            _AT_FDCWD,
            str(path).encode(),
            _AT_RECURSIVE,
            ctypes.byref(attr),
            ctypes.sizeof(attr),
        ),
        f"mount_setattr read-only on {path}",
    )


def read_paths(store_paths: Path) -> list[str]:
    """A closureInfo's `store-paths`: one absolute path per line."""
    return [line for line in store_paths.read_text().splitlines() if line]


def build(root: Path, paths: Iterable[str]) -> Path:
    """Make *root*/store the view of *paths*; return *root*.

    *root* is what a backend serves as the guest's `/nix`.
    """
    store = root / "store"
    store.mkdir(parents=True)
    _mount("tmpfs", store, "tmpfs", 0)
    for path in paths:
        source = Path(path)
        target = store / source.name
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
            continue
        if source.is_dir():
            target.mkdir()
        else:
            target.touch()
        _mount(path, target, None, _MS_BIND)
    _read_only(store)
    return root


def remove(root: Path) -> None:
    """Detach the view: the tmpfs and every bind under it, in one call."""
    store = root / "store"
    if os.path.ismount(store):
        _check(_libc.umount2(str(store).encode(), _MNT_DETACH), f"umount {store}")
