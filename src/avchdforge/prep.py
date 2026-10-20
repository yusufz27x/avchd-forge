"""Preparation steps: window restore, audio-core extraction, subtitle conversion.

Everything here is argv construction — pure functions that the tests can assert
against without running anything. The rules that matter:

* the video conformance-window repair is a *bitstream filter over a stream copy*
  (``-c:v copy -bsf:v h264_metadata=…``): the coded pictures are untouched, only
  the SPS is rewritten so the decoder shows rows that were always there;
* audio cores are only ever *copied* through a bitstream filter
  (``-c:a copy -bsf:a dca_core``) or demuxed (``-c:a copy``); there is no
  encoder flag anywhere in this module;
* text subtitles become UTF-8 SubRip so tsMuxeR can render them to PGS, or a raw
  ``.sup`` when the installed FFmpeg really has a PGS encoder;
* every produced file is verified afterwards with ffprobe
  (:func:`check_prep_output`), because a wrong extraction must not reach the muxer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .audio import AudioDecision, decide_audio_all
from .capabilities import FfmpegCapabilities
from .constants import (
    DEFAULT_BOTTOM_OFFSET,
    DEFAULT_FONT_COLOR,
    DEFAULT_FONT_NAME,
    DEFAULT_FONT_SIZE,
)
from .meta import MetaTrack
from .probe import MediaInfo
from .selection import TrackSelection
from .sps import WindowRestore

StepKind = Literal["video-window", "audio-core", "subtitle-convert", "subtitle-encode"]

#: Bitstream filters the window repair needs, in chain order. ``h264_metadata``
#: rewrites the SPS conformance window (FFmpeg 4.1+); ``dump_extra`` then puts
#: the rewritten parameter sets in-band. That second one is not optional: a
#: Matroska/MP4 source keeps its SPS/PPS in ``avcC`` only, and the filter does
#: not copy extradata to its output — without ``dump_extra`` the repaired file
#: is an elementary stream with no parameter sets, which neither ffprobe nor
#: tsMuxeR can read (see ``docs/CONTEXT.md`` §2.12).
WINDOW_BSFS: tuple[str, str] = ("h264_metadata", "dump_extra")

#: The SPS-rewriting filter on its own; also the name the error messages use.
WINDOW_BSF = WINDOW_BSFS[0]


def window_missing_bsfs(capabilities: FfmpegCapabilities) -> tuple[str, ...]:
    """The window-repair filters this ffmpeg build does not advertise."""
    return tuple(name for name in WINDOW_BSFS if not capabilities.has_bsf(name))


#: Progress lines are requested on stdout so the log pane stays readable.
FFMPEG_PROGRESS_ARGS: tuple[str, ...] = ("-progress", "pipe:1", "-nostats")

_UNSAFE_NAME = re.compile(r"[^\w.\-]+")


@dataclass(frozen=True, slots=True)
class PrepStep:
    """One ffmpeg invocation that produces a file for the mux step."""

    kind: StepKind
    #: ffprobe stream index this step reads with ``-map``.
    source_index: int
    #: Human-readable one-liner for the log pane and the progress label.
    label: str
    argv: tuple[str, ...]
    output: Path
    #: Codec the produced file must report when probed.
    expected_codec: str
    #: Codec tsMuxeR will be told about.
    mux_codec_tag: str
    #: ffprobe index of the stream the *user selected*; differs from
    #: ``source_index`` when the data comes from a companion stream, as with a
    #: TrueHD track whose AC-3 core lives in its own stream.
    selection_index: int | None = None
    language: str | None = None
    #: Set for subtitle steps that feed tsMuxeR's SRT renderer.
    text_subtitle: bool = False
    #: Video steps only: the size the produced file must display.
    expected_width: int | None = None
    expected_height: int | None = None

    @property
    def selected_stream(self) -> int:
        """Index in the user's selection that this step satisfies."""
        return self.source_index if self.selection_index is None else self.selection_index

    @property
    def command_line(self) -> str:
        from .errors import format_argv

        return format_argv(self.argv)


