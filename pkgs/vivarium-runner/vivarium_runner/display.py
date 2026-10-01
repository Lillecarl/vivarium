"""Keys and pointer events for a guest's screen, as QEMU takes them.

Pure: what to send, never the sending. :class:`Machine` sends it over
the monitor.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Button", "ABS_MAX", "key_for", "move_events", "button_events"]

ABS_MAX = 0x7FFF
"""QEMU's absolute axis runs 0 to this whatever the screen's size
(`INPUT_EVENT_ABS_MAX`); the tablet scales it to the guest's."""


class Button(StrEnum):
    LEFT = "left"
    MIDDLE = "middle"
    RIGHT = "right"
    WHEEL_UP = "wheel-up"
    WHEEL_DOWN = "wheel-down"


# nixos-test's CHAR_TO_KEY: `sendkey` names for what a US layout types
# with shift or with a scancode. Lowercase letters and digits are their
# own names.
CHAR_TO_KEY = {
    **{c.upper(): f"shift-{c}" for c in "abcdefghijklmnopqrstuvwxyz"},
    "-": "0x0C", "_": "shift-0x0C", "=": "0x0D", "+": "shift-0x0D",
    "[": "0x1A", "{": "shift-0x1A", "]": "0x1B", "}": "shift-0x1B",
    ";": "0x27", ":": "shift-0x27", "'": "0x28", '"': "shift-0x28",
    "`": "0x29", "~": "shift-0x29", "\\": "0x2B", "|": "shift-0x2B",
    ",": "0x33", "<": "shift-0x33", ".": "0x34", ">": "shift-0x34",
    "/": "0x35", "?": "shift-0x35", " ": "spc", "\n": "ret", "\t": "tab",
    "!": "shift-0x02", "@": "shift-0x03", "#": "shift-0x04", "$": "shift-0x05",
    "%": "shift-0x06", "^": "shift-0x07", "&": "shift-0x08", "*": "shift-0x09",
    "(": "shift-0x0A", ")": "shift-0x0B",
}  # fmt: skip


def key_for(char: str) -> str:
    """The ``sendkey`` name that types *char* on a US layout."""
    if char in CHAR_TO_KEY:
        return CHAR_TO_KEY[char]
    if len(char) == 1 and (char.islower() or char.isdigit()) and char.isascii():
        return char
    raise ValueError(f"no key types {char!r} on a US layout; send it with send_key")


def _axis(value: int, size: int) -> int:
    if not 0 <= value < size:
        raise ValueError(f"{value} is off a screen {size} pixels across")
    return round(value * ABS_MAX / max(size - 1, 1))


def move_events(x: int, y: int, width: int, height: int) -> list[dict]:
    """``input-send-event`` events that put the pointer on pixel (x, y)."""
    return [
        {"type": "abs", "data": {"axis": "x", "value": _axis(x, width)}},
        {"type": "abs", "data": {"axis": "y", "value": _axis(y, height)}},
    ]


def button_events(button: Button, down: bool) -> list[dict]:
    return [{"type": "btn", "data": {"button": str(button), "down": down}}]
