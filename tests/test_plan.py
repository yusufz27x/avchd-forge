"""Unit tests for :mod:`avchdforge.plan` (JobPlan assembly).

Two things are asserted here that no other module can check:

* the *whole* meta file the application sends to tsMuxeR, including the track
  ids that came out of a detection run (golden files, ``tests/plan_jobs.py``);
* that every stream of the finished disc has an expectation the backend can
  verify against, because tsMuxeR exits 0 after dropping a track.

The detection listings below are shortened to the streams a test cares about —
except where a source stream would then have no counterpart at all, which the
mapper refuses by design (``tests/test_tracks.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from avchdforge.capabilities import FfmpegCapabilities
from avchdforge.errors import TrackMappingError, ValidationFailed
from avchdforge.plan import (
    META_FILE_NAME,
    JobPlan,
    PlanOptions,
    build_plan,
    describe_plan,
    plan_for_source,
)
from avchdforge.probe import parse_ffprobe_json
from avchdforge.selection import TrackSelection, default_selection
from avchdforge.tracks import parse_detect_output
from avchdforge.validate_types import warning

from .conftest import astream, build_media, load_json, load_text, vstream
from .plan_jobs import AC3_DETECTION, HD_AUDIO_SELECTION, HD_DETECTION, ac3_media, hd_media

OUT = Path("/media/USB/AVCHD")
TEMP = Path("/tmp/job-1")
CAPS_WITH_PGS = FfmpegCapabilities(has_pgs_encoder=True)

#: The 720p fixture plus a detection listing that covers all of its streams.
LPCM_DETECTION = parse_detect_output(
    "Track ID:    1\nStream type: H.264\nStream ID:   V_MPEG4/ISO/AVC\n"
    "\nTrack ID:    2\nStream type: LPCM\nStream ID:   A_LPCM\n"
    "\nTrack ID:    3\nStream type: AAC\nStream ID:   A_AAC\n"
    "\nTrack ID:    4\nStream type: LPCM\nStream ID:   A_LPCM\n"
    "\nTrack ID:    5\nStream type: PGS\nStream ID:   S_HDMV/PGS\n"
)


def lpcm_media():
    return parse_ffprobe_json(load_json("mkv_720p_lpcm_multi.json"), "/in/720p.mkv")


def build(media, selection, detection, **kwargs) -> JobPlan:
    return build_plan(
        media,
        selection,
        detection,
        output_dir=OUT,
        temp_dir=TEMP,
        **kwargs,
    )


# --------------------------------------------------------------------------
# golden meta files
# --------------------------------------------------------------------------


def test_hd_plan_meta_matches_the_golden_file(golden):
    from .plan_jobs import plan_hd_cores

    assert plan_hd_cores().meta_text == golden("plan_hd_cores.meta")


def test_passthrough_plan_meta_matches_the_golden_file(golden):
    from .plan_jobs import plan_basic

    assert plan_basic().meta_text == golden("plan_basic.meta")


def test_plan_is_deterministic():
    from .plan_jobs import plan_hd_cores

    first, second = plan_hd_cores(), plan_hd_cores()
    assert first.meta_text == second.meta_text
    assert first.tracks == second.tracks
    assert first.prep_steps == second.prep_steps


# --------------------------------------------------------------------------
# passthrough
# --------------------------------------------------------------------------


def test_passthrough_selection_needs_no_prep_step():
    plan = build(ac3_media(), TrackSelection(audio=(1,)), AC3_DETECTION)

    assert plan.prep_steps == ()
    assert [track.kind for track in plan.tracks] == ["video", "audio"]
    assert plan.video is not None and plan.video.track_id == 1
    assert plan.audio_tracks()[0].track_id == 2
    assert plan.audio_tracks()[0].family == "ac3"
    assert plan.audio_tracks()[0].prepared is False
    assert plan.temp_files == (plan.meta_path,)


def test_audio_stays_in_source_stream_order_not_selection_order():
    plan = build(ac3_media(), TrackSelection(audio=(2, 1)), AC3_DETECTION)

    assert [track.index for track in plan.audio_tracks()] == [1, 2]
    assert [track.track_id for track in plan.audio_tracks()] == [2, 3]
    audio_lines = [line for line in plan.meta_text.splitlines() if line.startswith("A_AC3")]
    assert ", track=2," in audio_lines[0]
    assert ", track=3," in audio_lines[1]


def test_mux_command_is_meta_file_then_output_dir():
    plan = build(ac3_media(), TrackSelection(audio=(1,)), AC3_DETECTION)

    assert plan.mux_argv == ("tsMuxeR", str(TEMP / META_FILE_NAME), str(OUT))
    assert plan.command_line.startswith("tsMuxeR ")
    assert plan.meta_path == TEMP / META_FILE_NAME
    assert plan.splits is True


def test_split_size_can_be_disabled_for_a_non_fat32_target():
    plan = build(
        ac3_media(),
        TrackSelection(audio=(1,)),
        AC3_DETECTION,
        options=PlanOptions().with_split_size(None),
    )

    assert plan.splits is False
    assert "--split-size" not in plan.meta_text


def test_video_line_carries_the_stream_fixes_and_the_detected_id():
    plan = build(ac3_media(), TrackSelection(audio=(1,)), AC3_DETECTION)
    video_line = plan.meta_text.splitlines()[1]

    assert video_line.startswith("V_MPEG4/ISO/AVC, ")
    assert "insertSEI, contSPS" in video_line
    assert "fps=23.976" in video_line
    assert "track=1" in video_line


def test_the_plan_emits_exactly_one_video_line():
    plan = build(ac3_media(), TrackSelection(audio=(1,)), AC3_DETECTION)
    assert plan.meta_text.count("V_MPEG4/ISO/AVC") == 1


# --------------------------------------------------------------------------
# prepared tracks (extracted cores, converted subtitles)
# --------------------------------------------------------------------------


def test_dts_hd_becomes_a_track_zero_temp_file():
    plan = build(hd_media(), TrackSelection(audio=(1,)), HD_DETECTION)

    assert len(plan.prep_steps) == 1
    step = plan.prep_steps[0]
    assert step.kind == "audio-core"
    assert step.output == TEMP / "audio-1.dts"

    track = plan.audio_tracks()[0]
    assert (track.index, track.track_id, track.family) == (1, 0, "dts")
    assert track.prepared is True
    assert track.source == step.output
    assert f"A_DTS, {step.output}, track=0, lang=eng, timeshift=0ms" in plan.meta_text


def test_truehd_track_uses_the_companion_core_but_keeps_its_selection_index():
    plan = build(hd_media(), TrackSelection(audio=(3,)), HD_DETECTION)

    step = plan.prep_steps[0]
    assert step.source_index == 4, "the AC-3 core lives in its own stream"
    assert step.selected_stream == 3, "…and it belongs to the selected TrueHD track"
    assert step.output == TEMP / "audio-3.ac3"

    track = plan.audio_tracks()[0]
    assert (track.index, track.family, track.prepared) == (3, "ac3", True)
    assert "audio-3.ac3" in plan.meta_text


def test_selected_truehd_core_stream_is_not_mapped_twice():
    # Selecting both the TrueHD track and its AC-3 core must produce two audio
    # tracks (one prepared, one passthrough) without reusing a prep step.
    plan = build(hd_media(), TrackSelection(audio=(3, 4)), HD_DETECTION)

    prepared = [track for track in plan.audio_tracks() if track.prepared]
    straight = [track for track in plan.audio_tracks() if not track.prepared]
    assert [track.index for track in prepared] == [3]
    assert [track.index for track in straight] == [4]
    assert straight[0].track_id == 5, "the AC-3 stream's own detected Track ID"
    assert plan.meta_text.count("audio-3.ac3") == 1


def test_text_subtitle_without_a_pgs_encoder_goes_through_the_srt_renderer():
    plan = build(hd_media(), TrackSelection(audio=(1,), subtitles=(6,)), HD_DETECTION)

    step = plan.prep_steps[-1]
    assert step.kind == "subtitle-convert"
    assert step.output == TEMP / "sub-6.srt"
    assert step.argv[step.argv.index("-c:s") + 1] == "srt"

    line = plan.meta_text.splitlines()[-1]
    assert line.startswith("S_TEXT/UTF8, ")
    assert "track=0" in line
    assert "video-width=1920, video-height=1080, fps=23.976" in line
    assert (
        'font-name="DejaVu Sans", font-size=65, font-color=0xEBFFFFFF, bottom-offset=24' in line
    )

    subtitle = plan.subtitle_tracks()[0]
    assert (subtitle.index, subtitle.family, subtitle.prepared) == (6, "pgs", True)


def test_text_subtitle_uses_sup_when_ffmpeg_has_a_pgs_encoder():
    plan = build(
        hd_media(),
        TrackSelection(audio=(1,), subtitles=(6,)),
        HD_DETECTION,
        options=PlanOptions().with_capabilities(CAPS_WITH_PGS),
    )

    step = plan.prep_steps[-1]
    assert step.kind == "subtitle-encode"
    assert step.output == TEMP / "sub-6.sup"

    line = plan.meta_text.splitlines()[-1]
    assert line.startswith("S_HDMV/PGS, ")
    assert ", track=0, lang=eng" in line
    assert "fps=23.976" in line, "PGS timing needs the frame rate"


def test_subtitle_style_defaults_are_explicit_and_stable():
    plan = build(hd_media(), TrackSelection(audio=(1,), subtitles=(6,)), HD_DETECTION)

    assert "font-size=65" in plan.meta_text
    assert plan.options.subtitle_style.font_name == "DejaVu Sans"

    styled = PlanOptions().subtitle_style
    assert (styled.font_size, styled.bottom_offset, styled.font_color) == (
        65,
        24,
        "0xEBFFFFFF",
    )


def test_pgs_subtitle_passes_through_with_the_detected_id_and_video_context():
    plan = build(lpcm_media(), TrackSelection(audio=(1,), subtitles=(4,)), LPCM_DETECTION)

    assert plan.prep_steps == ()
    subtitle = plan.subtitle_tracks()[0]
    assert (subtitle.index, subtitle.track_id, subtitle.family, subtitle.prepared) == (
        4,
        5,
        "pgs",
        False,
    )
    line = plan.meta_text.splitlines()[-1]
    assert line == (
        "S_HDMV/PGS, /in/720p.mkv, track=5, lang=eng, "
        "video-width=1280, video-height=720, fps=59.9401"
    )


def test_prep_outputs_and_temp_files_cover_every_generated_file():
    plan = build(hd_media(), HD_AUDIO_SELECTION, HD_DETECTION)

    assert plan.prep_outputs == (
        TEMP / "audio-1.dts",
        TEMP / "audio-3.ac3",
        TEMP / "audio-5.ac3",
        TEMP / "sub-6.srt",
    )
    assert plan.temp_files == (*plan.prep_outputs, plan.meta_path)


def test_one_prep_step_per_selected_stream_at_most():
    plan = build(hd_media(), HD_AUDIO_SELECTION, HD_DETECTION)

    assert len(plan.prep_steps) == 4
    assert len({step.output for step in plan.prep_steps}) == 4, "no two steps share a file"


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_a_pgs_track_without_a_detected_id_is_refused():
    detection = tuple(track for track in LPCM_DETECTION if track.media_kind != "subtitle")

    with pytest.raises(TrackMappingError) as excinfo:
        build(lpcm_media(), TrackSelection(audio=(1,), subtitles=(4,)), detection)

    assert "stream 4" in str(excinfo.value)
    assert "Refusing to guess track numbers." in str(excinfo.value)


def test_a_useless_selection_blocks_the_plan_before_anything_is_mapped():
    with pytest.raises(ValidationFailed) as excinfo:
        build(ac3_media(), TrackSelection(), AC3_DETECTION)

    assert "No audio track selected" in str(excinfo.value)


def test_two_video_streams_are_refused_by_validation():
    media = parse_ffprobe_json(load_json("mkv_two_video_streams.json"), "/in/two.mkv")

    with pytest.raises(ValidationFailed) as excinfo:
        build(media, TrackSelection(audio=(2,)), AC3_DETECTION)

    assert "exactly one" in str(excinfo.value)


def test_build_plan_carries_the_warnings_the_ui_must_show():
    plan = build(ac3_media(), default_selection(ac3_media()), AC3_DETECTION)

    assert plan.report.ok
    assert plan.report.issues, "the source carries warnings that must reach the UI"


# --------------------------------------------------------------------------
# plan_for_source: probe + detection through the runner
# --------------------------------------------------------------------------


def test_plan_for_source_probes_then_detects(runner, tmp_media):
    source = tmp_media("fixture_1080p.mkv")
    runner.add_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))
    runner.add_stdout(load_text("detect_mkv.txt"))

    plan = plan_for_source(source, runner, output_dir=OUT, temp_dir=TEMP)

    assert plan.source == source
    assert [track.kind for track in plan.tracks] == ["video", "audio"]
    assert plan.audio_tracks()[0].track_id == 2
    assert runner.phases == ["probe", "detect"]
    assert runner.calls[0][0] == "ffprobe"
    assert runner.calls[1] == ["tsMuxeR", str(source)]


def test_plan_for_source_uses_smart_defaults(runner, tmp_media):
    source = tmp_media("fixture_1080p.mkv")
    runner.add_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))
    runner.add_stdout(load_text("detect_mkv.txt"))

    plan = plan_for_source(source, runner, output_dir=OUT, temp_dir=TEMP)

    assert plan.selection == default_selection(plan.media)
    assert plan.selection.audio == (1,), "the first compatible audio track is on"
    assert plan.selection.subtitles == (), "text subtitles need a deliberate choice"


def test_plan_for_source_honours_an_explicit_selection_and_tool_paths(runner, tmp_media):
    source = tmp_media("fixture_1080p.mkv")
    runner.add_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))
    runner.add_stdout(load_text("detect_mkv.txt"))

    plan = plan_for_source(
        source,
        runner,
        output_dir=OUT,
        temp_dir=TEMP,
        selection=TrackSelection(audio=(2,), subtitles=(3,)),
        options=PlanOptions(tsmuxer="/opt/tsMuxeR", ffmpeg="/opt/ffmpeg"),
    )

    assert [track.index for track in plan.audio_tracks()] == [2]
    assert [track.index for track in plan.subtitle_tracks()] == [3]
    assert runner.calls[1][0] == "/opt/tsMuxeR"
    assert plan.prep_steps[0].argv[0] == "/opt/ffmpeg"
    assert plan.mux_argv[0] == "/opt/tsMuxeR"


def test_plan_for_source_makes_paths_absolute(runner, tmp_media):
    source = tmp_media("fixture_1080p.mkv")
    runner.add_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))
    runner.add_stdout(load_text("detect_mkv.txt"))

    plan = plan_for_source(
        source,
        runner,
        output_dir=Path("out"),
        temp_dir=Path("tmp"),
    )

    assert plan.output_dir == Path.cwd() / "out"
    assert plan.temp_dir == Path.cwd() / "tmp"
    assert plan.source.is_absolute()
    assert plan.meta_path == Path.cwd() / "tmp" / META_FILE_NAME


def test_plan_for_source_validates_before_it_runs_the_detection(runner, tmp_media):
    source = tmp_media("clean.mkv")
    runner.add_json(load_json("mkv_1080p_hevc.json"))

    with pytest.raises(ValidationFailed):
        plan_for_source(source, runner, output_dir=OUT, temp_dir=TEMP)

    assert runner.phases == ["probe"], "a refused input never reaches tsMuxeR"


def test_plan_for_source_passes_extra_issues_into_the_report(runner, tmp_media):
    source = tmp_media("fixture_1080p.mkv")
    runner.add_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))
    runner.add_stdout(load_text("detect_mkv.txt"))

    plan = plan_for_source(
        source,
        runner,
        output_dir=OUT,
        temp_dir=TEMP,
        extra_issues=[warning("D1", "The drive is not FAT32.")],
    )

    assert "The drive is not FAT32." in plan.report.messages()


# --------------------------------------------------------------------------
# expectations for the post-mux verification
# --------------------------------------------------------------------------


def test_every_track_carries_a_codec_family_and_language():
    plan = build(hd_media(), HD_AUDIO_SELECTION, HD_DETECTION)

    assert [track.kind for track in plan.tracks] == [
        "video",
        "audio",
        "audio",
        "audio",
        "subtitle",
    ]
    assert [track.family for track in plan.tracks] == ["avc", "dts", "ac3", "ac3", "pgs"]
    assert [track.language for track in plan.tracks] == ["und", "eng", "fre", "ger", "eng"]


def test_channel_counts_are_recorded_for_audio_expectations():
    plan = build(ac3_media(), TrackSelection(audio=(1, 2)), AC3_DETECTION)

    assert [track.channels for track in plan.audio_tracks()] == [6, 2]


def test_lpcm_expectation_survives_the_family_normalisation():
    plan = build(lpcm_media(), TrackSelection(audio=(3,)), LPCM_DETECTION)

    track = plan.audio_tracks()[0]
    assert (track.index, track.family) == (3, "lpcm")


# --------------------------------------------------------------------------
# size estimate and logging
# --------------------------------------------------------------------------


def test_size_estimate_stays_close_to_the_source_when_everything_is_selected():
    plan = build(ac3_media(), TrackSelection(audio=(1, 2)), AC3_DETECTION)

    estimate = plan.estimated_size_bytes
    assert estimate is not None
    assert 0.9 * plan.media.size_bytes <= estimate <= 1.15 * plan.media.size_bytes


def test_size_estimate_shrinks_when_fewer_tracks_are_selected():
    small = build(ac3_media(), TrackSelection(audio=(2,)), AC3_DETECTION)
    large = build(ac3_media(), TrackSelection(audio=(1, 2)), AC3_DETECTION)

    assert small.estimated_size_bytes is not None
    assert large.estimated_size_bytes is not None
    assert small.estimated_size_bytes < large.estimated_size_bytes


def test_video_bitrate_is_derived_from_the_container_when_the_stream_has_none():
    media = ac3_media()
    assert media.video is not None and media.video.bitrate is None, "fixture has no video bitrate"

    plan = build(media, TrackSelection(audio=(1,)), AC3_DETECTION)

    assert plan.estimated_size_bytes is not None


def test_lpcm_without_a_bitrate_falls_back_to_channel_math():
    media = build_media(
        [vstream(0, bit_rate=9_000_000), astream(1, "pcm_bluray", channels=6, bit_rate=None)],
        duration="10.000000",
        bit_rate=None,
    )
    detection = parse_detect_output(
        "Track ID:    1\nStream type: H.264\nStream ID:   V_MPEG4/ISO/AVC\n"
        "\nTrack ID:    2\nStream type: LPCM\nStream ID:   A_LPCM\n"
    )

    plan = build(media, TrackSelection(audio=(1,)), detection)

    lpcm = 6 * 48_000 * 16
    expected = int((9_000_000 + lpcm) * 10.0 / 8 * 1.03)
    assert plan.estimated_size_bytes == expected


def test_size_estimate_is_none_when_nothing_is_known():
    media = build_media(
        [vstream(0, bit_rate=None), astream(1, "dts", bit_rate=None)], duration="N/A"
    )
    detection = parse_detect_output(
        "Track ID:    1\nStream type: H.264\nStream ID:   V_MPEG4/ISO/AVC\n"
        "\nTrack ID:    2\nStream type: DTS\nStream ID:   A_DTS\n"
    )

    plan = build(media, TrackSelection(audio=(1,)), detection)

    assert plan.estimated_size_bytes is None


def test_describe_plan_reads_like_a_runbook():
    plan = build(hd_media(), HD_AUDIO_SELECTION, HD_DETECTION)
    lines = describe_plan(plan)

    assert lines[0].startswith("source     ")
    assert any(Path(META_FILE_NAME).name in line for line in lines)
    assert any(line.startswith("audio #1: dts") for line in lines)
    assert any(line.startswith("prepare    ffmpeg") for line in lines)
    assert "splitting  on (4096 MB)" in lines

    unsplit = build(
        hd_media(),
        HD_AUDIO_SELECTION,
        HD_DETECTION,
        options=PlanOptions().with_split_size(None),
    )
    assert "splitting  off" in describe_plan(unsplit)
