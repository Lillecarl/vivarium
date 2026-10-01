"""Text on a guest's screen, with where it is.

tesseract reads a screenshot as TSV: one row per word, with its box. The
parsing here is pure, so it is tested without a guest. The boxes are what
make ``click_text`` possible: a test names what it sees and the pointer
goes there.

The preprocessing is nixos-test's (``test_driver/machine/ocr.py``):
resampled to 300 dpi, grayscale, posterized, and once more negated. A
light-on-dark terminal and a dark-on-light dialog each read best in one
of the two, so a match in any variant counts.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["OcrError", "Text", "Word", "Screen", "parse_tsv", "ppm_size", "read"]


class OcrError(Exception):
    """tesseract or magick failed, or the screenshot is not a PPM."""


@dataclass(frozen=True)
class Word:
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float
    line: tuple[int, int, int]
    """(block, paragraph, line): words sharing it are one line."""


@dataclass(frozen=True)
class Text:
    """A match on the screen: what was read, and where, in screen pixels."""

    text: str
    left: int
    top: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2


@dataclass(frozen=True)
class Screen:
    """One reading of a screenshot."""

    words: tuple[Word, ...]

    def lines(self) -> list[tuple[str, list[tuple[int, int, Word]]]]:
        """Each line's text, and each word's span in it."""
        grouped: dict[tuple[int, int, int], list[Word]] = {}
        for word in self.words:
            grouped.setdefault(word.line, []).append(word)
        out = []
        for key in sorted(grouped):
            text, spans = "", []
            for word in sorted(grouped[key], key=lambda w: w.left):
                if text:
                    text += " "
                spans.append((len(text), len(text) + len(word.text), word))
                text += word.text
            out.append((text, spans))
        return out

    @property
    def text(self) -> str:
        return "\n".join(text for text, _ in self.lines())

    def find(self, pattern: str | re.Pattern[str]) -> list[Text]:
        """Every match of *pattern* within one line, boxed.

        A match spanning lines is not found: tesseract's line breaks follow
        the layout, and a box around two lines is nowhere to click.
        """
        regex = re.compile(pattern)
        found = []
        for text, spans in self.lines():
            for match in regex.finditer(text):
                hit = [w for start, end, w in spans if start < match.end() and end > match.start()]
                if not hit:
                    continue
                left = min(w.left for w in hit)
                top = min(w.top for w in hit)
                right = max(w.left + w.width for w in hit)
                bottom = max(w.top + w.height for w in hit)
                found.append(Text(match.group(0), left, top, right - left, bottom - top))
        return found


def parse_tsv(tsv: str, scale: float = 1.0) -> Screen:
    """tesseract's TSV as words, boxes multiplied by *scale*.

    *scale* undoes a resample: boxes come back in the pixels tesseract
    read, and a click needs the pixels of the screen.
    """
    rows = tsv.splitlines()
    if not rows or not rows[0].startswith("level"):
        raise OcrError(f"not tesseract TSV: {tsv[:80]!r}")
    words = []
    for row in rows[1:]:
        cells = row.split("\t")
        # A word row is level 5 and has the text as its twelfth cell.
        if len(cells) < 12 or cells[0] != "5" or not cells[11].strip():
            continue
        block, par, line = int(cells[2]), int(cells[3]), int(cells[4])
        left, top, width, height = (int(c) for c in cells[6:10])
        words.append(
            Word(
                text=cells[11].strip(),
                left=round(left * scale),
                top=round(top * scale),
                width=round(width * scale),
                height=round(height * scale),
                confidence=float(cells[10]),
                line=(block, par, line),
            )
        )
    return Screen(tuple(words))


def ppm_size(data: bytes) -> tuple[int, int]:
    """Width and height from a binary PNM header (P5 or P6), as QEMU and
    magick write it: no comments."""
    fields = data[:64].split(maxsplit=3)
    if len(fields) < 3 or fields[0] not in (b"P5", b"P6"):
        raise OcrError(f"not a binary PNM: {data[:16]!r}")
    return int(fields[1]), int(fields[2])


async def _run(*argv: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise OcrError(f"{Path(argv[0]).name} exited {proc.returncode}: {err.decode(errors='replace')[-500:]}")
    return out


# nixos-test's, measured there.
_PREPROCESS = (
    "-filter", "Catrom", "-density", "72", "-resample", "300",
    "-contrast", "-normalize", "-despeckle", "-type", "grayscale",
    "-sharpen", "1", "-posterize", "3",
)  # fmt: skip
_BLUR = ("-gamma", "100", "-blur", "1x65535")


async def _tesseract(tesseract: Path, image: Path, scale: float) -> Screen:
    # --psm 11: sparse text, no assumed page layout; a screen is not a page.
    out = await _run(
        str(tesseract), str(image), "-", "--oem", "2", "--psm", "11",
        "-c", "debug_file=/dev/null", "tsv",
    )  # fmt: skip
    return parse_tsv(out.decode(errors="replace"), scale)


async def _variant(tesseract: Path, magick: Path, ppm: Path, negate: bool, width: int) -> Screen:
    out = ppm.with_name(f"{ppm.stem}.{'negative' if negate else 'positive'}.pgm")
    await _run(str(magick), str(ppm), *_PREPROCESS, *(("-negate",) if negate else ()), *_BLUR, str(out))
    try:
        processed, _ = ppm_size(out.read_bytes())
        return await _tesseract(tesseract, out, width / processed)
    finally:
        out.unlink(missing_ok=True)


async def read(ppm: Path, tesseract: Path, magick: Path | None = None) -> list[Screen]:
    """Read *ppm*: as it is, then each preprocessed variant if *magick*.

    PPM and not PNG: nixos-test's `imagemagick_light` has no PNG codec,
    and the full one is a much larger closure for one format.
    """
    width, _ = ppm_size(ppm.read_bytes()[:64])
    jobs = [_tesseract(tesseract, ppm, 1.0)]
    if magick is not None:
        jobs += [_variant(tesseract, magick, ppm, negate, width) for negate in (False, True)]
    return list(await asyncio.gather(*jobs))