def ffmpeg_preamble(ffmpeg: str | Path) -> list[str]:
    """Global flags shared by every ffmpeg call we make."""
    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        *FFMPEG_PROGRESS_ARGS,
    ]


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------


def build_audio_core_argv(
    ffmpeg: str | Path,
    source: Path | str,
    stream_index: int,
    *,
    bsf: str | None,
    container: str,
    output: Path | str,
) -> list[str]:
    """argv that write one audio stream to a raw elementary file, copy-only."""
    argv = [
        *ffmpeg_preamble(ffmpeg),
        "-i",
        str(source),
        "-map",
        f"0:{stream_index}",
        "-c:a",
        "copy",
    ]
    if bsf:
        argv += ["-bsf:a", bsf]
    argv += ["-f", container, str(output)]
    return argv


def build_subtitle_to_srt_argv(
    ffmpeg: str | Path,
    source: Path | str,
    stream_index: int,
    output: Path | str,
) -> list[str]:
    """argv that normalise any text subtitle track to UTF-8 SubRip."""
    return [
        *ffmpeg_preamble(ffmpeg),
        "-i",
        str(source),
        "-map",
        f"0:{stream_index}",
        "-c:s",
        "srt",
        "-f",
        "srt",
        str(output),
    ]


def build_subtitle_to_sup_argv(
    ffmpeg: str | Path,
    source: Path | str,
    stream_index: int,
    output: Path | str,
) -> list[str]:
    """argv that render a text subtitle to PGS (only for builds with a PGS encoder)."""
    return [
        *ffmpeg_preamble(ffmpeg),
        "-i",
        str(source),
        "-map",
        f"0:{stream_index}",
        "-c:s",
        "pgssub",
        "-f",
        "sup",
        str(output),
    ]


def build_window_restore_argv(
    ffmpeg: str | Path,
    source: Path | str,
    stream_index: int,
    *,
    restore: WindowRestore,
    output: Path | str,
) -> list[str]:
    """argv that open the SPS conformance window and copy the coded pictures.

    ``h264_metadata`` rewrites the SPS the stream already carries; with
    ``-c:v copy`` every coded picture passes through untouched. The option
    values replace the window rather than edit it (FFmpeg's default ``-1``
    means "leave alone"), which is why left/right/top are set to zero
    explicitly: the repaired frame must show the whole coded width and start at
    coded row 0. ``dump_extra`` follows so the rewritten SPS/PPS travel inside
    the elementary stream: the filter emits a headerless stream, and a probe
    (or tsMuxeR) reads the display size from those parameter sets.
    """
    return [
        *ffmpeg_preamble(ffmpeg),
        "-i",
        str(source),
        "-map",
        f"0:{stream_index}",
        "-c:v",
        "copy",
        "-bsf:v",
        (
            f"{WINDOW_BSF}=crop_left=0:crop_right=0:crop_top=0"
            f":crop_bottom={restore.crop_bottom},{WINDOW_BSFS[1]}"
        ),
        "-f",
        "h264",
        str(output),
    ]


def temp_name(kind: str, index: int, suffix: str, *, hint: str = "") -> str:
    """Deterministic temp file name: ``audio-1-dtscore.dts``, ``sub-4.srt``."""
    cleaned = _UNSAFE_NAME.sub("-", hint).strip("-")
    stem = "-".join(part for part in (kind, str(index), cleaned) if part)
    return f"{stem}{suffix}"


