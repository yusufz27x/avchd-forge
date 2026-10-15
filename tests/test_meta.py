"""Unit tests for :mod:`avchdforge.meta` (tsMuxeR meta generation).

Golden files live in ``tests/data/meta`` and are regenerated with
``scripts/update_golden_metas.py``; the job definitions both sides use are in
``tests/meta_jobs.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from avchdforge.meta import (
    MetaError,
    MetaTrack,
    MuxJob,
    MuxOptions,
    order_tracks,
    quote,
    render_meta,
    render_track_line,
    write_meta,
)

from .meta_jobs import JOBS, SOURCE, basic
from .plan_jobs import JOBS as PLAN_JOBS

GOLDEN = Path(__file__).resolve().parent / "data" / "meta"


# --------------------------------------------------------------------------
# golden files
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(JOBS))
def test_meta_matches_golden_file(name, golden):
    rendered = render_meta(JOBS[name]())
    assert rendered == golden(f"{name}.meta"), (
        f"{name}.meta changed. If that is intended, run "
        f"scripts/update_golden_metas.py and review the diff."
    )


@pytest.mark.parametrize("name", sorted(JOBS))
def test_rendering_is_deterministic(name):
    job = JOBS[name]()
    assert render_meta(job) == render_meta(JOBS[name]())


def test_every_golden_file_has_a_job():
    """Golden files come from a MuxJob or from a whole JobPlan (``plan_jobs``)."""
    golden_names = {path.stem for path in GOLDEN.glob("*.meta")}
    assert golden_names == set(JOBS) | set(PLAN_JOBS)


# --------------------------------------------------------------------------
# MUXOPT line
# --------------------------------------------------------------------------


def test_muxopt_line_uses_spaces_not_commas():
    line = render_meta(basic()).splitlines()[0]
    assert line.startswith("MUXOPT --avchd ")
    assert "," not in line
    assert "--split-size=4096MB" in line
    assert "--vbv-len=500" in line


def test_split_size_is_configurable_and_removable():
    assert "--split-size=2048MB" in render_meta(basic().with_options(split_size_mb=2048))
    assert "--split-size" not in render_meta(basic().with_options(split_size_mb=None))


def test_muxopt_token_order_and_optional_flags():
    options = MuxOptions(
        avchd=True,
        no_pcr_on_video_pid=False,
        new_audio_pes=False,
        vbr=False,
        vbv_len=None,
        split_size_mb=None,
    )
    assert options.tokens() == ["MUXOPT", "--avchd"]


def test_chapter_options_are_supported():
    line = render_meta(JOBS["chapters"]()).splitlines()[0]
    assert "--custom-chapters=00:00:00.000;00:10:30.500;01:02:00.000" in line

    auto = render_meta(basic().with_options(auto_chapters_minutes=10)).splitlines()[0]
    assert "--auto-chapters=10" in auto


# --------------------------------------------------------------------------
# track lines
# --------------------------------------------------------------------------


def test_video_line_has_bd_muxing_flags_and_track_id():
    line = render_track_line(MetaTrack.video(SOURCE, 1, language="eng", fps=23.976))
    assert line.startswith('V_MPEG4/ISO/AVC, "')
    assert "insertSEI, contSPS" in line
    assert "fps=23.976" in line
    assert "track=1" in line
    assert "lang=eng" in line


def test_video_flags_can_be_disabled():
    line = render_track_line(
        MetaTrack.video(SOURCE, 1, insert_sei=False, cont_sps=False)
    )
    assert "insertSEI" not in line
    assert "contSPS" not in line


@pytest.mark.parametrize(
    ("fps", "expected"),
    [
        (23.976023976023978, "fps=23.976"),
        (25.0, "fps=25"),
        (59.94, "fps=59.94"),
        (29.97002997, "fps=29.97"),
    ],
)
def test_fps_formatting_is_compact(fps, expected):
    line = render_track_line(MetaTrack.video(SOURCE, 1, fps=fps))
    assert expected in line


def test_audio_line_timeshift_signs():
    positive = render_track_line(MetaTrack.audio("A_AC3", SOURCE, 2, timeshift_ms=250))
    negative = render_track_line(MetaTrack.audio("A_AC3", SOURCE, 2, timeshift_ms=-80))
    none = render_track_line(MetaTrack.audio("A_AC3", SOURCE, 2))

    assert "timeshift=250ms" in positive
    assert "timeshift=-80ms" in negative
    assert "timeshift" not in none


def test_pgs_line_carries_no_font_options():
    line = render_track_line(
        MetaTrack.subtitle(SOURCE, 4, language="eng", pgs=True, video_width=1920, video_height=1080)
    )
    assert line.startswith("S_HDMV/PGS")
    assert "font-name" not in line
    assert "video-width=1920" in line
    assert "track=4" in line


def test_srt_line_carries_tsmuxer_render_options():
    line = render_track_line(
        MetaTrack.subtitle(
            "/tmp/sub.srt",
            0,
            language="jpn",
            pgs=False,
            video_width=1280,
            video_height=720,
            fps=59.94,
        )
    )
    assert line.startswith("S_TEXT/UTF8")
    assert 'font-name="DejaVu Sans"' in line
    assert "font-size=65" in line
    assert "font-color=0xEBFFFFFF" in line
    assert "bottom-offset=24" in line
    assert "video-height=720" in line


def test_language_is_omitted_when_unknown():
    line = render_track_line(MetaTrack.audio("A_AC3", SOURCE, 2, language=None))
    assert "lang=" not in line


def test_extra_options_render_bare_flags_and_values():
    track = MetaTrack.audio(
        "A_DTS",
        SOURCE,
        3,
        extra_options=(("default", None), ("timeshift", "2ms")),
    )
    line = render_track_line(track)
    assert line.endswith("default, timeshift=2ms")


# --------------------------------------------------------------------------
# quoting and path handling
# --------------------------------------------------------------------------


def test_quote_matrix():
    assert quote("plain.mkv") == "plain.mkv"
    assert quote("/a/b c/d.mkv") == '"/a/b c/d.mkv"'
    assert quote('weird"name.mkv') == '"weird\\"name.mkv"'
    assert quote("C:\\media\\file.mkv") == "C:\\media\\file.mkv", "backslashes stay untouched"


def test_windows_style_path_is_not_mangled():
    line = render_track_line(MetaTrack.video(r"C:\Media\My Movie\file.mkv", 1))
    assert r'"C:\Media\My Movie\file.mkv"' in line


def test_non_ascii_paths_survive():
    line = render_track_line(MetaTrack.video("/media/日本語/映画.mkv", 1))
    assert "/media/日本語/映画.mkv" in line


def test_line_breaks_in_paths_are_refused():
    with pytest.raises(MetaError):
        render_track_line(MetaTrack.video("/media/broken\nfile.mkv", 1))


def test_unsafe_utf8_is_written_as_utf8(tmp_path: Path):
    target = write_meta(tmp_path / "job.meta", JOBS["awkward_paths"]())
    raw = target.read_bytes()
    assert "日本語".encode() in raw
    assert target.read_text(encoding="utf-8") == render_meta(JOBS["awkward_paths"]())


# --------------------------------------------------------------------------
# job validation and ordering
# --------------------------------------------------------------------------


def test_video_track_comes_first_regardless_of_input_order():
    audio = MetaTrack.audio("A_AC3", SOURCE, 2, language="eng")
    video = MetaTrack.video(SOURCE, 1, language="eng")
    ordered = order_tracks((audio, video))
    assert ordered == (video, audio)
    assert render_meta(MuxJob(tracks=(audio, video))).splitlines()[1].startswith("V_MPEG4")


def test_audio_and_subtitle_order_follows_the_selection():
    first = MetaTrack.audio("A_AC3", SOURCE, 3, language="jpn")
    second = MetaTrack.audio("A_AC3", SOURCE, 2, language="eng")
    rendered = render_meta(MuxJob(tracks=(MetaTrack.video(SOURCE, 1), first, second)))
    lines = rendered.splitlines()
    assert lines[1].startswith("V_MPEG4")
    assert "lang=jpn" in lines[2]
    assert "lang=eng" in lines[3]


def test_empty_job_is_refused():
    with pytest.raises(MetaError, match="at least one track"):
        render_meta(MuxJob())


def test_job_without_video_is_refused():
    with pytest.raises(MetaError, match="video track"):
        render_meta(MuxJob(tracks=(MetaTrack.audio("A_AC3", SOURCE, 2),)))


def test_two_video_tracks_are_refused():
    job = MuxJob(
        tracks=(
            MetaTrack.video(SOURCE, 1),
            MetaTrack.video("/other.mkv", 1),
        )
    )
    with pytest.raises(MetaError, match="exactly one video"):
        render_meta(job)


def test_write_meta_creates_parent_directories_and_lf_endings(tmp_path: Path):
    target = write_meta(tmp_path / "nested" / "dir" / "job.meta", basic())

    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "\r" not in content
    assert content.endswith("\n")
    assert content.count("\n") == len(content.splitlines())
