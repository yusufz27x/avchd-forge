"""Mapping ffprobe streams onto tsMuxeR Track IDs.

tsMuxeR numbers the streams of an input differently per container (1-based file
order for MKV, PIDs for MPEG-TS) and **exits 0 with wrong output — or segfaults —
when the number in a meta file is wrong** (``docs/CONTEXT.md`` §2.1/§2.2).

So the number never comes from arithmetic: a detection run (``tsMuxeR <file>``,
no output argument) is a mandatory preflight, and its ``Track ID:`` values are
what we put in the meta file. Streams are matched by media kind and codec family
in file order, which is how both tools enumerate them; anything that does not
line up is left unmapped and reported instead of guessed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import ToolExecutionError, TrackMappingError
from .probe import MediaInfo
from .process import Runner

MediaKind = Literal["video", "audio", "subtitle", "unknown"]

#: Raw elementary streams are always referenced this way (tsMuxeR ignores it).
RAW_STREAM_TRACK_ID = 0

#: tsMuxeR ``Stream ID:`` -> codec family.
STREAM_ID_FAMILIES: dict[str, str] = {
    "V_MPEG4/ISO/AVC": "avc",
    "V_MPEG4/ISO/MVC": "avc",
    "V_MPEGH/ISO/HEVC": "hevc",
    "V_MPEGI/ISO/VVC": "vvc",
    "V_MPEG-2": "mpeg2",
    "V_MS/VFW/WVC1": "vc1",
    "A_AC3": "ac3",  # tsMuxeR groups AC-3, AC-3+ and TrueHD under A_AC3
    "A_DTS": "dts",
    "A_AAC": "aac",
    "A_LPCM": "lpcm",
    "A_MP3": "mp3",
    "S_HDMV/PGS": "pgs",
    "S_TEXT/UTF8": "srt",
}

#: ffprobe ``codec_name`` -> codec family.
CODEC_FAMILIES: dict[str, str] = {
    "h264": "avc",
    "avc1": "avc",
    "hevc": "hevc",
    "h265": "hevc",
    "mpeg2video": "mpeg2",
    "vc1": "vc1",
    "ac3": "ac3",
    "eac3": "ac3",
    "truehd": "ac3",
    "mlp": "ac3",
    "dts": "dts",
    "aac": "aac",
    "mp3": "mp3",
    "pcm_bluray": "lpcm",
    "pcm_dvd": "lpcm",
    "pcm_s16be": "lpcm",
    "pcm_s16le": "lpcm",
    "pcm_s24le": "lpcm",
    "hdmv_pgs_subtitle": "pgs",
    "pgssub": "pgs",
    "subrip": "srt",
    "srt": "srt",
    "ass": "ass",
    "ssa": "ass",
    "mov_text": "mov_text",
    "webvtt": "webvtt",
}

KIND_BY_PREFIX: dict[str, MediaKind] = {"V": "video", "A": "audio", "S": "subtitle"}

_TRACK_ID = re.compile(r"^Track ID:\s*(\d+)\s*$")
_STREAM_TYPE = re.compile(r"^Stream type:\s*(.+?)\s*$")
_STREAM_ID = re.compile(r"^Stream ID:\s*(\S+)\s*$")
_STREAM_INFO = re.compile(r"^Stream info:\s*(.*?)\s*$")
_STREAM_LANG = re.compile(r"^Stream lang:\s*(\S*)\s*$")


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TsmuxerTrack:
    """One entry of ``tsMuxeR <file>`` output."""

    #: ``None`` for raw elementary streams (tsMuxeR prints no ``Track ID:``).
    track_id: int | None
    stream_type: str
    stream_id: str
    language: str = ""
    info: str = ""
    #: Position in the detection listing, used for the alignment.
    ordinal: int = 0

    @property
    def media_kind(self) -> MediaKind:
        return KIND_BY_PREFIX.get(self.stream_id[:1], "unknown")

    @property
    def family(self) -> str:
        """Codec family, or the raw stream id when tsMuxeR used an unknown one."""
        return STREAM_ID_FAMILIES.get(self.stream_id, f"unknown:{self.stream_id}")

    def describe(self) -> str:
        identifier = "raw" if self.track_id is None else str(self.track_id)
        parts = [f"track {identifier}", self.stream_type, self.stream_id]
        if self.language:
            parts.append(self.language)
        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class TrackIdMap:
    """ffprobe stream index -> tsMuxeR Track ID, plus what could not be mapped."""

    ids: dict[int, int]
    diagnostics: tuple[str, ...] = ()

    def id_for(self, ffprobe_index: int) -> int | None:
        return self.ids.get(ffprobe_index)

    def require(self, indices: Iterable[int]) -> None:
        """Raise when any of ``indices`` could not be mapped."""
        missing = [index for index in indices if index not in self.ids]
        if missing:
            detail = "; ".join(self.diagnostics) or "no alignment information"
            raise TrackMappingError(
                "tsMuxeR could not be matched to "
                + ", ".join(f"stream {index}" for index in missing)
                + f" ({detail}). Refusing to guess track numbers.",
            )

    def __len__(self) -> int:
        return len(self.ids)


# --------------------------------------------------------------------------
# detection run
# --------------------------------------------------------------------------


def build_detect_argv(tsmuxer: str | Path, path: Path | str) -> list[str]:
    """argv for tsMuxeR's track-detection mode (one argument, no output)."""
    return [str(tsmuxer), str(path)]


