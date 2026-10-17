"""AVCHD compatibility rules.

Two entry points, both pure functions:

* :func:`validate_container` — is this file usable at all?
* :func:`validate_selection` — is what the user picked muxable?

Errors block the run; warnings are surfaced but let the user continue. Rule codes
(``V1`` …) are stable and referenced by tests and the plan.
"""

from __future__ import annotations

from collections.abc import Iterable

from .audio import AudioDecision, decide_audio_all
from .constants import (
    ALLOWED_INPUT_SUFFIXES,
    ALLOWED_RESOLUTIONS,
    AVCHD_LPCM_MAX_CHANNELS,
    AVCHD_LPCM_SAMPLE_RATE,
    MAX_FPS_BY_HEIGHT,
    MAX_H264_LEVEL,
    VIDEO_BITRATE_WARN_BPS,
)
from .probe import MediaInfo, VideoTrack
from .selection import TrackSelection
from .validate_types import Issue, IssueCollector, ValidationReport

# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------


def validate_container(
    media: MediaInfo,
    *,
    check_extension: bool = True,
    collector: IssueCollector | None = None,
) -> ValidationReport:
    """Everything that can be decided by looking at the source alone."""
    collect = collector or IssueCollector()

    if check_extension:
        _check_extension(media, collect)

    _check_video_streams(media, collect)
    _check_audio_streams(media, collect)
    _check_subtitle_streams(media, collect)

    if not media.duration:
        collect.warning(
            "V17",
            "The source does not report a duration; progress reporting will be approximate.",
        )

    return collect.report()


def _check_extension(media: MediaInfo, collect: IssueCollector) -> None:
    suffix = media.path.suffix.lower()
    if suffix not in ALLOWED_INPUT_SUFFIXES:
        allowed = ", ".join(ALLOWED_INPUT_SUFFIXES)
        collect.error(
            "V1",
            f"'{suffix or media.path.name}' is not a supported input container "
            f"(expected one of: {allowed}).",
        )


def _unfixable_resolution_message(video: VideoTrack) -> str:
    """Why a non-standard size cannot be repaired losslessly (V4, error case)."""
    base = (
        f"Video stream {video.index} is {video.resolution_label}; AVCHD requires exactly "
        "1920x1080 or 1280x720, and this file cannot be brought there losslessly: "
    )
    geometry = video.sps
    if geometry is None:
        return base + (
            "the H.264 sequence parameter set could not be read, so there is no way to "
            "tell whether the missing lines are still coded (avchd-forge never re-encodes)."
        )
    return base + (
        f"the coded frame is {geometry.coded_width}x{geometry.coded_height} and its "
        "conformance window cannot be rewritten to show the missing lines without cutting "
        "visible picture (avchd-forge never re-encodes)."
    )


def _check_video_streams(
    media: MediaInfo,
    collect: IssueCollector,
) -> None:
    if not media.videos:
        collect.error("V2", "No video stream found. Is this really a BDRemux?")
        return
    if len(media.videos) > 1:
        indices = ", ".join(str(track.index) for track in media.videos)
        collect.error(
            "V2",
            f"{len(media.videos)} video streams found (streams {indices}); AVCHD needs "
            "exactly one.",
        )

    for video in media.videos:
        if not video.is_avc:
            collect.error(
                "V3",
                f"Video stream {video.index} is {video.codec.upper() or 'unknown'}, not "
                "H.264/AVC. avchd-forge never re-encodes, so this file cannot be used.",
                video.index,
            )
            continue

        resolution = (video.width, video.height)
        if resolution not in ALLOWED_RESOLUTIONS:
            restore = video.restore_window()
            if restore is not None:
                collect.warning(
                    "V4",
                    f"Video stream {video.index} is {video.resolution_label} with the bars "
                    "still coded; the mux restores the H.264 crop window to "
                    f"{restore.target_width}x{restore.target_height} without touching a "
                    "single coded pixel.",
                    video.index,
                )
            else:
                collect.error("V4", _unfixable_resolution_message(video), video.index)

        if video.level is None:
            collect.warning(
                "V5",
                f"Could not determine the H.264 level of stream {video.index}; "
                "PS3 playback may or may not be fine.",
                video.index,
            )
        elif video.level > MAX_H264_LEVEL:
            collect.warning(
                "V5",
                f"Video stream {video.index} is level {video.level_label} "
                f"(AVCHD tops out at {MAX_H264_LEVEL:g}); the PS3 usually copes, but "
                "seek/decoder complaints are possible.",
                video.index,
            )

        ceiling = MAX_FPS_BY_HEIGHT.get(video.output_resolution[1] or 0)
        if ceiling and video.fps and video.fps > ceiling + 0.05:
            collect.warning(
                "V6",
                f"Video stream {video.index} runs at {video.fps:.3f} fps; AVCHD allows at "
                f"most {ceiling:g} fps at {video.height}p.",
                video.index,
            )

        if not video.is_progressive:
            collect.warning(
                "V7",
                f"Video stream {video.index} looks interlaced (field order "
                f"'{video.field_order}'); remuxing keeps it as-is.",
                video.index,
            )

        if video.bitrate and video.bitrate > VIDEO_BITRATE_WARN_BPS:
            collect.warning(
                "V8",
                f"Video stream {video.index} averages {video.bitrate / 1_000_000:.1f} Mbps; "
                "AVCHD's own limit is 17 Mbps and the PS3 is happiest below ~24 Mbps.",
                video.index,
            )


