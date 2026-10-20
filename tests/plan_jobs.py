"""Canonical :class:`~avchdforge.plan.JobPlan` factories for golden-file tests.

``build_plan`` is pure, so a plan is as assertable as a meta file: the same
factories are used by ``scripts/update_golden_metas.py`` and by
``tests/test_plan.py``. Paths are fictional but fixed, which keeps the golden
meta files readable.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from avchdforge.capabilities import FfmpegCapabilities
from avchdforge.plan import JobPlan, PlanOptions, build_plan
from avchdforge.probe import parse_ffprobe_json
from avchdforge.selection import TrackSelection
from avchdforge.tracks import parse_detect_output

SOURCE = Path("/media/BDRemux/My Movie (2019)/My Movie.mkv")
OUTPUT = Path("/media/USB_DRIVE/AVCHD")
TEMP = Path("/tmp/avchd-forge-abc123")

DATA_DIR = Path(__file__).resolve().parent / "data" / "ffprobe"


def fixture(name: str) -> Mapping[str, Any]:
    """Read an ffprobe fixture without importing the pytest conftest."""
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))

#: tsMuxeR's report for the HD fixture: video first, then the five audio streams
#: (DTS-HD MA, TrueHD, TrueHD, AC-3 core, E-AC-3), then the SubRip track.
HD_DETECTION = parse_detect_output(
    "tsMuxeR version 2.7.0. github.com/justdan96/tsMuxer\n"
    "Track ID:    1\n"
    "Stream type: H.264\n"
    "Stream ID:   V_MPEG4/ISO/AVC\n"
    "Stream info: Profile: High@4.1  Resolution: 1920:1080p  Frame rate: 23.976\n"
    "Stream lang: und\n"
    "\n"
    "Track ID:    2\n"
    "Stream type: DTS\n"
    "Stream ID:   A_DTS\n"
    "Stream lang: eng\n"
    "\n"
    "Track ID:    3\n"
    "Stream type: TrueHD\n"
    "Stream ID:   A_AC3\n"
    "Stream lang: eng\n"
    "\n"
    "Track ID:    4\n"
    "Stream type: TrueHD\n"
    "Stream ID:   A_AC3\n"
    "Stream lang: fra\n"
    "\n"
    "Track ID:    5\n"
    "Stream type: AC3\n"
    "Stream ID:   A_AC3\n"
    "Stream lang: fra\n"
    "\n"
    "Track ID:    6\n"
    "Stream type: E-AC3\n"
    "Stream ID:   A_AC3\n"
    "Stream lang: deu\n"
    "\n"
    "Track ID:    7\n"
    "Stream type: SRT\n"
    "Stream ID:   S_TEXT/UTF8\n"
    "Stream lang: eng\n"
)

#: Audio 1 (DTS-HD MA) and 3 (TrueHD with an embedded AC-3 core) need an
#: extraction step; audio 4 (the AC-3 core of 3) is not selected, and 5
#: (E-AC-3) is refused because it cannot become AC-3 without a re-encode.
HD_AUDIO_SELECTION = TrackSelection(audio=(1, 3), subtitles=(6,))


def hd_media():
    """The DTS-HD/TrueHD/E-AC-3 fixture with a realistic absolute source path."""
    return parse_ffprobe_json(fixture("mkv_1080p_dtshd_truehd_ac3core.json"), SOURCE)


def plan_hd_cores() -> JobPlan:
    """Every audio flavour that needs a core, plus one SubRip subtitle."""
    return build_plan(
        hd_media(),
        HD_AUDIO_SELECTION,
        HD_DETECTION,
        output_dir=OUTPUT,
        temp_dir=TEMP,
    )


#: tsMuxeR's report for the AC-3 fixture (two audio tracks, one SubRip track).
AC3_DETECTION = parse_detect_output(
    "tsMuxeR version 2.7.0. github.com/justdan96/tsMuxeR\n"
    "Track ID:    1\n"
    "Stream type: H.264\n"
    "Stream ID:   V_MPEG4/ISO/AVC\n"
    "Stream lang: und\n"
    "\n"
    "Track ID:    2\n"
    "Stream type: AC3\n"
    "Stream ID:   A_AC3\n"
    "Stream lang: eng\n"
    "\n"
    "Track ID:    3\n"
    "Stream type: AC3\n"
    "Stream ID:   A_AC3\n"
    "Stream lang: jpn\n"
    "\n"
    "Track ID:    4\n"
    "Stream type: SRT\n"
    "Stream ID:   S_TEXT/UTF8\n"
    "Stream lang: eng\n"
)


def ac3_media():
    return parse_ffprobe_json(fixture("mkv_1080p_h264_ac3x2_srt.json"), SOURCE)


def plan_basic() -> JobPlan:
    """Passthrough only: one AC-3 track, splitting disabled (non-FAT32 target)."""
    return build_plan(
        ac3_media(),
        TrackSelection(audio=(1,)),
        AC3_DETECTION,
        output_dir=OUTPUT,
        temp_dir=TEMP,
        options=PlanOptions().with_split_size(None),
    )


#: tsMuxeR's report for the window-cropped fixture (AC-3 + SubRip).
WINDOW_DETECTION = parse_detect_output(
    "tsMuxeR version 2.7.0. github.com/justdan96/tsMuxer\n"
    "Track ID:    1\n"
    "Stream type: H.264\n"
    "Stream ID:   V_MPEG4/ISO/AVC\n"
    "Stream info: Profile: High@4.1  Resolution: 1920:1036p  Frame rate: 23.976\n"
    "Stream lang: und\n"
    "\n"
    "Track ID:    2\n"
    "Stream type: AC3\n"
    "Stream ID:   A_AC3\n"
    "Stream lang: eng\n"
    "\n"
    "Track ID:    3\n"
    "Stream type: SRT\n"
    "Stream ID:   S_TEXT/UTF8\n"
    "Stream lang: eng\n"
)

#: The bitstream filters that rewrite the conformance window and put the
#: rewritten parameter sets in-band (FFmpeg 4.1+).
WINDOW_CAPABILITIES = FfmpegCapabilities(
    bitstream_filters=frozenset({"dca_core", "eac3_core", "h264_metadata", "dump_extra"})
)


def window_media():
    """1920x1088 coded, window at 1920x1036: the bars are still in the stream."""
    return parse_ffprobe_json(fixture("mkv_window_crop_h264_ac3_srt.json"), SOURCE)


def plan_window_restore() -> JobPlan:
    """Cropped 1036 source: the video ES is window-restored, then muxed."""
    return build_plan(
        window_media(),
        TrackSelection(audio=(1,), subtitles=(2,)),
        WINDOW_DETECTION,
        output_dir=OUTPUT,
        temp_dir=TEMP,
        options=PlanOptions().with_capabilities(WINDOW_CAPABILITIES),
    )


JOBS: dict[str, Callable[[], JobPlan]] = {
    "plan_hd_cores": plan_hd_cores,
    "plan_basic": plan_basic,
    "plan_window_restore": plan_window_restore,
}
