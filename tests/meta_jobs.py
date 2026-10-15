"""Canonical mux jobs shared by the meta tests and the golden-file updater.

Keeping the definitions in one module means ``scripts/update_golden_metas.py``
renders exactly what the tests assert against, so a golden file can never drift
from the test's intent.
"""

from __future__ import annotations

from pathlib import Path

from avchdforge.meta import MetaTrack, MuxJob, MuxOptions

SOURCE = Path("/media/BDRemux/My Movie (2019)/My Movie.mkv")
TEMP = Path("/tmp/avchd-forge-abc123/")


def basic() -> MuxJob:
    """1080p H.264 + the English AC-3, default FAT32 splitting."""
    return MuxJob(
        tracks=(
            MetaTrack.video(SOURCE, 1, language="eng", fps=23.976023976023978),
            MetaTrack.audio("A_AC3", SOURCE, 2, language="eng", timeshift_ms=0),
        )
    )


def split_disabled() -> MuxJob:
    """Same tracks, but the target is not FAT32 so nothing is split."""
    return basic().with_options(split_size_mb=None)


def audio_cores() -> MuxJob:
    """A DTS-HD MA core and a demuxed TrueHD AC-3 core, both from temp files."""
    return MuxJob(
        tracks=(
            MetaTrack.video(SOURCE, 1, language="eng", fps=23.976023976023978),
            MetaTrack.audio("A_DTS", TEMP / "audio-1.dts", 0, language="eng", timeshift_ms=0),
            MetaTrack.audio("A_AC3", TEMP / "audio-3.ac3", 0, language="fre", timeshift_ms=0),
        )
    )


def subtitles() -> MuxJob:
    """One PGS pass-through and one SRT that tsMuxeR renders to PGS."""
    return MuxJob(
        tracks=(
            MetaTrack.video(SOURCE, 1, language="eng", fps=23.976023976023978),
            MetaTrack.audio("A_AC3", SOURCE, 2, language="eng", timeshift_ms=0),
            MetaTrack.subtitle(
                SOURCE,
                4,
                language="eng",
                pgs=True,
                video_width=1920,
                video_height=1080,
                fps=23.976023976023978,
            ),
            MetaTrack.subtitle(
                TEMP / "sub-5.srt",
                0,
                language="jpn",
                pgs=False,
                video_width=1920,
                video_height=1080,
                fps=23.976023976023978,
            ),
        )
    )


def chapters() -> MuxJob:
    """Chapter options exist in the format; the UI never sets them (non-goal)."""
    return basic().with_options(
        custom_chapters=("00:00:00.000", "00:10:30.500", "01:02:00.000")
    )


def awkward_paths() -> MuxJob:
    """Spaces, quotes and non-ASCII in the source path."""
    path = Path('/media/My Movies/日本語 "quoted" release.mkv')
    return MuxJob(
        tracks=(
            MetaTrack.video(path, 1, language="jpn", fps=29.97002997002997),
            MetaTrack.audio("A_AAC", path, 2, language="jpn"),
        ),
        options=MuxOptions(),
    )


JOBS: dict[str, MuxJob] = {
    "basic": basic,
    "split_disabled": split_disabled,
    "audio_cores": audio_cores,
    "subtitles": subtitles,
    "chapters": chapters,
    "awkward_paths": awkward_paths,
}
