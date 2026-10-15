"""tsMuxeR meta-file generation.

The meta file is the contract between avchd-forge and tsMuxeR, so it is built by
pure functions and asserted against golden files.

Two rules that were established by experiment (see ``docs/CONTEXT.md`` §2.1) are
baked in here:

* ``track=`` is never computed in this module — the caller passes the Track ID
  that tsMuxeR itself reported, or ``0`` for a raw elementary stream;
* the raw elementary streams we produce (extracted DTS/AC-3 cores, converted
  SRT) are always given ``track=0``, which tsMuxeR ignores.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from .constants import (
    DEFAULT_BOTTOM_OFFSET,
    DEFAULT_FONT_COLOR,
    DEFAULT_FONT_NAME,
    DEFAULT_FONT_SIZE,
    DEFAULT_SPLIT_SIZE_MB,
    TSMUXER_PGS_TAG,
    TSMUXER_SRT_TAG,
)

TrackKind = Literal["video", "audio", "subtitle"]

#: Codec tag for AVC video in tsMuxeR meta files.
TSMUXER_AVC_TAG = "V_MPEG4/ISO/AVC"


class MetaError(ValueError):
    """The requested meta file cannot be expressed (bad path, no tracks…)."""


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetaTrack:
    """One ``<codec tag>, "<file>", options…`` line."""

    codec_tag: str
    path: Path
    track_id: int
    kind: TrackKind
    language: str | None = None
    #: audio only: positive values delay the track, negatives advance it.
    timeshift_ms: int | None = None
    #: video only: BD-recommended stream fixes (no re-encode, header level).
    insert_sei: bool = False
    cont_sps: bool = False
    #: video/subtitle: used by tsMuxeR's SRT renderer and for PGS timing.
    fps: float | None = None
    video_width: int | None = None
    video_height: int | None = None
    #: SRT rendering only.
    font_name: str | None = None
    font_size: int | None = None
    font_color: str | None = None
    bottom_offset: int | None = None
    #: extra options appended in the given order; ``None`` renders a bare flag
    #: (``default``), a string renders ``key=value``.
    extra_options: tuple[tuple[str, str | None], ...] = ()

    @classmethod
    def video(
        cls,
        path: Path | str,
        track_id: int,
        *,
        language: str | None = None,
        fps: float | None = None,
        insert_sei: bool = True,
        cont_sps: bool = True,
        extra_options: tuple[tuple[str, str | None], ...] = (),
    ) -> MetaTrack:
        return cls(
            codec_tag=TSMUXER_AVC_TAG,
            path=Path(path),
            track_id=track_id,
            kind="video",
            language=language,
            fps=fps,
            insert_sei=insert_sei,
            cont_sps=cont_sps,
            extra_options=extra_options,
        )

    @classmethod
    def audio(
        cls,
        codec_tag: str,
        path: Path | str,
        track_id: int,
        *,
        language: str | None = None,
        timeshift_ms: int | None = None,
        extra_options: tuple[tuple[str, str | None], ...] = (),
    ) -> MetaTrack:
        return cls(
            codec_tag=codec_tag,
            path=Path(path),
            track_id=track_id,
            kind="audio",
            language=language,
            timeshift_ms=timeshift_ms,
            extra_options=extra_options,
        )

    @classmethod
    def subtitle(
        cls,
        path: Path | str,
        track_id: int,
        *,
        language: str | None = None,
        pgs: bool = True,
        video_width: int | None = None,
        video_height: int | None = None,
        fps: float | None = None,
        font_name: str | None = DEFAULT_FONT_NAME,
        font_size: int | None = DEFAULT_FONT_SIZE,
        font_color: str | None = DEFAULT_FONT_COLOR,
        bottom_offset: int | None = DEFAULT_BOTTOM_OFFSET,
        extra_options: tuple[tuple[str, str | None], ...] = (),
    ) -> MetaTrack:
        """A subtitle track.

        ``pgs=True`` copies an existing PGS stream; ``pgs=False`` asks tsMuxeR to
        render UTF-8 SRT into PGS, which is where the font options apply.
        """
        return cls(
            codec_tag=TSMUXER_PGS_TAG if pgs else TSMUXER_SRT_TAG,
            path=Path(path),
            track_id=track_id,
            kind="subtitle",
            language=language,
            video_width=video_width,
            video_height=video_height,
            fps=fps,
            font_name=font_name if not pgs else None,
            font_size=font_size if not pgs else None,
            font_color=font_color if not pgs else None,
            bottom_offset=bottom_offset if not pgs else None,
            extra_options=extra_options,
        )


@dataclass(frozen=True, slots=True)
class MuxOptions:
    """The ``MUXOPT`` global parameters."""

    avchd: bool = True
    no_pcr_on_video_pid: bool = True
    new_audio_pes: bool = True
    vbr: bool = True
    vbv_len: int | None = 500
    #: ``None`` disables splitting (only sensible on a non-FAT32 target).
    split_size_mb: int | None = DEFAULT_SPLIT_SIZE_MB
    custom_chapters: tuple[str, ...] = ()
    auto_chapters_minutes: int | None = None

    def tokens(self) -> list[str]:
        tokens = ["MUXOPT"]
        if self.avchd:
            tokens.append("--avchd")
        if self.no_pcr_on_video_pid:
            tokens.append("--no-pcr-on-video-pid")
        if self.new_audio_pes:
            tokens.append("--new-audio-pes")
        if self.vbr:
            tokens.append("--vbr")
        if self.vbv_len is not None:
            tokens.append(f"--vbv-len={self.vbv_len}")
        if self.split_size_mb is not None:
            tokens.append(f"--split-size={self.split_size_mb}MB")
        if self.auto_chapters_minutes is not None:
            tokens.append(f"--auto-chapters={self.auto_chapters_minutes}")
        if self.custom_chapters:
            tokens.append("--custom-chapters=" + ";".join(self.custom_chapters))
        return tokens


@dataclass(frozen=True, slots=True)
class MuxJob:
    """Everything that ends up in one meta file."""

    tracks: tuple[MetaTrack, ...] = ()
    options: MuxOptions = field(default_factory=MuxOptions)

    def with_options(self, **changes: object) -> MuxJob:
        return replace(self, options=replace(self.options, **changes))

    @property
    def video_track(self) -> MetaTrack | None:
        return next((track for track in self.tracks if track.kind == "video"), None)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def quote(value: str) -> str:
    """Quote a meta-file value when it contains spaces or quotes.

    Backslashes are deliberately left alone: on Windows they are path separators
    and tsMuxeR does not unescape them, so doubling them would break every path.
    """
    if any(char in value for char in ' \t"'):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _track_options(track: MetaTrack) -> list[str]:
    options: list[str] = []
    if track.kind == "video":
        if track.insert_sei:
            options.append("insertSEI")
        if track.cont_sps:
            options.append("contSPS")
        if track.fps is not None:
            options.append(f"fps={_number(track.fps)}")
    options.append(f"track={track.track_id}")
    if track.language:
        options.append(f"lang={track.language}")
    if track.kind == "audio" and track.timeshift_ms is not None:
        options.append(f"timeshift={track.timeshift_ms}ms")
    if track.kind == "subtitle":
        if track.video_width is not None:
            options.append(f"video-width={track.video_width}")
        if track.video_height is not None:
            options.append(f"video-height={track.video_height}")
        if track.fps is not None:
            options.append(f"fps={_number(track.fps)}")
        if track.font_name is not None:
            options.append(f"font-name={quote(track.font_name)}")
        if track.font_size is not None:
            options.append(f"font-size={track.font_size}")
        if track.font_color is not None:
            options.append(f"font-color={track.font_color}")
        if track.bottom_offset is not None:
            options.append(f"bottom-offset={track.bottom_offset}")
    for key, value in track.extra_options:
        options.append(key if value is None else f"{key}={value}")
    return options


def _number(value: float) -> str:
    """Format a float the way tsMuxeR prints it (23.976, 25, 59.94)."""
    return f"{value:.6g}"


def render_track_line(track: MetaTrack) -> str:
    path = track.path
    if "\n" in str(path) or "\r" in str(path):
        raise MetaError(f"path contains a line break and cannot be used: {path!r}")
    options = _track_options(track)
    return ", ".join([track.codec_tag, quote(str(path)), *options])


def order_tracks(tracks: tuple[MetaTrack, ...]) -> tuple[MetaTrack, ...]:
    """Video first (tsMuxeR expects it), then audio, then subtitles.

    Everything already sits in the order produced by the muxer's own rules
    (video) or by the user's selection (audio/subtitles), so the sort is stable.
    """
    rank: dict[TrackKind, int] = {"video": 0, "audio": 1, "subtitle": 2}
    return tuple(sorted(tracks, key=lambda track: rank[track.kind]))


def render_meta(job: MuxJob) -> str:
    """Render a complete, deterministic meta file body (UTF-8, LF endings)."""
    if not job.tracks:
        raise MetaError("a meta file needs at least one track")
    if job.video_track is None:
        raise MetaError("a meta file needs a video track")
    if sum(1 for track in job.tracks if track.kind == "video") > 1:
        raise MetaError("AVCHD takes exactly one video track per mux")

    lines = [" ".join(job.options.tokens())]
    lines.extend(render_track_line(track) for track in order_tracks(job.tracks))
    return "\n".join(lines) + "\n"


def write_meta(path: Path | str, job: MuxJob) -> Path:
    """Write the meta file as UTF-8 (tsMuxeR requires exactly that)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_meta(job), encoding="utf-8", newline="\n")
    return target


__all__ = [
    "TSMUXER_AVC_TAG",
    "MetaError",
    "MetaTrack",
    "MuxJob",
    "MuxOptions",
    "order_tracks",
    "quote",
    "render_meta",
    "render_track_line",
    "write_meta",
]
