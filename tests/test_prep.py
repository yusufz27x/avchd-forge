"""Unit tests for :mod:`avchdforge.prep` (ffmpeg argv + output verification)."""

from __future__ import annotations

from pathlib import Path

import pytest

from avchdforge.capabilities import FfmpegCapabilities
from avchdforge.prep import (
    PrepStep,
    SubtitleStyle,
    VideoContext,
    build_audio_core_argv,
    build_subtitle_to_srt_argv,
    build_subtitle_to_sup_argv,
    build_window_restore_argv,
    check_prep_output,
    ffmpeg_preamble,
    plan_prep_steps,
    prep_step_to_meta_track,
    temp_name,
    window_missing_bsfs,
)
from avchdforge.probe import parse_ffprobe_json
from avchdforge.selection import TrackSelection
from avchdforge.sps import WindowRestore

from .conftest import DATA_DIR, astream, build_media, load_json, sstream, vstream

CAPS_PLAIN = FfmpegCapabilities(
    subtitle_encoders=frozenset({"srt", "ass"}),
    bitstream_filters=frozenset({"dca_core", "eac3_core"}),
    muxers=frozenset({"matroska", "mpegts", "sup", "srt", "dts", "ac3"}),
)
CAPS_WITH_PGS = FfmpegCapabilities(
    has_pgs_encoder=True,
    subtitle_encoders=frozenset({"srt", "ass", "pgssub"}),
    bitstream_filters=frozenset({"dca_core", "eac3_core"}),
    muxers=frozenset({"matroska", "sup", "srt"}),
)
#: A build that advertises the SPS-rewriting bitstream filter (FFmpeg 4.1+).
CAPS_WINDOW = FfmpegCapabilities(
    subtitle_encoders=frozenset({"srt", "ass"}),
    bitstream_filters=frozenset({"dca_core", "eac3_core", "h264_metadata", "dump_extra"}),
    muxers=frozenset({"matroska", "mpegts", "sup", "srt", "dts", "ac3"}),
)
#: The same build without ``dump_extra``: the repair cannot work, because the
#: rewritten SPS/PPS would never reach the elementary stream.
CAPS_WINDOW_NO_DUMP = FfmpegCapabilities(
    subtitle_encoders=frozenset({"srt", "ass"}),
    bitstream_filters=frozenset({"dca_core", "eac3_core", "h264_metadata"}),
    muxers=frozenset({"matroska", "mpegts", "sup", "srt", "dts", "ac3"}),
)


def sps_dump(name: str) -> str:
    return (DATA_DIR / "sps" / name).read_text(encoding="utf-8")


@pytest.fixture()
def hd_media():
    return parse_ffprobe_json(load_json("mkv_1080p_dtshd_truehd_ac3core.json"))


@pytest.fixture()
def ac3_media():
    return parse_ffprobe_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))


# --------------------------------------------------------------------------
# argv contracts
# --------------------------------------------------------------------------


def test_preamble_is_non_interactive_and_streams_progress():
    argv = ffmpeg_preamble("/usr/bin/ffmpeg")

    assert argv[0] == "/usr/bin/ffmpeg"
    assert "-nostdin" in argv, "ffmpeg must never grab the terminal"
    assert "-y" in argv
    assert argv[argv.index("-progress") + 1] == "pipe:1"
    assert "-nostats" in argv
    assert argv[argv.index("-v") + 1] == "error"


def test_audio_core_argv_copies_through_the_filter():
    argv = build_audio_core_argv(
        "ffmpeg", Path("/in/movie.mkv"), 1, bsf="dca_core", container="dts", output="/tmp/a.dts"
    )

    assert argv == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        "/in/movie.mkv",
        "-map",
        "0:1",
        "-c:a",
        "copy",
        "-bsf:a",
        "dca_core",
        "-f",
        "dts",
        "/tmp/a.dts",
    ]


def test_audio_core_argv_omits_the_filter_when_demuxing():
    argv = build_audio_core_argv(
        "ffmpeg", "in.mkv", 4, bsf=None, container="ac3", output="core.ac3"
    )
    assert "-bsf:a" not in argv
    assert argv[-7:] == ["-map", "0:4", "-c:a", "copy", "-f", "ac3", "core.ac3"]


