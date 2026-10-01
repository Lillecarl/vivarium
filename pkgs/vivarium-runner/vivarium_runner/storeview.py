"""A guest's store view: `/nix/store` holding only its closure.

A tmpfs with one read-only bind per path, made read-only as a whole in
one `mount_setattr` call. hostfs and virtiofsd walk paths, so they cross
the binds and a guest sees one filesystem; the guest puts its writable
overlay on top inside its own kernel. A container guest gets the
writable form instead; see `build`.

Needs root of a mount namespace: `vivarium run` enters one first
(`vivarium/namespace.py`). Direct syscalls, because a `mount` process per path
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
_AT_EMPTY_PATH = 0x1000
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


def _set_read_only(path: Path, read_only: bool, *, recursive: bool) -> None:
    attr = (
        _MountAttr(attr_set=_MOUNT_ATTR_RDONLY)
        if read_only
        else _MountAttr(attr_clr=_MOUNT_ATTR_RDONLY)
    )
    _check(
        _libc.syscall(
            _SYS_MOUNT_SETATTR,
            _AT_FDCWD,
            str(path).encode(),
            _AT_RECURSIVE if recursive else 0,
            ctypes.byref(attr),
            ctypes.sizeof(attr),
        ),
        f"mount_setattr {'read-only' if read_only else 'read-write'} on {path}",
    )


def _read_only(path: Path, *, recursive: bool) -> None:
    _set_read_only(path, True, recursive=recursive)


def read_only_tree(fd: int, what: str) -> None:
    """Make the detached mount *fd*, from `open_tree`, read-only."""
    attr = _MountAttr(attr_set=_MOUNT_ATTR_RDONLY)
    _check(
        _libc.syscall(_SYS_MOUNT_SETATTR, fd, b"", _AT_EMPTY_PATH, ctypes.byref(attr), ctypes.sizeof(attr)),
        f"mount_setattr read-only on {what}",
    )


def _mount_point(store: Path, path: str) -> bool:
    """Where *path* goes in the view; False for a symlink, made whole."""
    source = Path(path)
    target = store / source.name
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
        return False
    if source.is_dir():
        target.mkdir()
    else:
        target.touch()
    return True


def _bind_path(store: Path, path: str, *, read_only: bool) -> None:
    """One store path into the view, as a bind or a symlink."""
    if not _mount_point(store, path):
        return
    target = store / Path(path).name
    _mount(path, target, None, _MS_BIND)
    if read_only:
        _read_only(target, recursive=False)


def read_paths(store_paths: Path) -> list[str]:
    """A closureInfo's `store-paths`: one absolute path per line."""
    return [line for line in store_paths.read_text().splitlines() if line]


def build(root: Path, paths: Iterable[str], *, writable: bool = False) -> Path:
    """Make *root*/store the view of *paths*; return *root*.

    *root* is what a backend serves as the guest's `/nix`.

    Read-only: a tmpfs, read-only as a whole; a VM guest overlays it in
    its own kernel. Writable, for a container guest, whose overlay would
    be on the host and see none of the binds: nixkube's layout. A
    directory on disk holds the binds, each read-only, and new paths go
    beside them. It is bound onto itself, so one detach still removes it.
    """
    store = root / "store"
    store.mkdir(parents=True)
    if writable:
        _mount(str(store), store, None, _MS_BIND)
        store.chmod(0o1775)
    else:
        _mount("tmpfs", store, "tmpfs", 0)
    for path in paths:
        _bind_path(store, path, read_only=writable)
    if not writable:
        _read_only(store, recursive=True)
    return root


def add(root: Path, paths: Iterable[str], *, writable: bool = False) -> list[str]:
    """Put *paths* the view lacks into a running guest's view; return them.

    A guest that looked a name up before it existed keeps a negative
    dentry for it in its overlay, and then misses the new path until its
    dentry cache is dropped (measured under UML).

    Writable, for a container, only the mount points are made here: a
    bind in this namespace never reaches the guest's. crun clones each
    bind with `open_tree` and makes it private where the guest shares the
    runner's user namespace, so no propagation setting helps (measured).
    `container.attach` mounts them from inside.
    """
    store = root / "store"
    missing = [path for path in paths if not os.path.lexists(store / Path(path).name)]
    if not missing:
        return []
    if writable:
        for path in missing:
            _mount_point(store, path)
        return missing
    _set_read_only(store, False, recursive=False)
    try:
        for path in missing:
            _bind_path(store, path, read_only=True)
    finally:
        _read_only(store, recursive=False)
    return missing


def remove(root: Path) -> None:
    """Detach the view: the tmpfs and every bind under it, in one call."""
    store = root / "store"
    if os.path.ismount(store):
        _check(_libc.umount2(str(store).encode(), _MNT_DETACH), f"umount {store}")
