"""Progress parsing for tsMuxeR and ffmpeg output.

Both tools are chatty in their own way and neither has a machine-readable progress
stream, so the parsers here are deliberately forgiving: a line we do not recognise
is simply not progress, and the bar keeps whatever value it had.

Captured reality (``tests/data/tools``, ``tests/data/tsmuxer``):

* tsMuxeR writes everything to **stdout** — ``7.0% complete`` lines, an
  informational ``Processed N video frames``, and the success sentinel
  ``Mux successful complete``. On Linux every report after the first one that
  followed a log message is prefixed with ``ESC M`` (reverse index, so a
  terminal overwrites the previous report); the scanners strip control
  sequences before matching;
* ffmpeg is asked for ``-progress pipe:1 -nostats``, which prints ``key=value``
  blocks on stdout ending in ``progress=continue``/``progress=end``.

Nothing in this module imports Qt or spawns anything: the runners feed lines in,
:class:`~avchdforge.process.ProgressScanner`-shaped objects hand fractions back.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from .process import ProgressScanner, strip_ansi_sequences

#: Line that carries tsMuxeR's completion percentage.
_TSMUXER_PERCENT = re.compile(r"^(\d+(?:[.,]\d+)?)\s*%\s*complete\b", re.IGNORECASE)
#: Informational frame counter (used for the log line, not the progress value).
_TSMUXER_FRAMES = re.compile(r"^Processed\s+(\d+)\s+video frames\b", re.IGNORECASE)
#: Everything ffmpeg's ``-progress`` writes.
_FFMPEG_KEY_VALUE = re.compile(r"^([a-z_]+)=(.*)$")

#: tsMuxeR prints this exactly once, after the mux really finished.
MUX_SUCCESS_SENTINEL: Final[str] = "Mux successful complete"


# --------------------------------------------------------------------------
# tsMuxeR
# --------------------------------------------------------------------------


def parse_percent(text: str) -> float | None:
    """``"7.0% complete"`` → ``7.0``; anything else → ``None``.

    Accepts the comma decimal separator some locales/builds print, and ignores
    the terminal control sequences tsMuxeR wraps around in-place reports
    (``ESC M``, see :func:`~avchdforge.process.strip_ansi_sequences`).
    """
    match = _TSMUXER_PERCENT.match(strip_ansi_sequences(text or "").strip())
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return min(max(value, 0.0), 100.0)


def is_mux_success(output: str) -> bool:
    """True when tsMuxeR printed its success sentinel.

    Exit code 0 alone is not evidence (``docs/CONTEXT.md`` §2.2), but a missing
    sentinel together with exit 0 means we should look at the output before
    trusting it — which is exactly what the backend does.
    """
    return MUX_SUCCESS_SENTINEL in (output or "")


@dataclass(slots=True)
class TsmuxerScanner:
    """Tracks tsMuxeR's percentage, frame counter and success sentinel."""

    percent: float = 0.0
    frames: int = 0
    success: bool = False

    def feed(self, line: str) -> float | None:
        """Consume one output line; return the fraction 0..1 when it moved."""
        text = strip_ansi_sequences(line or "").strip()
        if not text:
            return None
        if MUX_SUCCESS_SENTINEL in text:
            self.success = True
            self.percent = 100.0
            return 1.0
        if match := _TSMUXER_FRAMES.match(text):
            self.frames = int(match.group(1))
            return None
        value = parse_percent(text)
        if value is None:
            return None
        self.percent = value
        return self.fraction

    @property
    def fraction(self) -> float:
        return min(max(self.percent / 100.0, 0.0), 1.0)

    @property
    def done(self) -> bool:
        return self.success or self.percent >= 100.0


# --------------------------------------------------------------------------
# ffmpeg -progress
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FfmpegProgress:
    """One ``-progress`` block (or the fields seen so far)."""

    out_time_us: int | None = None
    bitrate: str = ""
    total_size: int | None = None
    speed: str = ""
    done: bool = False

    def fraction(self, duration: float | None) -> float | None:
        """Position in the job, or ``None`` when the duration is unknown."""
        if self.done:
            return 1.0
        if not duration or duration <= 0 or self.out_time_us is None:
            return None
        return min(max(self.out_time_us / 1_000_000 / duration, 0.0), 1.0)

    @property
    def out_time_label(self) -> str:
        if self.out_time_us is None:
            return "?"
        total = int(self.out_time_us / 1_000_000)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"


@dataclass(slots=True)
class FfmpegProgressScanner:
    """Feeds on ffmpeg's ``-progress pipe:1`` key/value lines.

    ``duration`` is the *source* duration: ffmpeg reports ``out_time_us`` since
    the start of the input, which is what the bar should follow for an extract or
    a conversion.
    """

    duration: float | None = None
    last: FfmpegProgress = field(default_factory=FfmpegProgress)
    #: Every completed block, in order (useful for tests and for logging).
    blocks: list[FfmpegProgress] = field(default_factory=list)

    def feed(self, line: str) -> float | None:
        text = strip_ansi_sequences(line or "").strip()
        match = _FFMPEG_KEY_VALUE.match(text)
        if not match:
            # ffmpeg -v error should keep stdout clean, but a stray warning on
            # stderr arrives here too; it is not progress.
            return None
        key, raw = match.group(1), match.group(2).strip()
        if key == "progress":
            self.last = replace(self.last, done=raw == "end")
            self.blocks.append(self.last)
            return self.last.fraction(self.duration)
        if key == "out_time_us":
            self.last = replace(self.last, out_time_us=_int_or_none(raw))
        elif key == "bitrate":
            self.last = replace(self.last, bitrate=raw)
        elif key == "total_size":
            self.last = replace(self.last, total_size=_int_or_none(raw))
        elif key == "speed":
            self.last = replace(self.last, speed=raw)
        return None

    @property
    def fraction(self) -> float | None:
        return self.last.fraction(self.duration)

    @property
    def done(self) -> bool:
        return self.last.done


def _int_or_none(text: str) -> int | None:
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def scan_lines(lines: Iterable[str], scanner: ProgressScanner) -> list[float]:
    """Feed every line to a scanner; return the fractions it reported."""
    return [value for line in lines if (value := scanner.feed(line)) is not None]


def last_fraction(values: Sequence[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return value
    return None


__all__ = [
    "MUX_SUCCESS_SENTINEL",
    "FfmpegProgress",
    "FfmpegProgressScanner",
    "TsmuxerScanner",
    "is_mux_success",
    "last_fraction",
    "parse_percent",
    "scan_lines",
]