def test_no_audio_encoder_is_ever_requested():
    for kwargs in (
        {"bsf": "dca_core", "container": "dts"},
        {"bsf": None, "container": "ac3"},
    ):
        argv = build_audio_core_argv("ffmpeg", "in.mkv", 1, output="out.bin", **kwargs)
        assert argv[argv.index("-c:a") + 1] == "copy", "audio is always copied, never encoded"
        assert "-c:v" not in argv
        assert "-filter_complex" not in argv


def test_subtitle_srt_argv_is_text_to_text():
    argv = build_subtitle_to_srt_argv("ffmpeg", "in.mkv", 3, "out.srt")

    assert argv == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        "in.mkv",
        "-map",
        "0:3",
        "-c:s",
        "srt",
        "-f",
        "srt",
        "out.srt",
    ]


def test_subtitle_sup_argv_uses_the_pgs_encoder():
    argv = build_subtitle_to_sup_argv("ffmpeg", "in.mkv", 3, "out.sup")

    assert argv[argv.index("-c:s") + 1] == "pgssub"
    assert argv[-3:] == ["-f", "sup", "out.sup"]


def test_window_restore_argv_copies_the_stream_through_the_filter():
    restore = WindowRestore(target_width=1920, target_height=1080, crop_bottom=8)
    argv = build_window_restore_argv(
        "ffmpeg", Path("/in/movie.mkv"), 0, restore=restore, output="/tmp/v.264"
    )

    assert argv == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        "/in/movie.mkv",
        "-map",
        "0:0",
        "-c:v",
        "copy",
        "-bsf:v",
        "h264_metadata=crop_left=0:crop_right=0:crop_top=0:crop_bottom=8,dump_extra",
        "-f",
        "h264",
        "/tmp/v.264",
    ]
    assert argv[argv.index("-c:v") + 1] == "copy", "the video is never re-encoded"
    assert "dump_extra" in argv[argv.index("-bsf:v") + 1], (
        "without it the repaired stream carries no parameter sets"
    )
    assert "-vf" not in argv and "-filter:v" not in argv


# --------------------------------------------------------------------------
# temp naming
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "index", "suffix", "hint", "expected"),
    [
        ("audio", 1, ".dts", "", "audio-1.dts"),
        ("audio", 1, ".dts", "DTS core", "audio-1-DTS-core.dts"),
        ("sub", 4, ".srt", "", "sub-4.srt"),
        ("sub", 5, ".sup", "weird/hint", "sub-5-weird-hint.sup"),
    ],
)
def test_temp_name_is_filesystem_safe(kind, index, suffix, hint, expected):
    assert temp_name(kind, index, suffix, hint=hint) == expected


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


def test_passthrough_audio_needs_no_step(ac3_media, tmp_path):
    steps = plan_prep_steps(
        ac3_media, TrackSelection(audio=(1,)), temp_dir=tmp_path, capabilities=CAPS_PLAIN
    )
    assert steps == ()


def test_dts_hd_produces_one_extraction_step(hd_media, tmp_path):
    steps = plan_prep_steps(
        hd_media, TrackSelection(audio=(1,)), temp_dir=tmp_path, capabilities=CAPS_PLAIN
    )

    assert len(steps) == 1
    step = steps[0]
    assert step.kind == "audio-core"
    assert step.source_index == 1
    assert step.output == tmp_path / "audio-1.dts"
    assert step.expected_codec == "dts"
    assert step.mux_codec_tag == "A_DTS"
    assert step.language == "eng"
    assert "dca_core" in step.argv
    assert "DTS-HD" in step.label


def test_truehd_with_core_extracts_the_core_stream(hd_media, tmp_path):
    steps = plan_prep_steps(
        hd_media, TrackSelection(audio=(3,)), temp_dir=tmp_path, capabilities=CAPS_PLAIN
    )

    assert len(steps) == 1
    step = steps[0]
    assert step.source_index == 4, "the AC-3 core lives in its own stream"
    assert step.output == tmp_path / "audio-3.ac3"
    assert step.expected_codec == "ac3"
    assert "-bsf:a" not in step.argv
    assert step.language == "fre"


