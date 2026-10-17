"""The job plan: everything a mux run needs, computed before anything runs.

A :class:`JobPlan` is a pure function of

* the probed source (:class:`~avchdforge.probe.MediaInfo`),
* the tracks the user picked (:class:`~avchdforge.selection.TrackSelection`),
* the tsMuxeR detection run (:mod:`avchdforge.tracks`), and
* :class:`PlanOptions` (tool names, mux options, ffmpeg capabilities, fonts).

It bundles the ffmpeg preparation steps, the tsMuxeR meta file, the exact mux
command, and an expectation per stream of the finished disc. Nothing here talks
to a process: :func:`plan_for_source` wires the probe and detection run through a
:class:`~avchdforge.process.Runner`, which is what the UI and the tests inject.

The expectation list is not decoration: tsMuxeR 2.7.0 reports success while
dropping tracks whose ``track=`` index was wrong (``docs/CONTEXT.md`` §2.2), so
the backend re-probes the produced streams and compares them with
``plan.tracks`` before the job may be called successful.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from .audio import AudioDecision, decide_audio_all
from .capabilities import FfmpegCapabilities
from .errors import PlanError, ValidationFailed, format_argv
from .meta import MetaTrack, MuxJob, MuxOptions, TrackKind, render_meta
from .prep import (
    WINDOW_BSFS,
    PrepStep,
    SubtitleStyle,
    VideoContext,
    plan_prep_steps,
    prep_step_to_meta_track,
    window_missing_bsfs,
)
from .probe import MediaInfo, probe
from .process import Runner
from .selection import TrackSelection, default_selection
from .tooling import detect_capabilities
from .tracks import (
    RAW_STREAM_TRACK_ID,
    TrackIdMap,
    TsmuxerTrack,
    codec_family,
    detect_tracks,
    resolve_track_ids,
)
from .validate import validate_all
from .validate_types import Issue, ValidationReport

#: Name of the meta file inside the job's temp directory.
META_FILE_NAME: Final[str] = "tsmuxer-meta.txt"

#: Name of the folder a PS3 mounts AVCHD from at a USB drive root. The app
#: creates it inside the chosen output folder when that folder is not already
#: one (``docs/CONTEXT.md`` §2.11).
AVCHD_DIR_NAME: Final[str] = "AVCHD"

#: Container/mux overhead applied to the size estimate (a hint for free space).
OUTPUT_OVERHEAD: Final[float] = 1.03

#: Fallback bitrates (bit/s) for free-space estimates when ffprobe said nothing.
FALLBACK_AUDIO_BITRATES: Final[dict[str, int]] = {
    "ac3": 640_000,
    "dts": 1_536_000,
    "aac": 256_000,
    "mp3": 256_000,
}

#: LPCM has no bitrate in ffprobe; 16 bit samples is the Blu-ray default.
LPCM_BITS_PER_SAMPLE: Final[int] = 16


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedTrack:
    """One stream of the finished disc, plus how it gets there.

    ``family`` is the ffprobe codec family the muxed stream must report, which
    is how the backend proves tsMuxeR kept the track (see module docstring) and
    that anything that was DTS-HD/E-AC-3/TrueHD really arrived as a core.
    """

    kind: TrackKind
    #: ffprobe index of the *selected* source stream this track satisfies.
    index: int
    #: The ``track=`` value handed to tsMuxeR (0 for raw elementary streams).
    track_id: int
    family: str
    language: str
    channels: int | None = None
    #: True when tsMuxeR reads a file produced by a prep step.
    prepared: bool = False
    #: File the muxer actually reads for this track.
    source: Path = field(default_factory=lambda: Path("."))
    #: Source bitrate, used for the free-space estimate only.
    bitrate: int | None = None
    note: str = ""

    def describe(self) -> str:
        origin = "prepared" if self.prepared else "source"
        channels = f" {self.channels}ch" if self.channels else ""
        detail = f" — {self.note}" if self.note else ""
        return (
            f"{self.kind} #{self.index}: {self.family}{channels}, "
            f"track={self.track_id} ({origin}, {self.language}){detail}"
        )


@dataclass(frozen=True, slots=True)
class PlanOptions:
    """Everything that shapes a plan but is not in the source or the selection."""

    tsmuxer: str = "tsMuxeR"
    ffmpeg: str = "ffmpeg"
    mux: MuxOptions = field(default_factory=MuxOptions)
    capabilities: FfmpegCapabilities = field(default_factory=FfmpegCapabilities)
    subtitle_style: SubtitleStyle = field(default_factory=SubtitleStyle)

    def with_split_size(self, mb: int | None) -> PlanOptions:
        """FAT32 splitting on (default 4096 MB) or off."""
        return replace(self, mux=replace(self.mux, split_size_mb=mb))

    def with_capabilities(self, capabilities: FfmpegCapabilities) -> PlanOptions:
        return replace(self, capabilities=capabilities)


@dataclass(frozen=True, slots=True)
class JobPlan:
    """A complete, executable description of one remux job."""

    source: Path
    output_dir: Path
    temp_dir: Path
    media: MediaInfo
    selection: TrackSelection
    tracks: tuple[PlannedTrack, ...]
    prep_steps: tuple[PrepStep, ...]
    mux: MuxJob
    track_ids: TrackIdMap
    report: ValidationReport
    options: PlanOptions = field(default_factory=PlanOptions)

    @property
    def meta_path(self) -> Path:
        """Where the meta file is written (inside the job's temp directory)."""
        return self.temp_dir / META_FILE_NAME

    @property
    def meta_text(self) -> str:
        return render_meta(self.mux)

    @property
    def mux_argv(self) -> tuple[str, ...]:
        """The exact tsMuxeR command line (meta file first, output dir second)."""
        return (self.options.tsmuxer, str(self.meta_path), str(self.output_dir))

    @property
    def video(self) -> PlannedTrack | None:
        return next((track for track in self.tracks if track.kind == "video"), None)

    def audio_tracks(self) -> tuple[PlannedTrack, ...]:
        return tuple(track for track in self.tracks if track.kind == "audio")

    def subtitle_tracks(self) -> tuple[PlannedTrack, ...]:
        return tuple(track for track in self.tracks if track.kind == "subtitle")

    @property
    def prep_outputs(self) -> tuple[Path, ...]:
        return tuple(step.output for step in self.prep_steps)

    @property
    def temp_files(self) -> tuple[Path, ...]:
        """Everything the job wrote into the temp dir (cleaned up on success)."""
        return (*self.prep_outputs, self.meta_path)

    @property
    def splits(self) -> bool:
        return self.mux.options.split_size_mb is not None

    @property
    def estimated_size_bytes(self) -> int | None:
        """Rough output size for the free-space check, or ``None`` if unknown."""
        duration = self.media.duration
        if not duration or duration <= 0:
            return None
        video_rate = next(
            (track.bitrate for track in self.tracks if track.kind == "video"), None
        )
        if video_rate is None:
            video_rate = self._derived_video_bitrate()
        bits = 0.0
        for track in self.tracks:
            if track.kind == "subtitle":
                continue
            if track.kind == "video":
                rate = video_rate
            else:
                rate = track.bitrate or _fallback_bitrate(track)
            if rate is None:
                return None
            bits += rate * duration
        return int(bits / 8 * OUTPUT_OVERHEAD)

    def _derived_video_bitrate(self) -> int | None:
        """Video bitrate from the container total minus the other *known* streams.

        Stream-level ``bit_rate`` is often absent in MKV; the file total is not,
        and video dominates it, so this slightly *over*estimates (unknown audio
        bitrates stay in the video figure) — the right direction for free space.
        """
        total = self.media.bit_rate
        if not total:
            return None
        other = sum(track.bitrate or 0 for track in self.media.audio)
        return max(total - other, total // 2)

    @property
    def command_line(self) -> str:
        return format_argv(self.mux_argv)


def _fallback_bitrate(track: PlannedTrack) -> int | None:
    if track.family == "lpcm" and track.channels:
        return track.channels * 48_000 * LPCM_BITS_PER_SAMPLE
    return FALLBACK_AUDIO_BITRATES.get(track.family)


# --------------------------------------------------------------------------
# destination
# --------------------------------------------------------------------------


def avchd_destination(output_dir: Path | str) -> Path:
    """The folder the mux writes into, given the folder the user chose.

    A PS3 mounts AVCHD from a USB drive only when the tree sits in a folder
    called ``AVCHD`` at the drive root (``docs/CONTEXT.md`` §2.11), so the app
    creates that folder itself: the destination is ``<output_dir>/AVCHD`` unless
    the chosen folder already carries the name. The comparison is
    case-insensitive, like the FAT32 lookups the PS3 does — pointing at the
    drive root and pointing at its ``AVCHD`` folder both do the right thing.
    """
    target = Path(output_dir)
    if target.name.lower() == AVCHD_DIR_NAME.lower():
        return target
    return target / AVCHD_DIR_NAME


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------


def build_plan(
    media: MediaInfo,
    selection: TrackSelection,
    detection: Sequence[TsmuxerTrack],
    *,
    output_dir: Path | str,
    temp_dir: Path | str,
    options: PlanOptions | None = None,
    report: ValidationReport | None = None,
    decisions: dict[int, AudioDecision] | None = None,
) -> JobPlan:
    """Build the plan. Pure: same arguments, byte-identical meta file.

    The mux destination is normalised to the ``AVCHD`` folder a PS3 mounts from
    USB (:func:`avchd_destination`), so ``plan.output_dir`` is where the tree
    must end up, not necessarily the folder the user picked.

    Raises :class:`~avchdforge.errors.ValidationFailed` when the selection is not
    usable, and :class:`~avchdforge.errors.TrackMappingError` when a selected
    source stream has no tsMuxeR counterpart (never guess a ``track=`` number).
    """
    plan_options = options or PlanOptions()
    decisions = decisions if decisions is not None else decide_audio_all(media)
    report = report if report is not None else validate_all(
        media,
        selection,
        decisions=decisions,
    )
    if not report.ok:
        raise ValidationFailed(report)

    video = media.video
    if video is None:  # pragma: no cover - validation refuses video-less media
        raise PlanError("The source has no video stream to mux.")

    target_dir = avchd_destination(output_dir)
    work_dir = Path(temp_dir)
    track_ids = resolve_track_ids(media, detection)

    steps = plan_prep_steps(
        media,
        selection,
        temp_dir=work_dir,
        ffmpeg=plan_options.ffmpeg,
        capabilities=plan_options.capabilities,
        decisions=decisions,
    )

    meta_tracks: list[MetaTrack] = []
    planned: list[PlannedTrack] = []

    video_context = VideoContext.from_media(media)
    video_step = _step_for(steps, ("video-window",), video.index)
    if video.restore_window() is not None and video_step is None:
        missing = window_missing_bsfs(plan_options.capabilities)
        if plan_options.capabilities.is_empty:
            hint = (
                "the plan was built without ffmpeg capabilities, so there is no saying "
                "whether the configured ffmpeg has them: pass the result of "
                "avchdforge.tooling.detect_capabilities() through "
                "PlanOptions.capabilities (plan_for_source does that on its own)"
            )
        else:
            hint = (
                f"the configured ffmpeg ({plan_options.ffmpeg!r}) does not advertise "
                f"{', '.join(missing)}; install FFmpeg 4.1 or newer"
            )
        raise PlanError(
            f"Video stream {video.index} is {video.resolution_label} and the coded rows "
            f"that make it 1920x1080 again are in the file, but restoring them needs the "
            f"ffmpeg bitstream filters {' + '.join(WINDOW_BSFS)}: {hint}."
        )
    if video_step is not None:
        meta_tracks.append(prep_step_to_meta_track(video_step, video=video_context))
        planned.append(
            PlannedTrack(
                kind="video",
                index=video.index,
                track_id=RAW_STREAM_TRACK_ID,
                family=codec_family(video.codec),
                language=video.language,
                prepared=True,
                source=video_step.output,
                bitrate=video.bitrate,
                note=video_step.label,
            )
        )
    else:
        video_id = track_ids.require_one(video.index)
        meta_tracks.append(
            MetaTrack.video(media.path, video_id, language=video.language, fps=video.fps)
        )
        planned.append(
            PlannedTrack(
                kind="video",
                index=video.index,
                track_id=video_id,
                family=codec_family(video.codec),
                language=video.language,
                source=media.path,
                bitrate=video.bitrate,
            )
        )

    for index in selection.sorted_audio():
        track = media.audio_track(index)
        decision = decisions.get(index)
        if track is None or decision is None or not decision.supported:
            raise PlanError(  # pragma: no cover - validation refuses these already
                f"Audio stream {index} cannot be muxed: "
                f"{decision.note if decision else 'stream is gone'}"
            )
        step = _step_for(steps, ("audio-core",), index)
        if step is not None:
            meta_tracks.append(prep_step_to_meta_track(step))
            planned.append(
                PlannedTrack(
                    kind="audio",
                    index=index,
                    track_id=RAW_STREAM_TRACK_ID,
                    family=codec_family(step.expected_codec),
                    language=step.language or track.language,
                    channels=track.channels,
                    prepared=True,
                    source=step.output,
                    bitrate=track.bitrate,
                    note=step.label,
                )
            )
            continue
        if decision.mux_codec_tag is None:  # pragma: no cover - passthrough always has one
            raise PlanError(f"Audio stream {index} has no codec tag for the meta file.")
        source_id = track_ids.require_one(index)
        meta_tracks.append(
            MetaTrack.audio(
                decision.mux_codec_tag,
                media.path,
                source_id,
                language=track.language,
                timeshift_ms=0,
            )
        )
        planned.append(
            PlannedTrack(
                kind="audio",
                index=index,
                track_id=source_id,
                family=codec_family(track.codec),
                language=track.language,
                channels=track.channels,
                source=media.path,
                bitrate=track.bitrate,
                note=decision.describe(),
            )
        )

    for index in selection.sorted_subtitles():
        track = media.subtitle_track(index)
        if track is None or not track.is_supported:
            raise PlanError(  # pragma: no cover - validation refuses these already
                f"Subtitle stream {index} cannot be muxed."
            )
        step = _step_for(steps, ("subtitle-convert", "subtitle-encode"), index)
        if step is not None:
            meta_tracks.append(
                prep_step_to_meta_track(
                    step, video=video_context, style=plan_options.subtitle_style
                )
            )
            planned.append(
                PlannedTrack(
                    kind="subtitle",
                    index=index,
                    track_id=RAW_STREAM_TRACK_ID,
                    family="pgs",
                    language=step.language or track.language,
                    prepared=True,
                    source=step.output,
                    note=step.label,
                )
            )
            continue
        if not track.is_pgs:  # pragma: no cover - a text track always gets a step
            raise PlanError(f"Subtitle stream {index} has no conversion step.")
        source_id = track_ids.require_one(index)
        meta_tracks.append(
            MetaTrack.subtitle(
                media.path,
                source_id,
                language=track.language,
                pgs=True,
                video_width=video_context.width,
                video_height=video_context.height,
                fps=video_context.fps,
            )
        )
        planned.append(
            PlannedTrack(
                kind="subtitle",
                index=index,
                track_id=source_id,
                family="pgs",
                language=track.language,
                source=media.path,
                note="copied as PGS",
            )
        )

    return JobPlan(
        source=media.path,
        output_dir=target_dir,
        temp_dir=work_dir,
        media=media,
        selection=selection,
        tracks=tuple(planned),
        prep_steps=steps,
        mux=MuxJob(tracks=tuple(meta_tracks), options=plan_options.mux),
        track_ids=track_ids,
        report=report,
        options=plan_options,
    )


def _step_for(
    steps: Sequence[PrepStep], kinds: Iterable[str], index: int
) -> PrepStep | None:
    wanted = set(kinds)
    return next(
        (step for step in steps if step.kind in wanted and step.selected_stream == index),
        None,
    )


def plan_for_source(
    source: Path | str,
    runner: Runner,
    *,
    output_dir: Path | str,
    temp_dir: Path | str,
    selection: TrackSelection | None = None,
    options: PlanOptions | None = None,
    ffprobe: str = "ffprobe",
    language_priority: Sequence[str] = (),
    subtitle_language_priority: Sequence[str] = (),
    extra_issues: Iterable[Issue] = (),
) -> JobPlan:
    """Probe ``source``, run tsMuxeR's detection, then build the plan.

    This is the only function here that touches a process (and the only one that
    asks ffmpeg for its capabilities: a caller that did not check the toolchain
    still gets a working window repair). ``selection=None`` means "the smart
    defaults" (:func:`~avchdforge.selection.default_selection`).
    """
    plan_options = options or PlanOptions()
    path = _absolute(source)
    media = probe(path, runner, tool=ffprobe)
    if plan_options.capabilities.is_empty and _needs_window_repair(media):
        # The GUI checks the toolchain once at startup and passes the result
        # down; a library caller usually has not, and refusing a repairable
        # source over a capability nobody asked about would be wrong.
        plan_options = replace(
            plan_options,
            capabilities=detect_capabilities(plan_options.ffmpeg, runner),
        )
    chosen = selection if selection is not None else default_selection(
        media,
        audio_language_priority=language_priority,
        subtitle_language_priority=subtitle_language_priority,
    )
    report = validate_all(
        media,
        chosen,
        extra_issues=list(extra_issues),
    )
    if not report.ok:
        raise ValidationFailed(report)
    detection = detect_tracks(plan_options.tsmuxer, path, runner)
    return build_plan(
        media,
        chosen,
        detection,
        output_dir=_absolute(output_dir),
        temp_dir=_absolute(temp_dir),
        options=plan_options,
        report=report,
    )


def _needs_window_repair(media: MediaInfo) -> bool:
    """True when the video is cropped but the coded rows could be restored."""
    video = media.video
    return video is not None and video.restore_window() is not None


def _absolute(path: Path | str) -> Path:
    """Absolute, ``..``-free path without following symlinks (drives are links)."""
    return Path(path).expanduser().absolute()


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------


def describe_plan(plan: JobPlan) -> list[str]:
    """Lines for the log pane: what will happen, in order."""
    lines = [
        f"source     {plan.source}",
        f"output     {plan.output_dir}",
        f"temp       {plan.temp_dir}",
        f"mux        {plan.command_line}",
        f"splitting  {'on' if plan.splits else 'off'}"
        + (f" ({plan.mux.options.split_size_mb} MB)" if plan.splits else ""),
    ]
    lines.extend(track.describe() for track in plan.tracks)
    lines.extend(f"prepare    {step.command_line}" for step in plan.prep_steps)
    return lines


__all__ = [
    "AVCHD_DIR_NAME",
    "FALLBACK_AUDIO_BITRATES",
    "META_FILE_NAME",
    "OUTPUT_OVERHEAD",
    "JobPlan",
    "PlanOptions",
    "PlannedTrack",
    "avchd_destination",
    "build_plan",
    "describe_plan",
    "plan_for_source",
]
