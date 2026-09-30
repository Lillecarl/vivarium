"""A guest's journal, followed while the guest runs.

Each guest's `uml-journal` unit writes `journalctl --follow --output=json`
into `/artifacts/journal.jsonl`. hostfs and virtiofs are write-through
(measured, `default.nix` `incr`), so the file on the host grows entry by
entry, and whatever reached it survives the guest being killed.

Not over the agent's RPC channel, although it could carry it. A line in
flight over RPC lives in a Python process on each end, and either one
dying loses it; a line in the file is on the host's disk already. The
agent also answers one request at a time, so a stream there would stop
for as long as any command ran.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import anyio

from .events import Level

if TYPE_CHECKING:
    from pathlib import Path

FILE: Final = "journal.jsonl"
"""The name in each guest's `/artifacts`, and so in `artifacts/<name>/`."""

SETTLE: Final = "uml-settle"
"""The identifier of the marker `Session.settle` logs and waits for. Its
entries are the runner's own bookkeeping and never become events."""


@dataclass(frozen=True)
class Entry:
    message: str
    priority: int
    unit: str | None = None
    identifier: str | None = None
    pid: int | None = None

    def data(self) -> dict[str, object]:
        """The fields a reader filters on, without the absent ones."""
        fields: dict[str, object] = {"priority": self.priority}
        if self.unit is not None:
            fields["unit"] = self.unit
        if self.identifier is not None:
            fields["identifier"] = self.identifier
        if self.pid is not None:
            fields["pid"] = self.pid
        return fields


def _text(value: object) -> str | None:
    # journald writes a value that is not valid UTF-8 as a list of byte
    # values, and a field set twice in one entry as a list of strings.
    if value is None:
        return None
    if isinstance(value, list):
        if all(isinstance(item, int) for item in value):
            return bytes(value).decode("utf-8", "replace")
        return " ".join(str(item) for item in value)
    return str(value)


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except ValueError:
        return None


def parse(line: str) -> Entry | None:
    """One line of `journalctl --output=json`, or None if it is not one."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    priority = _int(raw.get("PRIORITY"))
    return Entry(
        message=_text(raw.get("MESSAGE")) or "",
        # 6 is journald's own default for an entry that names none.
        priority=6 if priority is None else priority,
        unit=_text(raw.get("_SYSTEMD_UNIT")),
        identifier=_text(raw.get("SYSLOG_IDENTIFIER")),
        pid=_int(raw.get("_PID")),
    )


def level(entry: Entry) -> Level:
    """`err` and worse is shown at `-v`; the rest sits with the console."""
    return Level.DETAIL if entry.priority <= 3 else Level.CONSOLE


def split(buffer: bytes) -> tuple[list[str], bytes]:
    """The complete lines in *buffer*, and the partial one after them.

    A read can end in the middle of an entry, because the file is
    growing while it is read. The tail waits for the next read.
    """
    *lines, rest = buffer.split(b"\n")
    return [line.decode("utf-8", "replace") for line in lines if line], rest


class Tail:
    """The new complete lines of one file, each time it is asked."""

    def __init__(self, path: Path) -> None:
        self.path = anyio.Path(path)
        self._offset = 0
        self._rest = b""

    @property
    def streaming(self) -> bool:
        """Has anything arrived? A guest with `boot.uml.journal` off never
        writes the file, and waiting on it would wait for nothing."""
        return self._offset > 0

    async def read(self) -> list[str]:
        try:
            size = (await self.path.stat()).st_size
        except FileNotFoundError:
            return []
        if size < self._offset:
            # `truncate:` in the unit: the guest started the stream again.
            self._offset, self._rest = 0, b""
        if size == self._offset:
            return []
        async with await self.path.open("rb") as handle:
            await handle.seek(self._offset)
            data = await handle.read(size - self._offset)
        self._offset += len(data)
        lines, self._rest = split(self._rest + data)
        return lines