def test_coreless_truehd_produces_no_step(hd_media, tmp_path):
    steps = plan_prep_steps(
        hd_media, TrackSelection(audio=(2,)), temp_dir=tmp_path, capabilities=CAPS_PLAIN
    )
    assert steps == ()


def test_eac3_produces_no_step_because_it_cannot_become_ac3(hd_media, tmp_path):
    steps = plan_prep_steps(
        hd_media, TrackSelection(audio=(5,)), temp_dir=tmp_path, capabilities=CAPS_PLAIN
    )

    assert steps == ()


def test_text_subtitle_converts_to_srt_without_a_pgs_encoder(ac3_media, tmp_path):
    steps = plan_prep_steps(
        ac3_media,
        TrackSelection(audio=(1,), subtitles=(3,)),
        temp_dir=tmp_path,
        capabilities=CAPS_PLAIN,
    )

    assert [step.kind for step in steps] == ["subtitle-convert"]
    step = steps[0]
    assert step.source_index == 3
    assert step.output == tmp_path / "sub-3.srt"
    assert step.expected_codec == "subrip"
    assert step.mux_codec_tag == "S_TEXT/UTF8"
    assert step.text_subtitle is True
    assert "SubRip" in step.label


def test_text_subtitle_uses_pgssub_when_the_encoder_exists(ac3_media, tmp_path):
    steps = plan_prep_steps(
        ac3_media,
        TrackSelection(audio=(1,), subtitles=(3,)),
        temp_dir=tmp_path,
        capabilities=CAPS_WITH_PGS,
    )

    assert [step.kind for step in steps] == ["subtitle-encode"]
    step = steps[0]
    assert step.output == tmp_path / "sub-3.sup"
    assert step.expected_codec == "hdmv_pgs_subtitle"
    assert step.mux_codec_tag == "S_HDMV/PGS"
    assert step.text_subtitle is False
    assert step.argv[step.argv.index("-c:s") + 1] == "pgssub"


def test_pgs_passthrough_needs_no_step():
    media = parse_ffprobe_json(load_json("mkv_720p_lpcm_multi.json"))
    steps = plan_prep_steps(
        media,
        TrackSelection(audio=(1,), subtitles=(4,)),
        temp_dir=Path("/tmp/x"),
        capabilities=CAPS_PLAIN,
    )
    assert steps == ()


def test_steps_are_ordered_audio_then_subtitles(hd_media, tmp_path):
    media = build_media(
        [
            vstream(),
            astream(1, "dts", profile="DTS-HD MA"),
            astream(2, "truehd", profile="Dolby TrueHD", channels=8, language="jpn"),
            sstream(3, "subrip", language="eng"),
            sstream(4, "ass", language="jpn"),
        ]
    )
    steps = plan_prep_steps(
        media,
        TrackSelection(audio=(1, 2), subtitles=(3, 4)),
        temp_dir=tmp_path,
        capabilities=CAPS_PLAIN,
    )

    assert [step.kind for step in steps] == ["audio-core", "subtitle-convert", "subtitle-convert"]
    assert [step.source_index for step in steps] == [1, 3, 4]


# --------------------------------------------------------------------------
# the SPS window repair
# --------------------------------------------------------------------------


@pytest.fixture()
def window_media():
    """1088-coded / 1036-shown video plus AC-3 and SubRip, from a real dump."""
    return build_media(
        [
            vstream(width=1920, height=1036, extradata=sps_dump("avcc_window_crop_1036.txt")),
            astream(),
            sstream(2, "subrip"),
        ]
    )


def test_window_crop_produces_a_video_step_before_everything_else(window_media, tmp_path):
    steps = plan_prep_steps(
        window_media,
        TrackSelection(audio=(1,), subtitles=(2,)),
        temp_dir=tmp_path,
        capabilities=CAPS_WINDOW,
    )

    assert [step.kind for step in steps] == ["video-window", "subtitle-convert"]
    step = steps[0]
    assert step.source_index == 0
    assert step.output == tmp_path / "video-0-window.264"
    assert step.expected_codec == "h264"
    assert step.expected_width == 1920
    assert step.expected_height == 1080
    assert step.mux_codec_tag == "V_MPEG4/ISO/AVC"
    assert "1920x1080" in step.label
    assert "crop_bottom=8" in step.argv[step.argv.index("-bsf:v") + 1]