def parse_detect_output(text: str) -> tuple[TsmuxerTrack, ...]:
    """Parse ``tsMuxeR <file>`` output. Tolerant of spacing and extra lines."""
    tracks: list[TsmuxerTrack] = []
    current: dict[str, object] | None = None

    def flush() -> None:
        if current is not None and "stream_id" in current:
            tracks.append(
                TsmuxerTrack(
                    track_id=current.get("track_id"),  # type: ignore[arg-type]
                    stream_type=str(current.get("stream_type", "")),
                    stream_id=str(current["stream_id"]),
                    language=str(current.get("language", "")),
                    info=str(current.get("info", "")),
                    ordinal=len(tracks),
                )
            )

    def begin() -> dict[str, object]:
        """Close the entry in progress (if it is complete) and start a new one."""
        flush()
        return {}

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if match := _TRACK_ID.match(line):
            # "Track ID:" is printed above the entry it belongs to; raw
            # elementary streams have none and open with "Stream type:" instead.
            current = begin()
            current["track_id"] = int(match.group(1))
            continue
        if match := _STREAM_TYPE.match(line):
            if current is None or "stream_type" in current or "stream_id" in current:
                current = begin()
            current["stream_type"] = match.group(1)
            continue
        if current is None:
            continue
        if match := _STREAM_ID.match(line):
            current["stream_id"] = match.group(1)
        elif match := _STREAM_INFO.match(line):
            current["info"] = match.group(1)
        elif match := _STREAM_LANG.match(line):
            current["language"] = match.group(1)
    flush()

    return tuple(tracks)


def detect_tracks(
    tsmuxer: str | Path, path: Path | str, runner: Runner
) -> tuple[TsmuxerTrack, ...]:
    """Run the detection mode; raise :class:`ToolExecutionError` when it fails."""
    argv = build_detect_argv(tsmuxer, path)
    result = runner.run(argv, phase="detect")
    if not result.ok:
        raise ToolExecutionError("tsMuxeR", argv, result.returncode, result.stderr, result.stdout)
    return parse_detect_output(result.output)


# --------------------------------------------------------------------------
# mapping
# --------------------------------------------------------------------------


def _family_of(codec: str) -> str:
    """Codec family of an ffprobe ``codec_name``.

    LPCM shows up under many names (``pcm_bluray``, ``pcm_s16le``…), so the
    ``pcm_`` prefix is treated as lpcm. Unknown codecs keep their name with an
    ``unknown:`` prefix so they can never accidentally match a detection entry.
    """
    if codec.startswith("pcm_"):
        return "lpcm"
    return CODEC_FAMILIES.get(codec, f"unknown:{codec}")


def _families_of_media(media: MediaInfo, kind: MediaKind) -> list[tuple[int, str]]:
    if kind == "video":
        streams: Sequence[object] = media.videos
    elif kind == "audio":
        streams = media.audio
    else:
        streams = media.subtitles
    return [(stream.index, _family_of(stream.codec)) for stream in streams]  # type: ignore[attr-defined]


def _align(ffprobe_families: list[str], detection_families: list[str]) -> list[int | None]:
    """Monotonic alignment: ffprobe family ``i`` -> detection position, or None.

    Both tools list streams in file order, so a monotonic match is the right
    shape. Extra detection entries (streams ffprobe does not expose, such as
    menus or second video angles) are skipped; extra ffprobe streams stay
    unmapped and are reported rather than guessed at.
    """
    mapping: list[int | None] = [None] * len(ffprobe_families)
    cursor = 0
    for position, family in enumerate(ffprobe_families):
        for candidate in range(cursor, len(detection_families)):
            if detection_families[candidate] == family:
                mapping[position] = candidate
                cursor = candidate + 1
                break
    return mapping


def resolve_track_ids(media: MediaInfo, detection: Sequence[TsmuxerTrack]) -> TrackIdMap:
    """Build the ffprobe-index -> Track ID map, reporting anything unmappable."""
    ids: dict[int, int] = {}
    diagnostics: list[str] = []

    for kind in ("video", "audio", "subtitle"):
        media_streams = _families_of_media(media, kind)
        detected = [track for track in detection if track.media_kind == kind]
        if not media_streams:
            continue
        if not detected:
            diagnostics.append(
                f"tsMuxeR reports no {kind} streams for this file "
                f"(ffprobe sees {len(media_streams)})"
            )
            continue

        alignment = _align(
            [family for _, family in media_streams],
            [track.family for track in detected],
        )
        for (index, family), position in zip(media_streams, alignment, strict=True):
            if position is None:
                diagnostics.append(
                    f"{kind} stream {index} ({family}) has no tsMuxeR counterpart"
                )
                continue
            detected_track = detected[position]
            if detected_track.track_id is None:
                diagnostics.append(
                    f"{kind} stream {index} has no Track ID in tsMuxeR's report"
                )
                continue
            ids[index] = detected_track.track_id

    return TrackIdMap(ids=ids, diagnostics=tuple(diagnostics))


def describe_detection(tracks: Sequence[TsmuxerTrack]) -> list[str]:
    """Lines for the log pane."""
    return [f"tsMuxeR sees {track.describe()}" for track in tracks]


def raw_track_id() -> int:
    """Track id used for raw elementary streams (extracted cores, SRT, SUP)."""
    return RAW_STREAM_TRACK_ID


__all__ = [
    "CODEC_FAMILIES",
    "RAW_STREAM_TRACK_ID",
    "STREAM_ID_FAMILIES",
    "TrackIdMap",
    "TsmuxerTrack",
    "build_detect_argv",
    "describe_detection",
    "detect_tracks",
    "parse_detect_output",
    "raw_track_id",
    "resolve_track_ids",
]