def _check_audio_streams(media: MediaInfo, collect: IssueCollector) -> None:
    decisions = decide_audio_all(media)
    for track in media.audio:
        if not track.channels:
            collect.warning(
                "V20",
                f"Audio stream {track.index} reports no channels; it is probably broken.",
                track.index,
            )
        decision = decisions[track.index]
        if decision.action.value == "unsupported":
            collect.warning(
                "V12" if track.codec in {"truehd", "mlp"} else "V10",
                f"Audio stream {track.index} ({track.codec_label}): {decision.note}",
                track.index,
            )


def _check_subtitle_streams(media: MediaInfo, collect: IssueCollector) -> None:
    unsupported = [track for track in media.subtitles if not track.is_supported]
    if unsupported:
        names = ", ".join(
            f"{track.index} ({track.codec_label})" for track in unsupported
        )
        collect.warning(
            "V18",
            f"Subtitle streams {names} cannot be converted to PGS and are not offered.",
        )


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def validate_selection(
    media: MediaInfo,
    selection: TrackSelection,
    *,
    decisions: dict[int, AudioDecision] | None = None,
    collector: IssueCollector | None = None,
) -> ValidationReport:
    """Everything that depends on which tracks the user picked."""
    collect = collector or IssueCollector()
    decisions = decisions if decisions is not None else decide_audio_all(media)

    if not selection.audio:
        collect.error(
            "V9",
            "No audio track selected. AVCHD discs need at least one audio track.",
        )

    for index in selection.sorted_audio():
        track = media.audio_track(index)
        if track is None:
            collect.error(
                "V9",
                f"Audio stream {index} was selected but does not exist in the source.",
            )
            continue
        decision = decisions.get(index)
        if decision is None:  # pragma: no cover - decisions cover every audio stream
            collect.error(
                "V9", f"Audio stream {index} could not be analysed.", index
            )
            continue
        if not decision.supported:
            collect.error("V12", f"Audio stream {index}: {decision.note}", index)
            continue
        if not track.channels:
            collect.error(
                "V9",
                f"Audio stream {index} has no usable channels and cannot be muxed.",
                index,
            )
        if track.codec == "aac":
            collect.warning(
                "V10",
                f"Audio stream {index} is AAC, which is outside the AVCHD "
                "specification. The PS3 plays it, other AVCHD players may not.",
                index,
            )
        if track.is_lpcm and track.sample_rate and track.sample_rate != AVCHD_LPCM_SAMPLE_RATE:
            collect.warning(
                "V11",
                f"Audio stream {index} is LPCM at {track.sample_rate_label}; AVCHD "
                "expects 48 kHz.",
                index,
            )
        if track.is_lpcm and (track.channels or 0) > AVCHD_LPCM_MAX_CHANNELS:
            collect.warning(
                "V11",
                f"Audio stream {index} is LPCM with {track.channels} channels; AVCHD "
                f"allows at most {AVCHD_LPCM_MAX_CHANNELS}.",
                index,
            )

    for index in selection.sorted_subtitles():
        track = media.subtitle_track(index)
        if track is None:
            collect.error(
                "V19", f"Subtitle stream {index} was selected but does not exist."
            )
            continue
        if not track.is_supported:
            collect.error(
                "V19",
                f"Subtitle stream {index} ({track.codec_label}) cannot be converted to "
                "PGS.",
                index,
            )
            continue
        video = media.video
        output_width, output_height = video.output_resolution if video else (None, None)
        if (
            track.is_pgs
            and track.width
            and track.height
            and output_width
            and output_height
            and (track.width > output_width or track.height > output_height)
        ):
            collect.warning(
                "V13",
                f"Subtitle stream {index} is {track.width}x{track.height}, larger than the "
                "video; it may be clipped on playback.",
                index,
            )

    if not selection.subtitles and media.subtitles:
        collect.warning(
            "V14",
            "No subtitle track selected; the disc will have no subtitles.",
        )

    return collect.report()


# --------------------------------------------------------------------------
# combined
# --------------------------------------------------------------------------


def validate_all(
    media: MediaInfo,
    selection: TrackSelection,
    *,
    decisions: dict[int, AudioDecision] | None = None,
    extra_issues: Iterable[Issue] = (),
    check_extension: bool = True,
) -> ValidationReport:
    """Container + selection + whatever the caller learned from the target drive."""
    decisions = decisions if decisions is not None else decide_audio_all(media)
    collector = IssueCollector()
    validate_container(
        media,
        check_extension=check_extension,
        collector=collector,
    )
    validate_selection(media, selection, decisions=decisions, collector=collector)
    collector.issues.extend(extra_issues)
    return collector.report()


__all__ = [
    "validate_all",
    "validate_container",
    "validate_selection",
]