def test_window_crop_needs_no_step_when_the_filter_is_missing(window_media, tmp_path):
    steps = plan_prep_steps(
        window_media,
        TrackSelection(audio=(1,)),
        temp_dir=tmp_path,
        capabilities=CAPS_PLAIN,
    )

    assert steps == ()


def test_window_crop_needs_the_extradata_injection_too(window_media, tmp_path):
    """``h264_metadata`` alone emits a stream with no SPS/PPS in it."""
    steps = plan_prep_steps(
        window_media,
        TrackSelection(audio=(1,)),
        temp_dir=tmp_path,
        capabilities=CAPS_WINDOW_NO_DUMP,
    )

    assert steps == ()
    assert window_missing_bsfs(CAPS_WINDOW_NO_DUMP) == ("dump_extra",)
    assert window_missing_bsfs(CAPS_WINDOW) == ()


def test_check_prep_output_accepts_the_restored_size(window_media, tmp_path):
    step = plan_prep_steps(
        window_media,
        TrackSelection(audio=(1,)),
        temp_dir=tmp_path,
        capabilities=CAPS_WINDOW,
    )[0]

    assert check_prep_output(step, build_media([vstream(width=1920, height=1080)])) is None


def test_check_prep_output_rejects_an_unrestored_window(window_media, tmp_path):
    step = plan_prep_steps(
        window_media,
        TrackSelection(audio=(1,)),
        temp_dir=tmp_path,
        capabilities=CAPS_WINDOW,
    )[0]

    problem = check_prep_output(step, build_media([vstream(width=1920, height=1036)]))

    assert problem is not None
    assert "displays 1920x1036, expected 1920x1080" in problem


def test_check_prep_output_rejects_a_video_step_without_video(window_media, tmp_path):
    step = plan_prep_steps(
        window_media,
        TrackSelection(audio=(1,)),
        temp_dir=tmp_path,
        capabilities=CAPS_WINDOW,
    )[0]

    assert "no video stream" in check_prep_output(step, build_media([astream()]))
    assert "expected h264" in check_prep_output(
        step, build_media([vstream(codec="hevc", width=1920, height=1080)])
    )


def test_video_context_uses_the_size_after_the_repair(window_media, ac3_media):
    repaired = VideoContext.from_media(window_media)
    assert (repaired.width, repaired.height) == (1920, 1080)

    untouched = VideoContext.from_media(ac3_media)
    assert (untouched.width, untouched.height) == (1920, 1080)


def test_video_step_becomes_a_track_zero_meta_line(window_media, tmp_path):
    step = plan_prep_steps(
        window_media,
        TrackSelection(audio=(1,)),
        temp_dir=tmp_path,
        capabilities=CAPS_WINDOW,
    )[0]

    track = prep_step_to_meta_track(step, video=VideoContext.from_media(window_media))

    assert track.codec_tag == "V_MPEG4/ISO/AVC"
    assert track.track_id == 0
    assert track.path == step.output
    assert track.fps == pytest.approx(23.976, abs=0.001)
    assert track.language == "und"


# --------------------------------------------------------------------------
# meta-track bridge
# --------------------------------------------------------------------------


def test_audio_step_becomes_a_track_zero_meta_line(hd_media, tmp_path):
    step = plan_prep_steps(
        hd_media, TrackSelection(audio=(1,)), temp_dir=tmp_path, capabilities=CAPS_PLAIN
    )[0]

    track = prep_step_to_meta_track(step)

    assert track.codec_tag == "A_DTS"
    assert track.track_id == 0
    assert track.path == step.output
    assert track.timeshift_ms == 0
    assert track.language == "eng"