def plan_prep_steps(
    media: MediaInfo,
    selection: TrackSelection,
    *,
    temp_dir: Path,
    ffmpeg: str | Path = "ffmpeg",
    capabilities: FfmpegCapabilities | None = None,
    decisions: dict[int, AudioDecision] | None = None,
) -> tuple[PrepStep, ...]:
    """The ffmpeg work a mux needs, in order (video, audio, then subtitles)."""
    caps = capabilities or FfmpegCapabilities()
    decisions = decisions if decisions is not None else decide_audio_all(media)
    steps: list[PrepStep] = []

    video = media.video
    if video is not None:
        restore = video.restore_window()
        if restore is not None and not window_missing_bsfs(caps):
            output = temp_dir / temp_name("video", video.index, ".264", hint="window")
            steps.append(
                PrepStep(
                    kind="video-window",
                    source_index=video.index,
                    label=(
                        f"restore the H.264 crop window to "
                        f"{restore.target_width}x{restore.target_height} (stream copy)"
                    ),
                    argv=tuple(
                        build_window_restore_argv(
                            ffmpeg,
                            media.path,
                            video.index,
                            restore=restore,
                            output=output,
                        )
                    ),
                    output=output,
                    expected_codec="h264",
                    mux_codec_tag="V_MPEG4/ISO/AVC",
                    language=video.language,
                    expected_width=restore.target_width,
                    expected_height=restore.target_height,
                )
            )

    for index in selection.sorted_audio():
        track = media.audio_track(index)
        decision = decisions.get(index)
        if track is None or decision is None or not decision.needs_extraction:
            continue
        output = temp_dir / temp_name("audio", index, decision.output_suffix or ".bin")
        steps.append(
            PrepStep(
                kind="audio-core",
                source_index=decision.source_index,
                selection_index=index,
                label=f"{decision.note or 'extract audio core'} (stream {decision.source_index})",
                argv=tuple(
                    build_audio_core_argv(
                        ffmpeg,
                        media.path,
                        decision.source_index,
                        bsf=decision.bsf,
                        container=decision.container or "raw",
                        output=output,
                    )
                ),
                output=output,
                expected_codec=decision.expected_codec or decision.resulting_codec,
                mux_codec_tag=decision.mux_codec_tag or "A_AC3",
                language=track.language,
            )
        )

    for index in selection.sorted_subtitles():
        track = media.subtitle_track(index)
        if track is None or not track.is_text:
            continue
        language = track.language
        if caps.has_pgs_encoder:
            output = temp_dir / temp_name("sub", index, ".sup")
            steps.append(
                PrepStep(
                    kind="subtitle-encode",
                    source_index=index,
                    selection_index=index,
                    label=f"render {track.codec_label} to PGS (ffmpeg pgssub)",
                    argv=tuple(
                        build_subtitle_to_sup_argv(ffmpeg, media.path, index, output)
                    ),
                    output=output,
                    expected_codec="hdmv_pgs_subtitle",
                    mux_codec_tag="S_HDMV/PGS",
                    language=language,
                    text_subtitle=False,
                )
            )
        else:
            output = temp_dir / temp_name("sub", index, ".srt")
            label = (
                "normalise SubRip to UTF-8 for tsMuxeR"
                if track.codec in {"subrip", "srt"}
                else f"convert {track.codec_label} to SubRip for tsMuxeR"
            )
            steps.append(
                PrepStep(
                    kind="subtitle-convert",
                    source_index=index,
                    selection_index=index,
                    label=label,
                    argv=tuple(
                        build_subtitle_to_srt_argv(ffmpeg, media.path, index, output)
                    ),
                    output=output,
                    expected_codec="subrip",
                    mux_codec_tag="S_TEXT/UTF8",
                    language=language,
                    text_subtitle=True,
                )
            )

    return tuple(steps)


@dataclass(frozen=True, slots=True)
class VideoContext:
    """Video properties tsMuxeR wants for subtitle tracks and AVC muxing."""

    fps: float | None = None
    width: int | None = None
    height: int | None = None

    @classmethod
    def from_media(cls, media: MediaInfo) -> VideoContext:
        video = media.video
        if video is None:
            return cls()
        width, height = video.output_resolution
        return cls(fps=video.fps, width=width, height=height)


