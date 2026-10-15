"""Media inspection through ``ffprobe``.

The parsing is deliberately split from the process call:

``build_ffprobe_argv()`` -> argv, ``parse_ffprobe_json()`` -> :class:`MediaInfo`,
and ``probe()`` which glues them together through a
:class:`~avchdforge.process.Runner`.

That way every decision downstream (validation, planning, meta generation) can be
tested against canned ffprobe JSON with no ffprobe binary in sight.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from .constants import (
    AUDIO_CODEC_LABELS,
    DEFAULT_LANGUAGE,
    DTS_HD_PROFILE_MARKERS,
    ISO639_1_TO_2,
    LANGUAGE_ALIASES,
    LPCM_CODEC_PREFIX,
    PGS_SUBTITLE_CODECS,
    PROGRESSIVE_FIELD_ORDERS,
    TEXT_SUBTITLE_CODECS,
    UNSUPPORTED_SUBTITLE_CODECS,
)
from .errors import ProbeError
from .process import Runner

# --------------------------------------------------------------------------
# Small parsers
# --------------------------------------------------------------------------


def parse_frame_rate(value: str | None) -> float | None:
    """Parse ffprobe's ``"24000/1001"`` style frame rate into a float."""
    if not value:
        return None
    text = value.strip()
    if text in {"0/0", "N/A"}:
        return None
    try:
        fraction = Fraction(text)
    except (ValueError, ZeroDivisionError):
        try:
            result = float(text)
        except ValueError:
            return None
        return result if result > 0 else None
    if fraction <= 0:
        return None
    return float(fraction)


def parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_language(value: Any) -> str:
    """Return a three-letter lowercase ISO 639-2 code that tsMuxeR accepts.

    ffprobe reports whatever the container holds: ``eng`` normally, sometimes a
    2-letter code, occasionally junk such as ``und`` or ``Unicode``. tsMuxeR
    rejects anything that is not exactly three letters, so unknown values
    collapse to ``und``.
    """
    if value is None:
        return DEFAULT_LANGUAGE
    text = str(value).strip().lower()
    if not text:
        return DEFAULT_LANGUAGE
    # Containers sometimes carry lists like "eng" / "en-US" / "jpn,eng".
    for separator in (",", "/", ";"):
        if separator in text:
            text = text.split(separator, 1)[0].strip()
    if "-" in text:
        text = text.split("-", 1)[0].strip()
    if len(text) == 2:
        return ISO639_1_TO_2.get(text, DEFAULT_LANGUAGE)
    text = LANGUAGE_ALIASES.get(text, text)
    if len(text) != 3 or not text.isalpha():
        return DEFAULT_LANGUAGE
    return text


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text not in {"", "0", "false", "no", "none"}


def _tags(stream: Mapping[str, Any]) -> Mapping[str, Any]:
    tags = stream.get("tags") or {}
    return tags if isinstance(tags, Mapping) else {}


def _disposition(stream: Mapping[str, Any]) -> Mapping[str, Any]:
    disposition = stream.get("disposition") or {}
    return disposition if isinstance(disposition, Mapping) else {}


def _language_of(stream: Mapping[str, Any]) -> str:
    tags = _tags(stream)
    return normalize_language(tags.get("language", tags.get("LANGUAGE")))


# --------------------------------------------------------------------------
# Track models
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VideoTrack:
    """One video stream as reported by ffprobe."""

    index: int
    codec: str
    profile: str | None = None
    level: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    pix_fmt: str | None = None
    bitrate: int | None = None
    field_order: str | None = None
    duration: float | None = None
    language: str = DEFAULT_LANGUAGE

    @property
    def resolution(self) -> tuple[int | None, int | None]:
        return (self.width, self.height)

    @property
    def is_avc(self) -> bool:
        return self.codec.lower() in {"h264", "avc1"}

    @property
    def is_progressive(self) -> bool:
        return (self.field_order or "").lower() in PROGRESSIVE_FIELD_ORDERS

    @property
    def resolution_label(self) -> str:
        if self.width is None or self.height is None:
            return "unknown resolution"
        suffix = "p" if self.is_progressive else "i"
        return f"{self.width}x{self.height}{suffix}"

    @property
    def level_label(self) -> str:
        return f"{self.level:g}" if self.level is not None else "unknown"

    def summary(self) -> str:
        fps = f"{self.fps:.3f} fps" if self.fps else "unknown fps"
        profile = self.profile or "unknown profile"
        return f"{self.codec.upper()} {profile}@{self.level_label} {self.resolution_label} {fps}"


