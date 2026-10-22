"""Which tracks go onto the disc, and how they are chosen by default.

The rules from the brief:

* audio — without a language priority, the first *compatible* track is on and
  every other audio track is off; with one, every compatible track whose
  language is preferred is on (at least one track is always required);
* subtitles — without a language priority, every PGS track is on and text
  subtitles start off (they need a conversion step, so switching one on is a
  deliberate act); with one, every supported track in the preferred languages is
  on, PGS and convertible text alike.

A priority that matches nothing leaves the audio fallback in place (the source's
default-flagged track, else the first compatible one) and selects no subtitles:
the preference is a language decision, not a suggestion to tick everything.

Track sets are immutable: the UI produces a new :class:`TrackSelection` instead
of mutating the current one, which keeps undo/re-run logic and tests trivial.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from .audio import AudioDecision, decide_audio
from .probe import MediaInfo


@dataclass(frozen=True, slots=True)
class TrackSelection:
    """Stream indices (ffprobe numbering) the user wants on the disc."""

    audio: tuple[int, ...] = ()
    subtitles: tuple[int, ...] = ()

    def with_audio(self, *indices: int) -> TrackSelection:
        return replace(self, audio=tuple(indices))

    def with_subtitles(self, *indices: int) -> TrackSelection:
        return replace(self, subtitles=tuple(indices))

    def toggle_audio(self, index: int) -> TrackSelection:
        return replace(self, audio=_toggle(self.audio, index))

    def toggle_subtitle(self, index: int) -> TrackSelection:
        return replace(self, subtitles=_toggle(self.subtitles, index))

    @property
    def is_empty(self) -> bool:
        return not self.audio and not self.subtitles

    def sorted_audio(self) -> tuple[int, ...]:
        return tuple(sorted(self.audio))

    def sorted_subtitles(self) -> tuple[int, ...]:
        return tuple(sorted(self.subtitles))


def _toggle(values: tuple[int, ...], index: int) -> tuple[int, ...]:
    if index in values:
        return tuple(value for value in values if value != index)
    return (*values, index)


def _normalized_priority(language_priority: Sequence[str]) -> list[str]:
    return [entry.strip().lower() for entry in language_priority if entry.strip()]


def _priority_rank(language: str, priority: Sequence[str]) -> int:
    """Lower is better; languages outside the (normalised) priority list come last."""
    try:
        return priority.index(language.lower())
    except ValueError:
        return len(priority)


def default_audio_indices(
    media: MediaInfo,
    *,
    language_priority: Sequence[str] = (),
    decisions: dict[int, AudioDecision] | None = None,
) -> tuple[int, ...]:
    """Pick the audio tracks that should be enabled by default.

    With a language priority, every compatible track in those languages is on,
    ordered by the priority, then the source's default flag, then stream order.
    Without one — or when nothing matches — a single track is chosen, because
    the disc needs at least one: the source's default-flagged track, else the
    first compatible one. Tracks the pipeline cannot use (for example TrueHD
    without an AC-3 core) never win.
    """
    decisions = decisions if decisions is not None else _decisions(media)
    candidates = [
        track
        for track in media.audio
        if (decision := decisions.get(track.index)) is not None and decision.supported
    ]
    if not candidates:
        return ()

    priority = _normalized_priority(language_priority)
    if priority:
        preferred = [
            track
            for track in candidates
            if _priority_rank(track.language, priority) < len(priority)
        ]
        if preferred:
            ordered = sorted(
                preferred,
                key=lambda track: (
                    _priority_rank(track.language, priority),
                    not track.is_default,
                    track.index,
                ),
            )
            return tuple(track.index for track in ordered)

    for track in candidates:
        if track.is_default:
            return (track.index,)
    return (candidates[0].index,)


def default_audio_index(
    media: MediaInfo,
    *,
    language_priority: Sequence[str] = (),
    decisions: dict[int, AudioDecision] | None = None,
) -> int | None:
    """The first track :func:`default_audio_indices` would enable, if any."""
    indices = default_audio_indices(
        media, language_priority=language_priority, decisions=decisions
    )
    return indices[0] if indices else None


def default_selection(
    media: MediaInfo,
    *,
    language_priority: Sequence[str] = (),
    audio_language_priority: Sequence[str] = (),
    subtitle_language_priority: Sequence[str] = (),
    decisions: dict[int, AudioDecision] | None = None,
) -> TrackSelection:
    """The selection the UI starts with for a freshly loaded file."""
    # `language_priority` is kept as the short spelling used by older callers.
    audio_priority = audio_language_priority or language_priority
    decisions = decisions if decisions is not None else _decisions(media)

    audio = default_audio_indices(
        media, language_priority=audio_priority, decisions=decisions
    )
    subtitles = default_subtitle_indices(
        media, language_priority=subtitle_language_priority
    )
    return TrackSelection(audio=audio, subtitles=subtitles)


def default_subtitle_indices(
    media: MediaInfo, *, language_priority: Sequence[str] = ()
) -> tuple[int, ...]:
    """Pick the subtitle tracks that should be enabled by default.

    With a language priority, every supported track in those languages is on —
    PGS is copied, text is converted — and nothing else. Without one, all PGS
    tracks are on and text tracks stay off (converting one is a deliberate act).
    """
    priority = _normalized_priority(language_priority)
    if not priority:
        return tuple(track.index for track in media.subtitles if track.is_pgs)
    preferred = [
        track
        for track in media.subtitles
        if track.is_supported
        and _priority_rank(track.language, priority) < len(priority)
    ]
    ranked = sorted(
        preferred,
        key=lambda track: (_priority_rank(track.language, priority), track.index),
    )
    return tuple(track.index for track in ranked)


def compatible_audio_indices(
    media: MediaInfo, decisions: dict[int, AudioDecision] | None = None
) -> tuple[int, ...]:
    decisions = decisions if decisions is not None else _decisions(media)
    return tuple(
        track.index
        for track in media.audio
        if decisions.get(track.index) is not None and decisions[track.index].supported
    )


def selectable_subtitle_indices(media: MediaInfo) -> tuple[int, ...]:
    return tuple(track.index for track in media.subtitles if track.is_supported)


def sanitize_selection(
    media: MediaInfo,
    selection: TrackSelection,
    decisions: dict[int, AudioDecision] | None = None,
) -> TrackSelection:
    """Drop anything that no longer exists or can no longer be used."""
    decisions = decisions if decisions is not None else _decisions(media)
    audio = tuple(
        index
        for index in selection.audio
        if media.audio_track(index) is not None
        and decisions.get(index) is not None
        and decisions[index].supported
    )
    subtitles = tuple(
        index
        for index in selection.subtitles
        if (track := media.subtitle_track(index)) is not None and track.is_supported
    )
    return TrackSelection(audio=audio, subtitles=subtitles)


def describe_selection(media: MediaInfo, selection: TrackSelection) -> list[str]:
    """Human-readable lines for the log pane."""
    lines: list[str] = []
    for index in selection.sorted_audio():
        track = media.audio_track(index)
        if track is not None:
            decision = decide_audio(media, track)
            lines.append(f"audio  #{index} {track.summary()} -> {decision.describe()}")
    for index in selection.sorted_subtitles():
        track = media.subtitle_track(index)
        if track is not None:
            action = "copy as PGS" if track.is_pgs else f"convert {track.codec_label} to PGS"
            lines.append(f"subs   #{index} {track.summary()} -> {action}")
    return lines


def _decisions(media: MediaInfo) -> dict[int, AudioDecision]:
    return {track.index: decide_audio(media, track) for track in media.audio}


def selection_from_indices(
    audio: Iterable[int] = (), subtitles: Iterable[int] = ()
) -> TrackSelection:
    return TrackSelection(audio=tuple(audio), subtitles=tuple(subtitles))


__all__ = [
    "TrackSelection",
    "compatible_audio_indices",
    "default_audio_index",
    "default_audio_indices",
    "default_selection",
    "default_subtitle_indices",
    "describe_selection",
    "sanitize_selection",
    "selectable_subtitle_indices",
    "selection_from_indices",
]