@dataclass(frozen=True, slots=True)
class SubtitleStyle:
    """Font settings for tsMuxeR's SRT renderer (Preferences, with defaults)."""

    font_name: str | None = DEFAULT_FONT_NAME
    font_size: int | None = DEFAULT_FONT_SIZE
    font_color: str | None = DEFAULT_FONT_COLOR
    bottom_offset: int | None = DEFAULT_BOTTOM_OFFSET


def prep_step_to_meta_track(
    step: PrepStep,
    *,
    video: VideoContext | None = None,
    style: SubtitleStyle | None = None,
) -> MetaTrack:
    """Turn a finished prep step into the meta line that consumes its output.

    Raw elementary streams always use ``track=0``: tsMuxeR ignores the parameter
    for them (verified), which removes every numbering question for prepared
    tracks.
    """
    video = video or VideoContext()
    style = style or SubtitleStyle()

    if step.kind == "video-window":
        return MetaTrack.video(step.output, 0, language=step.language, fps=video.fps)
    if step.kind == "audio-core":
        return MetaTrack.audio(
            step.mux_codec_tag,
            step.output,
            0,
            language=step.language,
            timeshift_ms=0,
        )
    if step.kind == "subtitle-encode":
        return MetaTrack.subtitle(
            step.output,
            0,
            language=step.language,
            pgs=True,
            video_width=video.width,
            video_height=video.height,
            fps=video.fps,
        )
    return MetaTrack.subtitle(
        step.output,
        0,
        language=step.language,
        pgs=False,
        video_width=video.width,
        video_height=video.height,
        fps=video.fps,
        font_name=style.font_name,
        font_size=style.font_size,
        font_color=style.font_color,
        bottom_offset=style.bottom_offset,
    )


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------


def check_prep_output(step: PrepStep, media: MediaInfo) -> str | None:
    """Return a problem description when ``media`` is not what ``step`` promised.

    Called with the result of probing the produced file. This is the guard that
    stops a silently wrong extraction (an extension layer that was not stripped,
    a subtitle that came out empty) from reaching tsMuxeR.
    """
    if not media.videos and not media.audio and not media.subtitles:
        return f"{step.output.name} contains no usable streams"

    if step.kind == "video-window":
        if not media.videos:
            return f"{step.output.name} contains no video stream"
        video = media.videos[0]
        if video.codec != step.expected_codec:
            return f"{step.output.name} contains {video.codec}, expected {step.expected_codec}"
        if step.expected_width and step.expected_height and (
            video.width,
            video.height,
        ) != (step.expected_width, step.expected_height):
            return (
                f"{step.output.name} displays {video.width}x{video.height}, expected "
                f"{step.expected_width}x{step.expected_height}: the crop window was not "
                "restored"
            )
        return None

    if step.kind == "audio-core":
        if not media.audio:
            return f"{step.output.name} contains no audio stream"
        codec = media.audio[0].codec
        if codec != step.expected_codec:
            if codec in {"eac3", "truehd"}:
                return (
                    f"{step.output.name} is still {codec.upper()}: this source has no "
                    f"{step.expected_codec.upper()} core that ffmpeg can extract without "
                    "re-encoding"
                )
            return f"{step.output.name} contains {codec}, expected {step.expected_codec}"
        if not media.audio[0].channels:
            return f"{step.output.name} has no channels"
        return None

    if not media.subtitles:
        return f"{step.output.name} contains no subtitle stream"
    codec = media.subtitles[0].codec
    if codec != step.expected_codec:
        return f"{step.output.name} contains {codec}, expected {step.expected_codec}"
    return None


__all__ = [
    "FFMPEG_PROGRESS_ARGS",
    "WINDOW_BSF",
    "WINDOW_BSFS",
    "PrepStep",
    "SubtitleStyle",
    "VideoContext",
    "build_audio_core_argv",
    "build_subtitle_to_srt_argv",
    "build_subtitle_to_sup_argv",
    "build_window_restore_argv",
    "check_prep_output",
    "ffmpeg_preamble",
    "plan_prep_steps",
    "prep_step_to_meta_track",
    "temp_name",
    "window_missing_bsfs",
]