@dataclass(frozen=True, slots=True)
class AudioTrack:
    """One audio stream as reported by ffprobe."""

    index: int
    codec: str
    profile: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    sample_rate: int | None = None
    bitrate: int | None = None
    language: str = DEFAULT_LANGUAGE
    title: str | None = None
    is_default: bool = False
    is_forced: bool = False
    duration: float | None = None

    @property
    def is_lpcm(self) -> bool:
        return self.codec.startswith(LPCM_CODEC_PREFIX) or self.codec in {"lpcm", "pcm_bluray"}

    @property
    def is_hd(self) -> bool:
        """True for HD extensions that AVCHD cannot carry (DTS-HD, DTS:X, TrueHD)."""
        if self.codec in {"truehd", "mlp"}:
            return True
        if self.codec == "dts" and self.profile:
            return any(marker in self.profile.upper() for marker in DTS_HD_PROFILE_MARKERS)
        if self.codec == "eac3":
            return True
        return False

    @property
    def codec_label(self) -> str:
        base = AUDIO_CODEC_LABELS.get(self.codec, self.codec.upper())
        if self.codec == "dts" and self.profile and self.profile.upper() not in {"DTS"}:
            return f"{base} ({self.profile})"
        if self.codec in {"truehd", "mlp"} and self.profile and "ATMOS" in self.profile.upper():
            return f"{base} (Atmos)"
        return base

    @property
    def channel_label(self) -> str:
        if self.channel_layout:
            return self.channel_layout
        if self.channels:
            return {
                1: "mono",
                2: "stereo",
                3: "2.1",
                6: "5.1",
                8: "7.1",
            }.get(self.channels, f"{self.channels} ch")
        return "unknown layout"

    @property
    def sample_rate_label(self) -> str:
        if not self.sample_rate:
            return "?"
        khz = self.sample_rate / 1000
        return f"{khz:g} kHz"

    @property
    def bitrate_label(self) -> str:
        if not self.bitrate:
            return "?"
        return f"{self.bitrate / 1000:.0f} kbps"

    def summary(self) -> str:
        parts = [
            self.codec_label,
            self.channel_label,
            self.sample_rate_label,
            self.bitrate_label,
            self.language,
        ]
        return " ".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    """One subtitle stream as reported by ffprobe."""

    index: int
    codec: str
    language: str = DEFAULT_LANGUAGE
    title: str | None = None
    is_default: bool = False
    is_forced: bool = False
    width: int | None = None
    height: int | None = None

    @property
    def is_pgs(self) -> bool:
        return self.codec in PGS_SUBTITLE_CODECS

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_SUBTITLE_CODECS

    @property
    def is_supported(self) -> bool:
        return self.is_pgs or self.is_text

    @property
    def codec_label(self) -> str:
        labels = {
            "hdmv_pgs_subtitle": "PGS",
            "pgssub": "PGS",
            "subrip": "SubRip",
            "srt": "SubRip",
            "ass": "ASS",
            "ssa": "SSA",
            "mov_text": "mov_text",
            "webvtt": "WebVTT",
            "dvd_subtitle": "VobSub",
            "dvb_subtitle": "DVB",
            "hdmv_text_subtitle": "HDMV text",
        }
        return labels.get(self.codec, self.codec.upper())

    def summary(self) -> str:
        bits = [self.codec_label, self.language]
        if self.is_forced:
            bits.append("forced")
        if self.is_default:
            bits.append("default")
        return " ".join(bits)


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Everything the rest of the app needs to know about the source file."""

    path: Path
    format_name: str = ""
    format_label: str = ""
    duration: float | None = None
    size_bytes: int | None = None
    bit_rate: int | None = None
    videos: tuple[VideoTrack, ...] = ()
    audio: tuple[AudioTrack, ...] = ()
    subtitles: tuple[SubtitleTrack, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def video(self) -> VideoTrack | None:
        return self.videos[0] if self.videos else None

    @property
    def streams(self) -> tuple[VideoTrack | AudioTrack | SubtitleTrack, ...]:
        everything = (*self.videos, *self.audio, *self.subtitles)
        return tuple(sorted(everything, key=lambda track: track.index))

    def audio_track(self, index: int) -> AudioTrack | None:
        return next((track for track in self.audio if track.index == index), None)

    def subtitle_track(self, index: int) -> SubtitleTrack | None:
        return next((track for track in self.subtitles if track.index == index), None)

    @property
    def duration_label(self) -> str:
        if not self.duration:
            return "unknown"
        total = int(self.duration)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"

    @property
    def size_label(self) -> str:
        if not self.size_bytes:
            return "unknown"
        size = float(self.size_bytes)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if size < 1024 or unit == "GiB":
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} GiB"


# --------------------------------------------------------------------------
# ffprobe plumbing
# --------------------------------------------------------------------------

FFPROBE_ENTRIES: Sequence[str] = (
    "stream=index,codec_name,codec_long_name,profile,codec_type,width,height,pix_fmt,"
    "level,r_frame_rate,avg_frame_rate,time_base,start_time,duration,bit_rate,"
    "channels,channel_layout,sample_rate,field_order,disposition,tags",
    "format=filename,format_name,format_long_name,start_time,duration,size,bit_rate,tags",
)


def build_ffprobe_argv(tool: str, path: Path | str) -> list[str]:
    """argv for a JSON probe of ``path``. Pure function, asserted by tests."""
    return [
        str(tool),
        "-hide_banner",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]


def parse_ffprobe_json(payload: Mapping[str, Any], path: Path | str | None = None) -> MediaInfo:
    """Turn an ffprobe JSON document into :class:`MediaInfo`.

    Tolerates missing keys, ``"N/A"`` strings and unexpected stream types: only
    the streams we care about are modelled, the raw payload is kept for logging.
    """
    if not isinstance(payload, Mapping):  # pragma: no cover - defensive
        raise ValueError("ffprobe payload must be a JSON object")

    format_info = payload.get("format") or {}
    if not isinstance(format_info, Mapping):  # pragma: no cover - defensive
        format_info = {}

    resolved_path = Path(path) if path is not None else Path(str(format_info.get("filename", "")))

    videos: list[VideoTrack] = []
    audio: list[AudioTrack] = []
    subtitles: list[SubtitleTrack] = []

    streams = payload.get("streams") or []
    for stream in streams:
        if not isinstance(stream, Mapping):
            continue
        kind = str(stream.get("codec_type", "")).lower()
        index = parse_int(stream.get("index"))
        if index is None:  # pragma: no cover - defensive
            continue
        if kind == "video":
            # Cover art and thumbnails show up as video streams with disposition.attached_pic.
            if _is_truthy(_disposition(stream).get("attached_pic")):
                continue
            videos.append(
                VideoTrack(
                    index=index,
                    codec=str(stream.get("codec_name", "")).lower(),
                    profile=_optional_str(stream.get("profile")),
                    level=_parse_level(stream.get("level")),
                    width=parse_int(stream.get("width")),
                    height=parse_int(stream.get("height")),
                    fps=parse_frame_rate(stream.get("avg_frame_rate"))
                    or parse_frame_rate(stream.get("r_frame_rate")),
                    pix_fmt=_optional_str(stream.get("pix_fmt")),
                    bitrate=parse_int(stream.get("bit_rate")),
                    field_order=_optional_str(stream.get("field_order")),
                    duration=parse_float(stream.get("duration")),
                    language=_language_of(stream),
                )
            )
        elif kind == "audio":
            disposition = _disposition(stream)
            audio.append(
                AudioTrack(
                    index=index,
                    codec=str(stream.get("codec_name", "")).lower(),
                    profile=_optional_str(stream.get("profile")),
                    channels=parse_int(stream.get("channels")),
                    channel_layout=_optional_str(stream.get("channel_layout")),
                    sample_rate=parse_int(stream.get("sample_rate")),
                    bitrate=parse_int(stream.get("bit_rate")),
                    language=_language_of(stream),
                    title=_optional_str(_tags(stream).get("title")),
                    is_default=_is_truthy(disposition.get("default")),
                    is_forced=_is_truthy(disposition.get("forced")),
                    duration=parse_float(stream.get("duration")),
                )
            )
        elif kind == "subtitle":
            disposition = _disposition(stream)
            subtitles.append(
                SubtitleTrack(
                    index=index,
                    codec=str(stream.get("codec_name", "")).lower(),
                    language=_language_of(stream),
                    title=_optional_str(_tags(stream).get("title")),
                    is_default=_is_truthy(disposition.get("default")),
                    is_forced=_is_truthy(disposition.get("forced")),
                    width=parse_int(stream.get("width")),
                    height=parse_int(stream.get("height")),
                )
            )

    return MediaInfo(
        path=resolved_path,
        format_name=str(format_info.get("format_name", "")),
        format_label=str(format_info.get("format_long_name", "")),
        duration=parse_float(format_info.get("duration")),
        size_bytes=parse_int(format_info.get("size")),
        bit_rate=parse_int(format_info.get("bit_rate")),
        videos=tuple(videos),
        audio=tuple(audio),
        subtitles=tuple(subtitles),
        raw=dict(payload),
    )


def probe(path: Path | str, runner: Runner, *, tool: str = "ffprobe") -> MediaInfo:
    """Run ffprobe and parse the result.

    Raises :class:`~avchdforge.errors.ProbeError` when ffprobe fails or when the
    output is not valid JSON.
    """
    target = Path(path)
    argv = build_ffprobe_argv(tool, target)
    result = runner.run(argv, phase="probe")
    if not result.ok:
        raise ProbeError(tool, argv, result.returncode, result.stderr, result.stdout)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(
            tool,
            argv,
            result.returncode,
            f"ffprobe returned output that is not JSON: {exc}",
            result.stdout[:2000],
        ) from exc
    return parse_ffprobe_json(payload, target)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return None
    return text


def _parse_level(value: Any) -> float | None:
    """ffprobe reports H.264 levels as integers (41 == level 4.1)."""
    number = parse_float(value)
    if number is None or number <= 0:
        return None
    if number >= 10:  # 41 -> 4.1
        return number / 10.0
    return number


__all__ = [
    "AudioTrack",
    "MediaInfo",
    "SubtitleTrack",
    "VideoTrack",
    "build_ffprobe_argv",
    "normalize_language",
    "parse_ffprobe_json",
    "parse_frame_rate",
    "probe",
]