def test_srt_step_carries_video_context_and_font_style(ac3_media, tmp_path):
    step = plan_prep_steps(
        ac3_media,
        TrackSelection(audio=(1,), subtitles=(3,)),
        temp_dir=tmp_path,
        capabilities=CAPS_PLAIN,
    )[0]

    track = prep_step_to_meta_track(
        step,
        video=VideoContext.from_media(ac3_media),
        style=SubtitleStyle(font_name="Arial", font_size=48, bottom_offset=10),
    )

    assert track.codec_tag == "S_TEXT/UTF8"
    assert track.video_width == 1920
    assert track.video_height == 1080
    assert track.fps == pytest.approx(23.976, abs=0.001)
    assert track.font_name == "Arial"
    assert track.font_size == 48
    assert track.bottom_offset == 10
    assert track.font_color is not None, "the default colour is applied"


def test_default_subtitle_style_matches_the_constants():
    style = SubtitleStyle()
    assert style.font_name == "DejaVu Sans"
    assert style.font_size == 65
    assert style.bottom_offset == 24


def test_sup_step_becomes_a_pgs_meta_line(ac3_media, tmp_path):
    step = plan_prep_steps(
        ac3_media,
        TrackSelection(audio=(1,), subtitles=(3,)),
        temp_dir=tmp_path,
        capabilities=CAPS_WITH_PGS,
    )[0]

    track = prep_step_to_meta_track(step, video=VideoContext.from_media(ac3_media))

    assert track.codec_tag == "S_HDMV/PGS"
    assert track.track_id == 0
    assert track.font_name is None, "PGS copy needs no font options"


# --------------------------------------------------------------------------
# verification of produced files
# --------------------------------------------------------------------------


def audio_step(tmp_path: Path, expected: str = "dts") -> PrepStep:
    return PrepStep(
        kind="audio-core",
        source_index=1,
        label="extract",
        argv=("ffmpeg",),
        output=tmp_path / "audio-1.dts",
        expected_codec=expected,
        mux_codec_tag="A_DTS",
    )


def subtitle_step(
    tmp_path: Path, expected: str = "subrip", kind: str = "subtitle-convert"
) -> PrepStep:
    return PrepStep(
        kind=kind,  # type: ignore[arg-type]
        source_index=3,
        label="convert",
        argv=("ffmpeg",),
        output=tmp_path / "sub-3.srt",
        expected_codec=expected,
        mux_codec_tag="S_TEXT/UTF8",
        text_subtitle=kind == "subtitle-convert",
    )


def test_audio_step_accepts_the_expected_codec(tmp_path):
    produced = build_media([astream(0, "dts", channels=6)])
    assert check_prep_output(audio_step(tmp_path), produced) is None


def test_audio_step_rejects_a_still_hd_stream(tmp_path):
    produced = build_media([astream(0, "eac3", channels=6)])
    problem = check_prep_output(audio_step(tmp_path, expected="ac3"), produced)

    assert problem is not None
    assert "still EAC3" in problem
    assert "without re-encoding" in problem


def test_audio_step_rejects_a_codec_mismatch(tmp_path):
    produced = build_media([astream(0, "ac3")])
    problem = check_prep_output(audio_step(tmp_path, expected="dts"), produced)
    assert problem is not None and "expected dts" in problem


def test_audio_step_rejects_channel_less_streams(tmp_path):
    produced = build_media([astream(0, "dts", channels=0)])
    problem = check_prep_output(audio_step(tmp_path), produced)
    assert problem is not None and "no channels" in problem


def test_audio_step_rejects_empty_files(tmp_path):
    problem = check_prep_output(audio_step(tmp_path), build_media([]))
    assert problem is not None and "no usable streams" in problem


def test_subtitle_step_checks_the_codec(tmp_path):
    good = build_media([sstream(0, "subrip")])
    assert check_prep_output(subtitle_step(tmp_path), good) is None

    wrong = build_media([sstream(0, "hdmv_pgs_subtitle")])
    problem = check_prep_output(subtitle_step(tmp_path), wrong)
    assert problem is not None and "expected subrip" in problem

    empty = build_media([vstream()])
    problem = check_prep_output(subtitle_step(tmp_path), empty)
    assert problem is not None and "no subtitle stream" in problem


def test_sup_step_expects_pgs(tmp_path):
    step = subtitle_step(tmp_path, expected="hdmv_pgs_subtitle", kind="subtitle-encode")
    good = build_media([sstream(0, "hdmv_pgs_subtitle")])
    assert check_prep_output(step, good) is None
